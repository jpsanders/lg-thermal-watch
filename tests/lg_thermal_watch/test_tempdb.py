"""Unit tests for the RoomTempLogger SQLite persistence."""

from __future__ import annotations

import asyncio
import os

from lg_thermal_watch.tempdb import RoomTempLogger


class MockAgent:
    """Mock of the agent temperature source."""

    def __init__(self, temps: list[float]) -> None:
        """Initialise the scripted readings."""
        self.temps = list(temps)

    async def get_room_temperature(self) -> float | None:
        """Return the next scripted temperature."""
        if not self.temps:
            return None
        return self.temps.pop(0)


class TestRoomTempLogger:
    def test_logs_readings_and_reports_min(self) -> None:
        """Readings are stored and the minimum is queryable."""
        db_path = "/tmp/opencode/lg_thinq_room_temps_test.db"
        if os.path.exists(db_path):
            os.remove(db_path)
        agent = MockAgent([21.5, 22.0, 20.5, 23.0])
        logger = RoomTempLogger(agent, db_path=db_path, interval_s=0.05)  # type: ignore[arg-type]
        asyncio.run(self._run(logger))
        assert logger.get_readings_count() >= 1
        minimum = logger.get_min_temp()
        assert minimum is not None
        assert abs(minimum - 20.5) < 1e-9
        os.remove(db_path)

    @staticmethod
    async def _run(logger: RoomTempLogger) -> None:
        """Run the logger briefly against the mock agent."""
        await logger.start()
        await asyncio.sleep(0.25)
        await logger.stop()

    def test_min_temp_over_window(self) -> None:
        """Minimum temperature is queryable over an arbitrary window."""
        db_path = "/tmp/opencode/lg_thinq_room_temps_test.db"
        if os.path.exists(db_path):
            os.remove(db_path)
        agent = MockAgent([21.5, 22.0, 20.5, 23.0])
        logger = RoomTempLogger(agent, db_path=db_path, interval_s=0.05)  # type: ignore[arg-type]
        asyncio.run(self._run(logger))
        minimum = logger.get_min_temp(start=None, end=None)
        assert minimum is not None
        assert abs(minimum - 20.5) < 1e-9
        assert logger.get_readings(start=None, end=None) is not None
        os.remove(db_path)
