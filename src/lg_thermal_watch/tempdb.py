"""Room temperature logger: read LG room temperature and store in SQLite."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Any

from lg_thermal_watch.agent import LGPC18SKAgent

_LOGGER = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/tmp/opencode/lg_thinq_room_temps.db"  # nosec B108
DEFAULT_INTERVAL_S = 60.0


class RoomTempLogger:
    """Logs room temperature readings from the LG PC18SK agent into SQLite."""

    def __init__(
        self,
        agent: LGPC18SKAgent,
        db_path: str = DEFAULT_DB_PATH,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Initialise the logger."""
        self.agent = agent
        self.db_path = db_path
        self.interval_s = interval_s
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._init_db()

    def _init_db(self) -> None:
        """Create the readings table if it does not exist."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS room_temps ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, "
                "temp_c REAL NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    async def _read_and_store(self) -> None:
        """Read one room temperature and store it."""
        temp = await self.agent.get_room_temperature()
        if temp is None:
            return
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.execute(
                "INSERT INTO room_temps (ts, temp_c) VALUES (?, ?)",
                (datetime.now().isoformat(), float(temp)),
            )
            conn.commit()
        finally:
            conn.close()

    async def _run(self) -> None:
        """Logger event loop."""
        while self._running:
            await self._read_and_store()
            await asyncio.sleep(self.interval_s)

    async def start(self) -> None:
        """Start the logger loop."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

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

    def get_min_temp(self, start: str | None = None, end: str | None = None) -> float | None:
        """Return the minimum recorded temperature within an optional window."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            if start is not None and end is not None:
                row = conn.execute(
                    "SELECT MIN(temp_c) FROM room_temps WHERE ts >= ? AND ts <= ?",
                    (start, end),
                ).fetchone()
            elif start is not None:
                row = conn.execute(
                    "SELECT MIN(temp_c) FROM room_temps WHERE ts >= ?",
                    (start,),
                ).fetchone()
            elif end is not None:
                row = conn.execute(
                    "SELECT MIN(temp_c) FROM room_temps WHERE ts <= ?",
                    (end,),
                ).fetchone()
            else:
                row = conn.execute("SELECT MIN(temp_c) FROM room_temps").fetchone()
            return float(row[0]) if row is not None and row[0] is not None else None
        finally:
            conn.close()

    def get_readings(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """Return readings within an optional window, oldest first."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            if start is not None and end is not None:
                rows = conn.execute(
                    "SELECT ts, temp_c FROM room_temps WHERE ts >= ? AND ts <= ? ORDER BY ts",
                    (start, end),
                ).fetchall()
            elif start is not None:
                rows = conn.execute(
                    "SELECT ts, temp_c FROM room_temps WHERE ts >= ? ORDER BY ts",
                    (start,),
                ).fetchall()
            elif end is not None:
                rows = conn.execute(
                    "SELECT ts, temp_c FROM room_temps WHERE ts <= ? ORDER BY ts",
                    (end,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT ts, temp_c FROM room_temps ORDER BY ts").fetchall()
            return [{"ts": ts, "temp_c": float(temp)} for ts, temp in rows]
        finally:
            conn.close()

    def get_readings_count(self) -> int:
        """Return the number of stored readings."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            row = conn.execute("SELECT COUNT(*) FROM room_temps").fetchone()
            return int(row[0]) if row is not None else 0
        finally:
            conn.close()
