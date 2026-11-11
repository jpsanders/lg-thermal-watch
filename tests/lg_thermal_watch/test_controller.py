"""Unit tests for the ThinQ-backed agent controller (mocked ThinQ API)."""

from __future__ import annotations

import asyncio
from typing import Any

from lg_thermal_watch.controller import ThinQAgentController, describe_error, is_powered_on, summarize_status


class MockThinQApi:
    """Mock of the thinqconnect ThinQApi surface used by the controller."""

    def __init__(self) -> None:
        """Initialise the mock status and recorded payloads."""
        self.status: dict[str, Any] = {
            "airConJobMode": {"currentJobMode": "HEAT"},
            "operation": {"airConOperationMode": "POWER_OFF"},
            "temperature": {"currentTemperature": 22.5, "targetTemperature": 26, "unit": "C"},
            "temperatureInUnits": [
                {"currentTemperature": 22.5, "targetTemperature": 26, "unit": "C"},
                {"currentTemperature": 73.0, "targetTemperature": 78.0, "unit": "F"},
            ],
            "airFlow": {"windStrength": "HIGH", "windStrengthDetail": "HIGH"},
        }
        self.controlled_payloads: list[dict[str, Any]] = []

    async def async_get_device_status(self, device_id: str) -> dict[str, Any]:
        """Return the mock status."""
        return self.status

    async def async_get_device_energy_profile(self, device_id: str) -> dict[str, Any] | None:
        """Return an energy profile stub."""
        return {"today": 0.0}

    async def async_post_device_control(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Record the payload and echo it back."""
        self.controlled_payloads.append(payload)
        return {"status": "ok", "payload": payload}


class TestThinQAgentController:
    def test_room_temperature_prefers_c_unit(self) -> None:
        """Celsius unit must win over the Fahrenheit fallback entry."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev")

        async def read() -> float | None:
            status = await controller.get_device_status()
            return controller._extract_temperature(status, "currentTemperature")

        value = asyncio.run(read())
        assert value is not None
        assert abs(float(value) - 22.5) < 1e-9

    def test_set_hvac_mode_off(self) -> None:
        """Power-off mode maps to the operation payload."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev")

        async def run() -> dict[str, Any] | None:
            return await controller.set_hvac_mode("off")

        response = asyncio.run(run())
        assert response is not None
        assert controller.api.controlled_payloads[-1] == {"operation": {"airConOperationMode": "POWER_OFF"}}

    def test_set_hvac_mode_heat(self) -> None:
        """Heat mode maps to the job-mode payload."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev")

        async def run() -> dict[str, Any] | None:
            return await controller.set_hvac_mode("heat")

        response = asyncio.run(run())
        assert response is not None
        assert controller.api.controlled_payloads[-1] == {"airConJobMode": {"currentJobMode": "HEAT"}}

    def test_set_fan_speed(self) -> None:
        """Fan speed upper-cases the wind strength."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev")

        async def run() -> dict[str, Any] | None:
            return await controller.set_fan_speed("low")

        response = asyncio.run(run())
        assert response is not None
        assert controller.api.controlled_payloads[-1] == {"airFlow": {"windStrength": "LOW"}}

    def test_set_target_temperature_requires_mode_context(self) -> None:
        """LG requires the current job mode to be present in the payload."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev")

        async def run() -> dict[str, Any] | None:
            return await controller.set_target_temperature(24.0)

        response = asyncio.run(run())
        assert response is not None
        payload = controller.api.controlled_payloads[-1]
        assert payload["airConJobMode"] == {"currentJobMode": "HEAT"}
        assert payload["temperatureInUnits"]["targetTemperatureC"] == 24

    def test_set_target_temperature_bounds(self) -> None:
        """Target temperatures outside the allowed band must be rejected."""
        controller = ThinQAgentController(MockThinQApi(), device_id="dev", temp_min_c=16.0, temp_max_c=30.0)

        async def run() -> None:
            try:
                await controller.set_target_temperature(40.0)
            except ValueError:
                return
            raise AssertionError("expected ValueError for out-of-range target temperature")

        asyncio.run(run())

    def test_set_target_temperature_with_explicit_job_mode_skips_status_read(self) -> None:
        """An explicit job mode is sent as-is, avoiding a racy status read right after a mode change."""
        api = MockThinQApi()
        api.status["airConJobMode"] = {"currentJobMode": "COOL"}
        controller = ThinQAgentController(api, device_id="dev")
        asyncio.run(controller.set_target_temperature(22.0, job_mode="HEAT"))
        assert api.controlled_payloads[-1]["airConJobMode"] == {"currentJobMode": "HEAT"}


def test_summarize_status_and_power() -> None:
    """The live PC18SK status flattens into the fields the dashboard and watch use."""
    status = MockThinQApi().status
    summary = summarize_status(status)
    assert summary == {
        "room_temp_c": 22.5,
        "target_temp_c": 26.0,
        "power": "POWER_OFF",
        "is_on": False,
        "job_mode": "HEAT",
        "fan_speed": "HIGH",
        "run_state": None,
    }
    assert is_powered_on({"operation": {"airConOperationMode": "POWER_ON"}}) is True
    assert is_powered_on({}) is None
    assert summarize_status(None)["room_temp_c"] is None


def test_describe_error_does_not_repeat_class_name() -> None:
    """Errors whose message already names the class are not prefixed twice."""

    class ThinQAPIException(Exception):
        def __str__(self) -> str:
            return "ThinQAPIException: COMMAND_NOT_SUPPORTED_IN_POWER_OFF (2304) - off"

    assert describe_error(ThinQAPIException()) == "ThinQAPIException: COMMAND_NOT_SUPPORTED_IN_POWER_OFF (2304) - off"
    assert describe_error(ValueError("bad")) == "ValueError: bad"
