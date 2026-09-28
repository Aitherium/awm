"""The v2 surfaces: MCP awm_history / awm_resolve_entity / awm_remember(subject), and the CLI."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from awm import mcp_server
from awm.cli import main, parse_as_of
from awm.scope import Scope, ScopeError
from awm.store import MemoryStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AWM_SCOPE", "AWM_USER", "AITHER_USER"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def srv(tmp_path):
    return mcp_server.AwmMcp(tmp_path / "m.db", cwd=tmp_path, user="alice")


def _call(srv, name, **args):
    resp = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
    res = resp["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


SC = "acme:alice:widgets"


# ------------------------------------------------------------ MCP
def test_remember_with_subject_updates_and_history_shows_both(srv) -> None:
    err, out = _call(srv, "awm_remember", subject="user.ui_theme", value="dark", scope=SC)
    assert not err and (out["action"], out["key"]) == ("add", "user.ui_theme")
    err, out = _call(srv, "awm_remember", subject="user.ui_theme", value="light", scope=SC)
    assert not err and out["action"] == "update"
    err, out = _call(srv, "awm_remember", subject="user.ui_theme", value="light", scope=SC)
    assert not err and out["action"] == "ignore"
    err, out = _call(srv, "awm_history", key="user.ui_theme", scope=SC)
    assert not err and [h["value"] for h in out["history"]] == ["dark", "light"]
    assert out["history"][0]["reason"] == "superseded"
    err, out = _call(srv, "awm_recall", scope=SC)
    assert [m["value"] for m in out["memories"]] == ["light"]


def test_remember_without_subject_still_refuses_a_silent_overwrite(srv) -> None:
    assert not _call(srv, "awm_remember", key="k", value="v1", scope=SC)[0]
    err, text = _call(srv, "awm_remember", key="k", value="v2", scope=SC)
    assert err and "REFUSED" in text
    err, out = _call(srv, "awm_remember", key="k", value="v2", overwrite=True, scope=SC)
    assert not err and out["replaced"] is True
    err, out = _call(srv, "awm_history", key="k", scope=SC)
    assert [h["value"] for h in out["history"]] == ["v1", "v2"]


def test_remember_needs_key_or_subject_not_both(srv) -> None:
    assert _call(srv, "awm_remember", value="v", scope=SC)[0]
    err, text = _call(srv, "awm_remember", key="k", subject="s", value="v", scope=SC)
    assert err and "not both" in text
    assert _call(srv, "awm_remember", subject=" ", value="v", scope=SC)[0]


def test_resolve_entity_tool(srv) -> None:
    err, v = _call(srv, "awm_resolve_entity", mention="vansh", scope=SC)
    assert not err and v["created_new"] and v["status"] == "confirmed"
    err, r = _call(srv, "awm_resolve_entity", mention="vansh from india", scope=SC)
    assert r["entity_id"] == v["entity_id"]
    err, vs = _call(srv, "awm_resolve_entity", mention="VS", scope=SC)
    assert vs["status"] == "possible" and vs["possible"] == [v["entity_id"]]
    err, text = _call(srv, "awm_resolve_entity", mention=" ", scope=SC)
    assert err
    err, other = _call(srv, "awm_resolve_entity", mention="vansh", scope="acme:bob:widgets")
    assert other["created_new"] and other["entity_id"] != v["entity_id"]


def test_history_tool_never_reads_a_sibling(srv) -> None:
    _call(srv, "awm_remember", subject="s", value="v1", scope="acme:bob:widgets")
    _call(srv, "awm_remember", subject="s", value="v2", scope="acme:bob:widgets")
    err, out = _call(srv, "awm_history", key="s", scope=SC)
    assert not err and out["history"] == []
    assert _call(srv, "awm_history", scope=SC)[0]


# ------------------------------------------------------------ CLI
def test_parse_as_of() -> None:
    assert parse_as_of("1700000000") == 1_700_000_000.0
    assert parse_as_of("2026-09-01T00:00:00Z") == 1_788_220_800.0
    assert parse_as_of("2026-09-01") == datetime(2026, 9, 1).timestamp()
    with pytest.raises(ScopeError):
        parse_as_of("last tuesday")


def test_cli_history_and_recall_as_of(tmp_path: Path, capsys) -> None:
    db = tmp_path / "m.db"
    sc = Scope.parse(SC)
    with MemoryStore(db) as st:
        st._clock = lambda: 1000.0
        st.remember(sc, "theme", "dark")
        st._clock = lambda: 2000.0
        st.remember(sc, "theme", "light")
    assert main(["--db", str(db), "history", "theme", "--scope", SC, "--json"]) == 0
    hist = json.loads(capsys.readouterr().out)
    assert [(h["value"], h["valid_to"]) for h in hist] == [("dark", 2000.0), ("light", None)]
    assert main(["--db", str(db), "history", "theme", "--scope", SC]) == 0
    assert "superseded" in capsys.readouterr().out
    assert main(["--db", str(db), "recall", "--scope", SC, "--as-of", "1500", "--json"]) == 0
    assert [m["value"] for m in json.loads(capsys.readouterr().out)] == ["dark"]
    assert main(["--db", str(db), "recall", "--scope", SC, "--as-of", "nope"]) == 2
    assert main(["--db", str(db), "history", "missing", "--scope", SC]) == 1


def test_cli_entity_round_trip(tmp_path: Path, capsys) -> None:
    db = ["--db", str(tmp_path / "m.db")]
    assert main([*db, "entity", "resolve", "vansh", "--scope", SC]) == 0
    vid = json.loads(capsys.readouterr().out)["entity_id"]
    assert main([*db, "entity", "resolve", "VS", "--scope", SC]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "possible"
    assert main([*db, "entity", "confirm", "VS", str(vid), "--scope", SC]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "confirmed"
    assert main([*db, "entity", "list", "--scope", SC, "--json"]) == 0
    [ent] = json.loads(capsys.readouterr().out)
    assert {a["alias"] for a in ent["aliases"]} == {"vansh", "vs"}
    assert main([*db, "entity", "reject", "VS", str(vid), "--scope", SC]) == 0
    assert json.loads(capsys.readouterr().out) == {"rejected": True}
    # Writes name their scope; a missing id is refused, not guessed.
    assert main([*db, "entity", "resolve", "x"]) == 2
    assert main([*db, "entity", "confirm", "VS", "--scope", SC]) == 2
    with pytest.raises(SystemExit):
        main([*db, "entity", "resolve", "--scope", SC])


def test_cli_version_matches_pyproject() -> None:
    import awm
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
    assert f'version = "{awm.__version__}"' in text
    assert awm.__version__ == "0.6.0"
