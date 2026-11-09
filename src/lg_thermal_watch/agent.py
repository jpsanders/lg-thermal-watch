"""AI-agent control interface for the LG PC18SK heat pump (ThinQ-backed)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from lg_thermal_watch.controller import (
    DEFAULT_TEMP_MAX_C,
    DEFAULT_TEMP_MIN_C,
    DEFAULT_TARGET_TEMP_C,
    ThinQAgentController,
)

from thinqconnect import ThinQAPIException

_LOGGER = logging.getLogger(__name__)

MAX_COMMANDS_PER_WINDOW = 6
WINDOW_SECONDS = 60.0


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
    ) -> None:
        """Initialise the agent control layer."""
        self.controller = controller
        self.temp_min_c = temp_min_c
        self.temp_max_c = temp_max_c
        self.temp_default_c = temp_default_c
        self.command_timeout_s = command_timeout_s
        self.max_commands_per_window = max_commands_per_window
        self.window_seconds = window_seconds
        self._window_start = time.monotonic()
        self._commands_in_window = 0
        self._last_command_at: float | None = None

    # ------------------------------------------------------------------ reads

    async def get_device_status(self) -> dict[str, Any] | None:
        """Read device status through the controller."""
        return await self.controller.get_device_status()

    async def get_room_temperature(self) -> float | None:
        """Read measured room temperature through the controller."""
        return await self.controller.get_room_temperature()

    async def get_target_temperature(self) -> float | None:
        """Read target temperature through the controller."""
        return await self.controller.get_target_temperature()

    async def get_energy_consumption(self) -> dict[str, Any] | None:
        """Read energy consumption through the controller."""
        return await self.controller.get_energy_consumption()

    # ------------------------------------------------------------------ writes

    async def _issue(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Throttle and time-bound issuing of a control command; returns API response."""
        now = time.monotonic()
        if now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._commands_in_window = 0
        if self._commands_in_window >= self.max_commands_per_window:
            raise RuntimeError("command rate limit exceeded")
        self._commands_in_window += 1
        self._last_command_at = now
        return await coro_factory()

    async def set_target_temperature(self, value_c: float) -> dict[str, Any] | None:
        """Set target temperature (Celsius) with safety bounds."""
        if not (self.temp_min_c <= value_c <= self.temp_max_c):
            raise ValueError(f"target temperature {value_c}C outside allowed [{self.temp_min_c}, {self.temp_max_c}]C")
        response = await self._issue(lambda: self.controller.set_target_temperature(value_c))
        return response

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Set HVAC mode (off/cool/heat/fan/dry/auto) with validation."""
        mode_norm = (mode or "").strip().lower()
        if mode_norm not in ("off", "cool", "heat", "fan", "dry", "auto"):
            raise ValueError(f"unsupported hvac mode: {mode}")
        response = await self._issue(lambda: self.controller.set_hvac_mode(mode_norm))
        return response

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Set fan speed through the throttled issue path."""
        response = await self._issue(lambda: self.controller.set_fan_speed(speed))
        return response

    async def set_swing_mode(self, mode: str) -> dict[str, Any] | None:
        """Set swing mode through the throttled issue path."""
        response = await self._issue(lambda: self.controller.set_swing_mode(mode))
        return response

    async def turn_on(self) -> dict[str, Any] | None:
        """Turn on through the throttled issue path."""
        response = await self._issue(lambda: self.controller.turn_on())
        return response

    async def turn_off(self) -> dict[str, Any] | None:
        """Turn off through the throttled issue path."""
        response = await self._issue(lambda: self.controller.turn_off())
        return response
