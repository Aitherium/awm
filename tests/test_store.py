"""MemoryStore: one-scope writes, ancestor-weighted reads, no sibling leaks."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from awm.scope import Scope, ScopeError
from awm.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path):
    with MemoryStore(tmp_path / "mem.db") as s:
        yield s


def S(text: str) -> Scope:
    return Scope.parse(text)


def test_remember_then_recall_at_the_same_scope(store: MemoryStore) -> None:
    store.remember(S("acme:alice:proj"), "db", "postgres 16")
    got = store.recall(S("acme:alice:proj"))
    assert [(m.key, m.value, m.weight) for m in got] == [("db", "postgres 16", 1.0)]


def test_remember_upserts_on_scope_and_key(store: MemoryStore) -> None:
    store.remember(S("acme:alice:*"), "k", "v1")
    store.remember(S("acme:alice:*"), "k", "v2")
    assert store.count(S("acme:alice:*")) == 1
    assert store.recall(S("acme:alice:*"))[0].value == "v2"


def test_the_same_key_at_two_scopes_is_two_memories(store: MemoryStore) -> None:
    store.remember(S("acme:alice:*"), "k", "user")
    store.remember(S("acme:alice:proj"), "k", "project")
    assert store.count() == 2


def test_nearer_scope_outranks_a_newer_ancestor_fact(store: MemoryStore) -> None:
    # Written in the order most likely to hide the bug: the project fact is
    # OLDEST and the platform fact newest; recency must not beat nearness.
    store.remember(S("acme:alice:proj"), "style", "project")
    time.sleep(0.01)
    store.remember(S("acme:alice:*"), "style", "user")
    time.sleep(0.01)
    store.remember(S("acme:*:*"), "style", "tenant")
    time.sleep(0.01)
    store.remember(S("platform:*:*"), "style", "platform")

    got = store.recall(S("acme:alice:proj"))
    assert [m.value for m in got] == ["project", "user", "tenant", "platform"]
    assert [m.weight for m in got] == [1.0, 0.5, 0.25, 0.125]


def test_within_one_scope_newest_wins(store: MemoryStore) -> None:
    store.remember(S("acme:*:*"), "a", "older")
    time.sleep(0.01)
    store.remember(S("acme:*:*"), "b", "newer")
    assert [m.key for m in store.recall(S("acme:*:*"))] == ["b", "a"]


def test_recall_never_sees_siblings_or_prefix_tenants(store: MemoryStore) -> None:
    store.remember(S("acme:bob:*"), "secret", "bob")
    store.remember(S("acme:alice:other"), "secret", "other project")
    store.remember(S("acmecorp:*:*"), "secret", "different tenant")
    store.remember(S("globex:*:*"), "secret", "different tenant")
    assert store.recall(S("acme:alice:proj")) == []


def test_recall_does_not_see_descendants(store: MemoryStore) -> None:
    store.remember(S("acme:alice:proj"), "k", "v")
    assert store.recall(S("acme:*:*")) == []
    assert store.recall(S("platform:*:*")) == []


def test_query_and_kind_filter(store: MemoryStore) -> None:
    store.remember(S("acme:*:*"), "deploy", "use podman quadlets", kind="rule")
    store.remember(S("acme:*:*"), "lunch", "tacos", kind="fact")
    assert [m.key for m in store.recall(S("acme:*:*"), query="PODMAN")] == ["deploy"]
    assert [m.key for m in store.recall(S("acme:*:*"), query="lunch")] == ["lunch"]
    assert [m.key for m in store.recall(S("acme:*:*"), kind="rule")] == ["deploy"]


def test_limit_applies_after_ranking(store: MemoryStore) -> None:
    store.remember(S("acme:alice:*"), "u", "user")
    time.sleep(0.01)
    store.remember(S("platform:*:*"), "p", "platform")
    got = store.recall(S("acme:alice:*"), limit=1)
    assert [m.value for m in got] == ["user"]


def test_meta_round_trips(store: MemoryStore) -> None:
    store.remember(S("acme:*:*"), "k", "v", meta={"src": "test", "n": 2})
    assert store.recall(S("acme:*:*"))[0].meta == {"src": "test", "n": 2}


def test_forget_is_exact_and_never_cascades(store: MemoryStore) -> None:
    store.remember(S("acme:*:*"), "k", "tenant")
    store.remember(S("acme:alice:*"), "k", "user")
    assert store.forget(S("acme:*:*"), "k") is True
    assert store.forget(S("acme:*:*"), "k") is False
    assert [m.value for m in store.recall(S("acme:alice:*"))] == ["user"]


@pytest.mark.parametrize("bad", ["acme:*:*", None])
def test_store_refuses_a_non_scope(store: MemoryStore, bad) -> None:
    with pytest.raises(ScopeError):
        store.remember(bad, "k", "v")  # type: ignore[arg-type]
    with pytest.raises(ScopeError):
        store.recall(bad)  # type: ignore[arg-type]


def test_empty_key_is_refused(store: MemoryStore) -> None:
    with pytest.raises(ScopeError):
        store.remember(S("acme:*:*"), "", "v")


def test_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    with MemoryStore(db) as s:
        s.remember(S("acme:*:*"), "k", "v")
    with MemoryStore(db) as s:
        assert s.recall(S("acme:*:*"))[0].value == "v"


def test_a_foreign_schema_version_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    MemoryStore(db).close()
    con = sqlite3.connect(str(db))
    con.execute("UPDATE schema_meta SET version = 99")
    con.commit()
    con.close()
    with pytest.raises(ScopeError, match="schema version 99"):
        MemoryStore(db)
