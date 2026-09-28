"""0.6.0: an older file opens in COMPAT mode and is migrated only when asked.

The failure this guards: 0.4/0.5 migrated a v1 file on open, bumping
`schema_meta` past what installed awm 0.3.x accepts -- one read from a new
install locked every old reader on the machine out of the shared file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from awm import mcp_server
from awm.cli import main
from awm.scope import Scope
from awm.store import (
    SCHEMA_VERSION,
    MemoryStore,
    MigrationError,
    NeedsMigration,
    probe_schema,
)

P = Scope("acme", "alice", "proj")
ALICE = Scope("acme", "alice")

_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE INDEX idx_scope ON memories(scope);
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
INSERT INTO memories(scope,key,value,kind,created,updated,hits,meta)
    VALUES ('acme:alice:*','theme','dark','fact',100.0,150.0,3,'{"src": "v1"}');
INSERT INTO memories(scope,key,value,kind,created,updated)
    VALUES ('acme:alice:proj','ui','light','fact',100.0,500.0);
"""


def _v1(path: Path) -> Path:
    """A v1 file exactly as awm 0.3.x wrote it -- built by hand, not by this code."""
    con = sqlite3.connect(str(path))
    con.executescript(_V1)
    con.commit()
    con.close()
    return path


def _v2(path: Path) -> Path:
    """A v2 file: a current one with the v3 tables dropped and the version set to 2."""
    MemoryStore(path).close()
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE transitions")
    con.execute("DROP TABLE entity_merges")
    con.execute("UPDATE schema_meta SET version = 2")
    con.execute("INSERT INTO memories(scope,key,value,kind,created,updated,meta,since) "
                "VALUES ('acme:alice:*','k','v1','fact',1.0,1.0,'{}',1.0)")
    con.commit()
    con.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT version FROM schema_meta").fetchone()[0]
    finally:
        con.close()


def _tables(path: Path) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def v1_reader_accepts(path: Path) -> bool:
    """awm 0.3.1's open check, verbatim in effect: version must be exactly 1."""
    con = sqlite3.connect(str(path))
    try:
        row = con.execute("SELECT version FROM schema_meta").fetchone()
        con.execute("SELECT scope,key,value,kind,created,updated,hits,meta FROM memories")
        return row is not None and row[0] == 1
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _no_env_auto(monkeypatch):
    monkeypatch.delenv("AWM_AUTO_MIGRATE", raising=False)


# ------------------------------------------------------------ compat mode: v1
def test_opening_a_v1_file_changes_no_byte(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    before = _sha(db)
    with MemoryStore(db) as st:
        assert (st.file_version, st.compat) == (1, True)
        assert {m.key for m in st.recall(P)} == {"theme", "ui"}
        assert st.count() == 2
    assert _sha(db) == before
    assert v1_reader_accepts(db)


@pytest.mark.parametrize("call", [
    lambda st: st.history(P, "ui"),
    lambda st: st.recall(P, as_of=600),
    lambda st: st.purge_history(P, "ui"),
    lambda st: st.reconcile_and_remember(P, "dark mode", subject="ui"),
    lambda st: st.resolve_entity(P, "vansh"),
    lambda st: st.entities(P),
    lambda st: st.encode_state(P),
    lambda st: st.transitions(P),
    lambda st: st.surprise_log(P, 0.0),
    lambda st: st.surprise_stats(P),
    lambda st: st.merge_entities(P, 1, 2),
])
def test_every_newer_feature_on_v1_raises_needs_migration(tmp_path: Path, call) -> None:
    db = _v1(tmp_path / "v1.db")
    before = _sha(db)
    with MemoryStore(db) as st:
        with pytest.raises(NeedsMigration, match="awm migrate --db") as exc:
            call(st)
    assert exc.value.found == 1 and exc.value.need >= 2
    assert _sha(db) == before


def test_v1_remember_and_forget_are_the_old_plain_writes(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    with MemoryStore(db) as st:
        st._clock = lambda: 900.0
        st.remember(P, "ui", "dark")
        st.remember(P, "new", "x")
        assert {m.key: m.value for m in st.recall(P)}["ui"] == "dark"
        assert st.forget(P, "new") is True
        assert st.count(P) == 1
    # Still v1, no history table, no `since` column: exactly what 0.3.x can read.
    assert _version(db) == 1
    assert "memory_history" not in _tables(db)
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
    row = con.execute("SELECT created, updated FROM memories WHERE key='ui'").fetchone()
    con.close()
    assert "since" not in cols
    assert row == (100.0, 900.0)  # upsert in place: created kept, updated bumped
    assert v1_reader_accepts(db)


# ------------------------------------------------------------ compat mode: v2
def test_a_v2_file_keeps_its_v2_features_and_refuses_v3_ones(tmp_path: Path) -> None:
    db = _v2(tmp_path / "v2.db")
    with MemoryStore(db) as st:
        assert st.file_version == 2
        st.remember(ALICE, "k", "v2")
        assert [h.value for h in st.history(ALICE, "k")] == ["v1", "v2"]
        assert st.resolve_entity(P, "vansh").created_new
        with pytest.raises(NeedsMigration, match="schema v3"):
            st.encode_state(P)
        with pytest.raises(NeedsMigration):
            st.merge_entities(P, 1, 2)
    assert _version(db) == 2
    assert "transitions" not in _tables(db)


# ------------------------------------------------------------ migrate
def test_migrate_backs_up_byte_for_byte_and_keeps_every_row(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    original = _sha(db)
    con = sqlite3.connect(str(db))
    rows_before = con.execute("SELECT * FROM memories ORDER BY id").fetchall()
    con.close()
    with MemoryStore(db) as st:
        rep = st.migrate()
        assert (rep["from"], rep["to"], rep["migrated"]) == (1, SCHEMA_VERSION, True)
        assert rep["before"]["memories"] == rep["after"]["memories"] == 2
        assert rep["before"]["memory_history"] is None and rep["after"]["memory_history"] == 0
        assert rep["digest_before"] == rep["digest_after"]
        # The same open store now has the newer features.
        assert st.history(P, "ui")[0].valid_from == 500.0
        assert st.transitions(P) == []
    backup = Path(rep["backup"])
    assert backup.parent == db.parent and backup.name.startswith("v1.db.v1-backup-")
    assert _sha(backup) == original == rep["backup_sha256"]
    assert v1_reader_accepts(backup)
    assert _version(db) == SCHEMA_VERSION
    con = sqlite3.connect(str(db))
    cols = "id,scope,key,value,kind,created,updated,hits,meta"
    assert con.execute(f"SELECT {cols} FROM memories ORDER BY id").fetchall() == rows_before
    con.close()
    # Idempotent: a second migrate is a reported no-op, and writes no backup.
    with MemoryStore(db) as st:
        again = st.migrate()
    assert again["migrated"] is False and again["backup"] is None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    before = _sha(db)
    with MemoryStore(db) as st:
        rep = st.migrate(dry_run=True)
    assert rep["migrated"] is False and rep["backup"] is None
    assert _sha(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1.db"]


def test_no_backup_writes_no_backup(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    with MemoryStore(db) as st:
        assert st.migrate(backup=False)["backup"] is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1.db"]
    assert _version(db) == SCHEMA_VERSION


def test_a_migration_that_changes_rows_is_rolled_back(tmp_path: Path, monkeypatch) -> None:
    db = _v1(tmp_path / "v1.db")
    st = MemoryStore(db)
    real = st._migrate_statements

    def lossy(found: int) -> None:
        real(found)
        st._db.execute("DELETE FROM memories WHERE key = 'ui'")

    monkeypatch.setattr(st, "_migrate_statements", lossy)
    with pytest.raises(MigrationError, match="rolled back"):
        st.migrate()
    assert st.file_version == 1
    st.close()
    assert _version(db) == 1
    assert "memory_history" not in _tables(db)
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    con.close()


def test_a_value_rewrite_during_migration_is_caught_by_the_digest(tmp_path: Path,
                                                                   monkeypatch) -> None:
    db = _v1(tmp_path / "v1.db")
    st = MemoryStore(db)
    real = st._migrate_statements

    def rewriting(found: int) -> None:
        real(found)
        st._db.execute("UPDATE memories SET value = 'tampered' WHERE key = 'ui'")

    monkeypatch.setattr(st, "_migrate_statements", rewriting)
    with pytest.raises(MigrationError, match="memories digest"):
        st.migrate()
    st.close()
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT value FROM memories WHERE key='ui'").fetchone()[0] == "light"
    con.close()


def test_env_opt_in_restores_migrate_on_open_with_a_backup(tmp_path: Path,
                                                           monkeypatch) -> None:
    db = _v1(tmp_path / "v1.db")
    original = _sha(db)
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    with MemoryStore(db) as st:
        assert st.file_version == SCHEMA_VERSION and not st.compat
    assert _version(db) == SCHEMA_VERSION
    backups = [p for p in tmp_path.iterdir() if ".v1-backup-" in p.name]
    assert len(backups) == 1 and _sha(backups[0]) == original


@pytest.mark.parametrize("value", ["0", "", "yes", "true"])
def test_env_values_other_than_1_do_not_migrate(tmp_path: Path, monkeypatch, value) -> None:
    db = _v1(tmp_path / "v1.db")
    monkeypatch.setenv("AWM_AUTO_MIGRATE", value)
    MemoryStore(db).close()
    assert _version(db) == 1


def test_a_new_file_is_created_at_the_current_schema(tmp_path: Path) -> None:
    with MemoryStore(tmp_path / "new.db") as st:
        assert (st.file_version, st.compat) == (SCHEMA_VERSION, False)
    assert _version(tmp_path / "new.db") == SCHEMA_VERSION
    assert "entity_merges" in _tables(tmp_path / "new.db")


# ------------------------------------------------------------ probe / doctor
def test_probe_schema_is_read_only_and_never_creates(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    info = probe_schema(missing)
    assert info["exists"] is False and not missing.exists()
    db = _v1(tmp_path / "v1.db")
    before = _sha(db)
    info = probe_schema(db)
    assert (info["file_version"], info["code_version"], info["compat"]) == (
        1, SCHEMA_VERSION, True)
    assert _sha(db) == before


def test_doctor_reports_file_vs_code_schema_and_compat(tmp_path: Path, capsys) -> None:
    db = _v1(tmp_path / "v1.db")
    main(["doctor", "--db", str(db)])
    out = capsys.readouterr().out
    assert f"file v1, code v{SCHEMA_VERSION}" in out
    assert "compat     ACTIVE" in out and "awm migrate --db" in out
    missing = tmp_path / "none.db"
    main(["doctor", "--db", str(missing)])
    assert "no memory file" in capsys.readouterr().out
    assert not missing.exists()


def test_doctor_verdict_fails_on_a_newer_file(tmp_path: Path, capsys) -> None:
    db = tmp_path / "m.db"
    MemoryStore(db).close()
    con = sqlite3.connect(str(db))
    con.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    from awm import doctor_local
    doctor_local.DB_PATH = db
    try:
        problems, unjudged = doctor_local._doctor_local_verdict()
    finally:
        doctor_local.DB_PATH = None
    assert problems and "newer" in problems[0] and unjudged == []


# ------------------------------------------------------------ CLI / MCP
def test_cli_migrate_prints_counts_and_refuses_a_missing_file(tmp_path: Path, capsys) -> None:
    assert main(["migrate", "--db", str(tmp_path / "missing.db")]) == 2
    assert not (tmp_path / "missing.db").exists()
    db = _v1(tmp_path / "v1.db")
    assert main(["migrate", "--db", str(db), "--dry-run"]) == 0
    assert "dry run: nothing written" in capsys.readouterr().out
    assert _version(db) == 1
    assert main(["--db", str(db), "recall", "--scope", str(P), "--as-of", "600"]) == 2
    assert "NEEDS MIGRATION" in capsys.readouterr().err
    assert main(["migrate", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "v1 -> v3" in out and "rows before  memories=2" in out
    assert "rows after   memories=2" in out and "byte-identical" in out
    assert main(["migrate", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] is False


def test_mcp_reports_needs_migration_as_a_tool_error(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    srv = mcp_server.AwmMcp(db, cwd=tmp_path, user="alice")
    res = srv.call("awm_history", {"key": "ui", "scope": str(P)})
    assert res["isError"] and "NEEDS MIGRATION" in res["content"][0]["text"]
    res = srv.call("awm_recall", {"scope": str(P)})
    assert not res["isError"]
    assert _version(db) == 1


# ------------------------------------------------------------ the real 0.3.x reader
_OLD_SRC = os.environ.get("AWM_OLD_READER_SRC", "")


@pytest.mark.skipif(not (_OLD_SRC and (Path(_OLD_SRC) / "awm" / "store.py").is_file()),
                    reason="set AWM_OLD_READER_SRC to an awm 0.3.x source tree")
def test_the_installed_old_reader_still_opens_a_compat_file(tmp_path: Path) -> None:
    db = _v1(tmp_path / "v1.db")
    with MemoryStore(db) as st:
        st.recall(P)
        st.remember(P, "added-by-0.6", "yes")
    code = ("import sys, awm; from pathlib import Path; from awm.store import MemoryStore, "
            "SCHEMA_VERSION; from awm.scope import Scope; "
            "st = MemoryStore(Path(sys.argv[1])); "
            "print(SCHEMA_VERSION, awm.__version__, st.count(), "
            "sorted(m.key for m in st.recall(Scope('acme','alice','proj'))))")
    env = dict(os.environ, PYTHONPATH=_OLD_SRC)
    out = subprocess.run([sys.executable, "-c", code, str(db)], env=env,
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    schema, version, count = out.stdout.split()[:3]
    # The old code, at its own schema, reading 3 rows (2 hand-made + 1 from 0.6).
    assert (schema, version[:4], count) == ("1", "0.3.", "3")
    assert "added-by-0.6" in out.stdout


# ------------------------------------------------------------ round-1 review regressions
def test_a_kill_mid_backup_leaves_no_truncated_file_under_a_backup_name(
        tmp_path: Path, monkeypatch) -> None:
    """The copy is written to .partial and renamed only once whole and hash-checked."""
    import awm.store as store_mod

    db = _v1(tmp_path / "v1.db")
    original = _sha(db)

    def half_then_die(src, dst, *a, **k):
        data = Path(src).read_bytes()
        Path(dst).write_bytes(data[: len(data) // 2])
        raise KeyboardInterrupt("killed mid-copy")

    monkeypatch.setattr(store_mod.shutil, "copyfile", half_then_die)
    with MemoryStore(db) as st:
        with pytest.raises(KeyboardInterrupt):
            st.migrate()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert [n for n in names if ".v1-backup-" in n] == [], names
    assert _sha(db) == original and _version(db) == 1


def test_doctor_flags_an_interrupted_partial_backup(tmp_path: Path, capsys) -> None:
    from awm import doctor_local

    db = _v1(tmp_path / "v1.db")
    (tmp_path / "v1.db.v1-backup-20260101T000000.partial").write_bytes(b"half")
    monkey = doctor_local.DB_PATH
    doctor_local.DB_PATH = db
    try:
        problems, _ = doctor_local._doctor_local_verdict()
        lines = doctor_local._doctor_local()
    finally:
        doctor_local.DB_PATH = monkey
    assert problems and "interrupted migration backup" in problems[0]
    assert any("INCOMPLETE" in ln for ln in lines)


def test_the_session_start_hook_never_migrates_even_with_the_env_opt_in(
        tmp_path: Path, monkeypatch) -> None:
    from awm import claude_hook

    db = _v1(tmp_path / "v1.db")
    original = _sha(db)
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    claude_hook.build(db, cwd=tmp_path, user="alice", scope=str(P))
    assert _version(db) == 1 and _sha(db) == original
    assert [p for p in tmp_path.iterdir() if ".v1-backup-" in p.name] == []


def test_the_mcp_server_never_migrates_even_with_the_env_opt_in(
        tmp_path: Path, monkeypatch) -> None:
    db = _v1(tmp_path / "v1.db")
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    srv = mcp_server.AwmMcp(db, cwd=tmp_path, user="alice")
    res = srv.call("awm_recall", {"scope": str(P)})
    assert not res["isError"]
    assert _version(db) == 1
    assert [p for p in tmp_path.iterdir() if ".v1-backup-" in p.name] == []


def test_cli_recall_never_migrates_even_with_the_env_opt_in(
        tmp_path: Path, monkeypatch, capsys) -> None:
    db = _v1(tmp_path / "v1.db")
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")
    main(["--db", str(db), "recall", "--scope", str(P)])
    assert _version(db) == 1
