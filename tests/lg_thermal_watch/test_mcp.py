import io
import json
from typing import Any

import pytest

from lg_thermal_watch import mcp


def ask(*messages: dict[str, Any]) -> list[dict[str, Any]]:
    out = io.StringIO()
    mcp.serve_stdio(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_tools_are_marked() -> None:
    (listed,) = ask({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    hints = {t["name"]: t["annotations"] for t in listed["result"]["tools"]}
    assert hints["status"]["readOnlyHint"] is True
    for name in ("heat", "off", "night_watch_set", "night_watch_start", "night_watch_stop"):
        assert hints[name]["destructiveHint"] is True


def test_call_goes_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake(method: str, path: str, arguments: dict[str, Any]) -> Any:
        calls.append((method, path, arguments))
        return {"ok": True, "target_c": 21}

    monkeypatch.setattr(mcp, "call_api", fake)
    (reply,) = ask(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "heat", "arguments": {"target_c": 21}}}
    )
    assert calls == [("POST", "/api/device/heat", {"target_c": 21})]
    assert reply["result"]["structuredContent"] == {"ok": True, "target_c": 21}


def test_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(method: str, path: str, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("the service is not reachable")

    monkeypatch.setattr(mcp, "call_api", down)
    (reply,) = ask({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status"}})
    assert reply["result"]["isError"] is True
    (bad,) = ask(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {"x": 1}}}
    )
    assert bad["result"]["isError"] is True
    (init,) = ask({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert init["result"]["serverInfo"]["name"] == "lg-thermal-watch"
