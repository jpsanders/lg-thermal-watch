"""
Who may talk to the dashboard and the API.

Every ``/api`` call except the health check needs the token, sent as ``Authorization: Bearer <token>`` or as the
``lgtw_token`` cookie that the dashboard sets after one visit to ``/?token=<token>``. The cookie is HttpOnly and
SameSite=Strict, so another site open in the same browser can neither read it nor send it. Requests that name a host
this machine does not answer to are refused before anything else, which stops DNS rebinding.

The token comes from ``LG_THERMAL_WATCH_TOKEN``, or from a file in the state folder that is created on first use
with mode 600. ``lg-thermal-watch token`` prints it with the dashboard link.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from pathlib import Path

TOKEN_ENV = "LG_THERMAL_WATCH_TOKEN"  # nosec B105: the variable name, not a password
COOKIE = "lgtw_token"  # nosec B105: the cookie name, not a password


def state_dir() -> Path:
    """
    Return the folder for runtime state such as the token.

    :returns: ``$XDG_STATE_HOME/lg-thermal-watch``, or ``~/.local/state/lg-thermal-watch``
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lg-thermal-watch"


def resolve_token() -> str:
    """
    Return the API token, creating the token file on first use.

    :returns: The token
    """
    from_env = os.environ.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env
    path = state_dir() / "token"
    try:
        saved = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        saved = ""
    if saved:
        return saved
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    return token


def same_token(given: str | None, expected: str) -> bool:
    """
    Compare a presented token with the real one in constant time.

    :param given: What the request carried, or None
    :param expected: The real token
    :returns: True when they match
    """
    return bool(given) and secrets.compare_digest(str(given).encode(), expected.encode())


def bearer(authorization: str | None) -> str | None:
    """
    Pull the token out of an ``Authorization: Bearer`` header.

    :param authorization: The header value, or None
    :returns: The token, or None
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


def hostname(host_header: str) -> str:
    """
    Strip the port, and the brackets of an IPv6 literal, from a Host header.

    :param host_header: Such as ``127.0.0.1:8000`` or ``[::1]:8000``
    :returns: The bare host name or address, lowercase
    """
    value = host_header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else ""
    return value.split(":", 1)[0]


def host_allowed(host_header: str, extra: frozenset[str] = frozenset()) -> bool:
    """
    Decide whether a request names a host this machine answers to.

    Allowed: ``localhost``, this machine's own names, Tailscale MagicDNS names (``*.ts.net``), any IP address, and the
    names in ``LG_THERMAL_WATCH_ALLOWED_HOSTS`` (comma separated) or ``extra``.

    :param host_header: The request's Host header
    :param extra: More names to allow
    :returns: True when the host is allowed
    """
    name = hostname(host_header)
    if not name:
        return False
    configured = {
        h.strip().lower() for h in os.environ.get("LG_THERMAL_WATCH_ALLOWED_HOSTS", "").split(",") if h.strip()
    }
    own = {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
    if name in own | configured | {h.lower() for h in extra} or name.endswith(".ts.net"):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True
