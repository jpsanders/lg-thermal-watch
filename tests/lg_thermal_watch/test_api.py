"""Unit tests for the FastAPI application endpoints."""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient

from lg_thermal_watch import api
from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.nightwatch import LGPC18SKNightWatch
from lg_thermal_watch.tempdb import RoomTempLogger


class MockAgent:
    """Mock of the LGPC18SKAgent control interface."""

    async def get_device_status(self) -> dict[str, object] | None:
        """Return a minimal status."""
        return {"airConJobMode": {"currentJobMode": "HEAT"}, "operation": {"airConOperationMode": "OFF"}}

    async def get_room_temperature(self) -> float | None:
        """Return a room temperature."""
        return 21.5

    async def get_target_temperature(self) -> float | None:
        """Return a target temperature."""
        return 24.0

    async def get_energy_consumption(self) -> dict[str, object] | None:
        """Return an energy stub."""
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Build a test client with a mocked agent and temp logger."""
    mock_agent = MockAgent()
    monkeypatch.setattr(api, "_agent", LGPC18SKAgent(mock_agent))  # type: ignore[arg-type]
    monkeypatch.setattr(
        api,
        "_temp_logger",
        RoomTempLogger(mock_agent, db_path="/tmp/opencode/lg_thinq_room_temps_test.db"),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(api, "_night_watch", LGPC18SKNightWatch(mock_agent))  # type: ignore[arg-type]
    app = api.create_app()
    with TestClient(app) as test_client:
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


def test_status_endpoint(client: TestClient) -> None:
    """The status endpoint returns the mocked device status."""
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["airConJobMode"]["currentJobMode"] == "HEAT"


def test_nightwatch_config_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The night watch config endpoint accepts a settings payload."""
    monkeypatch.setattr(api, "_night_watch", LGPC18SKNightWatch(MockAgent()))  # type: ignore[arg-type]
    response = client.post(
        "/api/nightwatch/config",
        json={
            "low_threshold_c": 18.0,
            "high_target_c": 22.0,
            "fan_low": True,
            "night_start_hour": 0,
            "night_end_hour": 7,
            "enabled": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["low_threshold_c"] == 18.0
    assert body["high_target_c"] == 22.0


def test_nightwatch_start_and_stop(client: TestClient) -> None:
    """Night watch start/stop endpoints respond ok."""
    assert client.post("/api/nightwatch/start").status_code == 200
    assert client.post("/api/nightwatch/stop").status_code == 200


def test_dashboard_serves_index(client: TestClient) -> None:
    """The dashboard root serves the static UI."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
