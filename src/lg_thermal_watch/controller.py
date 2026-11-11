"""ThinQ agent control interface for the LG heat pump via the LG ThinQ Connect API."""

from __future__ import annotations

import logging
import os
from typing import Any, cast

from aiohttp import ClientSession
from thinqconnect import ThinQApi

_LOGGER = logging.getLogger(__name__)

DEFAULT_TEMP_MIN_C = 16.0
DEFAULT_TEMP_MAX_C = 30.0
DEFAULT_TARGET_TEMP_C = 22.0

SUPPORTED_HVAC_MODES = ("off", "cool", "heat", "fan", "dry", "auto")

OPERATION_MODE_MAP: dict[str, str] = {
    "off": "POWER_OFF",
    "cool": "COOL",
    "heat": "HEAT",
    "fan": "FAN",
    "dry": "DRY",
    "auto": "AUTO",
}


def describe_error(exc: BaseException) -> str:
    """Return a one-line description of an error (ThinQ errors already carry their class name and LG code)."""
    text = str(exc) or repr(exc)
    name = type(exc).__name__
    return text if text.startswith(name) else f"{name}: {text}"


def extract_temperature(status: dict[str, Any] | None, key: str) -> float | None:
    """Extract a temperature value (Celsius preferred) from a ThinQ status dict."""
    status = status or {}
    value: Any = None
    units = status.get("temperatureInUnits")
    entries: list[dict[str, Any]] = []
    if isinstance(units, list):
        entries = [unit for unit in units if isinstance(unit, dict)]
    elif isinstance(units, dict):
        entries = [units]
    for entry in entries:
        if str(entry.get("unit") or "").upper() == "C" and entry.get(key) is not None:
            value = entry.get(key)
            break
    if value is None and isinstance(status.get("temperature"), dict):
        value = status["temperature"].get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_powered_on(status: dict[str, Any] | None) -> bool | None:
    """Return True/False from ``operation.airConOperationMode``, or None when the status does not say."""
    mode = ((status or {}).get("operation") or {}).get("airConOperationMode")
    if mode is None:
        return None
    return str(mode).upper() == "POWER_ON"


def summarize_status(status: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a raw ThinQ air-conditioner status into the handful of fields the watch and dashboard use."""
    status = status or {}
    return {
        "room_temp_c": extract_temperature(status, "currentTemperature"),
        "target_temp_c": extract_temperature(status, "targetTemperature"),
        "power": (status.get("operation") or {}).get("airConOperationMode"),
        "is_on": is_powered_on(status),
        "job_mode": (status.get("airConJobMode") or {}).get("currentJobMode"),
        "fan_speed": (status.get("airFlow") or {}).get("windStrength"),
        "run_state": (status.get("runState") or {}).get("currentState"),
    }


class ThinQAgentController:
    """Agent-side control interface to the LG air conditioner via LG ThinQ Connect API."""

    def __init__(
        self,
        api: ThinQApi,
        device_id: str | None = None,
        *,
        temp_min_c: float = DEFAULT_TEMP_MIN_C,
        temp_max_c: float = DEFAULT_TEMP_MAX_C,
        temp_default_c: float = DEFAULT_TARGET_TEMP_C,
    ) -> None:
        """Initialise the controller."""
        self.api = api
        self.device_id = device_id
        self.temp_min_c = temp_min_c
        self.temp_max_c = temp_max_c
        self.temp_default_c = temp_default_c

    async def get_device_status(self) -> dict[str, Any] | None:
        """Read full device status."""
        return cast("dict[str, Any] | None", await self.api.async_get_device_status(self.device_id))

    @staticmethod
    def _extract_temperature(status: dict[str, Any] | None, key: str) -> float | None:
        """Extract a temperature value (Celsius preferred) from a ThinQ status dict."""
        return extract_temperature(status, key)

    async def get_room_temperature(self) -> float | None:
        """Read room temperature (Celsius)."""
        status = await self.get_device_status() or {}
        return self._extract_temperature(status, "currentTemperature")

    async def get_target_temperature(self) -> float | None:
        """Read target temperature (Celsius)."""
        status = await self.get_device_status() or {}
        return self._extract_temperature(status, "targetTemperature")

    async def get_energy_consumption(self) -> dict[str, Any] | None:
        """Return the optional energy profile/usage via the ThinQ API."""
        return cast("dict[str, Any] | None", await self.api.async_get_device_energy_profile(self.device_id))

    async def _post_control(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Post a control command (accepted by LG cloud)."""
        return cast("dict[str, Any] | None", await self.api.async_post_device_control(self.device_id, payload))

    async def set_target_temperature(self, value_c: float, *, job_mode: str | None = None) -> dict[str, Any] | None:
        """Set target temperature (Celsius); LG requires job-mode context, read from the device unless given."""
        if not (self.temp_min_c <= value_c <= self.temp_max_c):
            raise ValueError(f"target temperature {value_c}C outside allowed [{self.temp_min_c}, {self.temp_max_c}]C")
        mode = job_mode
        if mode is None:
            status = await self.get_device_status() or {}
            mode = ((status.get("airConJobMode") or {}).get("currentJobMode")) or "HEAT"
        payload = {
            "airConJobMode": {"currentJobMode": mode},
            "temperatureInUnits": {"targetTemperatureC": int(round(value_c)), "unit": "C"},
        }
        return await self._post_control(payload)

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Set HVAC mode (off/cool/heat/fan/dry/auto)."""
        mode_norm = (mode or "").strip().lower()
        if mode_norm not in OPERATION_MODE_MAP:
            raise ValueError(f"unsupported hvac mode: {mode}")
        if mode_norm == "off":
            payload = {"operation": {"airConOperationMode": "POWER_OFF"}}
        else:
            payload = {"airConJobMode": {"currentJobMode": OPERATION_MODE_MAP[mode_norm]}}
        return await self._post_control(payload)

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Set fan speed (LOW/MEDIUM/HIGH)."""
        payload = {"airFlow": {"windStrength": str(speed).upper()}}
        return await self._post_control(payload)

    async def set_swing_mode(self, mode: str) -> dict[str, Any] | None:
        """Set the optional swing mode."""
        payload = {"windDirection": {"airGuideWind": str(mode).lower()}}
        return await self._post_control(payload)

    async def turn_on(self) -> dict[str, Any] | None:
        """Turn on (operation power-on)."""
        payload = {"operation": {"airConOperationMode": "POWER_ON"}}
        return await self._post_control(payload)

    async def turn_off(self) -> dict[str, Any] | None:
        """Turn off (operation power-off)."""
        payload = {"operation": {"airConOperationMode": "POWER_OFF"}}
        return await self._post_control(payload)


def create_thinq_agent_controller(
    session: ClientSession,
    *,
    access_token: str | None = None,
    client_id: str | None = None,
    country_code: str | None = None,
    device_id: str | None = None,
    temp_min_c: float = DEFAULT_TEMP_MIN_C,
    temp_max_c: float = DEFAULT_TEMP_MAX_C,
    temp_default_c: float = DEFAULT_TARGET_TEMP_C,
) -> ThinQAgentController:
    """Create the ThinQ agent controller; credentials default to LG_THINQ_* env vars."""
    token = access_token or os.environ.get("LG_THINQ_ACCESS_TOKEN")
    cid = client_id or os.environ.get("LG_THINQ_CLIENT_ID")
    cc = country_code or os.environ.get("LG_THINQ_COUNTRY_CODE")
    dev = device_id or os.environ.get("LG_THINQ_DEVICE_ID")

    if not token or not cid or not cc:
        raise RuntimeError(
            "ThinQ Connect credentials missing: set LG_THINQ_ACCESS_TOKEN, LG_THINQ_CLIENT_ID, LG_THINQ_COUNTRY_CODE"
        )

    api = ThinQApi(
        session=session,
        access_token=token,
        country_code=cc,
        client_id=cid,
        mock_response=False,
    )
    return ThinQAgentController(
        api,
        device_id=dev,
        temp_min_c=temp_min_c,
        temp_max_c=temp_max_c,
        temp_default_c=temp_default_c,
    )
