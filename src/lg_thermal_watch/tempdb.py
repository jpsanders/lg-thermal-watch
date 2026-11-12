"""
Room temperature logger: read LG room temperature and store in SQLite.

Timestamps are stored as UTC ISO-8601 strings with an explicit ``+00:00`` offset and seconds precision, so plain
string comparison orders them correctly. Rows written by older versions (naive local time) are migrated on start-up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lg_thermal_watch.controller import describe_error, extract_temperature

if TYPE_CHECKING:
    from lg_thermal_watch.agent import LGPC18SKAgent

_LOGGER = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(
    Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "lg-thermal-watch" / "room_temps.db"
)
DEFAULT_INTERVAL_S = 60.0
# Sentinels for open-ended windows: every canonical timestamp sorts between these.
_TS_MIN = ""
_TS_MAX = "9999"


def utc_iso(value: datetime) -> str:
    """Return the canonical stored form of ``value``: UTC, seconds precision; naive values are system-local."""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_ts(value: str) -> str:
    """Parse an ISO-8601 timestamp (naive means system-local) into the canonical stored form."""
    return utc_iso(datetime.fromisoformat(value.strip()))


class RoomTempLogger:
    """Logs room temperature readings from the LG PC18SK agent into SQLite."""

    def __init__(
        self,
        agent: LGPC18SKAgent | None,
        db_path: str = DEFAULT_DB_PATH,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Initialise the logger; ``agent`` may be None for read-only use (history without device access)."""
        self.agent = agent
        self.db_path = db_path
        self.interval_s = interval_s
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self.last_reading_at: datetime | None = None
        self.last_error: str | None = None
        # Callbacks (ts_iso, status) notified after every successful device-status read; the runtime
        # tracker uses these to follow power changes without any extra LG calls.
        self.status_observers: list[Callable[[str, dict[str, Any]], None]] = []
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the readings database."""
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self) -> None:
        """Create tables and indexes, and migrate legacy naive-local timestamps to UTC."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS room_temps ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, "
                "temp_c REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_room_temps_ts ON room_temps (ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS watch_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, "
                "kind TEXT NOT NULL, "
                "detail TEXT NOT NULL, "
                "temp_c REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_events_ts ON watch_events (ts)")
            legacy = conn.execute(
                "SELECT id, ts FROM room_temps WHERE ts NOT LIKE '%+00:00' OR length(ts) != 25"
            ).fetchall()
            if legacy:
                conn.executemany(
                    "UPDATE room_temps SET ts = ? WHERE id = ?", [(normalize_ts(ts), row_id) for row_id, ts in legacy]
                )
                _LOGGER.info("Migrated %d legacy readings to UTC timestamps", len(legacy))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ writing

    def add_reading(self, temp_c: float, at: datetime | None = None) -> None:
        """Store one reading (``at`` defaults to now)."""
        ts = utc_iso(at or datetime.now(timezone.utc))
        conn = self._connect()
        try:
            conn.execute("INSERT INTO room_temps (ts, temp_c) VALUES (?, ?)", (ts, float(temp_c)))
            conn.commit()
        finally:
            conn.close()

    def record_event(self, kind: str, detail: str, temp_c: float | None = None, at: datetime | None = None) -> None:
        """Store a night-watch event (heat_start, heat_stop, error)."""
        ts = utc_iso(at or datetime.now(timezone.utc))
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO watch_events (ts, kind, detail, temp_c) VALUES (?, ?, ?, ?)",
                (ts, kind, detail, None if temp_c is None else float(temp_c)),
            )
            conn.commit()
        finally:
            conn.close()

    async def _read_and_store(self) -> None:
        """Read one device status, store the room temperature and notify status observers."""
        if self.agent is None:
            raise RuntimeError("no device agent configured")
        status = await self.agent.get_device_status()
        if status is None:
            return
        now = datetime.now(timezone.utc)
        temp = extract_temperature(status, "currentTemperature")
        if temp is not None:
            self.add_reading(float(temp), at=now)
            self.last_reading_at = now
        for observer in self.status_observers:
            try:
                observer(utc_iso(now), status)
            except Exception as exc:  # a broken observer must never stop the logger
                _LOGGER.warning("Status observer failed: %s", describe_error(exc))

    async def _run(self) -> None:
        """Logger event loop; a failed read is logged and retried on the next interval."""
        while self._running:
            try:
                await self._read_and_store()
            except Exception as exc:  # keep logging through LG/network hiccups
                self.last_error = describe_error(exc)
                _LOGGER.warning("Temperature read failed: %s", self.last_error)
            else:
                self.last_error = None
            await asyncio.sleep(self.interval_s)

    async def start(self) -> None:
        """Start the logger loop."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="lg-temp-logger")

    async def stop(self) -> None:
        """Stop the logger loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def is_running(self) -> bool:
        """Return True when the loop task is alive."""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _bounds(start: str | None, end: str | None) -> tuple[str, str]:
        """Normalise optional window bounds into canonical comparable strings."""
        return (normalize_ts(start) if start else _TS_MIN, normalize_ts(end) if end else _TS_MAX)

    def get_min_temp(self, start: str | None = None, end: str | None = None) -> float | None:
        """Return the minimum recorded temperature within an optional window."""
        lo, hi = self._bounds(start, end)
        conn = self._connect()
        try:
            row = conn.execute("SELECT MIN(temp_c) FROM room_temps WHERE ts >= ? AND ts <= ?", (lo, hi)).fetchone()
            return float(row[0]) if row is not None and row[0] is not None else None
        finally:
            conn.close()

    def get_readings(
        self, start: str | None = None, end: str | None = None, bucket_s: int | None = None
    ) -> list[dict[str, Any]]:
        """Return readings within an optional window, oldest first; ``bucket_s`` averages into time buckets."""
        lo, hi = self._bounds(start, end)
        conn = self._connect()
        try:
            if not bucket_s or bucket_s <= 1:
                rows = conn.execute(
                    "SELECT ts, temp_c FROM room_temps WHERE ts >= ? AND ts <= ? ORDER BY ts", (lo, hi)
                ).fetchall()
                return [{"ts": ts, "temp_c": float(temp)} for ts, temp in rows]
            rows = conn.execute(
                "SELECT CAST(AVG(CAST(strftime('%s', ts) AS INTEGER)) AS INTEGER), AVG(temp_c), MIN(temp_c), "
                "MAX(temp_c), COUNT(*) FROM room_temps WHERE ts >= ? AND ts <= ? "
                "GROUP BY CAST(strftime('%s', ts) AS INTEGER) / ? ORDER BY 1",
                (lo, hi, int(bucket_s)),
            ).fetchall()
            return [
                {
                    "ts": datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds"),
                    "temp_c": round(float(avg), 2),
                    "min_c": float(lo_c),
                    "max_c": float(hi_c),
                    "n": int(n),
                }
                for epoch, avg, lo_c, hi_c, n in rows
            ]
        finally:
            conn.close()

    def get_stats(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Return count, min/max (with when they last occurred) and mean over an optional window."""
        lo, hi = self._bounds(start, end)
        conn = self._connect()
        try:
            count, mean = conn.execute(
                "SELECT COUNT(*), AVG(temp_c) FROM room_temps WHERE ts >= ? AND ts <= ?", (lo, hi)
            ).fetchone()
            low = conn.execute(
                "SELECT ts, temp_c FROM room_temps WHERE ts >= ? AND ts <= ? ORDER BY temp_c ASC, ts DESC LIMIT 1",
                (lo, hi),
            ).fetchone()
            high = conn.execute(
                "SELECT ts, temp_c FROM room_temps WHERE ts >= ? AND ts <= ? ORDER BY temp_c DESC, ts DESC LIMIT 1",
                (lo, hi),
            ).fetchone()
            latest = conn.execute("SELECT ts, temp_c FROM room_temps ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return {
            "count": int(count or 0),
            "mean_c": round(float(mean), 2) if mean is not None else None,
            "min": {"ts": low[0], "temp_c": float(low[1])} if low else None,
            "max": {"ts": high[0], "temp_c": float(high[1])} if high else None,
            "latest": {"ts": latest[0], "temp_c": float(latest[1])} if latest else None,
        }

    def get_events(self, start: str | None = None, end: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Return night-watch events within an optional window, oldest first."""
        lo, hi = self._bounds(start, end)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ts, kind, detail, temp_c FROM ("
                "SELECT ts, kind, detail, temp_c FROM watch_events WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?"
                ") ORDER BY ts",
                (lo, hi, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        return [{"ts": ts, "kind": kind, "detail": detail, "temp_c": temp} for ts, kind, detail, temp in rows]

    def get_readings_count(self) -> int:
        """Return the number of stored readings."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM room_temps").fetchone()
            return int(row[0]) if row is not None else 0
        finally:
            conn.close()
