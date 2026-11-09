"""Night-time gentle-heat watch for the LG PC18SK heat pump."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from lg_thermal_watch.agent import LGPC18SKAgent

_LOGGER = logging.getLogger(__name__)

DEFAULT_LOW_THRESHOLD_C = 18.0
DEFAULT_HIGH_TARGET_C = 22.0
DEFAULT_POLL_INTERVAL_S = 60.0
DEFAULT_NIGHT_START_HOUR = 0
DEFAULT_NIGHT_END_HOUR = 7


class LGPC18SKNightWatch:
    """Night-time gentle-heat watcher for the LG PC18SK heat pump."""

    def __init__(
        self,
        agent: LGPC18SKAgent,
        *,
        low_threshold_c: float = DEFAULT_LOW_THRESHOLD_C,
        high_target_c: float = DEFAULT_HIGH_TARGET_C,
        fan_low: bool = True,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        night_start_hour: int = DEFAULT_NIGHT_START_HOUR,
        night_end_hour: int = DEFAULT_NIGHT_END_HOUR,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the night watch."""
        self.agent = agent
        self.low_threshold_c = low_threshold_c
        self.high_target_c = high_target_c
        self.fan_low = fan_low
        self.poll_interval_s = poll_interval_s
        self.night_start_hour = night_start_hour
        self.night_end_hour = night_end_hour
        self.now_fn = now_fn or (lambda: datetime.now())
        self._running = False
        self._task: asyncio.Task | None = None
        self._heating = False

    async def start(self) -> None:
        """Start the night watch loop."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        _LOGGER.info(
            "Night watch started (window %02d:00-%02d:00, low=%.1f, high=%.1f)",
            self.night_start_hour,
            self.night_end_hour,
            self.low_threshold_c,
            self.high_target_c,
        )

    async def stop(self) -> None:
        """Stop the night watch loop."""
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

    def _in_night_window(self, now: datetime) -> bool:
        """Return True when the current hour is inside the night window."""
        return self.night_start_hour <= now.hour < self.night_end_hour

    async def _tick(self) -> None:
        """Single watch evaluation."""
        now = self.now_fn()
        if not self._in_night_window(now):
            return

        room_temp = await self.agent.get_room_temperature()
        if room_temp is None:
            return

        if not self._heating and room_temp < self.low_threshold_c:
            _LOGGER.info(
                "Night watch: room temp %.1fC below low threshold %.1fC -> gentle heat to %.1fC",
                room_temp,
                self.low_threshold_c,
                self.high_target_c,
            )
            await self.agent.set_fan_speed("LOW")
            await self.agent.set_hvac_mode("heat")
            await self.agent.set_target_temperature(self.high_target_c)
            self._heating = True
        elif self._heating and room_temp >= self.high_target_c:
            _LOGGER.info(
                "Night watch: room temp %.1fC reached high target %.1fC -> stop heating",
                room_temp,
                self.high_target_c,
            )
            await self.agent.turn_off()
            self._heating = False

    async def _run(self) -> None:
        """Night watch event loop."""
        while self._running:
            await self._tick()
            await asyncio.sleep(self.poll_interval_s)


def create_night_watch(agent: LGPC18SKAgent, **kwargs: Any) -> LGPC18SKNightWatch:
    """Factory for the night watch."""
    return LGPC18SKNightWatch(agent, **kwargs)
