# lg-thermal-watch

## NAME

lg-thermal-watch - a night watch, a temperature log and a dashboard for an LG heat pump on the LG ThinQ Connect API

## SYNOPSIS

```
lg-thermal-watch [serve]
lg-thermal-watch token [--json]
lg-thermal-watch mcp
lg-thermal-watch guide
lg-thermal-watch man [--install | --raw]
lg-thermal-watch --version
```

## DESCRIPTION

lg-thermal-watch drives an LG air to air heat pump through LG's official ThinQ Connect API. It was built for the
PC18SK indoor unit and works with any ThinQ Connect air conditioner that reports a room temperature.

Three things run in one small process:

- **The night watch.** When the room drops below a low threshold inside a night window (00:00 to 07:00 by default,
  in a timezone you set), it powers the unit on, heats gently to a target with the fan on LOW, then switches it off.
- **The temperature log.** Every 60 seconds the room temperature goes into SQLite, with min, max and mean over any
  window, and every on and off run is recorded with its duration and source.
- **The dashboard and API.** A page with the live room temperature against the thresholds, what the watch is doing
  and why, a history chart, the runs and an energy estimate, and manual heat and off controls.

Every command to the unit is rate limited and kept within safe bounds, and the unit only gets the settings it lacks.

## COMMANDS

| Command | What it does |
| --- | --- |
| `serve` | Run the service: the log, the night watch if enabled, the dashboard and the API. The default. |
| `token` | Print the API token and the dashboard link that stores it in your browser. |
| `mcp` | Serve the running service's tools to AI agents over the Model Context Protocol, on stdio. |
| `guide` | Print this manual as Markdown, for people and agents. |
| `man` | Show this manual as a man page. `--install` puts it where `man lg-thermal-watch` finds it. |

## CONFIGURATION

Everything is read from the environment; a systemd unit reads it from an environment file kept at mode 600.

| Variable | Meaning |
| --- | --- |
| `LG_THINQ_ACCESS_TOKEN` | Your ThinQ Connect personal access token. Required. |
| `LG_THINQ_CLIENT_ID` | A client id for ThinQ Connect. Required. |
| `LG_THINQ_COUNTRY_CODE` | The two letter country of your LG account, such as `PT`. Required. |
| `LG_THINQ_DEVICE_ID` | The unit's device id. |
| `LG_THERMAL_WATCH_HOST` | Address to listen on. Default `127.0.0.1`. |
| `LG_THERMAL_WATCH_PORT` | Port. Default `8000`. |
| `LG_THERMAL_WATCH_TOKEN` | The API token. Default: generated once into the state folder. |
| `LG_THERMAL_WATCH_ALLOWED_HOSTS` | Extra host names the dashboard may be reached by, comma separated. |
| `LG_THERMAL_WATCH_TIMEZONE` | IANA zone of the night window when the settings omit one, such as `Europe/Lisbon`. |
| `LG_THERMAL_WATCH_DB` | SQLite file. Default `~/.local/share/lg-thermal-watch/room_temps.db`. |
| `LG_THERMAL_WATCH_NIGHTWATCH_CONFIG` | Night watch settings. Default `~/.config/lg-thermal-watch/nightwatch.json`. |
| `LG_THERMAL_WATCH_NIGHTWATCH_STATE` | Night watch state. Default: a `.state.json` file beside the settings. |
| `LG_THERMAL_WATCH_AVG_POWER_KW` | Assumed average draw while running, for the estimate. Default `1.0`. |
| `LG_THERMAL_WATCH_URL` | Where `mcp` finds the running service. Default `http://127.0.0.1:<port>`. |

## NIGHT WATCH SETTINGS

`enabled` (true), `low_threshold_c` (18, from 5 to 29), `high_target_c` (22, from 16 to 30), `fan_low` (true),
`night_start_hour` (0) and `night_end_hour` (7, exclusive), and `timezone`. The dashboard edits them, and they are
saved in the settings file, so a restart or a reboot carries on where it was.

## SECURITY

Every `/api` call except `/api/health` needs the token, as `Authorization: Bearer <token>` or as the cookie the
dashboard sets. Open the link `lg-thermal-watch token` prints once per browser; the cookie is HttpOnly and
SameSite=Strict, so other sites cannot read or send it. Requests naming a host the machine does not answer to
(anything but localhost, the machine's own names, `*.ts.net` and IP addresses, plus `LG_THERMAL_WATCH_ALLOWED_HOSTS`)
are refused, which stops DNS rebinding.

To reach the dashboard from your other devices over Tailscale, listen on the Tailscale address only, so the local
network never sees the port: `LG_THERMAL_WATCH_HOST=$(tailscale ip -4)`. Never expose the port to the internet.

## API

| Request | What it does |
| --- | --- |
| `GET /api/health` | Health check, no token needed. |
| `GET /api/overview` | Everything the dashboard shows, in one call. |
| `GET /api/status` | The unit's raw ThinQ status. |
| `GET /api/temps` | Logged temperatures and stats; `timeline=24h`, `7d` or `30d`, or `start` and `end`. |
| `GET /api/runtime` | On and off runs and the monthly energy estimate; `months=1` to `24`. |
| `POST /api/device/heat` | Heat now: `{"target_c": 22, "fan_low": true}`. |
| `POST /api/device/off` | Turn the unit off. |
| `GET /api/nightwatch` | The night watch's live state. |
| `GET /api/nightwatch/config` | The night watch settings. |
| `POST /api/nightwatch/config` | Replace the settings, and start or stop the watch to match. |
| `POST /api/nightwatch/start` | Enable and start the watch. |
| `POST /api/nightwatch/stop` | Disable and stop it; turns the unit off if the watch was heating. |

## MCP SERVER

`lg-thermal-watch mcp` speaks JSON-RPC 2.0 on stdio and calls the running service with the same token, so the
service stays the only owner of the unit. Tools: `status`, `temperatures` and `runtime` are read only; `heat`, `off`,
`night_watch_set`, `night_watch_start` and `night_watch_stop` are marked destructive, so the harness asks first.

```
{"mcpServers": {"lg-thermal-watch": {"command": "lg-thermal-watch", "args": ["mcp"]}}}
```

## FILES

`~/.local/state/lg-thermal-watch/token` holds the token, mode 600. The database and settings live where the
variables above say. `~/.local/share/man/man1/lg-thermal-watch.1` is where `man --install` puts the man page.

## EXIT STATUS

`serve` runs until stopped. The other commands exit 0 on success and 2 when the arguments are wrong.

## EXAMPLES

Run it as a systemd user service:

```
# ~/.config/systemd/user/lg-thermal-watch.service
[Service]
EnvironmentFile=%h/.config/lg-thermal-watch/lg-thermal-watch.env
ExecStart=%h/.local/bin/lg-thermal-watch
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```
systemctl --user enable --now lg-thermal-watch
lg-thermal-watch token
curl -H "Authorization: Bearer $(cat ~/.local/state/lg-thermal-watch/token)" http://127.0.0.1:8000/api/overview
```

## FOR AGENTS

Read with `status` (or `GET /api/overview`) before anything else. `heat`, `off` and the night watch tools act on a
real heat pump in someone's home: call them only when the person asked for that change, say what you will do first,
and report the answer. Keep the token private; it controls the unit.

## SEE ALSO

https://github.com/jpsanders/lg-thermal-watch

## AUTHOR

James Phillip Sanders, https://jamesphillipsanders.com
