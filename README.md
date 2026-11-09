# lg-thermal-watch

**Local AI control and telemetry for the LG PC18SK heat pump via the official LG ThinQ Connect API.**

`lg-thermal-watch` is a small, open-source Python package that turns an LG air-conditioning /
heat-pump unit managed by the LG ThinQ Smart ThinQ ecosystem into a locally orchestrated,
AI-friendly thermal appliance. It pairs a validated ThinQ control client with two automation
loops — a **night-time gentle-heat watch** and a **room-temperature logger** — and exposes the
whole thing over a small FastAPI dashboard with a Chart.js temperature history view.

The project was born from a field-engineering mission: programmatically control the LG
PC18SK / S3NM18KL2DA heat pump over the LG ThinQ Connect API, validate every command live,
and close the loop with overnight gentle heating plus continuous room-temperature logging.

## Features

- **ThinQ control client** — status reads, room/target temperature reads, HVAC mode writes,
  target temperature writes (LG requires job-mode context in the payload), fan-speed writes,
  power on/off, with safety bounds and command rate limiting.
- **Night watch** — when the room temperature drops below a low threshold during a night
  window (default 00:00–07:00), the system gently heats to a high target temperature with the
  fan set to `LOW`.
- **Room temp logger** — reads the room temperature every 60 seconds and stores readings in a
  lightweight SQLite database, with min/max queries over arbitrary time windows (e.g. last 24 h).
- **FastAPI dashboard** — live room temperature, target temperature, device state, temperature
  history chart, and night-watch controls.

## How it works

```
LG ThinQ Connect API
        │
        ▼
LGPC18SKAgent  ── ThinQAgentController  ── thinqconnect SDK
        │
        ├───► LGPC18SKNightWatch   (gentle heat when cold)
        └───► RoomTempLogger       (SQLite persistence)
                   │
                   ▼
            FastAPI /api/*  ──►  static dashboard (Chart.js)
```

## Quick start

```bash
git clone git@github.com:jpsanders/lg-thermal-watch.git
cd lg-thermal-watch
make init-dev
make checks
```

## Configuration

Environment variables consumed by the controller:

| Variable | Purpose |
| --- | --- |
| `LG_THINQ_ACCESS_TOKEN` | LG ThinQ Connect access token |
| `LG_THINQ_CLIENT_ID` | LG ThinQ Connect client id |
| `LG_THINQ_COUNTRY_CODE` | LG country code (e.g. `NZ`) |
| `LG_THINQ_DEVICE_ID` | LG device id (e.g. the PC18SK air handler) |
| `LG_THERMAL_WATCH_DB` | SQLite path for temperature readings (default `/tmp/opencode/lg_thinq_room_temps.db`) |
| `LG_THERMAL_WATCH_NIGHTWATCH_CONFIG` | Night watch JSON config path (default `/tmp/opencode/lg_thermal_watch_nightwatch.json`) |

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| GET | `/api/status` | Live device status |
| GET | `/api/temps` | Temperature readings + min/max over an optional window (`?start=...&end=...`) |
| POST | `/api/nightwatch/config` | Update night watch settings (body: `NightWatchConfig`) |
| POST | `/api/nightwatch/start` | Start the night watch |
| POST | `/api/nightwatch/stop` | Stop the night watch |
| GET | `/` | Dashboard UI |

## Night watch tuning

The night watch is configured via `NightWatchConfig`:

- `low_threshold_c` — gentle heat triggers when room temperature is below this (default `18.0`).
- `high_target_c` — heating stops when room temperature reaches this (default `22.0`).
- `fan_low` — fan speed `LOW` during gentle heating (default `true`).
- `night_start_hour` / `night_end_hour` — night window hours (default `0` / `7`).

## Development

The project is scaffolded on top of the
[python-package-template](https://github.com/jpsanders/python-package-template) and keeps its
strict typing, formatting, and CI checks.

```bash
make checks   # isort + black + editorconfig + docstyle + flake8 + mypy + bandit + pytest
make build-docker
```

## License

MIT — see [LICENSE](LICENSE).
