---
name: lg-thermal-watch
description: Check and control an LG heat pump through a running lg-thermal-watch service (LG ThinQ Connect API): the room temperature, the unit's state, the night watch that heats gently on cold nights, temperature history and energy estimates. Use when the user asks how warm the room is, asks to heat or switch off the heat pump, or to change the night watch's thresholds or hours.
---

# LG heat pump with lg-thermal-watch

lg-thermal-watch runs as a service on the user's machine. It owns the LG ThinQ Connect session, logs the room
temperature every minute and runs a night watch that heats gently when the room gets cold at night. You talk to the
service, never to LG directly.

## Tools

With the Claude Code plugin, the `lg-thermal-watch` MCP server gives you:

| Tool | Changes something | What it does |
| --- | --- | --- |
| `status` | no | room temperature, the unit's state, what the night watch is doing, its settings |
| `temperatures` | no | logged temperatures with min, max and mean; `timeline` 24h, 7d or 30d |
| `runtime` | no | on and off runs and the monthly energy estimate |
| `heat` | yes | heat now to `target_c` (16 to 30) with the fan LOW |
| `off` | yes | switch the unit off |
| `night_watch_set` | yes | replace the night watch settings |
| `night_watch_start`, `night_watch_stop` | yes | enable or disable the night watch |

Without the MCP server, the same calls are HTTP: `GET http://127.0.0.1:8000/api/overview` with the header
`Authorization: Bearer $(cat ~/.local/state/lg-thermal-watch/token)`. `lg-thermal-watch guide` prints the full API.

## Rules

1. Read `status` first, every time.
2. A tool that changes something acts on a real heat pump in someone's home. Call it only when the person asked
   for that change, say what you will do before you do it, and report what the service answered.
3. For `night_watch_set`, read the current settings with `status` and send every field you want to keep.
4. If a tool says the service is not reachable, tell the person; do not try to start or restart services yourself
   unless they ask. `systemctl --user status lg-thermal-watch` shows whether it runs.
5. Never print or share the token. It controls the unit.
