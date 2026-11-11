"""Unit tests for the FastAPI application endpoints."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lg_thermal_watch import api
from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.nightwatch import LGPC18SKNightWatch
from lg_thermal_watch.tempdb import RoomTempLogger

VALID_CONFIG: dict[str, Any] = {
    "low_threshold_c": 20.0,
    "high_target_c": 22.0,
    "fan_low": True,
    "night_start_hour": 0,
    "night_end_hour": 7,
    "timezone": "Europe/Lisbon",
    "enabled": False,
}


class MockController:
    """Mock of the ThinQAgentController surface the agent uses."""

    def __init__(self) -> None:
        """Initialise the mock device."""
        self.power = "POWER_OFF"
        self.calls: list[str] = []
        self.fail_turn_off = False

    async def get_device_status(self) -> dict[str, Any] | None:
        """Return a ThinQ-shaped status."""
        return {
            "airConJobMode": {"currentJobMode": "HEAT"},
            "operation": {"airConOperationMode": self.power},
            "temperatureInUnits": [{"currentTemperature": 21.5, "targetTemperature": 26, "unit": "C"}],
            "airFlow": {"windStrength": "HIGH"},
        }

    async def get_energy_consumption(self) -> dict[str, Any] | None:
        """Return an energy stub."""
        return None

    async def turn_on(self) -> dict[str, Any] | None:
        """Record power on."""
        self.calls.append("turn_on")
        self.power = "POWER_ON"
        return {}

    async def turn_off(self) -> dict[str, Any] | None:
        """Record power off (or fail, when scripted to)."""
        if self.fail_turn_off:
            raise ConnectionError("LG cloud unreachable")
        self.calls.append("turn_off")
        self.power = "POWER_OFF"
        return {}

    async def set_hvac_mode(self, mode: str) -> dict[str, Any] | None:
        """Record mode."""
        self.calls.append(f"set_hvac_mode:{mode}")
        return {}

    async def set_target_temperature(self, value_c: float, *, job_mode: str | None = None) -> dict[str, Any] | None:
        """Record target."""
        self.calls.append(f"set_target_temperature:{value_c}:{job_mode}")
        return {}

    async def set_fan_speed(self, speed: str) -> dict[str, Any] | None:
        """Record fan speed."""
        self.calls.append(f"set_fan_speed:{speed}")
        return {}


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every file the app touches at a temporary directory (never the live config)."""
    monkeypatch.setattr(api, "DB_PATH", str(tmp_path / "temps.db"))
    monkeypatch.setattr(api, "NIGHTWATCH_CONFIG_PATH", str(tmp_path / "nightwatch.json"))
    monkeypatch.setattr(api, "NIGHTWATCH_STATE_PATH", "")
    monkeypatch.setattr(api, "_config", None)
    monkeypatch.setattr(api, "_agent_error", None)
    return tmp_path


@pytest.fixture
def controller() -> MockController:
    """Return a fresh mock device."""
    return MockController()


@pytest.fixture
def client(
    paths: Path, controller: MockController, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    """Build a test client over a mocked device."""
    agent = LGPC18SKAgent(controller)  # type: ignore[arg-type]
    logger = RoomTempLogger(agent, db_path=str(paths / "temps.db"))
    monkeypatch.setattr(api, "_agent", agent)
    monkeypatch.setattr(api, "_temp_logger", logger)
    watch = LGPC18SKNightWatch(agent, event_sink=logger.record_event, state_path=paths / "state.json")
    monkeypatch.setattr(api, "_night_watch", watch)
    with TestClient(api.create_app()) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    """The health endpoint returns ok."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_temps_endpoint(client: TestClient) -> None:
    """The temps endpoint returns readings with min/max."""
    response = client.get("/api/temps?end=2030-01-01T00:00:00")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 0
    assert "min_temp_c" in body
    assert "max_temp_c" in body
    assert "readings" in body


def test_temps_timeline_includes_logged_reading_and_stats(client: TestClient) -> None:
    """The logger starts with the app, and its readings show up in the 24h timeline with stats and events."""
    body = client.get("/api/temps?timeline=24h").json()
    assert body["count"] >= 1
    assert body["readings"][0]["temp_c"] == 21.5
    assert body["stats"]["latest"]["temp_c"] == 21.5
    assert body["events"] == []
    assert client.get("/api/temps?timeline=30d").json()["bucket_s"] == 1800


def test_temps_rejects_bad_timestamp(client: TestClient) -> None:
    """Malformed window bounds are a 422, not a 500."""
    assert client.get("/api/temps?start=yesterday").status_code == 422


def test_status_endpoint(client: TestClient) -> None:
    """The status endpoint returns the raw device status."""
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["airConJobMode"]["currentJobMode"] == "HEAT"


def test_overview_endpoint(client: TestClient) -> None:
    """The overview carries the normalised device state, watch state and logger state."""
    body = client.get("/api/overview").json()
    assert body["device"]["ok"] is True
    assert body["device"]["room_temp_c"] == 21.5
    assert body["device"]["is_on"] is False
    assert body["watch"]["enabled"] is False
    assert "window_start" in body["watch"]
    assert body["logger"]["running"] is True


def test_nightwatch_config_endpoint(client: TestClient, paths: Path) -> None:
    """The night watch config endpoint accepts a settings payload and persists it."""
    response = client.post("/api/nightwatch/config", json=VALID_CONFIG)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["low_threshold_c"] == 20.0
    assert body["high_target_c"] == 22.0
    assert json.loads((paths / "nightwatch.json").read_text())["timezone"] == "Europe/Lisbon"


def test_timezone_env_default(client: TestClient, paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An omitted timezone falls back to LG_THERMAL_WATCH_TIMEZONE; an explicit one still wins."""
    monkeypatch.setenv("LG_THERMAL_WATCH_TIMEZONE", "Pacific/Auckland")
    no_tz = {k: v for k, v in VALID_CONFIG.items() if k != "timezone"}
    response = client.post("/api/nightwatch/config", json=no_tz)
    assert response.status_code == 200
    assert response.json()["timezone"] == "Pacific/Auckland"
    assert json.loads((paths / "nightwatch.json").read_text())["timezone"] == "Pacific/Auckland"
    response = client.post("/api/nightwatch/config", json={**no_tz, "timezone": "Europe/Lisbon"})
    assert response.json()["timezone"] == "Europe/Lisbon"


def test_nightwatch_config_enable_starts_watch(client: TestClient) -> None:
    """Enabling via config starts the watch and the overview reflects it."""
    response = client.post("/api/nightwatch/config", json={**VALID_CONFIG, "enabled": True})
    assert response.json()["running"] is True
    watch = client.get("/api/nightwatch").json()
    assert watch["enabled"] is True and watch["running"] is True
    assert watch["timezone"] == "Europe/Lisbon"


@pytest.mark.parametrize(
    "change",
    [
        {"low_threshold_c": 22.0},
        {"high_target_c": 22.5},
        {"night_end_hour": 0},
        {"timezone": "Mars/Olympus"},
        {"night_start_hour": 24},
    ],
)
def test_nightwatch_config_validation(client: TestClient, change: dict[str, Any]) -> None:
    """Inconsistent settings are rejected with 422."""
    assert client.post("/api/nightwatch/config", json={**VALID_CONFIG, **change}).status_code == 422


def test_nightwatch_start_and_stop_persist_enabled(client: TestClient, paths: Path) -> None:
    """Start/stop endpoints respond ok and persist the enabled flag across restarts."""
    assert client.post("/api/nightwatch/start").status_code == 200
    assert json.loads((paths / "nightwatch.json").read_text())["enabled"] is True
    assert client.post("/api/nightwatch/stop").status_code == 200
    assert json.loads((paths / "nightwatch.json").read_text())["enabled"] is False


def test_saved_config_autostarts_watch(
    paths: Path, controller: MockController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On start-up the app loads the saved config and starts the watch when it is enabled."""
    (paths / "nightwatch.json").write_text(json.dumps({**VALID_CONFIG, "enabled": True}))
    agent = LGPC18SKAgent(controller)  # type: ignore[arg-type]
    monkeypatch.setattr(api, "_agent", agent)
    monkeypatch.setattr(api, "_temp_logger", None)
    monkeypatch.setattr(api, "_night_watch", None)
    with TestClient(api.create_app()) as test_client:
        watch = test_client.get("/api/nightwatch").json()
    assert watch["running"] is True
    assert watch["low_threshold_c"] == 20.0 and watch["high_target_c"] == 22.0
    assert watch["timezone"] == "Europe/Lisbon"


def test_device_heat_powers_on_first(
    client: TestClient, controller: MockController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual heat powers the unit on first, then sends only what differs (the mock unit is already in HEAT)."""
    monkeypatch.setattr(api, "DEFAULT_POWER_ON_SETTLE_S", 0)
    response = client.post("/api/device/heat", json={"target_c": 22})
    assert response.status_code == 200
    assert response.json()["commands"] == ["turn_on", "set_target_temperature", "set_fan_speed"]
    assert controller.calls == ["turn_on", "set_target_temperature:22.0:HEAT", "set_fan_speed:LOW"]
    assert client.post("/api/device/off").status_code == 200
    assert controller.calls[-1] == "turn_off"


def test_missing_credentials_degrades_gracefully(paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without LG credentials the device endpoints say why (503), and history still works."""
    for name in ("LG_THINQ_ACCESS_TOKEN", "LG_THINQ_CLIENT_ID", "LG_THINQ_COUNTRY_CODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api, "_agent", None)
    monkeypatch.setattr(api, "_temp_logger", None)
    monkeypatch.setattr(api, "_night_watch", None)
    with TestClient(api.create_app()) as test_client:
        status = test_client.get("/api/status")
        assert status.status_code == 503
        assert "credentials" in status.json()["detail"]
        overview = test_client.get("/api/overview").json()
        assert overview["device"]["ok"] is False
        assert test_client.get("/api/temps?timeline=24h").status_code == 200


def test_dashboard_serves_index(client: TestClient) -> None:
    """The dashboard root serves the static UI."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_device_heat_refuses_when_budget_is_short(client: TestClient, controller: MockController) -> None:
    """Manual heat is refused up front (429) rather than half-applied when the throttle is nearly spent."""
    agent = api._agent
    assert agent is not None
    agent._commands_in_window = agent.max_commands_per_window - 1
    response = client.post("/api/device/heat", json={"target_c": 22})
    assert response.status_code == 429
    assert controller.calls == []


def test_disable_reports_unit_left_on(client: TestClient, controller: MockController) -> None:
    """Disabling the watch while it heats, with the unit refusing to switch off, is a clear 502."""
    watch = api._night_watch
    assert watch is not None
    controller.power = "POWER_ON"
    watch._heating = True
    controller.fail_turn_off = True
    response = client.post("/api/nightwatch/stop")
    assert response.status_code == 502
    assert "switching the heat pump off failed" in response.json()["detail"]
