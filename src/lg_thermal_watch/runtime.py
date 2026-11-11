"""
Runtime tracker: when the heat pump is on, for how long, and estimated energy per month.

The tracker observes the device-status polls the temperature logger already makes and opens a
session when the unit powers on, closing it on the next powered-off observation. Sessions live in
the same SQLite database as the temperature readings, so an open session survives a restart.

Only observed time is credited: when two observations of an open session are further apart than
``gap_split_s`` (the service was down, or polling stalled), the gap is not counted — the open
session ends at its last observed moment, and the next powered-on observation starts a fresh one.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lg_thermal_watch.controller import is_powered_on
from lg_thermal_watch.nightwatch import system_timezone_name
from lg_thermal_watch.tempdb import utc_iso

_LOGGER = logging.getLogger(__name__)

# Observations of an open session further apart than this end the session at the last observation
# (the gap is unobserved downtime, so it is not credited as running time).
DEFAULT_GAP_SPLIT_S = 600.0


def _shift_month(month_start: datetime, delta: int) -> datetime:
    """Shift a month-start datetime by ``delta`` months, keeping its zone."""
    total = month_start.month - 1 + delta
    return month_start.replace(year=month_start.year + total // 12, month=total % 12 + 1)


class RuntimeTracker:
    """Records heat-pump on/off sessions from status observations and estimates monthly energy."""

    def __init__(
        self,
        db_path: str,
        *,
        source_fn: Callable[[], str] | None = None,
        tz_name: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
        gap_split_s: float = DEFAULT_GAP_SPLIT_S,
    ) -> None:
        """Initialise the tracker; ``tz_name`` buckets calendar months (default: the system zone)."""
        self.db_path = db_path
        self.source_fn: Callable[[], str] = source_fn or (lambda: "manual")
        self.tz_name = tz_name
        self.now_fn: Callable[[], datetime] = now_fn or (lambda: datetime.now(timezone.utc))
        self.gap_split_s = gap_split_s
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection to the runtime database."""
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self) -> None:
        """Create the sessions table."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runtime_sessions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "started_at TEXT NOT NULL, "
                "last_seen_at TEXT NOT NULL, "
                "ended_at TEXT, "
                "duration_s REAL, "
                "source TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_sessions_started ON runtime_sessions (started_at)")
            conn.commit()
        finally:
            conn.close()

    def _resolve_zone(self, tz_name: str | None) -> ZoneInfo:
        """Resolve a zone for month bucketing: explicit, else the instance default, else system, else UTC."""
        name = tz_name or self.tz_name or system_timezone_name() or "UTC"
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            _LOGGER.warning("Unknown timezone %r; bucketing months in UTC", name)
            return ZoneInfo("UTC")

    # ------------------------------------------------------------------ observing

    def observe(self, ts: str, status: dict[str, Any] | None) -> None:
        """Record one status observation (called for every device-status poll)."""
        if status is None:
            return
        on = is_powered_on(status)
        if on is None:
            return
        at = datetime.fromisoformat(ts)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, started_at, last_seen_at FROM runtime_sessions "
                "WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if on:
                if row is None:
                    self._open(conn, at)
                else:
                    self._continue(conn, row, at)
            elif row is not None:
                self._close(conn, row, at)
            conn.commit()
        finally:
            conn.close()

    def _open(self, conn: sqlite3.Connection, at: datetime) -> None:
        """Start a new open session at ``at``."""
        conn.execute(
            "INSERT INTO runtime_sessions (started_at, last_seen_at, source) VALUES (?, ?, ?)",
            (utc_iso(at), utc_iso(at), self.source_fn()),
        )

    def _continue(self, conn: sqlite3.Connection, row: tuple[Any, ...], at: datetime) -> None:
        """Keep the open session alive, splitting it when the observations have an unexplained gap."""
        session_id, started_at, last_seen_at = row
        last_seen = datetime.fromisoformat(last_seen_at)
        if (at - last_seen).total_seconds() > self.gap_split_s:
            self._end(conn, int(session_id), str(started_at), last_seen)
            self._open(conn, at)
        else:
            conn.execute("UPDATE runtime_sessions SET last_seen_at = ? WHERE id = ?", (utc_iso(at), session_id))

    def _close(self, conn: sqlite3.Connection, row: tuple[Any, ...], at: datetime) -> None:
        """End the open session: at ``at``, or at its last observation when ``at`` follows a gap."""
        session_id, started_at, last_seen_at = row
        last_seen = datetime.fromisoformat(last_seen_at)
        gap = (at - last_seen).total_seconds() > self.gap_split_s
        self._end(conn, int(session_id), str(started_at), last_seen if gap else at)

    def _end(self, conn: sqlite3.Connection, session_id: int, started_at: str, ended: datetime) -> None:
        """Finalise a session with its end timestamp and duration."""
        duration_s = max(0.0, (ended - datetime.fromisoformat(started_at)).total_seconds())
        conn.execute(
            "UPDATE runtime_sessions SET ended_at = ?, duration_s = ? WHERE id = ?",
            (utc_iso(ended), duration_s, session_id),
        )

    # ------------------------------------------------------------------ querying

    def current_session(self) -> dict[str, Any] | None:
        """Return the open session (the unit is on), or None when it is off."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, started_at, last_seen_at, source FROM runtime_sessions "
                "WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        session_id, started_at, last_seen_at, source = row
        started = datetime.fromisoformat(str(started_at))
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.astimezone()
        return {
            "id": int(session_id),
            "started_at": str(started_at),
            "last_seen_at": str(last_seen_at),
            "source": str(source),
            "duration_s": max(0.0, (now - started).total_seconds()),
        }

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent completed sessions, newest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, started_at, ended_at, duration_s, source FROM runtime_sessions "
                "WHERE ended_at IS NOT NULL ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": int(session_id),
                "started_at": str(started_at),
                "ended_at": str(ended_at),
                "duration_s": float(duration_s or 0.0),
                "source": str(source),
            }
            for session_id, started_at, ended_at, duration_s, source in rows
        ]

    def monthly_usage(
        self, months: int = 6, avg_power_kw: float = 1.0, tz_name: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Return per-calendar-month hours and estimated kWh, oldest first.

        Sessions are bucketed by the month they started in (the watch zone); an open session
        contributes its running time to its start month.
        """
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.astimezone()
        zone = self._resolve_zone(tz_name)
        month_start = now.astimezone(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        starts = [_shift_month(month_start, -(months - 1 - i)) for i in range(months)]
        buckets: list[dict[str, Any]] = []
        index: dict[tuple[int, int], int] = {}
        for i, start in enumerate(starts):
            buckets.append(
                {
                    "month": f"{start.year:04d}-{start.month:02d}",
                    "label": start.strftime("%b %Y"),
                    "sessions": 0,
                    "hours": 0.0,
                    "kwh": 0.0,
                }
            )
            index[(start.year, start.month)] = i
        lower = starts[0].astimezone(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT started_at, ended_at, duration_s FROM runtime_sessions "
                "WHERE started_at >= ? ORDER BY started_at",
                (lower,),
            ).fetchall()
        finally:
            conn.close()
        for started_at, ended_at, duration_s in rows:
            started = datetime.fromisoformat(str(started_at))
            if duration_s is None:  # an open session runs until now
                duration_s = max(0.0, (now.astimezone(timezone.utc) - started).total_seconds())
            slot = index.get((started.astimezone(zone).year, started.astimezone(zone).month))
            if slot is None:
                continue
            buckets[slot]["sessions"] += 1
            buckets[slot]["hours"] += float(duration_s) / 3600.0
        for bucket in buckets:
            bucket["hours"] = round(float(bucket["hours"]), 2)
            bucket["kwh"] = round(float(bucket["hours"]) * avg_power_kw, 2)
        return buckets
