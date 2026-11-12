"""
Night-time gentle-heat watch for the LG PC18SK heat pump.

Inside the night window the watch heats gently whenever the room drops below the low threshold, and turns the
unit off again once the room reaches the high target. If it is still heating when the window closes (or when the
watch is disabled) it turns the unit off, so the heat pump is never left running with nothing watching it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.controller import describe_error, is_powered_on, summarize_status

_LOGGER = logging.getLogger(__name__)

DEFAULT_LOW_THRESHOLD_C = 18.0
DEFAULT_HIGH_TARGET_C = 22.0
DEFAULT_POLL_INTERVAL_S = 60.0
DEFAULT_NIGHT_START_HOUR = 0
DEFAULT_NIGHT_END_HOUR = 7
# LG rejects commands while the unit is off (COMMAND_NOT_SUPPORTED_IN_POWER_OFF, 2304); give it a moment after power-on.
DEFAULT_POWER_ON_SETTLE_S = 2.0
# A persisted "heating" flag older than this is ignored on start-up (the unit may have been used by hand since).
STATE_MAX_AGE = timedelta(hours=2)

EventSink = Callable[[str, str, float | None], None]


def _iso(value: datetime | None) -> str | None:
    """Format an optional datetime as ISO-8601."""
    return value.isoformat(timespec="seconds") if value is not None else None


def is_valid_timezone(name: str) -> bool:
    """Return True when ``name`` is a known IANA timezone."""
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return False
    return True


# The unit beeps for every command it accepts, and LG's API has no way to mute it. It remembers mode, target and
# fan while off, so heating only re-sends what differs. A mode change re-sends target and fan too (the unit keeps
# separate heat/cool/auto targets).
HEAT_COMMANDS = ("turn_on", "set_hvac_mode", "set_target_temperature", "set_fan_speed")


def heating_plan(status: dict[str, Any] | None, target_c: float, *, fan_low: bool) -> list[str]:
    """Return the commands needed to heat to ``target_c``, in the order LG accepts them (power-on first)."""
    summary = summarize_status(status)
    plan: list[str] = []
    if summary["is_on"] is not True:
        plan.append("turn_on")
    mode_change = str(summary["job_mode"] or "").upper() != "HEAT"
    if mode_change:
        plan.append("set_hvac_mode")
    target = summary["target_temp_c"]
    if mode_change or target is None or abs(target - target_c) >= 0.25:
        plan.append("set_target_temperature")
    if fan_low and (mode_change or str(summary["fan_speed"] or "").upper() != "LOW"):
        plan.append("set_fan_speed")
    return plan


def system_timezone_name() -> str | None:
    """Return the system's IANA timezone (e.g. ``Europe/Lisbon``), so DST is handled; None if undeterminable."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        if name and is_valid_timezone(name):
            return name
    return None


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
        timezone_name: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
        event_sink: EventSink | None = None,
        state_path: str | Path | None = None,
        power_on_settle_s: float = DEFAULT_POWER_ON_SETTLE_S,
    ) -> None:
        """Initialise the night watch; ``timezone_name`` is an IANA zone (default: the system's local zone)."""
        self.agent = agent
        self.low_threshold_c = low_threshold_c
        self.high_target_c = high_target_c
        self.fan_low = fan_low
        self.poll_interval_s = poll_interval_s
        self.night_start_hour = night_start_hour
        self.night_end_hour = night_end_hour
        self.timezone_name = timezone_name
        self.now_fn = now_fn
        self.event_sink = event_sink
        self.state_path = Path(state_path) if state_path is not None else None
        self.power_on_settle_s = power_on_settle_s
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._heating = False
        self._setup_pending = False
        self.heating_since: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_room_temp_c: float | None = None
        self.last_action: str | None = None
        self.last_action_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self._load_state()

    # ------------------------------------------------------------------ time

    @property
    def tz(self) -> tzinfo:
        """Return the timezone the night window is evaluated in (configured zone, else the system zone)."""
        name = self.timezone_name or system_timezone_name()
        if name:
            return ZoneInfo(name)
        local = datetime.now().astimezone().tzinfo
        return local if local is not None else timezone.utc

    def now(self) -> datetime:
        """Return the current time as an aware datetime in the watch timezone."""
        if self.now_fn is None:
            return datetime.now(self.tz)
        return self._localize(self.now_fn())

    def _localize(self, value: datetime) -> datetime:
        """Treat naive datetimes as watch-local wall time; convert aware ones into the watch timezone."""
        if value.tzinfo is None:
            return value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz)

    def _in_night_window(self, now: datetime) -> bool:
        """Return True when ``now`` falls inside the night window (windows may wrap past midnight)."""
        hour = self._localize(now).hour
        start, end = self.night_start_hour, self.night_end_hour
        if start < end:
            return start <= hour < end
        if start > end:
            return hour >= start or hour < end
        return True  # start == end: a full 24-hour window

    def window_bounds(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """Return the (start, end) of the window in progress, or of the next one if none is."""
        local = self._localize(now) if now is not None else self.now()
        hours = (self.night_end_hour - self.night_start_hour) % 24 or 24
        anchor = local.replace(hour=self.night_start_hour, minute=0, second=0, microsecond=0)
        for day_offset in (-1, 0, 1):
            start = anchor + timedelta(days=day_offset)
            end = start + timedelta(hours=hours)
            if local < end:
                return start, end
        start = anchor + timedelta(days=2)  # unreachable for sane inputs; keeps the return type total
        return start, start + timedelta(hours=hours)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the night watch loop."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="lg-night-watch")
        _LOGGER.info(
            "Night watch started (window %02d:00-%02d:00 %s, low=%.1f, high=%.1f, fan_low=%s)",
            self.night_start_hour,
            self.night_end_hour,
            self.timezone_name or "local",
            self.low_threshold_c,
            self.high_target_c,
            self.fan_low,
        )

    async def stop(self, *, release: bool = True) -> None:
        """
        Stop the loop; with ``release`` (the default) also turn the unit off if the watch was heating.

        :param release: Turn the unit off if the watch is heating (False on process shutdown, to resume after).
        :raises Exception: Whatever turning the unit off raised; the loop is stopped regardless.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if release and self._heating:
            try:
                await self._stop_heating("night watch disabled")
            except Exception as exc:  # the loop is stopped either way; the caller must hear the unit is still on
                self._note_error(exc)
                raise

    @property
    def is_running(self) -> bool:
        """Return True when the loop task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def is_heating(self) -> bool:
        """Return True while the watch owns the unit (it turned the heating on and has not turned it off)."""
        return self._heating

    def settings_changed(self) -> None:
        """Re-apply mode/target/fan on the next tick if the watch is heating (e.g. the high target moved)."""
        if self._heating:
            self._setup_pending = True

    # ------------------------------------------------------------------ logic

    async def _tick(self) -> None:
        """Single watch evaluation."""
        now = self.now()
        self.last_tick_at = now
        if not self._in_night_window(now):
            if self._heating:
                await self._stop_heating("night window ended")
            return

        status = await self.agent.get_device_status()
        room_temp = summarize_status(status)["room_temp_c"]
        self.last_room_temp_c = room_temp
        if room_temp is None:
            _LOGGER.warning("Night watch: no room temperature in device status; skipping tick")
            return

        if self._heating:
            if room_temp >= self.high_target_c:
                await self._stop_heating(
                    f"room reached {room_temp:.1f}°C (target {self.high_target_c:.1f}°C)", room_temp, status
                )
                return
            if self._setup_pending:
                await self._apply_heating()
                return
            if is_powered_on(status) is not False:
                return
            # Someone turned the unit off by hand mid-cycle: let go of it and resume watching from scratch.
            self._heating = False
            self.heating_since = None
            self._save_state()
            self._record("released", f"Unit was turned off outside the night watch at {room_temp:.1f}°C", room_temp)
        if room_temp < self.low_threshold_c:
            self._heating = True
            self._setup_pending = True
            self.heating_since = now
            self._save_state()
            self._record(
                "heat_start",
                f"Room {room_temp:.1f}°C below {self.low_threshold_c:.1f}°C, heating to {self.high_target_c:.1f}°C"
                + (" with fan LOW" if self.fan_low else ""),
                room_temp,
            )
            await self._apply_heating()

    async def _apply_heating(self) -> None:
        """Power on if needed, then send only the settings the unit lacks; retried next tick if any step fails."""
        status = await self.agent.get_device_status(max_age_s=0)
        plan = heating_plan(status, self.high_target_c, fan_low=self.fan_low)
        available = self.agent.commands_available()
        if available < len(plan):
            # Never start a sequence the throttle would cut short (e.g. powered on, still at the old target).
            raise RuntimeError(f"only {available} of {len(plan)} commands available this minute; retrying next tick")
        if "turn_on" in plan:
            await self.agent.turn_on()
            if self.power_on_settle_s > 0:
                await asyncio.sleep(self.power_on_settle_s)
        if "set_hvac_mode" in plan:
            await self.agent.set_hvac_mode("heat")
        if "set_target_temperature" in plan:
            await self.agent.set_target_temperature(self.high_target_c, job_mode="HEAT")
        if "set_fan_speed" in plan:
            await self.agent.set_fan_speed("LOW")
        self._setup_pending = False
        self._save_state()

    async def _stop_heating(
        self, reason: str, room_temp: float | None = None, status: dict[str, Any] | None = None
    ) -> None:
        """Turn the unit off (if it is on) and release ownership."""
        if status is None:
            status = await self.agent.get_device_status(max_age_s=0)
        if is_powered_on(status) is not False:
            await self.agent.turn_off()
        duration = ""
        if self.heating_since is not None:
            minutes = int((self.now() - self.heating_since).total_seconds() // 60)
            duration = f" after {minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f" after {minutes} min"
        self._heating = False
        self._setup_pending = False
        self.heating_since = None
        self._save_state()
        self._record("heat_stop", f"Heating off{duration}: {reason}", room_temp)

    async def _run(self) -> None:
        """Night watch event loop; a failed tick is logged and retried, it never ends the loop."""
        while self._running:
            try:
                await self._tick()
            except Exception as exc:  # keep watching through LG/network hiccups
                self._note_error(exc)
            else:
                self.last_error = None
            await asyncio.sleep(self.poll_interval_s)

    # ------------------------------------------------------------------ bookkeeping

    def _record(self, kind: str, detail: str, room_temp: float | None = None) -> None:
        """Remember the last action and forward it to the event sink."""
        _LOGGER.info("Night watch: %s", detail)
        self.last_action = detail
        self.last_action_at = self.now()
        if self.event_sink is not None:
            try:
                self.event_sink(kind, detail, room_temp)
            except Exception:  # event history is best-effort; never let it break heating control
                _LOGGER.exception("Night watch: failed to record event")

    def _note_error(self, exc: BaseException) -> None:
        """Log a failed tick, and record it as an event when it differs from the previous failure."""
        message = describe_error(exc)
        _LOGGER.error("Night watch tick failed: %s", message, exc_info=exc)
        if message != self.last_error and self.event_sink is not None:
            try:
                self.event_sink("error", message, self.last_room_temp_c)
            except Exception:
                _LOGGER.exception("Night watch: failed to record error event")
        self.last_error = message
        self.last_error_at = self.now()

    def _save_state(self) -> None:
        """Persist the heating flag so a restart mid-heat keeps ownership of the unit."""
        if self.state_path is None:
            return
        state = {
            "heating": self._heating,
            "setup_pending": self._setup_pending,
            "heating_since": _iso(self.heating_since),
            "saved_at": _iso(datetime.now(timezone.utc)),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            _LOGGER.exception("Night watch: failed to persist state to %s", self.state_path)

    def _load_state(self) -> None:
        """Restore a recent heating flag written by a previous run."""
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            saved_at = datetime.fromisoformat(state["saved_at"])
            if not state.get("heating") or datetime.now(timezone.utc) - saved_at > STATE_MAX_AGE:
                return
            self._heating = True
            self._setup_pending = bool(state.get("setup_pending"))
            since = state.get("heating_since")
            self.heating_since = datetime.fromisoformat(since) if since else None
            _LOGGER.info("Night watch: resumed heating ownership from %s", self.state_path)
        except OSError, ValueError, KeyError, TypeError:
            _LOGGER.exception("Night watch: ignoring unreadable state file %s", self.state_path)

    def snapshot(self) -> dict[str, Any]:
        """Return the watch's live state for the dashboard."""
        now = self.now()
        window_start, window_end = self.window_bounds(now)
        return {
            "running": self.is_running,
            "heating": self._heating,
            "setup_pending": self._setup_pending,
            "in_window": self._in_night_window(now),
            "timezone": self.timezone_name or system_timezone_name() or str(self.tz),
            "now": _iso(now),
            "window_start": _iso(window_start),
            "window_end": _iso(window_end),
            "low_threshold_c": self.low_threshold_c,
            "high_target_c": self.high_target_c,
            "fan_low": self.fan_low,
            "night_start_hour": self.night_start_hour,
            "night_end_hour": self.night_end_hour,
            "poll_interval_s": self.poll_interval_s,
            "heating_since": _iso(self.heating_since),
            "last_tick_at": _iso(self.last_tick_at),
            "last_room_temp_c": self.last_room_temp_c,
            "last_action": self.last_action,
            "last_action_at": _iso(self.last_action_at),
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
        }


def create_night_watch(agent: LGPC18SKAgent, **kwargs: Any) -> LGPC18SKNightWatch:
    """Create the night watch."""
    return LGPC18SKNightWatch(agent, **kwargs)
