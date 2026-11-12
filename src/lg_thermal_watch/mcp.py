"""
A Model Context Protocol server for AI agents, on stdio, with no dependencies beyond the standard library.

It talks to the running service's HTTP API (``LG_THERMAL_WATCH_URL``, by default ``http://127.0.0.1:<port>``) with
the same token the dashboard uses, so the service stays the one owner of the LG session and the unit. Reading tools
are marked read only; the ones that switch the unit or change the night watch are marked destructive, so the
agent's harness asks the person first.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

from lg_thermal_watch import __version__
from lg_thermal_watch.security import resolve_token

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "lg-thermal-watch runs an LG heat pump's night watch and logs the room temperature. Use status for the room "
    "temperature, the unit's state and what the night watch is doing; temperatures for history; runtime for on and "
    "off sessions and the energy estimate. heat, off, night_watch_set, night_watch_start and night_watch_stop act on "
    "a real heat pump in someone's home: only call them when the person asked for that change, and say what you "
    "will do first."
)

# name: (method, path, description, input properties, read only)
TOOLS: dict[str, tuple[str, str, str, dict[str, Any], bool]] = {
    "status": (
        "GET",
        "/api/overview",
        "Room temperature, the unit's state, the night watch and its settings.",
        {},
        True,
    ),
    "temperatures": (
        "GET",
        "/api/temps",
        "Logged room temperatures with min, max and mean over a window.",
        {"timeline": {"enum": ["24h", "7d", "30d"], "description": "the window, default 24h"}},
        True,
    ),
    "runtime": (
        "GET",
        "/api/runtime",
        "Recent on and off sessions and the estimated energy use per month.",
        {"months": {"type": "integer", "minimum": 1, "maximum": 24}},
        True,
    ),
    "heat": (
        "POST",
        "/api/device/heat",
        "Heat now: power the unit on if needed and set HEAT, the target and fan LOW. Acts on a real heat pump.",
        {
            "target_c": {"type": "number", "minimum": 16, "maximum": 30, "description": "target temperature, C"},
            "fan_low": {"type": "boolean", "description": "fan speed LOW, default true"},
        },
        False,
    ),
    "off": ("POST", "/api/device/off", "Turn the unit off. Acts on a real heat pump.", {}, False),
    "night_watch_set": (
        "POST",
        "/api/nightwatch/config",
        "Replace the night watch settings (thresholds, window, timezone, enabled). Read status first and send every "
        "field you want to keep.",
        {
            "enabled": {"type": "boolean"},
            "low_threshold_c": {"type": "number", "minimum": 5, "maximum": 29},
            "high_target_c": {"type": "number", "minimum": 16, "maximum": 30},
            "fan_low": {"type": "boolean"},
            "night_start_hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "night_end_hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "timezone": {"type": "string", "description": "IANA zone, such as Europe/Lisbon"},
        },
        False,
    ),
    "night_watch_start": ("POST", "/api/nightwatch/start", "Enable and start the night watch.", {}, False),
    "night_watch_stop": (
        "POST",
        "/api/nightwatch/stop",
        "Disable and stop the night watch; turns the unit off if the watch was heating.",
        {},
        False,
    ),
}


def base_url() -> str:
    """
    Return the running service's address.

    :returns: ``LG_THERMAL_WATCH_URL``, or the local address on ``LG_THERMAL_WATCH_PORT``
    """
    port = os.environ.get("LG_THERMAL_WATCH_PORT", "8000")
    return os.environ.get("LG_THERMAL_WATCH_URL", f"http://127.0.0.1:{port}").rstrip("/")


def tools() -> list[dict[str, Any]]:
    """
    Describe the tools for ``tools/list``.

    :returns: MCP tool definitions
    """
    return [
        {
            "name": name,
            "description": about,
            "inputSchema": {"type": "object", "properties": props, "additionalProperties": False},
            "annotations": {
                "readOnlyHint": readonly,
                "destructiveHint": not readonly,
                "idempotentHint": readonly,
                "openWorldHint": not readonly,
            },
        }
        for name, (_, _, about, props, readonly) in TOOLS.items()
    ]


def call_api(method: str, path: str, arguments: dict[str, Any]) -> Any:
    """
    Call the running service.

    :param method: GET or POST
    :param path: The API path
    :param arguments: Query parameters for GET, the JSON body for POST
    :returns: The decoded JSON answer
    :raises RuntimeError: When the service is down or answers with an error
    """
    url = base_url() + path
    data = None
    if method == "GET" and arguments:
        url += "?" + urllib.parse.urlencode(arguments)
    elif method == "POST":
        data = json.dumps(arguments).encode()
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 local service, http only
    request.add_header("Authorization", f"Bearer {resolve_token()}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310: the URL is the local service
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"the service answered {error.code}: {error.read().decode(errors='replace')[:300]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"the service is not reachable at {base_url()}: {error.reason}") from error


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    Answer one JSON-RPC message.

    :param message: The request or notification
    :returns: The response, or None for a notification
    """
    mid = message.get("id")
    if mid is None:
        return None
    method = str(message.get("method", ""))
    params = message.get("params") or {}
    result: dict[str, Any]
    if method == "initialize":
        asked = str(params.get("protocolVersion", ""))
        result = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "lg-thermal-watch", "title": "LG Thermal Watch", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools()}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    elif method == "tools/call":
        result = _call(str(params.get("name", "")), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"Error: unknown tool {name!r}"}], "isError": True}
    method, path, _, props, _ = TOOLS[name]
    extra = set(arguments) - set(props)
    if extra:
        return {"content": [{"type": "text", "text": f"Error: unknown argument {sorted(extra)}"}], "isError": True}
    try:
        data = call_api(method, path, arguments)
    except RuntimeError as error:
        return {"content": [{"type": "text", "text": f"Error: {error}"}], "isError": True}
    out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "isError": False}
    if isinstance(data, dict):
        out["structuredContent"] = data
    return out


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """
    Serve MCP over stdio until the input closes.

    :param stdin: Input stream, standard input by default
    :param stdout: Output stream, standard output by default
    """
    reader, writer = stdin or sys.stdin, stdout or sys.stdout
    for line in reader:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            replies: list[dict[str, Any]] = [
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            ]
        else:
            batch = message if isinstance(message, list) else [message]
            replies = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        for reply in replies:
            writer.write(json.dumps(reply) + "\n")
        writer.flush()
