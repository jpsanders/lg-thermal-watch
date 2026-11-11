"""Unit tests for the runtime session tracker."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from lg_thermal_watch.runtime import RuntimeTracker

UTC = timezone.utc
STATUS_ON = {"operation": {"airConOperationMode": "POWER_ON"}}
STATUS_OFF = {"operation": {"airConOperationMode": "POWER_OFF"}}


@pytest.fixture
def tracker(tmp_path: Path) -> RuntimeTracker:
    """A tracker over a temporary database, fixed clock at 2026-11-15 12:00 UTC, generous gap."""
    fixed_now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    return RuntimeTracker(
        db_path=str(tmp_path / "runtime.db"),
        now_fn=lambda: fixed_now,
        tz_name="UTC",
        gap_split_s=3600.0,
    )


def test_open_and_close(tracker: RuntimeTracker) -> None:
    """Powered-on observations open a session; the next powered-off observation closes it."""
    tracker.observe("2026-11-15T10:00:00+00:00", STATUS_OFF)
    assert tracker.current_session() is None
    tracker.observe("2026-11-15T10:01:00+00:00", STATUS_ON)
    current = tracker.current_session()
    assert current is not None
    assert current["started_at"] == "2026-11-15T10:01:00+00:00"
    assert current["duration_s"] == pytest.approx(7140.0)  # fixed clock reads 12:00
    tracker.observe("2026-11-15T10:31:00+00:00", STATUS_OFF)
    assert tracker.current_session() is None
    recent = tracker.recent_sessions()
    assert len(recent) == 1
    assert recent[0]["duration_s"] == pytest.approx(1800.0)
    assert recent[0]["ended_at"] == "2026-11-15T10:31:00+00:00"


def test_unknown_power_state_is_ignored(tracker: RuntimeTracker) -> None:
    """A missing status or a status without an operation mode changes nothing."""
    tracker.observe("2026-11-15T10:00:00+00:00", None)
    tracker.observe("2026-11-15T10:00:00+00:00", {"operation": {}})
    assert tracker.current_session() is None
    tracker.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    assert tracker.current_session() is not None
    tracker.observe("2026-11-15T10:01:00+00:00", {"temperatureInUnits": [{"unit": "C", "currentTemperature": 21}]})
    assert tracker.current_session() is not None


def test_restart_keeps_open_session(tmp_path: Path) -> None:
    """A new tracker on the same database resumes the open session from the previous process."""
    first = RuntimeTracker(db_path=str(tmp_path / "runtime.db"), now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    first.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    second = RuntimeTracker(
        db_path=str(tmp_path / "runtime.db"), now_fn=lambda: datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    )
    current = second.current_session()
    assert current is not None
    assert current["started_at"] == "2026-11-15T10:00:00+00:00"
    second.observe("2026-11-15T10:05:00+00:00", STATUS_OFF)
    recent = second.recent_sessions()
    assert len(recent) == 1
    assert recent[0]["duration_s"] == pytest.approx(300.0)


def test_gap_between_observations_is_not_credited(tracker: RuntimeTracker) -> None:
    """Unobserved gaps (service down) end the open session at its last observation."""
    tracker.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    tracker.observe("2026-11-15T11:01:00+00:00", STATUS_ON)  # 61 min later, beyond the 60 min split
    current = tracker.current_session()
    assert current is not None
    assert current["started_at"] == "2026-11-15T11:01:00+00:00"
    closed = tracker.recent_sessions()
    assert len(closed) == 1
    assert closed[0]["ended_at"] == "2026-11-15T10:00:00+00:00"
    assert closed[0]["duration_s"] == pytest.approx(0.0)
    tracker.observe("2026-11-15T11:05:00+00:00", STATUS_OFF)
    recent = tracker.recent_sessions(10)
    assert recent[0]["duration_s"] == pytest.approx(240.0)  # 11:01 -> 11:05


def test_gap_before_off_ends_at_last_seen(tmp_path: Path) -> None:
    """A powered-off observation after a long gap closes the session at its last observed moment."""
    t = RuntimeTracker(
        db_path=str(tmp_path / "runtime.db"), now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC), gap_split_s=60.0
    )
    t.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    t.observe("2026-11-15T10:02:00+00:00", STATUS_OFF)  # 2 min gap > 60 s split
    recent = t.recent_sessions()
    assert len(recent) == 1
    assert recent[0]["ended_at"] == "2026-11-15T10:00:00+00:00"
    assert recent[0]["duration_s"] == pytest.approx(0.0)
    assert t.current_session() is None


def test_source_fn_labels_sessions(tmp_path: Path) -> None:
    """The source callback is evaluated when a session opens."""
    source = {"value": "manual"}
    t = RuntimeTracker(
        db_path=str(tmp_path / "runtime.db"),
        now_fn=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        source_fn=lambda: source["value"],
        tz_name="UTC",
    )
    t.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    current = t.current_session()
    assert current is not None and current["source"] == "manual"
    t.observe("2026-11-15T10:01:00+00:00", STATUS_OFF)
    source["value"] = "watch"
    t.observe("2026-11-15T10:02:00+00:00", STATUS_ON)
    current = t.current_session()
    assert current is not None and current["source"] == "watch"


def test_monthly_usage_aggregates(tracker: RuntimeTracker) -> None:
    """Closed and open sessions bucket by start month; kWh is hours times the assumed draw."""
    tracker.observe("2026-10-05T10:00:00+00:00", STATUS_ON)
    tracker.observe("2026-10-05T10:30:00+00:00", STATUS_OFF)
    tracker.observe("2026-11-01T09:00:00+00:00", STATUS_ON)
    tracker.observe("2026-11-01T10:00:00+00:00", STATUS_OFF)
    tracker.observe("2026-11-15T11:00:00+00:00", STATUS_ON)  # still open at the fixed clock (12:00)
    months = tracker.monthly_usage(months=3, avg_power_kw=2.0)
    assert [m["label"] for m in months] == ["Sep 2026", "Oct 2026", "Nov 2026"]
    assert [m["month"] for m in months] == ["2026-09", "2026-10", "2026-11"]
    assert [m["sessions"] for m in months] == [0, 1, 2]
    assert [m["hours"] for m in months] == [0.0, 0.5, 2.0]
    assert [m["kwh"] for m in months] == [0.0, 1.0, 4.0]


def test_monthly_usage_buckets_in_the_watch_zone(tmp_path: Path) -> None:
    """Month buckets follow the configured zone, not UTC: NZDT evenings start the next month."""
    t = RuntimeTracker(
        db_path=str(tmp_path / "runtime.db"),
        now_fn=lambda: datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("Pacific/Auckland")),
        tz_name="Pacific/Auckland",
        gap_split_s=3600.0,
    )
    # 2026-10-31 13:00 UTC is 2026-11-01 02:00 NZDT: November in the room's zone.
    t.observe("2026-10-31T13:00:00+00:00", STATUS_ON)
    t.observe("2026-10-31T13:30:00+00:00", STATUS_OFF)
    months = t.monthly_usage(months=2, avg_power_kw=1.0)
    assert [(m["month"], m["sessions"], m["hours"], m["kwh"]) for m in months] == [
        ("2026-10", 0, 0.0, 0.0),
        ("2026-11", 1, 0.5, 0.5),
    ]


def test_monthly_usage_ignores_sessions_outside_window(tracker: RuntimeTracker) -> None:
    """Sessions older than the requested window are not counted."""
    tracker.observe("2026-05-01T10:00:00+00:00", STATUS_ON)
    tracker.observe("2026-05-01T11:00:00+00:00", STATUS_OFF)
    months = tracker.monthly_usage(months=3, avg_power_kw=1.0)
    assert all(m["sessions"] == 0 and m["hours"] == 0.0 for m in months)
    assert [m["month"] for m in months] == ["2026-09", "2026-10", "2026-11"]


def test_statuses_without_power_are_never_stored(tracker: RuntimeTracker) -> None:
    """Observations that cannot say whether the unit is on never open or close a session."""
    tracker.observe("2026-11-15T10:00:00+00:00", STATUS_ON)
    before: dict[str, Any] = tracker.current_session() or {}
    tracker.observe("2026-11-15T10:02:00+00:00", {"operation": {"airConOperationMode": None}})
    after = tracker.current_session() or {}
    assert after.get("started_at") == before.get("started_at")
