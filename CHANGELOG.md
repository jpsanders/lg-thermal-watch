# Changelog

Versions follow [semantic versioning](https://semver.org). Tags carry the bare version, such as `1.0.0`.

## 1.0.0 (2026-11-12)

The first release.

- Security: every `/api` call except the health check now needs a token, sent as a bearer header or as the
  dashboard's HttpOnly, SameSite=Strict cookie, and requests naming an unknown host are refused. Before this, anyone
  who could reach the port could switch the unit, and a web page could try to.
- `lg-thermal-watch token` prints the token and the dashboard link that stores it in the browser.
- An MCP server, `lg-thermal-watch mcp`, that reads the room and the runs and, when asked, heats, switches off or
  changes the night watch; the switching tools are marked destructive. A Claude Code plugin with a skill.
- A full manual: `guide` prints it, `man` shows it, `man --install` makes `man lg-thermal-watch` work.
- Defaults moved out of `/tmp`: the database goes to `~/.local/share/lg-thermal-watch/`, the settings to
  `~/.config/lg-thermal-watch/`. Set `LG_THERMAL_WATCH_DB` and `LG_THERMAL_WATCH_NIGHTWATCH_CONFIG` to keep old paths.
- Fixed: installs failed because the project declared no version; CI had been red since the first commit. The
  version now comes from the git tag.
- Python 3.14 or later; CI tests 3.14 and 3.15 on Linux and macOS. A release workflow, Dependabot, issue forms,
  AGENTS.md, llms.txt, a security policy and a code of conduct.

Built before this release: the ThinQ control client, the night watch, the temperature log, runtime tracking and the
dashboard.
