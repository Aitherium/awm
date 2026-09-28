"""0.6.0 entities: near-miss spellings, initials ranked first, reversible merge/split."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from awm import mcp_server
from awm.cli import main
from awm.entities import edit_distance, initials_of, typo_of, written_as_initials
from awm.scope import Scope, ScopeError
from awm.store import MemoryStore

ALICE = Scope("acme", "alice")
PROJ = Scope("acme", "alice", "proj")
OTHER = Scope("acme", "alice", "other")
BOB = Scope("acme", "bob", "proj")


@pytest.fixture()
def store(tmp_path: Path):
    with MemoryStore(tmp_path / "mem.db") as s:
        yield s


def _alias_rows(st: MemoryStore, scope: Scope):
    return sorted(tuple(r) for r in st._db.execute(
        "SELECT scope, alias_norm, entity_id, status, evidence, created FROM entity_aliases "
        "WHERE scope = ?", (str(scope),)))


def _entity_rows(st: MemoryStore):
    return sorted(tuple(r) for r in st._db.execute("SELECT * FROM entities"))


# ------------------------------------------------------------ pure helpers
@pytest.mark.parametrize("a,b,d", [
    ("vansh", "vansh", 0), ("vanhs", "vansh", 1), ("vansh", "vanshi", 1),
    ("vansh", "vasnh", 1), ("kitten", "sitting", 3), ("", "abc", 3),
    ("vnash sahrma", "vansh sharma", 2),
])
def test_edit_distance_counts_a_neighbour_swap_as_one(a, b, d) -> None:
    assert edit_distance(a, b) == d == edit_distance(b, a)


@pytest.mark.parametrize("mention,known,hit", [
    ("vanhs", "vansh", True),            # len 5, distance 1
    ("vanshh", "vansh", True),           # insertion
    ("alxce", "alice", True),
    ("bbo", "bob", False),               # too short for any tolerance
    ("vanhz", "vansh", False),           # distance 2 at length 5
    ("vnash sahrma", "vansh sharma", True),   # distance 2 at length >= 8
    ("vnash sahrmx", "vansh sharma", False),  # distance 3
    ("server01", "server02", False),     # digits differ: a different thing
    ("carol0", "carol1", False),
    ("vansh", "vansh", False),           # exact is not a typo
])
def test_typo_of_bands(mention, known, hit) -> None:
    assert (typo_of(mention, known) is not None) is hit


@pytest.mark.parametrize("raw,yes", [
    ("VS", True), ("V.S.", True), ("v s", True), ("V S K", True),
    ("vs", False), ("Vansh", False), ("V", False), ("VSKLM", False), ("VS!", False),
])
def test_written_as_initials(raw, yes) -> None:
    assert written_as_initials(raw) is yes


def test_initials_of_needs_a_multi_word_name() -> None:
    assert initials_of("vs", "vansh sharma")
    assert not initials_of("vs", "vosk")
    assert not initials_of("vk", "vansh sharma")


# ------------------------------------------------------------ G2a near misses
def test_a_typo_is_only_ever_a_possible_link(store) -> None:
    v = store.resolve_entity(PROJ, "vansh")
    r = store.resolve_entity(PROJ, "Vanhs")
    assert (r.status, r.entity_id, r.possible, r.created_new) == (
        "possible", None, [v.entity_id], False)
    assert len(store.entities(PROJ)) == 1
    # Asking again does not harden it into a confirmed link.
    assert store.resolve_entity(PROJ, "vanhs").status == "possible"
    ev = [a["evidence"] for a in store.entities(PROJ)[0].aliases if a["alias"] == "vanhs"]
    assert ev and "edit distance 1" in ev[0]


def test_a_typo_of_a_long_name_tolerates_two_edits(store) -> None:
    v = store.resolve_entity(PROJ, "Vansh Sharma")
    r = store.resolve_entity(PROJ, "Vnash Sahrma")
    assert (r.status, r.possible) == ("possible", [v.entity_id])


def test_too_far_or_too_short_is_a_new_entity(store) -> None:
    store.resolve_entity(PROJ, "vansh")
    assert store.resolve_entity(PROJ, "vanhz").created_new  # distance 2 at length 5
    # One edit, but 3 letters: no typo tolerance. (A different first letter, so
    # the older abbreviation rule cannot match it either.)
    store.resolve_entity(PROJ, "jan")
    assert store.resolve_entity(PROJ, "ian").created_new


def test_a_rejected_typo_is_not_proposed_again(store) -> None:
    v = store.resolve_entity(PROJ, "vansh")
    store.resolve_entity(PROJ, "vanhs")
    assert store.reject_alias(PROJ, "vanhs", v.entity_id) is True
    r = store.resolve_entity(PROJ, "vanhs")
    assert r.status == "confirmed" and r.created_new and r.entity_id != v.entity_id


def test_a_typo_never_reaches_a_sibling_entity(store) -> None:
    store.resolve_entity(BOB, "vansh")
    r = store.resolve_entity(PROJ, "vanhs")
    assert r.created_new and r.possible == []


def test_a_typo_matches_the_canonical_name_too(store) -> None:
    # "Vansh Sharma (Delhi)" is created with canonical "Vansh Sharma".
    v = store.resolve_entity(PROJ, "Vansh Sharma, from Delhi")
    r = store.resolve_entity(PROJ, "vansh sharmaa")
    assert r.status == "possible" and v.entity_id in r.possible


# ------------------------------------------------------------ G2c initials
def test_caps_initials_rank_the_multi_word_name_first(store) -> None:
    vosk = store.resolve_entity(PROJ, "Vosk")            # 'vs' abbreviates it too
    vs = store.resolve_entity(PROJ, "Vansh Sharma")
    vk = store.resolve_entity(PROJ, "Vikram Shah")
    assert vosk.created_new and vs.created_new and vk.created_new
    r = store.resolve_entity(PROJ, "VS")
    assert r.status == "possible" and r.entity_id is None
    assert r.possible[0] == vs.entity_id
    assert set(r.possible[:2]) == {vs.entity_id, vk.entity_id}
    assert r.possible[2] == vosk.entity_id
    assert store.resolve_entity(PROJ, "V.S.").possible[0] == vs.entity_id
    # Lowercase "vs" is not written as initials: the old order (oldest first) stands.
    assert store.resolve_entity(PROJ, "vs").possible[0] == vosk.entity_id


def test_caps_initials_with_one_candidate(store) -> None:
    vs = store.resolve_entity(PROJ, "vansh sharma")
    r = store.resolve_entity(PROJ, "VS")
    assert (r.status, r.possible) == ("possible", [vs.entity_id])


# ------------------------------------------------------------ G2b merge / split
def test_merge_moves_aliases_and_split_restores_them_exactly(store) -> None:
    vs = store.resolve_entity(PROJ, "Vansh Sharma")
    vk = store.resolve_entity(PROJ, "Vikram Shah")
    store.resolve_entity(PROJ, "VS")                       # possible -> both
    store.confirm_alias(PROJ, "vikram", vk.entity_id)
    store.reject_alias(PROJ, "vansh", vk.entity_id)
    aliases_before, entities_before = _alias_rows(store, PROJ), _entity_rows(store)

    m = store.merge_entities(PROJ, vs.entity_id, vk.entity_id)
    assert (m["merged_id"], m["keep_id"]) == (vk.entity_id, vs.entity_id)
    ids = [e.id for e in store.entities(PROJ)]
    assert vk.entity_id not in ids
    assert store.resolve_entity(PROJ, "vikram shah").entity_id == vs.entity_id
    assert store.resolve_entity(PROJ, "vikram").entity_id == vs.entity_id

    out = store.split_entity(PROJ, vk.entity_id)
    assert out["kept_on_survivor"] == []
    assert _alias_rows(store, PROJ) == aliases_before
    assert _entity_rows(store) == entities_before
    assert store.resolve_entity(PROJ, "vikram shah").entity_id == vk.entity_id
    with pytest.raises(ScopeError, match="no merge"):
        store.split_entity(PROJ, vk.entity_id)             # already split


def test_an_alias_gained_after_the_merge_stays_with_the_survivor(store) -> None:
    vs = store.resolve_entity(PROJ, "Vansh Sharma")
    vk = store.resolve_entity(PROJ, "Vikram Shah")
    store.merge_entities(PROJ, vs.entity_id, vk.entity_id)
    store.confirm_alias(PROJ, "the designer", vs.entity_id)
    out = store.split_entity(PROJ, vk.entity_id)
    assert out["kept_on_survivor"] == ["the designer"]
    assert store.resolve_entity(PROJ, "the designer").entity_id == vs.entity_id


def test_merge_and_split_obey_scope(store) -> None:
    tenant_level = store.resolve_entity(ALICE, "Vikram Shah")
    vs = store.resolve_entity(PROJ, "Vansh Sharma")
    bob = store.resolve_entity(BOB, "Bobby")
    # A sibling's entity is invisible: the same refusal as a missing id.
    with pytest.raises(ScopeError, match="no entity"):
        store.merge_entities(PROJ, vs.entity_id, bob.entity_id)
    with pytest.raises(ScopeError, match="no entity"):
        store.merge_entities(PROJ, bob.entity_id, vs.entity_id)
    # An ancestor's entity cannot be dropped from a narrower scope.
    with pytest.raises(ScopeError, match="merge it from there"):
        store.merge_entities(PROJ, vs.entity_id, tenant_level.entity_id)
    # But it can be the survivor.
    m = store.merge_entities(PROJ, tenant_level.entity_id, vs.entity_id)
    # The split is a write at exactly that scope: a sibling cannot undo it.
    with pytest.raises(ScopeError, match="no merge"):
        store.split_entity(OTHER, m["merged_id"])
    with pytest.raises(ScopeError, match="no merge"):
        store.split_entity(BOB, m["merged_id"])
    store.split_entity(PROJ, m["merged_id"])


def test_a_descendant_alias_is_retargeted_by_the_merge_and_restored_by_split(store) -> None:
    # Review 3: a descendant confirming an alias for an ancestor's entity used to
    # veto every merge of it at the ancestor, and the refusal leaked that it had.
    keep = store.resolve_entity(ALICE, "Vansh Sharma")
    drop = store.resolve_entity(ALICE, "Vikram Shah")
    store.confirm_alias(PROJ, "vik", drop.entity_id)       # a narrower scope names it
    m = store.merge_entities(ALICE, keep.entity_id, drop.entity_id)
    assert store.resolve_entity(PROJ, "vik").entity_id == keep.entity_id
    store.split_entity(ALICE, m["merged_id"])
    assert store.resolve_entity(PROJ, "vik").entity_id == drop.entity_id


def test_merge_refuses_a_confirmed_versus_rejected_contradiction(store) -> None:
    a = store.resolve_entity(PROJ, "Vansh Sharma")
    b = store.resolve_entity(PROJ, "Vikram Shah")
    store.confirm_alias(PROJ, "boss", b.entity_id)
    store.reject_alias(PROJ, "boss", a.entity_id)
    with pytest.raises(ScopeError, match="settle it"):
        store.merge_entities(PROJ, a.entity_id, b.entity_id)
    with pytest.raises(ScopeError, match="itself"):
        store.merge_entities(PROJ, a.entity_id, a.entity_id)


def test_splits_are_last_in_first_out(store) -> None:
    a = store.resolve_entity(PROJ, "Vansh Sharma")
    b = store.resolve_entity(PROJ, "Vikram Shah")
    c = store.resolve_entity(PROJ, "Carol Danvers")
    before = _alias_rows(store, PROJ)
    store.merge_entities(PROJ, a.entity_id, b.entity_id)
    store.merge_entities(PROJ, a.entity_id, c.entity_id)
    with pytest.raises(ScopeError, match="split that one first"):
        store.split_entity(PROJ, b.entity_id)
    # A survivor that absorbed someone cannot itself be merged away yet.
    d = store.resolve_entity(PROJ, "Dan Brown")
    with pytest.raises(ScopeError, match="already absorbed"):
        store.merge_entities(PROJ, d.entity_id, a.entity_id)
    store.split_entity(PROJ, c.entity_id)
    store.split_entity(PROJ, b.entity_id)
    after = [r for r in _alias_rows(store, PROJ) if r[2] != d.entity_id]
    assert after == before


def test_merge_works_on_a_v3_file_written_before_the_merge_table(tmp_path: Path) -> None:
    db = tmp_path / "v050.db"
    MemoryStore(db).close()
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE entity_merges")               # as awm 0.5.0 wrote it
    con.commit()
    con.close()
    with MemoryStore(db) as st:
        a = st.resolve_entity(PROJ, "Vansh Sharma")
        b = st.resolve_entity(PROJ, "Vikram Shah")
        with pytest.raises(ScopeError, match="no merge"):
            st.split_entity(PROJ, b.entity_id)
        st.merge_entities(PROJ, a.entity_id, b.entity_id)
        st.split_entity(PROJ, b.entity_id)
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchone()[0] == 3
    con.close()


# ------------------------------------------------------------ surfaces
def test_cli_merge_and_split(tmp_path: Path, capsys) -> None:
    db = ["--db", str(tmp_path / "m.db")]
    sc = str(PROJ)
    assert main([*db, "entity", "resolve", "Vansh Sharma", "--scope", sc]) == 0
    keep = json.loads(capsys.readouterr().out)["entity_id"]
    assert main([*db, "entity", "resolve", "Vikram Shah", "--scope", sc]) == 0
    drop = json.loads(capsys.readouterr().out)["entity_id"]
    assert main([*db, "entity", "merge", str(keep), str(drop), "--scope", sc]) == 0
    assert json.loads(capsys.readouterr().out)["merged_id"] == drop
    assert main([*db, "entity", "split", str(drop), "--scope", sc]) == 0
    assert json.loads(capsys.readouterr().out)["restored"] == 1
    assert main([*db, "entity", "merge", str(keep), "--scope", sc]) == 2
    assert main([*db, "entity", "split", "notanid", "--scope", sc]) == 2
    assert main([*db, "entity", "merge", str(keep), str(drop)]) == 2  # no --scope


def test_mcp_merge_and_split(tmp_path: Path) -> None:
    srv = mcp_server.AwmMcp(tmp_path / "m.db", cwd=tmp_path, user="alice")

    def call(name, **args):
        res = srv.call(name, {"scope": str(PROJ), **args})
        return res["isError"], res["content"][0]["text"]

    keep = json.loads(call("awm_resolve_entity", mention="Vansh Sharma")[1])["entity_id"]
    drop = json.loads(call("awm_resolve_entity", mention="Vikram Shah")[1])["entity_id"]
    err, text = call("awm_merge_entities", keep=keep, drop=drop)
    assert not err and json.loads(text)["merged_id"] == drop
    err, text = call("awm_split_entity", merged_id=drop)
    assert not err and json.loads(text)["keep_id"] == keep
    err, text = call("awm_merge_entities", keep=keep, drop="x")
    assert err and "integer" in text
