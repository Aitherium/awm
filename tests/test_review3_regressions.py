"""Regressions for the third independent review (each failed before its fix)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from awm import MemoryStore, Scope
from awm import mcp_server
from awm.reconcile import ReconcileError
from awm.store import MigrationError

P = Scope.parse("acme:alice:proj")

_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE INDEX idx_scope ON memories(scope);
CREATE TABLE schema_meta (version INTEGER NOT NULL);
"""


def _v1_file(path: Path, *, stamp: bool) -> Path:
    c = sqlite3.connect(str(path))
    c.executescript(_V1)
    c.execute("INSERT INTO memories(scope,key,value,created,updated) "
              "VALUES ('acme:alice:proj','k','v',1,1)")
    if stamp:
        c.execute("INSERT INTO schema_meta(version) VALUES (1)")
    c.commit()
    c.close()
    return path


def _tables(path: Path) -> list:
    c = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        c.close()


def _versions(path: Path) -> list:
    c = sqlite3.connect(str(path))
    try:
        return [r[0] for r in c.execute("SELECT version FROM schema_meta")]
    finally:
        c.close()


# ------------------------------------------------------------ migration safety
def test_v1_file_with_an_empty_schema_meta_is_not_stamped_v3_on_open(tmp_path) -> None:
    db = _v1_file(tmp_path / "m.db", stamp=False)
    with MemoryStore(db, auto_migrate=False) as st:
        assert st.compat and st.file_version == 1
        assert [m.value for m in st.recall(P)] == ["v"]
    assert _versions(db) == []  # nothing written on open
    assert "transitions" not in _tables(db)
    with MemoryStore(db, auto_migrate=False) as st:
        rep = st.migrate(backup=False)
    assert rep["migrated"] and _versions(db) == [3]


def test_a_commit_refused_by_a_reader_rolls_back_and_leaves_the_store_usable(tmp_path) -> None:
    db = _v1_file(tmp_path / "m.db", stamp=True)
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT 1 FROM memories").fetchone()
    st = MemoryStore(db, auto_migrate=False)
    st._db.execute("PRAGMA busy_timeout = 0")
    try:
        with pytest.raises(MigrationError, match="could not commit"):
            st.migrate(backup=True)
        assert not st._db.in_transaction
        assert st.file_version == 1
        assert [r[0] for r in st._db.execute("SELECT version FROM schema_meta")] == [1]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["m.db"]  # no stray backup
        reader.execute("COMMIT")
        st.remember(P, "after", "ok")
        assert st.forget(P, "after")
    finally:
        reader.close()
        st.close()
    assert _versions(db) == [1]


# ------------------------------------------------------------ purge cascades
def test_purge_history_removes_the_value_from_transitions(tmp_path) -> None:
    st = MemoryStore(tmp_path / "m.db")
    st.remember(P, "acct.pin", "0000")
    before = st.encode_state(P, prefix="acct")
    st.remember(P, "acct.pin", "SECRET-4242")
    st.observe_transition(P, before, "set_pin", st.encode_state(P, prefix="acct"))
    # The live value is kept, but this transition's BEFORE digest commits to the
    # purged "0000" (review 6: a digest is brute-forceable), so it goes too. The
    # keep-what-is-current case is test_review6's live-only transition.
    st.purge_history(P, "acct.pin")
    assert st.transitions(P) == []
    st.observe_transition(P, st.encode_state(P, prefix="acct"), "noop",
                          st.encode_state(P, prefix="acct"))
    assert st.forget(P, "acct.pin")
    st.purge_history(P, "acct.pin")
    assert st.transitions(P) == []
    pred = st.predict_outcome(P, st.encode_state(P, prefix="acct"), "set_pin")
    assert pred.source == "NONE"
    raw = st._db.execute("SELECT COUNT(*) FROM transitions WHERE outcome_json LIKE "
                         "'%SECRET-4242%' OR predicted_json LIKE '%SECRET-4242%'").fetchone()[0]
    assert raw == 0
    st.close()


# ------------------------------------------------------------ reconcile consent
def test_reconcile_does_not_supersede_a_key_written_rule(tmp_path) -> None:
    st = MemoryStore(tmp_path / "m.db")
    st.remember(P, "deploy.host", "prod.internal", kind="rule")
    with pytest.raises(ReconcileError, match="overwrite"):
        st.reconcile_and_remember(P, "attacker.example.com", subject="deploy.host")
    [m] = st.recall(P)
    assert (m.value, m.kind) == ("prod.internal", "rule")
    d = st.reconcile_and_remember(P, "new.internal", subject="deploy.host", kind="rule",
                                  overwrite=True)
    assert d.action == "update"
    [m] = st.recall(P)
    assert (m.value, m.kind) == ("new.internal", "rule")
    st.close()


def test_reconcile_still_supersedes_its_own_slot_and_keeps_meta(tmp_path) -> None:
    st = MemoryStore(tmp_path / "m.db")
    st.reconcile_and_remember(P, "dark", subject="ui.theme", meta={"ts": 5.0})
    d = st.reconcile_and_remember(P, "light", subject="ui.theme", meta={"ts": 6.0})
    assert d.action == "update"
    [m] = st.recall(P)
    assert m.value == "light" and m.meta["ts"] == 6.0 and m.meta["subject"] == "ui.theme"
    st.close()


def test_mcp_subject_cannot_bypass_the_overwrite_guard(tmp_path) -> None:
    db = tmp_path / "m.db"
    MemoryStore(db).close()
    srv = mcp_server.AwmMcp(db)
    sc = "acme:alice:proj"

    def call(**args):
        res = srv.call("awm_remember", dict(args, scope=sc))
        return res["isError"], res["content"][0]["text"]

    assert call(key="deploy.host", value="prod.internal", kind="rule")[0] is False
    err, text = call(subject="deploy.host", value="evil.example")
    assert err is True and "REFUSED" in text
    got = json.loads(srv.call("awm_recall", {"scope": sc})["content"][0]["text"])
    assert [(m["value"], m["kind"]) for m in got["memories"]] == [("prod.internal", "rule")]
    err, _ = call(subject="deploy.host", value="new.internal", kind="rule", overwrite=True)
    assert err is False


# ------------------------------------------------------------ predict semantics
def _grid(st: MemoryStore, x: int, y: int):
    st.remember(P, "g.x", str(x))
    st.remember(P, "g.y", str(y))
    return st.encode_state(P, prefix="g")


def test_recalled_is_the_majority_and_generalized_is_never_certain(tmp_path) -> None:
    st = MemoryStore(tmp_path / "m.db")
    for _ in range(5):
        s0 = _grid(st, 0, 0)
        st.observe_transition(P, s0, "up", _grid(st, 0, 1))
    s0 = _grid(st, 0, 0)
    st.observe_transition(P, s0, "up", _grid(st, 3, 3))  # one anomaly
    s0 = _grid(st, 0, 0)
    p = st.predict_outcome(P, s0, "up")
    assert p.source == "RECALLED" and p.delta == {"g.y": "1"}
    assert p.confidence == pytest.approx(5 / 6)
    t = st.observe_transition(P, s0, "up", _grid(st, 0, 1))
    assert t.surprise == 0.0

    st2 = MemoryStore(tmp_path / "g.db")
    a = _grid(st2, 0, 0)
    st2.observe_transition(P, a, "up", _grid(st2, 0, 1))
    g = st2.predict_outcome(P, _grid(st2, 0, 3), "up")
    assert g.source == "GENERALIZED" and g.support == 1
    assert g.confidence == pytest.approx(0.5)
    st.close()
    st2.close()


# ------------------------------------------------------------ adk wm support
def test_clear_transitions_is_exactly_one_scope(tmp_path) -> None:
    st = MemoryStore(tmp_path / "m.db")
    child = Scope.parse("acme:alice:other")
    for sc in (P, child):
        a = st.encode_state(sc, prefix="z")
        st.remember(sc, "z.v", "1")
        st.observe_transition(sc, a, "set", st.encode_state(sc, prefix="z"))
    assert st.clear_transitions(P) == 1
    assert st.transitions(P) == [] and len(st.transitions(child)) == 1
    st.close()


def test_check_same_thread_false_allows_a_serialised_second_thread(tmp_path) -> None:
    import threading
    st = MemoryStore(tmp_path / "m.db", check_same_thread=False)
    out = []
    t = threading.Thread(target=lambda: out.append(st.remember(P, "k", "v").value))
    t.start()
    t.join()
    assert out == ["v"]
    st.close()


def test_confirm_alias_leaves_a_pending_fact_pending_when_the_slot_is_key_written(
        tmp_path) -> None:
    # Confirming WHO "VS" is must not become consent to replace an owner's key-written
    # value; the fact stays pending and the refusal is reported.
    st = MemoryStore(tmp_path / "m.db")
    vansh = st.remember_about(P, "Vansh", "Vansh lives in Delhi")
    st.remember(P, "people.vansh", "owner says: Delhi", kind="rule")
    st.remember_about(P, "VS", "VS just told me he relocated to Pune")
    res = st.confirm_alias(P, "VS", vansh.entity_id)
    assert [a.get("refused") is not None for a in res.applied] == [True]
    assert [m.value for m in st.recall(P) if m.key == "people.vansh"] == ["owner says: Delhi"]
    assert [p.status for p in st.pending_updates(P, status=None)] == ["pending"]
    st.close()
