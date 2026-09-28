"""Regressions for defects found by independent review of the v2 (0.4.0) surfaces.

Each test reproduces one reported defect and asserts the corrected behaviour.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from awm import mcp_server
from awm.reconcile import LLMReconciler, ReconcileError
from awm.scope import Scope
from awm.store import MemoryStore

P = Scope("acme", "alice", "proj")


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: Clock):
    with MemoryStore(tmp_path / "mem.db") as s:
        s._clock = clock
        yield s


def _hist(store, scope, key):
    return [(h.value, h.valid_from, h.valid_to) for h in store.history(scope, key)]


# ------------------------------------------------------------ intervals
def test_purge_history_does_not_backdate_the_live_value(store, clock) -> None:
    store.remember(P, "k", "dark")
    clock.t = 200.0
    store.remember(P, "k", "light")
    assert [m.value for m in store.recall(P, as_of=150)] == ["dark"]
    store.purge_history(P, "k")
    # The purged value is gone, and "light" is NOT claimed for a time before it existed.
    assert store.recall(P, as_of=150) == []
    assert _hist(store, P, "k") == [("light", 200.0, None)]


def test_clock_is_read_inside_the_write_lock(store) -> None:
    seen = []

    def clk() -> float:
        seen.append(store._db.in_transaction)
        return 100.0

    store._clock = clk
    store.remember(P, "k", "v")
    store.remember(P, "k", "w")
    store.forget(P, "k")
    assert seen and all(seen)


def test_a_skewed_peer_clock_never_records_an_inverted_interval(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    with MemoryStore(db) as s0:
        s0._clock = lambda: 50.0
        s0.remember(P, "k", "v0")
    with MemoryStore(db) as b:
        b._clock = lambda: 101.0
        b.remember(P, "k", "B")
    with MemoryStore(db) as a:          # commits last, clock behind the peer's
        a._clock = lambda: 100.0
        a.remember(P, "k", "A")
    with MemoryStore(db) as s:
        hist = s.history(P, "k")
        assert [h.value for h in hist] == ["v0", "B", "A"]
        assert all(h.valid_to is None or h.valid_from < h.valid_to for h in hist)
        assert [m.value for m in s.recall(P, as_of=101.0)] == ["B"]


def test_kind_or_meta_change_keeps_the_old_version(store, clock) -> None:
    store.remember(P, "k", "v", kind="decision", meta={"source": "owner"})
    clock.t = 200.0
    store.remember(P, "k", "v", kind="fact", meta={"source": "guess"})
    hist = store.history(P, "k")
    assert [(h.kind, h.meta, h.valid_from, h.valid_to) for h in hist] == [
        ("decision", {"source": "owner"}, 100.0, 200.0),
        ("fact", {"source": "guess"}, 200.0, None)]
    assert [m.kind for m in store.recall(P, as_of=150, kind="decision")] == ["decision"]
    # The identical version rewritten is still no history row, and keeps its start.
    clock.t = 300.0
    store.remember(P, "k", "v", kind="fact", meta={"source": "guess"})
    assert len(store.history(P, "k")) == 2
    assert store.history(P, "k")[-1].valid_from == 200.0


def test_history_reads_one_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    r, w = MemoryStore(db), MemoryStore(db)
    try:
        w._clock = lambda: 100.0
        w.remember(P, "ui", "dark")
        w._clock = lambda: 200.0
        w.remember(P, "ui", "light")
        w._db.execute("PRAGMA busy_timeout = 50")

        class Interleave:
            """Lets a peer write land right after the reader's history SELECT."""

            def __init__(self, con):
                self.con, self.fired = con, False

            def execute(self, sql, *a):
                cur = self.con.execute(sql, *a)
                if "FROM memory_history" in sql and not self.fired:
                    rows, self.fired = cur.fetchall(), True
                    w._clock = lambda: 300.0
                    try:
                        w.remember(P, "ui", "sepia")
                    except sqlite3.OperationalError:
                        pass  # the reader's snapshot holds the peer off: consistent
                    return iter(rows)
                return cur

            def __getattr__(self, n):
                return getattr(self.con, n)

        real = r._db
        r._db = Interleave(real)
        seen = [h.value for h in r.history(P, "ui")]
        r._db = real
        # Either the peer was held off, or it landed whole: never "light" missing.
        assert seen in (["dark", "light"], ["dark", "light", "sepia"])
    finally:
        r.close()
        w.close()


_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE INDEX idx_scope ON memories(scope);
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
INSERT INTO memories(scope,key,value,kind,created,updated)
    VALUES ('acme:alice:proj','ui','light','fact',100.0,500.0);
"""


def test_a_v1_row_is_not_claimed_before_its_last_write(tmp_path: Path) -> None:
    db = tmp_path / "v1.db"
    con = sqlite3.connect(str(db))
    con.executescript(_V1)
    con.commit()
    con.close()
    with MemoryStore(db, auto_migrate=True) as s:
        assert s.recall(P, as_of=200) == []
        assert [m.value for m in s.recall(P, as_of=600)] == ["light"]
        s._clock = lambda: 600.0
        s.remember(P, "ui", "dark")
        assert _hist(s, P, "ui") == [("light", 500.0, 600.0), ("dark", 600.0, None)]


# ------------------------------------------------------------ entities
def test_entity_ids_do_not_count_other_tenants(store) -> None:
    a, b = P, Scope("globex", "bob", "p")
    gaps = set()
    for i in range(3):
        a1 = store.resolve_entity(a, f"zed{i}").entity_id
        for n in ("carol", "dave", "erin"):
            store.resolve_entity(b, f"{n}{i}")
        a2 = store.resolve_entity(a, f"yan{i}").entity_id
        gaps.add(a2 - a1 - 1)
    assert gaps != {3}
    assert all(0 < e.id < 2 ** 53 for e in store.entities(a))


@pytest.mark.parametrize("mention", ["Vansh Kumar, the designer", "Vansh Kumar (Delhi)",
                                     "Vansh A. Sharma"])
def test_a_different_vansh_is_never_confirmed_into_vansh(store, mention) -> None:
    v = store.resolve_entity(P, "vansh")
    r = store.resolve_entity(P, mention)
    assert (r.status, r.entity_id, r.possible) == ("possible", None, [v.entity_id])


def test_a_delimiter_right_after_the_head_still_confirms(store) -> None:
    v = store.resolve_entity(P, "vansh")
    assert store.resolve_entity(P, "Vansh, the designer").entity_id == v.entity_id
    assert store.resolve_entity(P, "Vansh (Delhi)").entity_id == v.entity_id


def test_qualified_mention_first_then_bare_name_is_one_entity(store) -> None:
    a = store.resolve_entity(P, "vansh from india")
    assert (a.canonical, a.status, a.created_new) == ("vansh", "confirmed", True)
    b = store.resolve_entity(P, "vansh")
    assert (b.entity_id, b.status, b.created_new) == (a.entity_id, "confirmed", False)
    c = store.resolve_entity(P, "Vansh from India")
    assert c.entity_id == a.entity_id
    assert len(store.entities(P)) == 1
    # A multi-word name with "of" is a name, not a head plus a qualifier.
    assert store.resolve_entity(P, "Bank of America").canonical == "Bank of America"


def test_a_qualified_first_mention_with_an_ambiguous_head_merges_nothing(store) -> None:
    k = store.resolve_entity(P, "Vansh Sharma")
    r = store.resolve_entity(P, "vansh from india")
    assert (r.status, r.possible) == ("possible", [k.entity_id])
    assert len(store.entities(P)) == 1


def test_me_reuses_the_owner_entity_named_first(store) -> None:
    a = store.resolve_entity(P, "Alice")
    me = store.resolve_entity(P, "me")
    assert (me.entity_id, me.created_new) == (a.entity_id, False)
    assert store.resolve_entity(P, "alice").entity_id == a.entity_id
    assert len(store.entities(P)) == 1


# ------------------------------------------------------------ reconcile
def test_slot_reconciler_only_ignores_the_subjects_own_value(store) -> None:
    store.remember(P, "user.pref.editor", "vim")
    d = store.reconcile_and_remember(P, "vim", subject="user.pref")
    assert (d.action, d.key) == ("add", "user.pref")
    assert [m.value for m in store.recall(P, query="user.pref")] == ["vim", "vim"]
    again = store.reconcile_and_remember(P, "vim", subject="user.pref")
    assert again.action == "ignore"


@pytest.mark.parametrize("key", ["user.ui_theme.", "user..ui_theme", "user.ui_theme. x"])
def test_llm_decision_with_a_malformed_key_is_refused(store, key) -> None:
    store.remember(P, "user.ui_theme", "dark")
    reply = json.dumps({"action": "add", "key": key, "value": "light", "reason": "r"})
    with pytest.raises(ReconcileError, match="segment"):
        store.reconcile_and_remember(P, "light", subject="user.ui_theme",
                                     reconciler=LLMReconciler(lambda _p: reply))
    assert [m.key for m in store.recall(P)] == ["user.ui_theme"]


def test_llm_decision_value_is_stored_without_padding(store) -> None:
    store.reconcile_and_remember(P, "dark", subject="user.ui_theme")
    reply = json.dumps({"action": "update", "key": "user.ui_theme", "value": "  light  ",
                        "reason": "r"})
    store.reconcile_and_remember(P, "light", subject="user.ui_theme",
                                 reconciler=LLMReconciler(lambda _p: reply))
    assert [m.value for m in store.recall(P)] == ["light"]


# ------------------------------------------------------------ MCP
def _call(srv, name, **args):
    resp = mcp_server.handle(srv, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
    res = resp["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


def test_mcp_recall_honours_as_of_and_settles_possible_aliases(tmp_path, monkeypatch) -> None:
    for var in ("AWM_SCOPE", "AWM_USER", "AITHER_USER"):
        monkeypatch.delenv(var, raising=False)
    srv = mcp_server.AwmMcp(tmp_path / "m.db", cwd=tmp_path, user="alice")
    sc = str(P)
    _call(srv, "awm_remember", subject="user.ui_theme", value="dark mode", scope=sc)
    _call(srv, "awm_remember", subject="user.ui_theme", value="light mode", scope=sc)
    err, out = _call(srv, "awm_recall", scope=sc, as_of=1)
    assert err is False and out["count"] == 0
    err, out = _call(srv, "awm_recall", scope=sc, as_of="1970-01-02")
    assert err is False and out["count"] == 0
    err, _ = _call(srv, "awm_recall", scope=sc, as_of="not a date")
    assert err is True

    _, v = _call(srv, "awm_resolve_entity", mention="vansh", scope=sc)
    _, k = _call(srv, "awm_resolve_entity", mention="Vikram Shah", scope=sc)
    _, vs = _call(srv, "awm_resolve_entity", mention="VS", scope=sc)
    assert vs["status"] == "possible"
    err, out = _call(srv, "awm_reject_alias", alias="VS", entity_id=k["entity_id"], scope=sc)
    assert err is False and out["rejected"] is True
    err, out = _call(srv, "awm_confirm_alias", alias="VS", entity_id=v["entity_id"], scope=sc)
    assert err is False and out["status"] == "confirmed"
    _, again = _call(srv, "awm_resolve_entity", mention="vs", scope=sc)
    assert (again["entity_id"], again["status"]) == (v["entity_id"], "confirmed")
    err, _ = _call(srv, "awm_confirm_alias", alias="VS", entity_id="1", scope=sc)
    assert err is True
