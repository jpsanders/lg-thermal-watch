"""Unit tests for the LGPC18SKAgent command throttle and safety wrappers."""

from __future__ import annotations

import asyncio
from typing import Any

from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.controller import ThinQAgentController


class MockController:
    """Mock controller recording issued commands."""

    def __init__(self) -> None:
        """Initialise the mock controller."""
        self.calls: list[str] = []
        self.threw: list[str] = []

    async def get_device_status(self) -> dict[str, Any] | None:
        """Return a minimal status."""
        return {"airConJobMode": {"currentJobMode": "HEAT"}}

    async def get_room_temperature(self) -> float | None:
        """Return a room temperature."""
        return 21.5

    async def get_target_temperature(self) -> float | None:
        """Return a target temperature."""
        return 24.0

    async def get_energy_consumption(self) -> dict[str, Any] | None:
        """Return an energy stub."""
        return None

    async def set_target_temperature(self, value_c: float) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append(f"set_target_temperature:{value_c}")
        return {"ok": True}

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append(f"set_hvac_mode:{mode}")
        return {"ok": True}

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append(f"set_fan_speed:{speed}")
        return {"ok": True}

    async def set_swing_mode(self, mode: str) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append(f"set_swing_mode:{mode}")
        return {"ok": True}

    async def turn_on(self) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append("turn_on")
        return {"ok": True}

    async def turn_off(self) -> dict[str, Any] | None:
        """Record and return."""
        self.calls.append("turn_off")
        return {"ok": True}


class StubApi:
    """Minimal stand-in for the thinqconnect ThinQApi."""

    async def async_get_device_status(self, device_id: str) -> dict[str, Any]:
        """Return status."""
        return {"airConJobMode": {"currentJobMode": "HEAT"}}

    async def async_post_device_control(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Record and return."""
        return {"ok": True}


def make_agent(**kwargs: Any) -> tuple[LGPC18SKAgent, MockController]:
    """Build an agent over a fresh mock controller."""
    mock = MockController()
    controller = ThinQAgentController(StubApi(), device_id="dev")
    agent = LGPC18SKAgent(controller, **kwargs)
    return agent, mock


class TestLGPC18SKAgentThrottle:
    def test_turn_on_and_off_issue_commands(self) -> None:
        """Turn on/off round-trip through the controller."""
        agent, _mock = make_agent()

        async def run() -> list[dict[str, Any] | None]:
            on = await agent.turn_on()
            off = await agent.turn_off()
            return [on, off]

        responses = asyncio.run(run())
        assert responses[0] is not None and responses[0]["ok"] is True
        assert responses[1] is not None and responses[1]["ok"] is True

    def test_command_rate_limit(self) -> None:
        """More than the per-window allowance must raise."""
        agent, _mock = make_agent(max_commands_per_window=2, window_seconds=60.0)

        async def run() -> list[dict[str, Any] | None]:
            results: list[dict[str, Any] | None] = []
            for _ in range(3):
                results.append(await agent.turn_on())
            return results

        try:
            asyncio.run(run())
        except RuntimeError as exc:
            assert "rate limit" in str(exc)
        else:
            raise AssertionError("expected rate-limit RuntimeError")

    def test_set_target_temperature_out_of_bounds_rejected(self) -> None:
        """Safety bounds reject out-of-range targets."""
        agent, _mock = make_agent(temp_min_c=16.0, temp_max_c=30.0)

        async def run() -> None:
            try:
                await agent.set_target_temperature(50.0)
            except ValueError:
                return
            raise AssertionError("expected ValueError for out-of-range target temperature")

        asyncio.run(run())
