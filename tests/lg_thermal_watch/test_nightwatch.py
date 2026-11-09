"""Unit tests for the LGPC18SKNightWatch automation loop."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from lg_thermal_watch.nightwatch import LGPC18SKNightWatch


class MockAgent:
    """Scripted mock of the LGPC18SKAgent control interface."""

    def __init__(self) -> None:
        """Initialise the scripted agent state."""
        self.room_temp: float | None = 21.5
        self.fan_speeds: list[str] = []
        self.modes: list[str] = []
        self.target_temps: list[float] = []
        self.power_states: list[str] = []
        self.heating_started = False
        self.turned_off = False

    async def get_room_temperature(self) -> float | None:
        """Return the scripted room temperature."""
        return self.room_temp

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Record fan speed."""
        self.fan_speeds.append(speed)
        return {"ok": True}

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Record HVAC mode."""
        self.modes.append(mode)
        return {"ok": True}

    async def set_target_temperature(self, value_c: float) -> dict[str, Any] | None:
        """Record target temperature."""
        self.target_temps.append(value_c)
        return {"ok": True}

    async def turn_on(self) -> dict[str, Any] | None:
        """Record power on."""
        self.power_states.append("on")
        return {"ok": True}

    async def turn_off(self) -> dict[str, Any] | None:
        """Record power off."""
        self.turned_off = True
        self.power_states.append("off")
        return {"ok": True}


def make_watch(agent: MockAgent, **kwargs: Any) -> LGPC18SKNightWatch:
    """Build a night watch with a scripted agent and deterministic time source."""
    watch = LGPC18SKNightWatch(
        agent,  # type: ignore[arg-type]
        low_threshold_c=18.0,
        high_target_c=22.0,
        poll_interval_s=0.05,
        night_start_hour=0,
        night_end_hour=7,
        now_fn=lambda: datetime(2026, 11, 6, 2, 0, 0),
        **kwargs,
    )
    return watch


class TestLGPC18SKNightWatch:
    def test_room_temp_above_low_threshold_does_not_heat(self) -> None:
        """Room temperature above the low threshold must not trigger heat."""
        agent = MockAgent()
        agent.room_temp = 20.0
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.fan_speeds == []
        assert agent.modes == []
        assert agent.turned_off is False

    def test_room_temp_below_low_threshold_triggers_gentle_heat(self) -> None:
        """Room temperature below the low threshold triggers gentle heat with low fan."""
        agent = MockAgent()
        agent.room_temp = 16.5
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.fan_speeds == ["LOW"]
        assert agent.modes == ["heat"]
        assert agent.target_temps == [22.0]
        assert agent.turned_off is False

    def test_heating_stops_once_high_target_reached(self) -> None:
        """Heating stops once the room reaches the high target temperature."""
        agent = MockAgent()
        agent.room_temp = 16.5
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.room_temp = 22.0
        asyncio.run(watch._tick())
        assert agent.turned_off is True
