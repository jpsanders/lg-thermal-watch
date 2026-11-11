"""FastAPI application exposing the LG Thermal Watch control and telemetry API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from aiohttp import ClientSession, ClientTimeout
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.controller import create_thinq_agent_controller, describe_error, summarize_status
from lg_thermal_watch.nightwatch import (
    DEFAULT_POWER_ON_SETTLE_S,
    LGPC18SKNightWatch,
    heating_plan,
    is_valid_timezone,
    system_timezone_name,
)
from lg_thermal_watch.runtime import RuntimeTracker
from lg_thermal_watch.tempdb import RoomTempLogger, normalize_ts

_LOGGER = logging.getLogger(__name__)

DB_PATH = os.environ.get("LG_THERMAL_WATCH_DB", "/tmp/opencode/lg_thinq_room_temps.db")  # nosec B108
NIGHTWATCH_CONFIG_PATH = os.environ.get(
    "LG_THERMAL_WATCH_NIGHTWATCH_CONFIG", "/tmp/opencode/lg_thermal_watch_nightwatch.json"  # nosec B108
)
# Runtime state (whether the watch currently owns the unit); defaults to a file next to the config.
NIGHTWATCH_STATE_PATH = os.environ.get("LG_THERMAL_WATCH_NIGHTWATCH_STATE", "")
STATIC_DIR = Path(__file__).parent / "static"
TIMELINE_HOURS = {"24h": 24, "7d": 168, "30d": 720}
TIMELINE_BUCKET_S = {"24h": 0, "7d": 300, "30d": 1800}


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default`` when unset or non-numeric."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOGGER.warning("Ignoring non-numeric %s=%r", name, raw)
        return default


# Average electrical draw (kW) assumed while the unit runs; the PC18SK is a 5.0 kW-class inverter
# with roughly 0.8-1.1 kW rated heating input, so 1.0 kW is a fair default for an estimate.
AVG_POWER_KW = _env_float("LG_THERMAL_WATCH_AVG_POWER_KW", 1.0)

_agent: LGPC18SKAgent | None = None
_agent_error: str | None = None
_temp_logger: RoomTempLogger | None = None
_night_watch: LGPC18SKNightWatch | None = None
_runtime_tracker: RuntimeTracker | None = None
_config: NightWatchConfig | None = None
_session: ClientSession | None = None

T = TypeVar("T")


class NightWatchConfig(BaseModel):
    """Night watch configuration."""

    low_threshold_c: float = Field(default=18.0, ge=5.0, le=29.0, description="Heat when the room drops below (C).")
    high_target_c: float = Field(default=22.0, ge=16.0, le=30.0, description="Heat up to, then turn off at (C).")
    fan_low: bool = Field(default=True, description="Fan speed LOW during gentle heat.")
    night_start_hour: int = Field(default=0, ge=0, le=23, description="Night window start hour (0-23).")
    night_end_hour: int = Field(default=7, ge=0, le=23, description="Night window end hour (0-23, exclusive).")
    timezone: str | None = Field(
        default=None,
        validate_default=True,
        description="IANA timezone of the window (default: LG_THERMAL_WATCH_TIMEZONE, else the system zone).",
    )
    enabled: bool = Field(default=True, description="Whether the watch is enabled.")

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        """Reject unknown timezone names; fall back to LG_THERMAL_WATCH_TIMEZONE when unset."""
        fallback = os.environ.get("LG_THERMAL_WATCH_TIMEZONE", "")
        value = (value or fallback).strip() or None
        if value is not None and not is_valid_timezone(value):
            raise ValueError(f"unknown timezone {value!r}; use an IANA name such as Europe/Lisbon")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> NightWatchConfig:
        """Check the thresholds and window make sense together."""
        if self.low_threshold_c >= self.high_target_c:
            raise ValueError("low threshold must be below the high target")
        if self.high_target_c != int(self.high_target_c):
            raise ValueError("high target must be a whole number of degrees (the unit takes whole °C)")
        if self.night_start_hour == self.night_end_hour:
            raise ValueError("night window start and end must differ")
        return self


class HeatRequest(BaseModel):
    """Manual heat request."""

    target_c: float = Field(default=22.0, ge=16.0, le=30.0, description="Target temperature (C).")
    fan_low: bool = Field(default=True, description="Fan speed LOW.")


# ---------------------------------------------------------------------- wiring


def get_agent() -> LGPC18SKAgent:
    """Return the shared live agent, creating it lazily on first use."""
    global _agent
    if _agent is None:
        controller = create_thinq_agent_controller(_session)  # type: ignore[arg-type]
        _agent = LGPC18SKAgent(controller)
    return _agent


def _try_agent() -> LGPC18SKAgent | None:
    """Return the agent, or None (remembering why) when credentials are missing."""
    global _agent_error
    try:
        agent = get_agent()
    except RuntimeError as exc:
        _agent_error = str(exc)
        return None
    _agent_error = None
    return agent


def _require_agent() -> LGPC18SKAgent:
    """Return the agent or fail the request with 503."""
    agent = _try_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail=_agent_error or "device unavailable")
    return agent


def get_temp_logger() -> RoomTempLogger:
    """Return the shared temperature logger, creating it lazily on first use."""
    global _temp_logger
    if _temp_logger is None:
        _temp_logger = RoomTempLogger(_try_agent(), db_path=DB_PATH, interval_s=60.0)
    return _temp_logger


def _state_path() -> Path:
    """Return where the night watch persists its runtime state."""
    if NIGHTWATCH_STATE_PATH:
        return Path(NIGHTWATCH_STATE_PATH)
    config = Path(NIGHTWATCH_CONFIG_PATH)
    return config.with_name(config.stem + ".state.json")


def get_night_watch() -> LGPC18SKNightWatch:
    """Return the shared night watch, creating it lazily on first use."""
    global _night_watch
    if _night_watch is None:
        _night_watch = LGPC18SKNightWatch(
            _require_agent(), event_sink=get_temp_logger().record_event, state_path=_state_path()
        )
    return _night_watch


def _runtime_source() -> str:
    """Label a run's source: the night watch when it owns the unit, otherwise manual use."""
    watch = _night_watch
    if watch is not None and watch.is_heating:
        return "watch"
    return "manual"


def _watch_zone() -> str:
    """Return the IANA zone the night watch (and therefore the dashboard) evaluates time in."""
    config = get_config()
    return config.timezone or system_timezone_name() or "UTC"


def get_runtime_tracker() -> RuntimeTracker:
    """Return the shared runtime tracker, wiring it to the status polls on first use."""
    global _runtime_tracker
    if _runtime_tracker is None:
        _runtime_tracker = RuntimeTracker(db_path=DB_PATH, source_fn=_runtime_source)
    tracker = _runtime_tracker
    logger = get_temp_logger()
    if tracker.observe not in logger.status_observers:
        logger.status_observers.append(tracker.observe)
    return tracker


def get_config() -> NightWatchConfig:
    """Return the night watch config, loading it from disk on first use."""
    global _config
    if _config is None:
        path = Path(NIGHTWATCH_CONFIG_PATH)
        try:
            _config = NightWatchConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _config = NightWatchConfig(enabled=False)
        except (OSError, ValidationError, ValueError) as exc:
            _LOGGER.error("Ignoring unreadable night watch config %s: %s", path, exc)
            _config = NightWatchConfig(enabled=False)
    return _config


def _save_config(config: NightWatchConfig) -> None:
    """Persist the night watch config atomically and make it current."""
    global _config
    path = Path(NIGHTWATCH_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config.model_dump(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    _config = config


def _apply_config(watch: LGPC18SKNightWatch, config: NightWatchConfig) -> None:
    """Push config values into the running watch."""
    retarget = watch.high_target_c != config.high_target_c or watch.fan_low != config.fan_low
    watch.low_threshold_c = config.low_threshold_c
    watch.high_target_c = config.high_target_c
    watch.fan_low = config.fan_low
    watch.night_start_hour = config.night_start_hour
    watch.night_end_hour = config.night_end_hour
    watch.timezone_name = config.timezone
    if retarget:
        watch.settings_changed()


async def _device_call(awaitable: Awaitable[T]) -> T:
    """Await a device call, mapping failures onto meaningful HTTP errors."""
    try:
        return await awaitable
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LG ThinQ request failed: {describe_error(exc)}") from exc


async def _stop_watch(watch: LGPC18SKNightWatch) -> None:
    """Stop the watch (releasing the unit), reporting clearly if the unit could not be switched off."""
    try:
        await watch.stop()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The night watch is off, but switching the heat pump off failed ({describe_error(exc)}). "
            "Use Turn off.",
        ) from exc


def _iso(value: datetime | None) -> str | None:
    """Format an optional datetime as ISO-8601."""
    return value.isoformat(timespec="seconds") if value is not None else None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the aiohttp session, and run the temperature logger and night watch for the app's lifetime."""
    global _session
    _session = ClientSession(timeout=ClientTimeout(total=30))
    logger = get_temp_logger()
    get_runtime_tracker()  # follows power changes via the logger's status polls; no extra LG calls
    if _try_agent() is None:
        _LOGGER.error("%s; serving history only (no logging, no night watch)", _agent_error)
    else:
        await logger.start()
        watch = get_night_watch()
        config = get_config()
        _apply_config(watch, config)
        if config.enabled:
            await watch.start()
        else:
            _LOGGER.info("Night watch is disabled in %s", NIGHTWATCH_CONFIG_PATH)
    try:
        yield
    finally:
        if _night_watch is not None:
            await _night_watch.stop(release=False)  # a restart keeps ownership via the state file
        if _temp_logger is not None:
            await _temp_logger.stop()
        if _session is not None and not _session.closed:
            await _session.close()


# ---------------------------------------------------------------------- app


def create_app() -> FastAPI:
    """Create and return the FastAPI application for LG Thermal Watch."""
    app = FastAPI(
        title="LG Thermal Watch",
        description="Local AI control and telemetry for the LG PC18SK heat pump via the LG ThinQ Connect API.",
        lifespan=lifespan,
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """Health check."""
        return {"status": "ok"}

    @app.get("/api/temps")
    async def get_temps(
        start: str | None = None,
        end: str | None = None,
        timeline: str | None = Query(default=None, description="24h, 7d, or 30d preset window"),
        bucket_s: int | None = Query(default=None, ge=0, le=86400, description="Average into buckets of N seconds"),
    ) -> dict[str, Any]:
        """Return temperature readings, stats and night-watch events within a window or preset timeline."""
        logger = get_temp_logger()
        if timeline and not (start or end):
            hours = TIMELINE_HOURS.get(timeline, 24)
            start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            if bucket_s is None:
                bucket_s = TIMELINE_BUCKET_S.get(timeline, 0)
        try:
            start_n = normalize_ts(start) if start else None
            end_n = normalize_ts(end) if end else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid timestamp: {exc}") from exc
        readings = logger.get_readings(start=start_n, end=end_n, bucket_s=bucket_s)
        stats = logger.get_stats(start=start_n, end=end_n)
        return {
            "readings": readings,
            "min_temp_c": stats["min"]["temp_c"] if stats["min"] else None,
            "max_temp_c": stats["max"]["temp_c"] if stats["max"] else None,
            "mean_temp_c": stats["mean_c"],
            "count": stats["count"],
            "points": len(readings),
            "bucket_s": bucket_s or 0,
            "stats": stats,
            "events": logger.get_events(start=start_n, end=end_n),
            "timeline": timeline,
            "start": start_n,
            "end": end_n,
        }

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """Return live device status (raw ThinQ payload)."""
        agent = _require_agent()
        status = await _device_call(agent.get_device_status())
        if status is None:
            raise HTTPException(status_code=503, detail="device status unavailable")
        return status

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        """Return everything the dashboard shows: device, night watch, logger and config, in one call."""
        config = get_config()
        logger = get_temp_logger()
        result: dict[str, Any] = {
            "now": _iso(datetime.now(timezone.utc)),
            "config": config.model_dump(),
            "logger": {
                "running": logger.is_running,
                "interval_s": logger.interval_s,
                "last_reading_at": _iso(logger.last_reading_at),
                "last_error": logger.last_error,
            },
        }
        agent = _try_agent()
        if agent is None:
            result["device"] = {"ok": False, "error": _agent_error}
            result["watch"] = {"enabled": config.enabled, "running": False, "error": _agent_error}
            return result
        try:
            status = await agent.get_device_status()
            result["device"] = {"ok": status is not None, **summarize_status(status)}
        except Exception as exc:  # the rest of the overview is still useful without the device
            result["device"] = {"ok": False, "error": describe_error(exc)}
        result["watch"] = {"enabled": config.enabled, **get_night_watch().snapshot()}
        return result

    @app.post("/api/device/heat")
    async def device_heat(request: HeatRequest) -> dict[str, Any]:
        """Heat now: power on if needed, then only the HEAT mode / target / fan LOW settings the unit lacks."""
        agent = _require_agent()
        status = await _device_call(agent.get_device_status(max_age_s=0))
        plan = heating_plan(status, request.target_c, fan_low=request.fan_low)
        if agent.commands_available() < len(plan):
            raise HTTPException(status_code=429, detail="Too many commands in the last minute; try again shortly.")
        if "turn_on" in plan:
            await _device_call(agent.turn_on())
            await asyncio.sleep(DEFAULT_POWER_ON_SETTLE_S)
        if "set_hvac_mode" in plan:
            await _device_call(agent.set_hvac_mode("heat"))
        if "set_target_temperature" in plan:
            await _device_call(agent.set_target_temperature(request.target_c, job_mode="HEAT"))
        if "set_fan_speed" in plan:
            await _device_call(agent.set_fan_speed("LOW"))
        return {"ok": True, "target_c": request.target_c, "fan_low": request.fan_low, "commands": plan}

    @app.post("/api/device/off")
    async def device_off() -> dict[str, Any]:
        """Turn the unit off."""
        agent = _require_agent()
        await _device_call(agent.turn_off())
        return {"ok": True}

    @app.get("/api/nightwatch")
    async def nightwatch_state() -> dict[str, Any]:
        """Return the night watch's live state."""
        return {"enabled": get_config().enabled, **get_night_watch().snapshot()}

    @app.get("/api/nightwatch/config")
    async def nightwatch_config_get() -> dict[str, Any]:
        """Return current night watch settings."""
        config = get_config()
        running = _night_watch.is_running if _night_watch is not None else False
        return {**config.model_dump(), "running": running}

    @app.post("/api/nightwatch/config")
    async def nightwatch_config(config: NightWatchConfig) -> dict[str, Any]:
        """Update night watch settings and (re)start or stop the watch."""
        watch = get_night_watch()
        _save_config(config)
        _apply_config(watch, config)
        if config.enabled:
            await watch.start()
        else:
            await _stop_watch(watch)
        return {**config.model_dump(), "running": watch.is_running}

    @app.post("/api/nightwatch/start")
    async def nightwatch_start() -> dict[str, Any]:
        """Start (and enable) the night watch."""
        watch = get_night_watch()
        _save_config(get_config().model_copy(update={"enabled": True}))
        await watch.start()
        return {"started": True}

    @app.post("/api/nightwatch/stop")
    async def nightwatch_stop() -> dict[str, Any]:
        """Stop (and disable) the night watch; turns the unit off if the watch was heating."""
        watch = get_night_watch()
        _save_config(get_config().model_copy(update={"enabled": False}))
        await _stop_watch(watch)
        return {"stopped": True}

    @app.get("/api/runtime")
    async def runtime_state(months: int = Query(default=6, ge=1, le=24)) -> dict[str, Any]:
        """Return on/off sessions and the estimated monthly energy use."""
        tracker = get_runtime_tracker()
        zone = _watch_zone()
        return {
            "now": _iso(datetime.now(timezone.utc)),
            "timezone": zone,
            "avg_power_kw": AVG_POWER_KW,
            "estimate": True,
            "current": tracker.current_session(),
            "recent": tracker.recent_sessions(10),
            "monthly": tracker.monthly_usage(months=months, avg_power_kw=AVG_POWER_KW, tz_name=zone),
        }

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

    @app.get("/", response_class=FileResponse)
    async def dashboard() -> FileResponse:
        """Serve the dashboard UI."""
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def main() -> None:
    """Run the LG Thermal Watch API server with uvicorn."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("LG_THERMAL_WATCH_HOST", "127.0.0.1")
    port = int(os.environ.get("LG_THERMAL_WATCH_PORT", "8000"))
    _LOGGER.info(
        "Starting LG Thermal Watch on %s:%d (db=%s, config=%s, system tz=%s)",
        host,
        port,
        DB_PATH,
        NIGHTWATCH_CONFIG_PATH,
        system_timezone_name(),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
