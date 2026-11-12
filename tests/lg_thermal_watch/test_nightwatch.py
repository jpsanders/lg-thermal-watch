"""Unit tests for the LGPC18SKNightWatch automation loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from lg_thermal_watch.nightwatch import LGPC18SKNightWatch, heating_plan


class MockAgent:
    """Scripted mock of the LGPC18SKAgent control interface, tracking power state like the real unit."""

    def __init__(
        self,
        room_temp: float | None = 21.5,
        power: str = "POWER_OFF",
        job_mode: str | None = None,
        target: float = 26,
        fan: str | None = None,
    ) -> None:
        """Initialise the scripted agent state; mode, target and fan are remembered while off, like the unit."""
        self.room_temp = room_temp
        self.power = power
        self.job_mode = job_mode
        self.target = target
        self.fan = fan
        self.calls: list[str] = []
        self.fail_on: set[str] = set()
        self.fail_status = False
        self.budget = 99

    def commands_available(self) -> int:
        """Return the scripted command budget."""
        return self.budget

    def _maybe_fail(self, name: str) -> None:
        """Raise once for a command scheduled to fail."""
        if name in self.fail_on:
            self.fail_on.discard(name)
            raise RuntimeError(f"{name} failed")

    async def get_device_status(self, *, max_age_s: float | None = None) -> dict[str, Any] | None:
        """Return a ThinQ-shaped status for the scripted state."""
        if self.fail_status:
            raise ConnectionError("LG cloud unreachable")
        status: dict[str, Any] = {"operation": {"airConOperationMode": self.power}}
        if self.job_mode is not None:
            status["airConJobMode"] = {"currentJobMode": self.job_mode}
        if self.fan is not None:
            status["airFlow"] = {"windStrength": self.fan}
        if self.room_temp is not None:
            status["temperatureInUnits"] = [
                {"currentTemperature": self.room_temp, "targetTemperature": self.target, "unit": "C"}
            ]
        return status

    async def turn_on(self) -> dict[str, Any] | None:
        """Record power on."""
        self._maybe_fail("turn_on")
        self.calls.append("turn_on")
        self.power = "POWER_ON"
        return {"ok": True}

    async def turn_off(self) -> dict[str, Any] | None:
        """Record power off."""
        self._maybe_fail("turn_off")
        self.calls.append("turn_off")
        self.power = "POWER_OFF"
        return {"ok": True}

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Record HVAC mode; like LG, refuse while the unit is off."""
        self._maybe_fail("set_hvac_mode")
        if self.power != "POWER_ON":
            raise RuntimeError("COMMAND_NOT_SUPPORTED_IN_POWER_OFF (2304)")
        self.calls.append(f"set_hvac_mode:{mode}")
        self.job_mode = mode.upper()
        return {"ok": True}

    async def set_target_temperature(self, value_c: float, *, job_mode: str | None = None) -> dict[str, Any] | None:
        """Record target temperature; like LG, refuse while the unit is off."""
        self._maybe_fail("set_target_temperature")
        if self.power != "POWER_ON":
            raise RuntimeError("COMMAND_NOT_SUPPORTED_IN_POWER_OFF (2304)")
        self.calls.append(f"set_target_temperature:{value_c}:{job_mode}")
        self.target = value_c
        return {"ok": True}

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Record fan speed; like LG, refuse while the unit is off."""
        self._maybe_fail("set_fan_speed")
        if self.power != "POWER_ON":
            raise RuntimeError("COMMAND_NOT_SUPPORTED_IN_POWER_OFF (2304)")
        self.calls.append(f"set_fan_speed:{speed}")
        self.fan = speed.upper()
        return {"ok": True}


class Clock:
    """Mutable clock for deterministic window tests."""

    def __init__(self, value: datetime) -> None:
        """Initialise the clock."""
        self.value = value

    def __call__(self) -> datetime:
        """Return the current fake time."""
        return self.value


HEAT_SEQUENCE = ["turn_on", "set_hvac_mode:heat", "set_target_temperature:22.0:HEAT", "set_fan_speed:LOW"]


def make_watch(agent: MockAgent, clock: Clock | None = None, **kwargs: Any) -> LGPC18SKNightWatch:
    """Build a night watch over a scripted agent with a deterministic time source (02:00 Lisbon by default)."""
    options: dict[str, Any] = {
        "low_threshold_c": 20.0,
        "high_target_c": 22.0,
        "poll_interval_s": 0.01,
        "night_start_hour": 0,
        "night_end_hour": 7,
        "timezone_name": "Europe/Lisbon",
        "power_on_settle_s": 0,
        "now_fn": clock or Clock(datetime(2026, 11, 10, 2, 0, 0)),
    }
    options.update(kwargs)
    return LGPC18SKNightWatch(agent, **options)  # type: ignore[arg-type]


class TestHeatingDecisions:
    def test_room_above_low_threshold_does_not_heat(self) -> None:
        """Room temperature above the low threshold must not trigger heat."""
        agent = MockAgent(room_temp=20.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == []
        assert watch.is_heating is False

    def test_cold_room_with_unit_off_powers_on_before_anything_else(self) -> None:
        """LG rejects commands while off (2304), so power-on must come first, then heat/target/fan."""
        agent = MockAgent(room_temp=19.5, power="POWER_OFF")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE
        assert watch.is_heating is True

    def test_cold_room_with_unit_on_skips_power_on(self) -> None:
        """An already-running unit is just switched to gentle heat."""
        agent = MockAgent(room_temp=19.5, power="POWER_ON")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE[1:]

    def test_heating_stops_once_high_target_reached(self) -> None:
        """Heating stops (unit off) once the room reaches the high target."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.room_temp = 21.5
        asyncio.run(watch._tick())
        assert agent.calls[-1] != "turn_off"
        agent.room_temp = 22.0
        asyncio.run(watch._tick())
        assert agent.calls[-1] == "turn_off"
        assert agent.power == "POWER_OFF"
        assert watch.is_heating is False

    def test_does_not_retrigger_between_thresholds(self) -> None:
        """After stopping at the high target, heat only restarts below the low threshold (hysteresis)."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.room_temp = 22.0
        asyncio.run(watch._tick())
        calls = len(agent.calls)
        agent.room_temp = 20.5
        asyncio.run(watch._tick())
        assert len(agent.calls) == calls

    def test_missing_room_temperature_is_skipped(self) -> None:
        """A status without a room temperature never triggers commands."""
        agent = MockAgent(room_temp=None)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == []


class TestWindow:
    def test_cold_room_outside_window_is_left_alone(self) -> None:
        """Outside the night window the watch never heats."""
        agent = MockAgent(room_temp=15.0)
        watch = make_watch(agent, Clock(datetime(2026, 11, 10, 12, 0, 0)))
        asyncio.run(watch._tick())
        assert agent.calls == []

    def test_window_end_turns_heating_off(self) -> None:
        """If the watch is still heating when the window closes, it hands the unit back off."""
        agent = MockAgent(room_temp=19.0)
        clock = Clock(datetime(2026, 11, 10, 6, 59, 0))
        watch = make_watch(agent, clock)
        asyncio.run(watch._tick())
        assert watch.is_heating is True
        clock.value = datetime(2026, 11, 10, 7, 0, 0)
        asyncio.run(watch._tick())
        assert agent.calls[-1] == "turn_off"
        assert watch.is_heating is False

    def test_window_wrapping_midnight(self) -> None:
        """A 22:00-07:00 window covers late evening and early morning, not midday."""
        watch = make_watch(MockAgent(), night_start_hour=22, night_end_hour=7)
        assert watch._in_night_window(datetime(2026, 11, 10, 23, 0))
        assert watch._in_night_window(datetime(2026, 11, 10, 3, 0))
        assert not watch._in_night_window(datetime(2026, 11, 10, 12, 0))
        assert not watch._in_night_window(datetime(2026, 11, 10, 7, 0))

    def test_window_is_evaluated_in_configured_timezone(self) -> None:
        """23:30 UTC in July is 00:30 in Lisbon (WEST, UTC+1): inside a Lisbon 00-07 window, outside a UTC one."""
        summer_utc = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
        lisbon = make_watch(MockAgent(), Clock(summer_utc), timezone_name="Europe/Lisbon")
        utc = make_watch(MockAgent(), Clock(summer_utc), timezone_name="UTC")
        assert lisbon._in_night_window(lisbon.now())
        assert not utc._in_night_window(utc.now())

    def test_window_bounds_before_and_during_window(self) -> None:
        """The dashboard's 'next window' is tonight's when before it and the current one while inside it."""
        before = make_watch(MockAgent(), Clock(datetime(2026, 11, 9, 23, 59)))
        start, end = before.window_bounds()
        assert (start.isoformat(), end.isoformat()) == ("2026-11-10T00:00:00+00:00", "2026-11-10T07:00:00+00:00")
        during = make_watch(MockAgent(), Clock(datetime(2026, 11, 10, 3, 0)), night_start_hour=22)
        start, end = during.window_bounds()
        assert (start.isoformat(), end.isoformat()) == ("2026-11-09T22:00:00+00:00", "2026-11-10T07:00:00+00:00")


class TestOutOfWindowGuard:
    """The guard: outside the window the unit must never stay on, unless Heat now granted an allowance."""

    def make(self, agent: MockAgent, clock: Clock, **kwargs: Any) -> tuple[LGPC18SKNightWatch, list[tuple[str, str]]]:
        """Build a watch with a recording event sink (mid-afternoon Lisbon by default)."""
        events: list[tuple[str, str]] = []
        watch = make_watch(agent, clock, event_sink=lambda kind, detail, temp: events.append((kind, detail)), **kwargs)
        return watch, events

    def test_unit_on_outside_window_is_powered_off(self) -> None:
        """An external power-on outside the window (an app schedule, another box) is switched back off."""
        agent = MockAgent(room_temp=23.5, power="POWER_ON", job_mode="HEAT", target=26, fan="HIGH")
        clock = Clock(datetime(2026, 11, 10, 14, 0, 0))
        watch, events = self.make(agent, clock)
        asyncio.run(watch._tick())
        assert agent.calls == ["turn_off"]
        assert agent.power == "POWER_OFF"
        assert watch.is_heating is False
        assert [kind for kind, _ in events] == ["out_of_window_off"]
        assert "outside the 00:00-07:00 window" in events[0][1]

    def test_unit_off_outside_window_is_untouched(self) -> None:
        """When the unit is off out of window, the guard does nothing (and does not poll more than needed)."""
        agent = MockAgent(room_temp=23.5, power="POWER_OFF")
        clock = Clock(datetime(2026, 11, 10, 14, 0, 0))
        watch, events = self.make(agent, clock)
        asyncio.run(watch._tick())
        assert agent.calls == []
        assert events == []

    def test_heat_now_allowance_pauses_the_guard(self) -> None:
        """A daytime allowance (Heat now) lets the unit run; once it expires the guard takes over again."""
        agent = MockAgent(room_temp=23.5, power="POWER_ON", job_mode="HEAT", target=26, fan="HIGH")
        clock = Clock(datetime(2026, 11, 10, 14, 0, 0))
        watch, events = self.make(agent, clock)
        watch.allow_daytime(minutes=120)
        asyncio.run(watch._tick())
        assert agent.calls == []  # still within the two-hour allowance
        clock.value = datetime(2026, 11, 10, 16, 1, 0)
        asyncio.run(watch._tick())
        assert agent.calls == ["turn_off"]
        assert [kind for kind, _ in events] == ["out_of_window_off"]

    def test_clearing_the_allowance_rearms_the_guard(self) -> None:
        """Revoking the allowance (e.g. Turn off) re-arms the guard immediately."""
        agent = MockAgent(room_temp=23.5, power="POWER_ON", job_mode="HEAT", target=26, fan="HIGH")
        clock = Clock(datetime(2026, 11, 10, 14, 0, 0))
        watch, events = self.make(agent, clock)
        watch.allow_daytime(minutes=120)
        watch.clear_day_allowance()
        asyncio.run(watch._tick())
        assert agent.calls == ["turn_off"]
        assert events != []

    def test_guard_can_be_disabled(self) -> None:
        """With the guard disabled the watch leaves the unit alone outside the window (old behaviour)."""
        agent = MockAgent(room_temp=23.5, power="POWER_ON", job_mode="HEAT", target=26, fan="HIGH")
        clock = Clock(datetime(2026, 11, 10, 14, 0, 0))
        watch, events = self.make(agent, clock, enforce_off_hours=False)
        asyncio.run(watch._tick())
        assert agent.calls == []
        assert events == []

    def test_window_still_opens_normally_with_guard_enabled(self) -> None:
        """The guard must not interfere with the in-window logic: a cold room still triggers the heat sequence."""
        agent = MockAgent(room_temp=19.0, power="POWER_OFF")
        clock = Clock(datetime(2026, 11, 10, 2, 0, 0))
        watch, events = self.make(agent, clock)
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE
        assert [kind for kind, _ in events] == ["heat_start"]


class TestResilience:
    def test_failed_setup_is_retried_next_tick(self) -> None:
        """A command failing mid-sequence leaves the watch owning the unit and sending only what's missing next."""
        agent = MockAgent(room_temp=19.0)
        agent.fail_on = {"set_target_temperature"}
        watch = make_watch(agent)
        with pytest.raises(RuntimeError):
            asyncio.run(watch._tick())
        assert watch.is_heating is True
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE

    def test_failed_turn_off_is_retried(self) -> None:
        """If turning off fails at the window end, the next tick tries again."""
        agent = MockAgent(room_temp=19.0)
        clock = Clock(datetime(2026, 11, 10, 6, 59))
        watch = make_watch(agent, clock)
        asyncio.run(watch._tick())
        clock.value = datetime(2026, 11, 10, 7, 1)
        agent.fail_on = {"turn_off"}
        with pytest.raises(RuntimeError):
            asyncio.run(watch._tick())
        asyncio.run(watch._tick())
        assert agent.power == "POWER_OFF"
        assert watch.is_heating is False

    def test_loop_survives_errors(self) -> None:
        """A failing tick is recorded, and the loop keeps running instead of dying silently."""
        agent = MockAgent()
        agent.fail_status = True
        events: list[tuple[str, str]] = []
        watch = make_watch(agent, event_sink=lambda kind, detail, temp: events.append((kind, detail)))

        async def run() -> bool:
            await watch.start()
            await asyncio.sleep(0.05)
            alive = watch.is_running
            await watch.stop()
            return alive

        assert asyncio.run(run()) is True
        assert watch.last_error is not None and "unreachable" in watch.last_error
        assert [kind for kind, _ in events] == ["error"]  # repeated identical errors are recorded once

    def test_manual_off_releases_ownership(self) -> None:
        """If someone turns the unit off mid-cycle, the watch lets go and waits for the low threshold again."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.power = "POWER_OFF"
        agent.room_temp = 20.5
        asyncio.run(watch._tick())
        assert watch.is_heating is False
        assert agent.calls == HEAT_SEQUENCE

    def test_settings_change_while_heating_reapplies_target(self) -> None:
        """Raising the high target mid-cycle pushes the new target to the unit on the next tick."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        watch.high_target_c = 23.0
        watch.settings_changed()
        asyncio.run(watch._tick())
        assert agent.calls[len(HEAT_SEQUENCE) :] == ["set_target_temperature:23.0:HEAT"]


class TestCommandBudget:
    def test_short_budget_sends_nothing_then_retries(self) -> None:
        """With too few throttle slots left, no command is sent (no half-applied heat); next tick completes."""
        agent = MockAgent(room_temp=19.0)
        agent.budget = 3
        watch = make_watch(agent)
        with pytest.raises(RuntimeError, match="commands available"):
            asyncio.run(watch._tick())
        assert agent.calls == []
        assert watch.is_heating is True
        agent.budget = 99
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE

    def test_failed_release_on_stop_is_reported(self) -> None:
        """If the unit can't be switched off when the watch is disabled, stop() says so instead of hiding it."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.fail_on = {"turn_off"}
        with pytest.raises(RuntimeError, match="turn_off failed"):
            asyncio.run(watch.stop())
        assert watch.is_running is False
        assert watch.is_heating is True  # still owned: re-enabling resumes control and turns it off later


class TestStopAndState:
    def test_stop_while_heating_turns_unit_off(self) -> None:
        """Disabling the watch mid-cycle never leaves the unit heating unattended."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        asyncio.run(watch.stop())
        assert agent.calls[-1] == "turn_off"

    def test_shutdown_keeps_ownership_across_restart(self, tmp_path: Path) -> None:
        """A process restart (stop without release) keeps heating ownership via the state file."""
        state = tmp_path / "state.json"
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent, state_path=state)
        asyncio.run(watch._tick())
        asyncio.run(watch.stop(release=False))
        assert agent.calls == HEAT_SEQUENCE
        restarted = make_watch(agent, state_path=state)
        assert restarted.is_heating is True
        agent.room_temp = 22.0
        asyncio.run(restarted._tick())
        assert agent.calls[-1] == "turn_off"

    def test_events_are_recorded(self) -> None:
        """Heat start/stop are forwarded to the event sink with the room temperature."""
        events: list[tuple[str, float | None]] = []
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent, event_sink=lambda kind, detail, temp: events.append((kind, temp)))
        asyncio.run(watch._tick())
        agent.room_temp = 22.5
        asyncio.run(watch._tick())
        assert events == [("heat_start", 19.5), ("heat_stop", 22.5)]

    def test_snapshot_reports_state(self) -> None:
        """The snapshot exposes window, thresholds and heating state for the dashboard."""
        watch = make_watch(MockAgent(room_temp=19.5))
        asyncio.run(watch._tick())
        snap = watch.snapshot()
        assert snap["heating"] is True
        assert snap["in_window"] is True
        assert snap["timezone"] == "Europe/Lisbon"
        assert snap["low_threshold_c"] == 20.0 and snap["high_target_c"] == 22.0
        assert snap["last_room_temp_c"] == 19.5


class TestFewestCommands:
    """The unit beeps for every accepted command, so heating sends only what it needs."""

    def test_remembered_settings_need_only_power_on(self) -> None:
        """Once the unit remembers HEAT / 22 / LOW, starting to heat is a single power-on."""
        agent = MockAgent(room_temp=19.5, job_mode="HEAT", target=22, fan="LOW")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == ["turn_on"]
        assert watch.is_heating is True

    def test_live_unit_state_skips_the_mode(self) -> None:
        """The real unit's remembered HEAT / 26 / HIGH needs power-on, target and fan, not the mode."""
        agent = MockAgent(room_temp=19.5, job_mode="HEAT", target=26, fan="HIGH")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == ["turn_on", "set_target_temperature:22.0:HEAT", "set_fan_speed:LOW"]

    def test_second_cycle_is_one_command_each_way(self) -> None:
        """After one heat cycle the unit remembers the settings: later cycles are power-on and power-off only."""
        agent = MockAgent(room_temp=19.5)
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        agent.room_temp = 22.0
        asyncio.run(watch._tick())
        agent.room_temp = 19.5
        first_cycle = len(agent.calls)
        asyncio.run(watch._tick())
        agent.room_temp = 22.0
        asyncio.run(watch._tick())
        assert agent.calls[first_cycle:] == ["turn_on", "turn_off"]

    def test_mode_change_resends_target_and_fan(self) -> None:
        """Switching from COOL re-sends target and fan, since the unit keeps per-mode targets."""
        agent = MockAgent(room_temp=19.5, job_mode="COOL", target=22, fan="LOW")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == HEAT_SEQUENCE

    def test_already_heating_as_wanted_sends_nothing(self) -> None:
        """A unit already on, in HEAT at the target with fan LOW, gets no commands at all."""
        agent = MockAgent(room_temp=19.5, power="POWER_ON", job_mode="HEAT", target=22, fan="LOW")
        watch = make_watch(agent)
        asyncio.run(watch._tick())
        assert agent.calls == []
        assert watch.is_heating is True


def test_heating_plan() -> None:
    """The plan lists power-on first and skips settings the unit already has."""
    off_remembered = {
        "operation": {"airConOperationMode": "POWER_OFF"},
        "airConJobMode": {"currentJobMode": "HEAT"},
        "temperatureInUnits": [{"targetTemperature": 22, "unit": "C"}],
        "airFlow": {"windStrength": "LOW"},
    }
    assert heating_plan(off_remembered, 22.0, fan_low=True) == ["turn_on"]
    assert heating_plan(off_remembered, 23.0, fan_low=True) == ["turn_on", "set_target_temperature"]
    assert heating_plan(None, 22.0, fan_low=False) == ["turn_on", "set_hvac_mode", "set_target_temperature"]
