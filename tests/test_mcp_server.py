"""`awm mcp`, the derived default scope, and `awm recall --claude-hook`.

Each tool is driven through the same `handle()` a client reaches over stdio, and
one test spawns the real server process and speaks JSON-RPC to it -- a server
that constructs but cannot answer on the wire looks, to a client, exactly like a
memory with nothing in it.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from awm import claude_hook, defaults, mcp_server
from awm.scope import Scope, ScopeError
from awm.store import MemoryStore

PKG_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AWM_SCOPE", "AWM_USER", "AITHER_USER"):
        monkeypatch.delenv(var, raising=False)


def _repo(tmp_path: Path, url: str = "https://github.com/Acme/widgets.git",
          name: str = "widgets") -> Path:
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n'
        f'\tfetch = +refs/heads/*:refs/remotes/origin/*\n', encoding="utf-8")
    (root / "sub").mkdir()
    return root


# ------------------------------------------------------------ defaults
@pytest.mark.parametrize("url,owner", [
    ("https://github.com/Acme/widgets.git", "Acme"),
    ("https://github.com/Acme/widgets", "Acme"),
    ("git@github.com:Acme/widgets.git", "Acme"),
    ("ssh://git@host.example/Acme/widgets.git", "Acme"),
    ("C:/Users/me/backup.git", None),
    ("/srv/git/widgets.git", None),
])
def test_owner_from_url(url, owner):
    assert defaults.owner_from_url(url) == owner


def test_derive_scope_from_remote_user_and_dir(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("AWM_USER", "alice")
    sc, how = defaults.derive_scope(root / "sub")
    assert str(sc) == "acme:alice:widgets"
    assert how["user"] == "AWM_USER"


def test_derive_scope_follows_a_worktree_to_its_common_dir(tmp_path):
    main = _repo(tmp_path)
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt-checkout"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert str(defaults.derive_scope(wt, user="bob")[0]) == "acme:bob:wt-checkout"


def test_awm_scope_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_SCOPE", "globex:carol:*")
    assert str(defaults.derive_scope(tmp_path)[0]) == "globex:carol:*"


def test_wildcard_user_with_named_project_is_still_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("AWM_SCOPE", "acme:*:widgets")
    with pytest.raises(ScopeError):
        defaults.derive_scope(root)
    monkeypatch.delenv("AWM_SCOPE")
    # A '*' login cannot smuggle the wildcard in through the derived path either.
    with pytest.raises(ScopeError):
        defaults.derive_scope(root, user="*")


def test_no_repo_or_no_remote_refuses(tmp_path):
    with pytest.raises(ScopeError):
        defaults.derive_scope(tmp_path, user="alice")
    bare = tmp_path / "noremote"
    (bare / ".git").mkdir(parents=True)
    (bare / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    with pytest.raises(ScopeError):
        defaults.derive_scope(bare, user="alice")


# ------------------------------------------------------------ MCP tools
@pytest.fixture()
def srv(tmp_path):
    root = _repo(tmp_path)
    return mcp_server.AwmMcp(tmp_path / "m.db", cwd=root, user="alice")


def _call(srv, name, **args):
    resp = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
    res = resp["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


def test_initialize_and_tools_list(srv):
    init = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"]["tools"] is not None
    assert mcp_server.handle(srv, {"jsonrpc": "2.0", "method": "notifications/initialized"}) \
        is None
    tools = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert names == {"awm_scope", "awm_recall", "awm_list", "awm_remember", "awm_forget",
                     "awm_history", "awm_resolve_entity", "awm_confirm_alias",
                     "awm_reject_alias", "awm_world_state", "awm_predict",
                     "awm_observe", "awm_merge_entities", "awm_split_entity",
                     "awm_surprise_stats"}
    unknown = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert unknown["error"]["code"] == -32601


def test_awm_scope_tool(srv):
    err, out = _call(srv, "awm_scope")
    assert not err and out["scope"] == "acme:alice:widgets"


def test_remember_then_recall_and_list_at_the_derived_scope(srv):
    err, out = _call(srv, "awm_remember", key="db", value="postgres is dual-mode")
    assert not err and out["scope"] == "acme:alice:widgets"
    err, out = _call(srv, "awm_recall", query="POSTGRES")
    assert not err and [m["key"] for m in out["memories"]] == ["db"]
    err, out = _call(srv, "awm_list")
    assert not err and out["keys"][0]["key"] == "db" and "value" not in out["keys"][0]


def test_recall_never_sees_a_sibling_user(srv):
    with MemoryStore(srv.db) as st:
        st.remember(Scope("acme", "bob", "widgets"), "secret", "bob only")
    err, out = _call(srv, "awm_recall")
    assert not err and out["count"] == 0


def test_remember_refuses_a_silent_overwrite(srv):
    _call(srv, "awm_remember", key="k", value="first")
    err, text = _call(srv, "awm_remember", key="k", value="second")
    assert err and "overwrite=true" in text
    # the same value again is idempotent, not a refusal
    assert _call(srv, "awm_remember", key="k", value="first")[0] is False
    err, out = _call(srv, "awm_remember", key="k", value="second", overwrite=True)
    assert not err and out["replaced"] is True


def test_explicit_wildcard_scope_is_refused_as_a_tool_error(srv):
    err, text = _call(srv, "awm_recall", scope="acme:*:widgets")
    assert err and "REFUSED" in text


def test_forget_is_gated_by_the_server_flag(srv, tmp_path):
    _call(srv, "awm_remember", key="k", value="v")
    err, text = _call(srv, "awm_forget", key="k")
    assert err and "--allow-forget" in text
    open_srv = mcp_server.AwmMcp(srv.db, cwd=srv.cwd, user="alice", allow_forget=True)
    err, out = _call(open_srv, "awm_forget", key="k")
    assert not err and out["forgotten"] == "k"
    err, text = _call(open_srv, "awm_forget", key="k")
    assert err and "no memory" in text


def test_serve_over_a_byte_stream(srv):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    inp = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in lines) + b"not json\n")
    out = io.BytesIO()
    assert mcp_server.serve(srv, inp, out) == 0
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[-1]["error"]["code"] == -32700


def test_real_process_answers_on_stdio(tmp_path):
    root = _repo(tmp_path)
    db = tmp_path / "m.db"
    with MemoryStore(db) as st:
        st.remember(Scope("acme", "alice", "widgets"), "trap", "stdio works")
    env_py = [sys.executable, "-c",
              "import sys; sys.path.insert(0, sys.argv[1]); from awm.cli import main; "
              "sys.exit(main(sys.argv[2:]))", str(PKG_ROOT), "mcp", "--db", str(db),
              "--user", "alice"]
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "awm_recall", "arguments": {"query": "stdio"}}},
    ]
    proc = subprocess.run(env_py, input=b"".join(json.dumps(m).encode() + b"\n" for m in msgs),
                          capture_output=True, cwd=str(root), timeout=60)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(x) for x in proc.stdout.splitlines()]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    body = json.loads(replies[1]["result"]["content"][0]["text"])
    assert [m["key"] for m in body["memories"]] == ["trap"]


# ------------------------------------------------------------ SessionStart hook
def test_hook_emits_capped_session_start_json(tmp_path):
    root = _repo(tmp_path)
    db = tmp_path / "m.db"
    with MemoryStore(db) as st:
        for i in range(30):
            st.remember(Scope("acme", "alice", "widgets"), f"k{i:02d}", "x" * 400)
    out = claude_hook.build(db, root, user="alice")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "acme:alice:widgets" in ctx and "of 30" in ctx
    assert len(ctx) <= claude_hook.MAX_CHARS
    assert ctx.count("\n- ") <= claude_hook.MAX_FACTS


def test_hook_is_silent_and_never_creates_a_db(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path)
    missing = tmp_path / "nope" / "m.db"
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert claude_hook.run(missing, user="alice") == 0
    assert capsys.readouterr().out == ""
    assert not missing.exists()
    # outside any repo: the scope cannot be derived -> still silent, still 0
    db = tmp_path / "m.db"
    MemoryStore(db).close()
    monkeypatch.chdir(tmp_path)
    assert claude_hook.run(db, user="alice") == 0
    assert capsys.readouterr().out == ""


def test_hook_reads_cwd_from_the_hook_payload(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path)
    db = tmp_path / "m.db"
    with MemoryStore(db) as st:
        st.remember(Scope("acme", "alice", "widgets"), "trap", "payload cwd wins")
    monkeypatch.chdir(tmp_path)  # NOT the repo; only the payload says where
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(root)})))
    assert claude_hook.run(db, user="alice") == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "payload cwd wins" in ctx
