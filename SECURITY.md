# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's
[private vulnerability reporting](https://github.com/jpsanders/lg-thermal-watch/security/advisories/new), not in a
public issue. You will get an answer within a week.

## How it is protected

- Every `/api` call except the health check needs the token, as a bearer header or as the dashboard's cookie. The
  token is random, generated once into `~/.local/state/lg-thermal-watch/token` with mode 600, or set in
  `LG_THERMAL_WATCH_TOKEN`.
- The dashboard's cookie is HttpOnly and SameSite=Strict, so another site in the same browser can neither read it
  nor send it, which stops cross site requests that would switch the unit.
- Requests that name a host the machine does not answer to are refused, which stops DNS rebinding.
- It listens on 127.0.0.1 by default. To use it from other devices, listen on the Tailscale address only
  (`LG_THERMAL_WATCH_HOST=$(tailscale ip -4)`), never on a public address.
- The LG credentials stay in the environment file the service reads; they are never logged or returned by the API.
- Every command to the unit is bounded and rate limited, and the unit only gets the settings it lacks.

Versions before 1.0.0 had no token: anyone who could reach the port could switch the unit. Update, and if you
listened on `0.0.0.0`, check your network.
