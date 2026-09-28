"""Superseded history, `as_of`, forget-into-history, purge, and the v1 -> v2 migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from awm.scope import Scope, ScopeError
from awm.store import SCHEMA_VERSION, MemoryStore


class Clock:
    """A pinned clock: `as_of` "between two writes" is only testable at known instants."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def S(text: str) -> Scope:
    return Scope.parse(text)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: Clock):
    with MemoryStore(tmp_path / "mem.db") as s:
        s._clock = clock
        yield s


def _values(store, scope, key):
    return [(h.value, h.reason) for h in store.history(scope, key)]


def test_a_changed_value_is_superseded_not_destroyed(store, clock) -> None:
    store.remember(S("acme:alice:*"), "k", "v1")
    clock.t = 2000
    store.remember(S("acme:alice:*"), "k", "v2")
    hist = store.history(S("acme:alice:*"), "k")
    assert [(h.value, h.valid_from, h.valid_to, h.reason) for h in hist] == [
        ("v1", 1000, 2000, "superseded"), ("v2", 2000, None, "current")]
    assert [m.value for m in store.recall(S("acme:alice:*"))] == ["v2"]


def test_the_same_value_rewritten_records_no_history(store, clock) -> None:
    store.remember(S("acme:*:*"), "k", "v")
    clock.t = 2000
    store.remember(S("acme:*:*"), "k", "v")
    assert _values(store, S("acme:*:*"), "k") == [("v", "current")]


def test_a_reaffirmed_value_keeps_its_true_start(store, clock) -> None:
    # Rewriting the same value bumps `updated`; the value's interval must still
    # start at its FIRST write, or as_of between the two misses it.
    store.remember(S("acme:*:*"), "k", "dark")
    clock.t = 2000
    store.remember(S("acme:*:*"), "k", "dark")
    clock.t = 3000
    store.remember(S("acme:*:*"), "k", "light")
    assert [m.value for m in store.recall(S("acme:*:*"), as_of=1500)] == ["dark"]
    assert store.history(S("acme:*:*"), "k")[0].valid_from == 1000


def test_old_kind_and_meta_travel_into_history(store, clock) -> None:
    store.remember(S("acme:*:*"), "k", "v1", kind="rule", meta={"src": "a"})
    clock.t = 2000
    store.remember(S("acme:*:*"), "k", "v2")
    old = store.history(S("acme:*:*"), "k")[0]
    assert (old.kind, old.meta) == ("rule", {"src": "a"})


def test_as_of_returns_the_value_current_then(store, clock) -> None:
    store.remember(S("acme:*:*"), "k", "v1")
    clock.t = 2000
    store.remember(S("acme:*:*"), "k", "v2")
    clock.t = 3000
    store.remember(S("acme:*:*"), "k", "v3")
    at = lambda ts: [m.value for m in store.recall(S("acme:*:*"), as_of=ts)]  # noqa: E731
    assert at(999) == []
    assert at(1000) == ["v1"]
    assert at(1999.9) == ["v1"]
    assert at(2000) == ["v2"]
    assert at(2500) == ["v2"]
    assert at(10_000) == ["v3"]


def test_as_of_keeps_scope_weighting_and_filters(store, clock) -> None:
    store.remember(S("acme:*:*"), "style", "tenant-old", kind="rule")
    store.remember(S("acme:alice:*"), "db", "pg15")
    clock.t = 2000
    store.remember(S("acme:*:*"), "style", "tenant-new", kind="rule")
    store.remember(S("acme:alice:*"), "db", "pg16")
    got = store.recall(S("acme:alice:*"), as_of=1500)
    assert [(m.value, m.weight) for m in got] == [("pg15", 1.0), ("tenant-old", 0.5)]
    assert [m.value for m in store.recall(S("acme:alice:*"), as_of=1500, kind="rule")] \
        == ["tenant-old"]
    assert [m.value for m in store.recall(S("acme:alice:*"), as_of=1500, query="PG")] \
        == ["pg15"]


def test_forget_is_recorded_in_history(store, clock) -> None:
    store.remember(S("acme:*:*"), "k", "v")
    clock.t = 2000
    assert store.forget(S("acme:*:*"), "k") is True
    assert store.recall(S("acme:*:*")) == []
    hist = store.history(S("acme:*:*"), "k")
    assert [(h.value, h.valid_to, h.reason) for h in hist] == [("v", 2000, "forgotten")]
    assert [m.value for m in store.recall(S("acme:*:*"), as_of=1500)] == ["v"]
    assert store.recall(S("acme:*:*"), as_of=2500) == []


def test_remember_after_forget_starts_a_new_interval(store, clock) -> None:
    store.remember(S("acme:*:*"), "k", "v1")
    clock.t = 2000
    store.forget(S("acme:*:*"), "k")
    clock.t = 3000
    store.remember(S("acme:*:*"), "k", "v2")
    assert store.recall(S("acme:*:*"), as_of=2500) == []
    hist = store.history(S("acme:*:*"), "k")
    assert [(h.value, h.valid_from, h.valid_to) for h in hist] == [
        ("v1", 1000, 2000), ("v2", 3000, None)]


def test_forget_still_never_cascades(store) -> None:
    store.remember(S("acme:*:*"), "k", "tenant")
    store.remember(S("acme:alice:*"), "k", "user")
    store.forget(S("acme:*:*"), "k")
    assert [h.scope for h in store.history(S("acme:alice:*"), "k")] == [
        "acme:*:*", "acme:alice:*"]
    assert [m.value for m in store.recall(S("acme:alice:*"))] == ["user"]


def test_purge_history_is_exact(store, clock) -> None:
    for sc in ("acme:*:*", "acme:alice:*"):
        store.remember(S(sc), "k", "v1")
    clock.t = 2000
    for sc in ("acme:*:*", "acme:alice:*"):
        store.remember(S(sc), "k", "v2")
    assert store.purge_history(S("acme:*:*"), "k") == 1
    assert store.purge_history(S("acme:*:*"), "k") == 0
    hist = store.history(S("acme:alice:*"), "k")
    assert [(h.scope, h.value) for h in hist] == [
        ("acme:alice:*", "v1"), ("acme:*:*", "v2"), ("acme:alice:*", "v2")]
    # The live value is not history; purging never touches it.
    assert store.count() == 2


def test_history_includes_ancestors_never_siblings_or_prefix_tenants(store, clock) -> None:
    for sc in ("acme:bob:*", "acmecorp:*:*", "acme:alice:other", "platform:*:*"):
        store.remember(S(sc), "k", f"{sc}-v1")
    clock.t = 2000
    for sc in ("acme:bob:*", "acmecorp:*:*", "acme:alice:other"):
        store.remember(S(sc), "k", f"{sc}-v2")
    hist = store.history(S("acme:alice:proj"), "k")
    assert [h.scope for h in hist] == ["platform:*:*"]
    assert [m.scope for m in store.recall(S("acme:alice:proj"), as_of=1500)] == [
        "platform:*:*"]


def test_history_refuses_a_non_scope_and_empty_key(store) -> None:
    with pytest.raises(ScopeError):
        store.history("acme:*:*", "k")  # type: ignore[arg-type]
    with pytest.raises(ScopeError):
        store.history(S("acme:*:*"), "")
    with pytest.raises(ScopeError):
        store.purge_history("acme:*:*", "k")  # type: ignore[arg-type]


# ------------------------------------------------------------ migration
_V1_SCHEMA = """
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
"""


def _v1_file(path: Path) -> Path:
    """A v1 store exactly as awm 0.3.x wrote it — built by hand, not by this code."""
    con = sqlite3.connect(str(path))
    con.executescript(_V1_SCHEMA)
    con.commit()
    con.close()
    return path


def _tables(path: Path) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_v1_file_migrates_to_v2_with_rows_untouched(tmp_path: Path) -> None:
    db = _v1_file(tmp_path / "v1.db")
    before = sqlite3.connect(str(db)).execute("SELECT * FROM memories").fetchall()
    # 0.6.0: migration is opt-in (see test_migration.py); this is the opt-in path.
    with MemoryStore(db, auto_migrate=True) as st:
        m = st.recall(S("acme:alice:*"))[0]
        assert (m.value, m.created, m.updated, m.hits, m.meta) == (
            "dark", 100.0, 150.0, 3, {"src": "v1"})
        # v1 overwrote in place: the live value is only known to hold from
        # `updated`, never from `created` (a different value may have held then).
        assert st.history(S("acme:alice:*"), "theme")[0].valid_from == 150.0
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchall() == [(SCHEMA_VERSION,)]
    cols = "id,scope,key,value,kind,created,updated,hits,meta"
    assert con.execute(f"SELECT {cols} FROM memories").fetchall() == before
    con.close()
    assert {"memory_history", "entities", "entity_aliases"} <= _tables(db)
    # Re-opening a migrated file is a no-op, not a second migration.
    with MemoryStore(db) as st:
        st.remember(S("acme:alice:*"), "theme", "light")
        assert [h.value for h in st.history(S("acme:alice:*"), "theme")] == ["dark", "light"]


def test_a_failed_migration_leaves_a_v1_file(tmp_path: Path, monkeypatch) -> None:
    import awm.store as store_mod
    db = _v1_file(tmp_path / "v1.db")
    monkeypatch.setattr(store_mod, "_SCHEMA_V2",
                        (store_mod._SCHEMA_V2[0], "THIS IS NOT SQL"))
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore(db, auto_migrate=True)
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    con.close()
    assert "memory_history" not in _tables(db)


def test_a_fresh_file_is_current(tmp_path: Path) -> None:
    MemoryStore(tmp_path / "new.db").close()
    con = sqlite3.connect(str(tmp_path / "new.db"))
    assert SCHEMA_VERSION == 3
    assert con.execute("SELECT version FROM schema_meta").fetchall() == [(SCHEMA_VERSION,)]
    con.close()


def test_a_newer_schema_is_still_refused(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    MemoryStore(db).close()
    con = sqlite3.connect(str(db))
    con.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION + 1,))
    con.commit()
    con.close()
    with pytest.raises(ScopeError, match=f"schema version {SCHEMA_VERSION + 1}"):
        MemoryStore(db)
