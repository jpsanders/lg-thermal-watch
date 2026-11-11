"""AI-agent control interface for the LG PC18SK heat pump (ThinQ-backed)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from lg_thermal_watch.controller import (
    DEFAULT_TARGET_TEMP_C,
    DEFAULT_TEMP_MAX_C,
    DEFAULT_TEMP_MIN_C,
    ThinQAgentController,
    extract_temperature,
)

_LOGGER = logging.getLogger(__name__)

MAX_COMMANDS_PER_WINDOW = 6
WINDOW_SECONDS = 60.0
# The logger, the night watch and the dashboard all poll the same unit; a short cache lets them share one LG call.
DEFAULT_STATUS_TTL_S = 15.0


class LGPC18SKAgent:
    """AI-agent control interface for the LG PC18SK heat pump (ThinQ-backed)."""

    def __init__(
        self,
        controller: ThinQAgentController,
        *,
        temp_min_c: float = DEFAULT_TEMP_MIN_C,
        temp_max_c: float = DEFAULT_TEMP_MAX_C,
        temp_default_c: float = DEFAULT_TARGET_TEMP_C,
        command_timeout_s: float = 30.0,
        max_commands_per_window: int = MAX_COMMANDS_PER_WINDOW,
        window_seconds: float = WINDOW_SECONDS,
        status_ttl_s: float = DEFAULT_STATUS_TTL_S,
    ) -> None:
        """Initialise the agent control layer."""
        self.controller = controller
        self.temp_min_c = temp_min_c
        self.temp_max_c = temp_max_c
        self.temp_default_c = temp_default_c
        self.command_timeout_s = command_timeout_s
        self.max_commands_per_window = max_commands_per_window
        self.window_seconds = window_seconds
        self.status_ttl_s = status_ttl_s
        self._window_start = time.monotonic()
        self._commands_in_window = 0
        self._last_command_at: float | None = None
        self._status_cache: tuple[float, dict[str, Any] | None] | None = None
        self._status_lock = asyncio.Lock()

    # ------------------------------------------------------------------ reads

    async def get_device_status(self, *, max_age_s: float | None = None) -> dict[str, Any] | None:
        """Read device status, reusing a reading younger than ``max_age_s`` (default: the status TTL)."""
        limit = self.status_ttl_s if max_age_s is None else max_age_s
        async with self._status_lock:
            cached = self._status_cache
            if cached is not None and time.monotonic() - cached[0] <= limit:
                return cached[1]
            status = await self.controller.get_device_status()
            self._status_cache = (time.monotonic(), status)
            return status

    async def get_room_temperature(self) -> float | None:
        """Read measured room temperature (Celsius)."""
        return extract_temperature(await self.get_device_status(), "currentTemperature")

    async def get_target_temperature(self) -> float | None:
        """Read target temperature (Celsius)."""
        return extract_temperature(await self.get_device_status(), "targetTemperature")

    async def get_energy_consumption(self) -> dict[str, Any] | None:
        """Read energy consumption through the controller."""
        return await self.controller.get_energy_consumption()

    # ------------------------------------------------------------------ writes

    def commands_available(self) -> int:
        """Return how many throttled commands may be issued right now (check before multi-step sequences)."""
        if time.monotonic() - self._window_start >= self.window_seconds:
            return self.max_commands_per_window
        return max(0, self.max_commands_per_window - self._commands_in_window)

    async def _issue(self, coro_factory: Callable[[], Awaitable[Any]], *, essential: bool = False) -> Any:
        """Throttle and time-bound a control command; ``essential`` (power-off) is counted but never refused."""
        now = time.monotonic()
        if now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._commands_in_window = 0
        if self._commands_in_window >= self.max_commands_per_window and not essential:
            raise RuntimeError("command rate limit exceeded")
        self._commands_in_window += 1
        self._last_command_at = now
        try:
            return await asyncio.wait_for(coro_factory(), timeout=self.command_timeout_s)
        finally:
            # Whatever happened, the cached status no longer describes the unit.
            self._status_cache = None

    async def set_target_temperature(self, value_c: float, *, job_mode: str | None = None) -> dict[str, Any] | None:
        """Set target temperature (Celsius) with safety bounds; ``job_mode`` avoids a racy status read."""
        if not (self.temp_min_c <= value_c <= self.temp_max_c):
            raise ValueError(f"target temperature {value_c}C outside allowed [{self.temp_min_c}, {self.temp_max_c}]C")
        response = await self._issue(lambda: self.controller.set_target_temperature(value_c, job_mode=job_mode))
        return cast("dict[str, Any] | None", response)

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Set HVAC mode (off/cool/heat/fan/dry/auto) with validation."""
        mode_norm = (mode or "").strip().lower()
        if mode_norm not in ("off", "cool", "heat", "fan", "dry", "auto"):
            raise ValueError(f"unsupported hvac mode: {mode}")
        response = await self._issue(lambda: self.controller.set_hvac_mode(mode_norm), essential=mode_norm == "off")
        return cast("dict[str, Any] | None", response)

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Set fan speed through the throttled issue path."""
        response = await self._issue(lambda: self.controller.set_fan_speed(speed))
        return cast("dict[str, Any] | None", response)

    async def set_swing_mode(self, mode: str) -> dict[str, Any] | None:
        """Set swing mode through the throttled issue path."""
        response = await self._issue(lambda: self.controller.set_swing_mode(mode))
        return cast("dict[str, Any] | None", response)

    async def turn_on(self) -> dict[str, Any] | None:
        """Turn on through the throttled issue path."""
        response = await self._issue(lambda: self.controller.turn_on())
        return cast("dict[str, Any] | None", response)

    async def turn_off(self) -> dict[str, Any] | None:
        """Turn off; counted by the throttle but never refused, since switching off is the fail-safe direction."""
        response = await self._issue(lambda: self.controller.turn_off(), essential=True)
        return cast("dict[str, Any] | None", response)
