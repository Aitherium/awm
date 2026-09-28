"""Round-5 review regressions: restore safety, read paths that wrote, id side channels,
merge/split disclosure and tombstones, the predict --json exit code."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from awm import MemoryStore, Scope
from awm import integrations
from awm.cli import main

SC = Scope.parse("acme:alice:proj")

_V1_TABLES = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE INDEX IF NOT EXISTS idx_scope ON memories(scope);
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
"""


def _v1(path: Path) -> Path:
    c = sqlite3.connect(str(path))
    c.executescript(_V1_TABLES)
    c.execute("INSERT INTO memories(scope,key,value,created,updated) VALUES (?,?,?,1,1)",
              (str(SC), "k", "v"))
    c.execute("INSERT INTO schema_meta(version) VALUES (1)")
    c.commit()
    c.close()
    return path


def _version(path: Path) -> list:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT version FROM schema_meta").fetchall()
    finally:
        c.close()


def _digest(path: Path):
    c = sqlite3.connect(str(path))
    try:
        return (c.execute("PRAGMA integrity_check").fetchone()[0],
                c.execute("SELECT id, key, value FROM memories ORDER BY id").fetchall())
    finally:
        c.close()


def _has_key(path: Path, key: str) -> bool:
    c = sqlite3.connect(str(path))
    try:
        return bool(c.execute("SELECT count(*) FROM memories WHERE key = ?", (key,)).fetchone()[0])
    finally:
        c.close()


@pytest.fixture
def fake_recover(monkeypatch, tmp_path):
    """A stand-in for awrecover: `restore` copies the file `snap` into the out dir."""
    snap = tmp_path / "snap.db"

    def restore(store, label, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snap, Path(out) / "memory.db")

    monkeypatch.setattr(integrations, "_awrecover", lambda: SimpleNamespace(restore=restore))
    return snap


# ------------------------------------------------------------ restore + hot journal
def test_restore_is_not_rolled_back_by_a_crashed_writers_hot_journal(tmp_path, fake_recover):
    db = tmp_path / "memory.db"
    c = sqlite3.connect(str(db))
    c.executescript(_V1_TABLES)
    c.executemany("INSERT INTO memories(scope,key,value,created,updated) VALUES (?,?,?,1,1)",
                  [(str(SC), f"a{i}", "a" * 400) for i in range(1500)])
    c.commit()
    c.close()
    shutil.copyfile(db, fake_recover)
    want = _digest(fake_recover)
    c = sqlite3.connect(str(db))
    c.executemany("INSERT INTO memories(scope,key,value,created,updated) VALUES (?,?,?,1,1)",
                  [(str(SC), f"b{i}", "b" * 400) for i in range(1500)])
    c.commit()
    c.close()
    before_crash = _digest(db)
    child = ("import sqlite3,os,sys;c=sqlite3.connect(sys.argv[1]);c.execute('pragma cache_size=10');"
             "c.execute('begin');c.execute(\"update memories set value=value||'CRASH'\");os._exit(9)")
    subprocess.run([sys.executable, "-c", child, str(db)], check=False)
    assert (tmp_path / "memory.db-journal").stat().st_size > 0  # the hot journal exists

    out = integrations.restore_db(tmp_path / "snaps", "A", db)
    assert _digest(db) == want  # checked FIRST: opening rolls a stale journal back
    assert not (tmp_path / "memory.db-journal").exists()
    # The kept copy is the committed pre-crash state, not a torn file.
    assert _digest(Path(out["previous"])) == before_crash


def test_a_second_restore_never_overwrites_the_first_kept_copy(tmp_path, fake_recover):
    db = tmp_path / "memory.db"
    with MemoryStore(db) as st:
        st.remember(SC, "k", "snap")
    shutil.copyfile(db, fake_recover)
    with MemoryStore(db) as st:
        st.remember(SC, "ONLY_IN_B", "precious")
    first = integrations.restore_db(tmp_path / "snaps", "A", db)["previous"]
    second = integrations.restore_db(tmp_path / "snaps", "A", db)["previous"]
    assert first != second
    kept = list(tmp_path.glob("memory.db.before-restore-*"))
    assert len(kept) == 2
    held = [k for k in kept if _has_key(k, "ONLY_IN_B")]
    assert [str(k) for k in held] == [first]


# ------------------------------------------------------------ read commands never migrate
@pytest.mark.parametrize("argv", [["entity", "list"], ["world", "state"], ["world", "stats"],
                                  ["world", "surprises"], ["world", "predict", "x"]])
def test_read_commands_never_migrate_under_auto_migrate(tmp_path, monkeypatch, argv):
    db = _v1(tmp_path / "memory.db")
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    rc = main(["--db", str(db), *argv, "--scope", str(SC)])
    assert rc == 2  # NEEDS MIGRATION, said out loud
    assert _version(db) == [(1,)]
    assert not list(tmp_path.glob("memory.db.v1-backup-*"))


def test_a_read_of_a_missing_db_creates_no_file(tmp_path, capsys):
    db = tmp_path / "missing" / "memory.db"
    assert main(["--db", str(db), "recall", "--scope", str(SC), "--query", "x"]) == 0
    assert "no memories" in capsys.readouterr().out
    assert not db.exists() and not db.parent.exists()
    with MemoryStore(db, create=False) as st:
        assert st.ephemeral and st.recall(SC) == []
    assert not db.exists()


def test_mcp_read_tool_on_a_missing_db_creates_no_file(tmp_path):
    from awm.mcp_server import AwmMcp
    db = tmp_path / "nope" / "memory.db"
    srv = AwmMcp(db, cwd=tmp_path)
    srv.awm_recall({"scope": str(SC), "query": "x"})
    assert not db.exists()


# ------------------------------------------------------------ predict --json exit code
def test_world_predict_json_exits_1_when_nothing_was_recorded(tmp_path, capsys):
    db = tmp_path / "m.db"
    MemoryStore(db).close()
    assert main(["--db", str(db), "world", "predict", "x", "--scope", str(SC), "--json"]) == 1
    assert '"NONE"' in capsys.readouterr().out


# ------------------------------------------------------------ ids are not a counter
def test_public_ids_do_not_count_other_tenants_rows(tmp_path):
    acme, globex = Scope.parse("acme:alice:*"), Scope.parse("globex:bob:*")
    with MemoryStore(tmp_path / "m.db") as st:
        def step(sc, i):
            before = st.encode_state(sc, prefix="w")
            st.remember(sc, "w.x", str(i))
            return st.observe_transition(sc, before, "inc", st.encode_state(sc, prefix="w"))
        t1 = step(acme, 1)
        for i in range(7):
            step(globex, i)
        t2 = step(acme, 2)
        assert (t1.rid, t2.rid) == (1, 9)  # the in-file counter still orders rows
        assert t2.id - t1.id != 8 and {t1.id, t2.id}.isdisjoint({1, 9})
        assert [t.id for t in st.transitions(acme)] == [t1.id, t2.id]  # stable
        for sc in (acme, globex):
            st.resolve_entity(sc, "Vansh Sharma")
            st.resolve_entity(sc, "Vikram Shah")
        p1 = st.remember_about(acme, "VS", "f1").pending
        for i in range(5):
            st.remember_about(globex, "VS", f"g{i}")
        p2 = st.remember_about(acme, "VS", "f2").pending
        assert p2[0].rid - p1[0].rid == 12
        assert p2[0].id - p1[0].id != 12 and p1[0].id != p1[0].rid
        assert p1[0].group_id == p1[1].group_id  # one fact, one public group
        assert {p.id for p in st.pending_updates(acme)} == {p.id for p in p1 + p2}
    with MemoryStore(tmp_path / "m.db") as st:  # stable across opens
        assert [t.id for t in st.transitions(acme)] == [t1.id, t2.id]


# ------------------------------------------------------------ merge / split
def test_merge_does_not_disclose_a_descendants_pending_rows(tmp_path):
    org, alice = Scope.parse("acme:*:*"), Scope.parse("acme:alice:*")
    with MemoryStore(tmp_path / "m.db") as st:
        a = st.resolve_entity(org, "Vansh Sharma").entity_id
        b = st.resolve_entity(org, "Vikram Shah").entity_id
        r = st.remember_about(alice, "VS", "private note")
        assert len(r.pending) == 2
        assert st.pending_updates(org) == []
        out = st.merge_entities(org, a, b)
        assert out["pending_moved"] == []
        # alice's row still followed the merge.
        assert {p.entity_id for p in st.pending_updates(alice)} == {a}


def test_merge_reports_own_pending_rows_by_public_id(tmp_path):
    C = Scope.parse("acme:alice:p")
    with MemoryStore(tmp_path / "m.db") as st:
        y = st.resolve_entity(C, "Vansh Sharma").entity_id
        x = st.resolve_entity(C, "Bob Jones").entity_id
        st.remember_about(C, "Vansh Sharna", "lives in Delhi")
        pend = st.pending_updates(C)
        out = st.merge_entities(C, x, y)
        assert out["pending_moved"] == [p.id for p in pend if p.entity_id == y]


def test_split_keeps_a_descendants_post_merge_rejection(tmp_path):
    org, alice = Scope.parse("acme:*:*"), Scope.parse("acme:alice:*")
    with MemoryStore(tmp_path / "m.db") as st:
        k = st.resolve_entity(org, "Vansh Sharma").entity_id
        d = st.resolve_entity(org, "Vikram Shah").entity_id
        st.confirm_alias(alice, "boss", d)
        st.merge_entities(org, k, d)
        st.reject_alias(alice, "boss", k)
        st.split_entity(org, d)
        rows = {(a["alias"], a["status"], e.id) for e in st.entities(alice)
                for a in e.aliases if a["scope"] == str(alice)}
        assert ("boss", "confirmed", d) not in rows
        assert st.resolve_entity(alice, "boss").entity_id not in (k, d)


def test_split_still_restores_an_untouched_descendant_alias(tmp_path):
    org, alice = Scope.parse("acme:*:*"), Scope.parse("acme:alice:*")
    with MemoryStore(tmp_path / "m.db") as st:
        k = st.resolve_entity(org, "Vansh Sharma").entity_id
        d = st.resolve_entity(org, "Vikram Shah").entity_id
        st.confirm_alias(alice, "boss", d)
        st.merge_entities(org, k, d)
        st.split_entity(org, d)
        assert st.resolve_entity(alice, "boss").entity_id == d
