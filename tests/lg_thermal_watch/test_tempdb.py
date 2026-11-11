"""Unit tests for the RoomTempLogger SQLite persistence."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lg_thermal_watch.tempdb import RoomTempLogger, normalize_ts, utc_iso


class MockAgent:
    """Mock of the agent temperature source."""

    def __init__(self, temps: list[float], fail_first: int = 0) -> None:
        """Initialise the scripted readings."""
        self.temps = list(temps)
        self.fail_first = fail_first

    async def get_room_temperature(self) -> float | None:
        """Return the next scripted temperature (after any scripted failures)."""
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("LG cloud unreachable")
        if not self.temps:
            return None
        return self.temps.pop(0)


async def run_briefly(logger: RoomTempLogger, seconds: float = 0.25) -> None:
    """Run the logger briefly."""
    await logger.start()
    await asyncio.sleep(seconds)
    await logger.stop()


def make_logger(tmp_path: Path, agent: MockAgent | None = None) -> RoomTempLogger:
    """Build a logger on a fresh database."""
    return RoomTempLogger(agent, db_path=str(tmp_path / "temps.db"), interval_s=0.02)  # type: ignore[arg-type]


class TestRoomTempLogger:
    def test_logs_readings_and_reports_min(self, tmp_path: Path) -> None:
        """Readings are stored and the minimum is queryable."""
        logger = make_logger(tmp_path, MockAgent([21.5, 22.0, 20.5, 23.0]))
        asyncio.run(run_briefly(logger))
        assert logger.get_readings_count() == 4
        assert logger.get_min_temp() == 20.5

    def test_timestamps_are_canonical_utc(self, tmp_path: Path) -> None:
        """Stored timestamps are UTC with an explicit offset."""
        logger = make_logger(tmp_path, MockAgent([21.0]))
        asyncio.run(run_briefly(logger, 0.05))
        ts = logger.get_readings()[0]["ts"]
        assert ts.endswith("+00:00") and len(ts) == 25

    def test_loop_survives_read_errors(self, tmp_path: Path) -> None:
        """A failed read is retried on the next interval instead of killing the logger."""
        logger = make_logger(tmp_path, MockAgent([21.0, 21.5], fail_first=2))
        asyncio.run(run_briefly(logger, 0.3))
        assert logger.get_readings_count() == 2
        assert logger.last_error is None

    def test_legacy_naive_timestamps_are_migrated(self, tmp_path: Path) -> None:
        """Rows written by older versions in naive local time are converted to UTC on start-up."""
        db = tmp_path / "temps.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE room_temps (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, temp_c REAL)")
        naive = "2026-11-05T23:28:59.568129"
        conn.execute("INSERT INTO room_temps (ts, temp_c) VALUES (?, ?)", (naive, 22.5))
        conn.commit()
        conn.close()
        logger = RoomTempLogger(None, db_path=str(db))
        expected = datetime.fromisoformat(naive).astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")
        assert logger.get_readings() == [{"ts": expected, "temp_c": 22.5}]

    def test_window_filters_accept_any_iso_form(self, tmp_path: Path) -> None:
        """Window bounds may be naive, offset or Z-suffixed; all are compared in UTC."""
        logger = make_logger(tmp_path)
        base = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
        for minute in range(0, 120, 10):
            logger.add_reading(20.0 + minute / 100, at=base + timedelta(minutes=minute))
        in_window = logger.get_readings(start="2026-11-09T23:00:00Z", end="2026-11-10T00:30:00+01:00")
        assert [r["ts"][11:16] for r in in_window] == ["23:00", "23:10", "23:20", "23:30"]

    def test_bucketed_readings_average(self, tmp_path: Path) -> None:
        """Bucketing averages readings into fixed time buckets (for 7- and 30-day views)."""
        logger = make_logger(tmp_path)
        base = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
        for minute in range(120):
            logger.add_reading(20.0 if minute < 60 else 22.0, at=base + timedelta(minutes=minute))
        buckets = logger.get_readings(bucket_s=3600)
        assert [(b["temp_c"], b["n"]) for b in buckets] == [(20.0, 60), (22.0, 60)]
        assert buckets[0]["ts"].startswith("2026-11-09T00:29")

    def test_stats_and_events(self, tmp_path: Path) -> None:
        """Stats give min/max with when they happened; events round-trip."""
        logger = make_logger(tmp_path)
        base = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)
        for minute, temp in enumerate([21.0, 19.5, 22.5, 21.0]):
            logger.add_reading(temp, at=base + timedelta(minutes=minute))
        stats = logger.get_stats()
        assert stats["count"] == 4 and stats["mean_c"] == 21.0
        assert stats["min"] == {"ts": "2026-11-09T00:01:00+00:00", "temp_c": 19.5}
        assert stats["max"]["temp_c"] == 22.5
        assert stats["latest"] == {"ts": "2026-11-09T00:03:00+00:00", "temp_c": 21.0}
        logger.record_event("heat_start", "cold", 19.5, at=base)
        events = logger.get_events(start="2026-11-08T00:00:00Z")
        assert events == [{"ts": "2026-11-09T00:00:00+00:00", "kind": "heat_start", "detail": "cold", "temp_c": 19.5}]


def test_normalize_ts_forms() -> None:
    """All ISO forms normalise to the same canonical UTC string."""
    assert normalize_ts("2026-11-10T01:00:00+01:00") == "2026-11-10T00:00:00+00:00"
    assert normalize_ts("2026-11-10T00:00:00Z") == "2026-11-10T00:00:00+00:00"
    assert utc_iso(datetime(2026, 11, 10, tzinfo=timezone.utc)) == "2026-11-10T00:00:00+00:00"
