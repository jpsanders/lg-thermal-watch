"""FastAPI application exposing the LG Thermal Watch control and telemetry API."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from aiohttp import ClientSession
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.controller import create_thinq_agent_controller
from lg_thermal_watch.nightwatch import LGPC18SKNightWatch
from lg_thermal_watch.tempdb import RoomTempLogger

DB_PATH = os.environ.get("LG_THERMAL_WATCH_DB", "/tmp/opencode/lg_thinq_room_temps.db")  # nosec B108
NIGHTWATCH_CONFIG_PATH = os.environ.get(
    "LG_THERMAL_WATCH_NIGHTWATCH_CONFIG", "/tmp/opencode/lg_thermal_watch_nightwatch.json"  # nosec B108
)
STATIC_DIR = Path(__file__).parent / "static"

_agent: LGPC18SKAgent | None = None
_temp_logger: RoomTempLogger | None = None
_night_watch: LGPC18SKNightWatch | None = None
_session = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Manage the shared aiohttp session for the application lifetime."""
    global _session
    from aiohttp import ClientSession

    _session = ClientSession()
    yield
    if _session is not None and not _session.closed:
        await _session.close()


def get_agent() -> LGPC18SKAgent:
    """Return the shared live agent, creating it lazily on first use."""
    global _agent
    if _agent is None:
        controller = create_thinq_agent_controller(cast(ClientSession, _session))
        _agent = LGPC18SKAgent(controller)
    return _agent


def get_temp_logger() -> RoomTempLogger:
    """Return the shared temperature logger, creating it lazily on first use."""
    global _temp_logger
    if _temp_logger is None:
        _temp_logger = RoomTempLogger(get_agent(), db_path=DB_PATH, interval_s=60.0)
    return _temp_logger


def get_night_watch() -> LGPC18SKNightWatch:
    """Return the shared night watch, creating it lazily on first use."""
    global _night_watch
    if _night_watch is None:
        _night_watch = LGPC18SKNightWatch(get_agent())
    return _night_watch


class NightWatchConfig(BaseModel):
    """Night watch configuration."""

    low_threshold_c: float = Field(default=18.0, description="Low room temperature trigger (C).")
    high_target_c: float = Field(default=22.0, description="High target temperature (C).")
    fan_low: bool = Field(default=True, description="Fan speed LOW during gentle heat.")
    night_start_hour: int = Field(default=0, description="Night window start hour (0-23).")
    night_end_hour: int = Field(default=7, description="Night window end hour (0-23, exclusive).")
    enabled: bool = Field(default=True, description="Whether the watch is enabled.")


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
    ) -> dict[str, Any]:
        """Return temperature readings (and min/max) within an optional window or preset timeline."""
        logger = get_temp_logger()
        if timeline and not (start or end):
            now = datetime.now(timezone.utc)
            deltas = {"24h": 24, "7d": 168, "30d": 720}
            hours = deltas.get(timeline, 24)
            start_dt = now - timedelta(hours=hours)
            start = start_dt.isoformat()
        readings = logger.get_readings(start=start, end=end)
        temps = [float(r["temp_c"]) for r in readings]
        return {
            "readings": readings,
            "min_temp_c": min(temps) if temps else None,
            "max_temp_c": max(temps) if temps else None,
            "count": len(temps),
            "timeline": timeline,
        }

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """Return live device status."""
        agent = get_agent()
        status = await agent.get_device_status()
        if status is None:
            raise HTTPException(status_code=503, detail="device status unavailable")
        return status

    @app.get("/api/nightwatch/config")
    async def nightwatch_config_get() -> dict[str, Any]:
        """Return current night watch settings."""
        watch = get_night_watch()
        return {
            "enabled": watch.is_running,
            "low_threshold_c": watch.low_threshold_c,
            "high_target_c": watch.high_target_c,
            "fan_low": watch.fan_low,
            "night_start_hour": watch.night_start_hour,
            "night_end_hour": watch.night_end_hour,
        }

    @app.post("/api/nightwatch/config")
    async def nightwatch_config(config: NightWatchConfig) -> dict[str, Any]:
        """Update night watch settings and (re)start or stop the watch."""
        path = Path(NIGHTWATCH_CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(json.loads(config.model_dump_json()), indent=2), encoding="utf-8")
        watch = get_night_watch()
        watch.low_threshold_c = config.low_threshold_c
        watch.high_target_c = config.high_target_c
        watch.fan_low = config.fan_low
        watch.night_start_hour = config.night_start_hour
        watch.night_end_hour = config.night_end_hour
        if config.enabled:
            await watch.start()
        else:
            await watch.stop()
        return {
            "enabled": config.enabled,
            "low_threshold_c": config.low_threshold_c,
            "high_target_c": config.high_target_c,
            "fan_low": config.fan_low,
            "night_start_hour": config.night_start_hour,
            "night_end_hour": config.night_end_hour,
        }

    @app.post("/api/nightwatch/start")
    async def nightwatch_start() -> dict[str, Any]:
        """Start the night watch."""
        watch = get_night_watch()
        await watch.start()
        return {"started": True}

    @app.post("/api/nightwatch/stop")
    async def nightwatch_stop() -> dict[str, Any]:
        """Stop the night watch."""
        watch = get_night_watch()
        await watch.stop()
        return {"stopped": True}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

    @app.get("/", response_class=FileResponse)
    async def dashboard() -> FileResponse:
        """Serve the dashboard UI."""
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    """Run the LG Thermal Watch API server with uvicorn."""
    import uvicorn

    host = os.environ.get("LG_THERMAL_WATCH_HOST", "127.0.0.1")
    port = int(os.environ.get("LG_THERMAL_WATCH_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
