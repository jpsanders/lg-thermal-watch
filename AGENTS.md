# AGENTS.md

Instructions for AI coding agents working on lg-thermal-watch itself. To use the service, read
`skills/lg-thermal-watch/SKILL.md` or run `lg-thermal-watch guide`.

## Commands

```sh
make init-dev     # install the package and the development tools into the active environment
make format       # apply black and isort
make checks       # every check CI runs, in CI's order; run it before you say a change is done
make man          # rebuild the man page and the packaged manual after editing docs/MANUAL.md
```

Use Python 3.15 for development; the package supports 3.14 and later. Keep the code runnable on 3.12 too: the
author's own unit runs from a source tree on 3.12.

## Layout

```text
src/lg_thermal_watch/api.py         FastAPI app, the token guard, the command line
src/lg_thermal_watch/security.py    the token, the cookie and the Host check
src/lg_thermal_watch/mcp.py         the MCP server, a client of the running service
src/lg_thermal_watch/agent.py       safe, rate limited commands to the unit
src/lg_thermal_watch/controller.py  the ThinQ Connect SDK wrapper
src/lg_thermal_watch/nightwatch.py  the night watch loop
src/lg_thermal_watch/tempdb.py      the temperature log, SQLite
src/lg_thermal_watch/runtime.py     on and off runs and the energy estimate
src/lg_thermal_watch/static/        the dashboard, one HTML file
docs/MANUAL.md                      the one source for the guide and the man page
```

## Rules

- Tests never talk to LG. Use the mock controller in `tests/lg_thermal_watch/test_api.py`.
- Every new `/api` route is behind the token guard unless there is a written reason; `/api/health` is the only one.
- A tool that switches the unit or changes the night watch is marked destructive in `mcp.py`.
- Never commit credentials, device ids, home details or the token.
- Tags carry the bare version, `1.2.0`, never `v1.2.0`.
