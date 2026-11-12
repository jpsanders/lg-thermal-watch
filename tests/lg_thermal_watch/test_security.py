import os
import stat
from pathlib import Path

import pytest

from lg_thermal_watch import security


def test_token_file_is_created_private_and_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(security.TOKEN_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    first = security.resolve_token()
    path = tmp_path / "lg-thermal-watch" / "token"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert len(first) >= 40
    assert security.resolve_token() == first


def test_token_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(security.TOKEN_ENV, "from-env")
    assert security.resolve_token() == "from-env"


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("localhost:8000", True),
        ("127.0.0.1:8000", True),
        ("[::1]:8000", True),
        ("100.101.102.103:8000", True),
        ("box.tail1234.ts.net", True),
        ("evil.example:8000", False),
        ("", False),
    ],
)
def test_host_allowed(header: str, allowed: bool) -> None:
    assert security.host_allowed(header) is allowed


def test_bearer_and_compare() -> None:
    assert security.bearer("Bearer abc") == "abc"
    assert security.bearer("Basic abc") is None
    assert security.same_token("abc", "abc")
    assert not security.same_token(None, "abc")
    assert not security.same_token("abd", "abc")
