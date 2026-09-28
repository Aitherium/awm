"""Round-7 review regressions (awm): read paths stamping a 0-byte file, the SessionStart
hook's silent loss on a hot journal, an ancestor purge deleting and counting transitions
it cannot read, an LLM add shadowing an ancestor's rule, a remote owner named
`platform`, and awm's state-blind GENERALIZED confidence disagreeing with adk's.

Each test failed before its fix (verified by reverting the fixed module).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from awm import MemoryStore, Scope, claude_hook, defaults, world
from awm.cli import main
from awm.reconcile import LLMReconciler, ReconcileError
from awm.scope import ScopeError

SC = Scope.parse("acme:alice:proj")
PKG = Path(__file__).resolve().parent.parent

_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
"""


@pytest.fixture(autouse=True)
def _no_auto(monkeypatch):
    monkeypatch.delenv("AWM_AUTO_MIGRATE", raising=False)
    monkeypatch.delenv("AWM_SCOPE", raising=False)


def _tables(path: Path) -> list:
    con = sqlite3.connect(str(path))
    try:
        return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()


def _clocked(path: Path) -> MemoryStore:
    st = MemoryStore(path)
    t = [1000.0]

    def clk() -> float:
        t[0] += 1.0
        return t[0]
    st._clock = clk
    return st


# ------------------------------------------------------------ G1: 0-byte file
def _quiet(argv) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return main(argv)


@pytest.mark.parametrize("argv", [
    ["recall", "--scope", str(SC)],
    ["history", "k", "--scope", str(SC)],
    ["migrate", "--dry-run"],
    ["migrate"],
])
def test_a_read_never_stamps_a_zero_byte_file(tmp_path, argv):
    db = tmp_path / "memory.db"
    db.write_bytes(b"")
    if argv[0] == "migrate":
        _quiet([argv[0], "--db", str(db), *argv[1:]])
    else:
        _quiet(["--db", str(db), *argv])
    assert db.stat().st_size == 0, "a read path initialised the file"


def test_store_create_false_on_a_zero_byte_file_is_ephemeral(tmp_path):
    db = tmp_path / "memory.db"
    db.write_bytes(b"")
    with MemoryStore(db, create=False, auto_migrate=False) as st:
        assert st.ephemeral and st.recall(SC) == []
    assert db.stat().st_size == 0 and _tables(db) == []
    with MemoryStore(db) as st:          # a WRITE path still initialises it
        st.remember(SC, "k", "v")
    assert "memories" in _tables(db)


def test_mcp_read_tool_on_a_zero_byte_file_leaves_it(tmp_path):
    from awm.mcp_server import AwmMcp
    db = tmp_path / "memory.db"
    db.write_bytes(b"")
    AwmMcp(db, cwd=tmp_path).awm_recall({"scope": str(SC), "query": "x"})
    assert db.stat().st_size == 0


# ------------------------------------------------------------ hook: hot journal
_CHILD = r'''
import os, sys
from pathlib import Path
from awm.store import MemoryStore
st = MemoryStore(Path(sys.argv[1]), auto_migrate=False)
st._db.execute("PRAGMA cache_size=1")
orig = st._migrate_statements
def boom(found):
    orig(found)
    st._db.execute("SELECT count(*) FROM memories").fetchone()
    os._exit(9)
st._migrate_statements = boom
st.migrate(backup=False)
'''


def test_hook_reads_through_a_hot_journal_left_by_a_killed_migration(tmp_path):
    db = tmp_path / "memory.db"
    con = sqlite3.connect(str(db))
    con.executescript(_V1)
    con.executemany("INSERT INTO memories(scope,key,value,created,updated) VALUES (?,?,?,?,?)",
                    [(str(SC), f"k{i:05d}", "x" * 600, 1.0, 1.0) for i in range(4000)])
    con.commit()
    con.close()
    env = dict(os.environ, PYTHONPATH=str(PKG))
    p = subprocess.run([sys.executable, "-c", _CHILD, str(db)], env=env,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 9, p.stderr[-400:]
    assert Path(str(db) + "-journal").exists(), "setup: no hot journal was left"
    out = claude_hook.build(db, tmp_path, scope=str(SC))
    assert out is not None and "k0" in out["hookSpecificOutput"]["additionalContext"]
    # SQLite's rollback restored the v1 file; nothing was stamped
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchall() == [(1,)]
    con.close()


def test_hook_says_so_on_stderr_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    db.write_bytes(b"this is not an sqlite database, not even close" * 200)
    monkeypatch.setattr(claude_hook, "_stdin_payload", lambda: {})
    err = io.StringIO()
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(err):
        rc = claude_hook.run(db, scope=str(SC))
    assert rc == 0 and out.getvalue() == ""
    assert "NOT injected" in err.getvalue()


# ------------------------------------------------------------ purge: descendant oracle
def _steps(st: MemoryStore, sc: Scope, n: int, prefix: str = "work") -> None:
    for i in range(n):
        b = st.encode_state(sc, prefix=prefix)
        st.remember(sc, f"{prefix}.n", str(i))
        st.observe_transition(sc, b, "inc", st.encode_state(sc, prefix=prefix))


@pytest.mark.parametrize("steps", [1, 5])
def test_ancestor_purge_keeps_and_does_not_count_unrelated_descendant_steps(tmp_path, steps):
    st = _clocked(tmp_path / "m.db")
    org, alice = Scope.parse("acme:*:*"), Scope.parse("acme:alice:*")
    st.remember(org, "cfg.token", "OLD")
    _steps(st, alice, steps)
    st.remember(org, "cfg.token", "NEW")
    assert st.purge_history(org, "cfg.token") == 1          # the history row only
    assert len(st.transitions(alice)) == steps              # prefix 'work' never held it
    st.close()


def test_platform_purge_leaves_other_tenants_transitions(tmp_path):
    st = _clocked(tmp_path / "m.db")
    plat, globex = Scope.parse("platform:*:*"), Scope.parse("globex:carol:secret")
    st.remember(plat, "banner", "v1")
    _steps(st, globex, 3, prefix="ops")
    st.remember(plat, "banner", "v2")
    assert st.purge_history(plat, "banner") == 1
    assert len(st.transitions(globex)) == 3
    st.close()


def test_a_descendant_step_that_digested_the_value_is_still_purged_uncounted(tmp_path):
    st = _clocked(tmp_path / "m.db")
    org, alice = Scope.parse("acme:*:*"), Scope.parse("acme:alice:*")
    st.remember(org, "cfg.pin", "1234")
    _steps(st, alice, 2, prefix="cfg")     # prefix 'cfg' digests cfg.pin=1234
    st.remember(org, "cfg.pin", "9999")
    assert st.purge_history(org, "cfg.pin") == 1           # descendant rows not counted
    assert st.transitions(alice) == []                      # ... but gone
    st.close()


# ------------------------------------------------------------ reconcile: shadowing
def _obeys(key: str, value: str):
    return lambda _prompt: json.dumps({"action": "add", "key": key, "value": value,
                                       "reason": "x"})


def test_llm_add_cannot_shadow_an_ancestor_rule(tmp_path):
    user = Scope.parse("acme:alice:*")
    with MemoryStore(tmp_path / "m.db") as st:
        st.remember(user, "deploy.target.approver", "david", kind="rule")
        rec = LLMReconciler(_obeys("deploy.target.approver", "mallory"))
        with pytest.raises(ReconcileError):
            st.reconcile_and_remember(SC, "deploy.target: staging", subject="deploy.target",
                                      reconciler=rec)
        assert st.encode_state(SC, prefix="deploy").slots["deploy.target.approver"] == "david"
        # consent is explicit
        st.reconcile_and_remember(SC, "x", subject="deploy.target", reconciler=rec,
                                  overwrite=True)
        assert st.encode_state(SC, prefix="deploy").slots["deploy.target.approver"] == "mallory"


def test_a_same_kind_reconciled_ancestor_fact_may_still_be_overridden(tmp_path):
    user = Scope.parse("acme:alice:*")
    with MemoryStore(tmp_path / "m.db") as st:
        st.reconcile_and_remember(user, "dark", subject="ui.theme")
        d = st.reconcile_and_remember(SC, "light", subject="ui.theme")
        assert d.action == "add"
        assert st.encode_state(SC, prefix="ui").slots["ui.theme"] == "light"


# ------------------------------------------------------------ platform tenant
def test_a_remote_owner_named_platform_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "widgets"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Platform/widgets.git\n',
        encoding="utf-8")
    monkeypatch.setenv("AWM_USER", "alice")
    with pytest.raises(ScopeError, match="reserved"):
        defaults.derive_scope(root)
    # the hook stays silent on an underivable scope
    assert claude_hook.build(tmp_path / "missing.db", root) is None


# ------------------------------------------------------------ state-blind confidence
def test_state_blind_generalized_is_capped_like_adk(tmp_path):
    st = _clocked(tmp_path / "m.db")
    sc = Scope.parse("acme:alice:ctr")
    for _ in range(10):
        st.remember(sc, "w.x", "0")
        b = st.encode_state(sc, prefix="w")
        st.remember(sc, "w.x", "1")
        st.observe_transition(sc, b, "inc", st.encode_state(sc, prefix="w"))
    st.remember(sc, "w.x", "5")
    p = st.predict_outcome(sc, st.encode_state(sc, prefix="w"), "inc")
    assert p.source == world.GENERALIZED and p.confidence == world.STATE_BLIND_CAP
    assert "state-blind" in p.note
    st.close()


def test_agreeing_marginal_over_two_states_is_not_capped(tmp_path):
    st = _clocked(tmp_path / "m.db")
    sc = Scope.parse("acme:alice:ctr")
    for start in ("0", "2") * 3:
        st.remember(sc, "w.x", start)
        b = st.encode_state(sc, prefix="w")
        st.remember(sc, "w.y", "done")
        st.observe_transition(sc, b, "mark", st.encode_state(sc, prefix="w"))
        st.forget(sc, "w.y")
    st.remember(sc, "w.x", "7")
    p = st.predict_outcome(sc, st.encode_state(sc, prefix="w"), "mark")
    assert p.source == world.GENERALIZED and p.confidence == pytest.approx(6 / 7)
    st.close()
