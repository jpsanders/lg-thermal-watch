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
  window (default 00:00–07:00, in a configurable timezone), the system powers the unit on and
  gently heats to a high target temperature with the fan set to `LOW`, then switches it off.
- **Room temp logger** — reads the room temperature every 60 seconds and stores readings in a
  lightweight SQLite database (UTC timestamps), with min/max/mean queries and time-bucketed
  history over arbitrary windows (e.g. last 24 h).
- **FastAPI dashboard** — live room temperature against the night-watch thresholds, what the
  watch is doing and why, a temperature history chart (night windows, heating periods and
  thresholds marked, with a table view), night-watch settings, and manual heat/off controls.
- **Runtime tracking** — every on/off run is recorded with its duration and source (night watch
  or manual), and the dashboard shows recent runs plus an estimated monthly energy chart.

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
| `LG_THERMAL_WATCH_DB` | SQLite path for readings (default `/tmp/opencode/lg_thinq_room_temps.db`) |
| `LG_THERMAL_WATCH_NIGHTWATCH_CONFIG` | Watch settings (default `/tmp/opencode/lg_thermal_watch_nightwatch.json`) |
| `LG_THERMAL_WATCH_NIGHTWATCH_STATE` | Watch runtime state (default: a `.state.json` file next to the settings) |
| `LG_THERMAL_WATCH_TIMEZONE` | Fallback IANA zone for the night window when the config omits one (e.g. `Europe/Lisbon`) |
| `LG_THERMAL_WATCH_AVG_POWER_KW` | Assumed average electrical draw (kW) while the unit runs, for the energy estimate (default `1.0`) |
| `LG_THERMAL_WATCH_HOST` | Bind host (default `127.0.0.1`; `0.0.0.0` exposes it on LAN / Tailscale) |
| `LG_THERMAL_WATCH_PORT` | Bind port (default `8000`) |

The defaults live in `/tmp`, which most systems clear on reboot; point the paths somewhere persistent
(e.g. `~/.local/share/lg-thermal-watch/`) for anything long-running.

## Running as a service

On start-up the app begins logging temperatures and, if the saved config has `"enabled": true`, starts
the night watch, so a restart (or a reboot) needs no manual step. A systemd user unit keeps it running:

```ini
# ~/.config/systemd/user/lg-thermal-watch.service
[Unit]
Description=LG Thermal Watch

[Service]
EnvironmentFile=%h/.config/lg-thermal-watch/lg-thermal-watch.env
ExecStart=%h/.local/bin/lg-thermal-watch
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Put the `LG_THINQ_*` credentials and `LG_THERMAL_WATCH_*` settings in the (mode `600`) environment file,
then `systemctl --user enable --now lg-thermal-watch` and follow it with
`journalctl --user -u lg-thermal-watch -f`. User services stop when you log out unless lingering is
enabled (`loginctl enable-linger`).

## Exposing on Tailscale

The dashboard binds to `127.0.0.1:8000` by default. To reach it from your Tailscale network, bind to
all interfaces so Tailscale can route to the machine's Tailscale IP:

```bash
# On the host machine
export LG_THERMAL_WATCH_HOST=0.0.0.0
export LG_THERMAL_WATCH_PORT=8000
make init-dev   # or: pip install -e .
lg-thermal-watch   # runs uvicorn on 0.0.0.0:8000
```

Then open `http://<your-tailscale-ip>:8000` from any device on your Tailnet. Find your Tailscale IP
with:

```bash
tailscale ip
```

> ⚠️ Binding to `0.0.0.0` exposes the control API to anyone who can reach the machine (your LAN as
> well as your Tailscale IP). Only your joined devices can reach a Tailscale IP, but do not expose
> this port to the public internet.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| GET | `/api/overview` | Device summary, night watch state, logger state and config in one call |
| GET | `/api/status` | Raw device status from LG ThinQ |
| GET | `/api/temps` | Readings, stats and night watch events for a window, see below |
| GET | `/api/nightwatch` | Night watch live state (window, heating, last check, last action, errors) |
| GET | `/api/runtime` | On/off sessions, recent runs and estimated monthly energy use (`?months=1-24`, default 6) |
| GET | `/api/nightwatch/config` | Night watch settings |
| POST | `/api/nightwatch/config` | Update and persist settings (body: `NightWatchConfig`); starts/stops |
| POST | `/api/nightwatch/start` | Enable and start the night watch |
| POST | `/api/nightwatch/stop` | Disable the night watch (switches the unit off if it was heating) |
| POST | `/api/device/heat` | Heat now: power on, `HEAT`, target (body: `{"target_c": 22, "fan_low": true}`) |
| POST | `/api/device/off` | Switch the unit off |
| GET | `/` | Dashboard UI |

`/api/temps` takes `?timeline=24h|7d|30d` or `?start=...&end=...` (ISO-8601; naive means server-local
time), plus an optional `bucket_s` to average readings into buckets (7d and 30d default to 5 and 30
minutes). Device endpoints answer `503` without credentials and `429` when the command throttle is spent.

## Dashboard

`static/index.html` is a single-file page: TailAdmin-style shell (ink-black sidebar, sticky header,
cream content) built with Tailwind utilities via the Tailwind browser CDN, plus a hand-written CSS
layer for the palette variables and the data components (scale, charts, event lists, form controls).
The palette is cream surfaces, ink-black text and sidebar, and a burnt-orange accent; the room
temperature line is drawn in ink (cream in dark mode) so heating activity stays the only orange on
the chart. Light and dark themes share the same variable set, driven by `data-theme` and
`prefers-color-scheme` with the existing toggle.

The data-layer JavaScript is plain, framework-free, and shared by both themes: charts read their
colours from the CSS variables at render time, so a theme switch just re-renders. If the Tailwind or
Chart.js CDN is unreachable the page degrades — shell spacing and charts are lost, but the layout,
forms and controls keep working. Layout and drawer mechanics are plain CSS, not utility-dependent.

## Night watch

The night watch is configured via `NightWatchConfig` (from the dashboard or the API):

- `low_threshold_c` — gentle heat starts when the room is below this (default `18.0`).
- `high_target_c` — heat up to this, then switch off (default `22.0`; whole degrees, as the unit takes).
- `fan_low` — fan speed `LOW` while heating (default `true`).
- `night_start_hour` / `night_end_hour` — the window (default `0` / `7`); it may wrap past midnight.
- `timezone` — IANA zone for the window, e.g. `Europe/Lisbon` (default: `LG_THERMAL_WATCH_TIMEZONE`, else the
  system zone). Pin this when the server runs in a different zone from the room — the window is wall-clock
  time *here*, not the heat pump's or the server's.
- `enabled` — whether the watch runs; persisted, so it survives restarts.
- `enforce_off_hours` — the out-of-window guard (default `true`): when the unit turns on outside the
  night window — an app schedule, another machine, anything — the watch switches it back off within
  one poll cycle and records an `out_of_window_off` event, so the unit only ever runs under the
  night-watch criteria.
- `day_override_minutes` — how long Heat now pauses the guard (default `120`): a deliberate daytime
  heat session started from the dashboard is not fought by the guard until this expires; Turn off
  revokes it immediately.

Behaviour worth knowing:

- LG rejects commands while the unit is off (`2304`), so heating starts with power-on, then mode,
  target and fan, in that order. A sequence is only started when the command throttle can finish it.
- The unit beeps for each command it accepts, and LG's API offers no way to mute it (the PC18SK's
  capability profile has no sound control). The unit remembers mode, target and fan while off, so the
  watch sends only the settings that differ: once it has heated once, a cycle is one power-on and one
  power-off.
- When the window closes, or the watch is disabled, while it is heating, it switches the unit off.
  A process restart does not: the watch remembers that it owns the unit and carries on.
- If someone switches the unit off mid-cycle, the watch lets go and waits for the low threshold again.
- A failed check (network, LG cloud) is logged and retried on the next minute; it never stops the watch.
- Switching off is never throttled.

## Runtime tracking & energy estimates

The runtime tracker follows the heat pump's power state from the same device-status polls the
temperature logger already makes, so it costs no extra LG calls and no state of its own to keep
fresh. When the unit powers on it opens a session (labelled `watch` when the night watch owns the
unit, otherwise `manual`); the next powered-off reading closes it with its duration. Sessions live
in the `runtime_sessions` table next to the temperature readings, so an open session survives a
restart.

Only observed time is credited. If the service was down (or polling stalled) for longer than ten
minutes between two observations, the gap is not counted as running time: the open session ends at
its last observed moment and the next powered-on reading starts a fresh one. Within normal polling
the end of a session is the powered-off reading itself.

`GET /api/runtime` returns the open session, the recent completed runs, and a per-calendar-month
summary (`hours` and an estimated `kwh`) bucketed in the watch timezone. The energy figure is an
**estimate**: `hours × LG_THERMAL_WATCH_AVG_POWER_KW` (default 1.0 kW, the PC18SK's approximate
rated heating input). It shows how long the unit ran, not what the meter read — LG's API does not
expose per-session consumption.

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
