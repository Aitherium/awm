"""Entity resolution: certain links confirm, plausible ones only propose, siblings never see."""

from __future__ import annotations

from pathlib import Path

import pytest
from awm.entities import abbreviation_of, compact, head_match, normalize
from awm.scope import Scope, ScopeError
from awm.store import MemoryStore

ALICE = Scope("acme", "alice")
PROJ = Scope("acme", "alice", "proj")


@pytest.fixture()
def store(tmp_path: Path):
    with MemoryStore(tmp_path / "mem.db") as s:
        yield s


# ------------------------------------------------------------ pure matching
@pytest.mark.parametrize("raw,norm", [
    ("  Vansh  ", "vansh"),
    ("VANSH, from India!", "vansh from india"),
    ("V.S.", "v s"),
    ("Straße", "strasse"),
    ("?!", ""),
])
def test_normalize(raw, norm) -> None:
    assert normalize(raw) == norm


def test_normalize_refuses_a_non_string() -> None:
    with pytest.raises(ScopeError):
        normalize(None)  # type: ignore[arg-type]


def test_compact_joins_runs_of_initials_only() -> None:
    assert compact("v s") == "vs"
    assert compact("vansh s") == "vansh s"


@pytest.mark.parametrize("norm,raw,known,want", [
    ("vansh from india", "vansh from india", "vansh", (True, "from india")),
    ("vansh the designer", "Vansh, the designer", "vansh", (True, "the designer")),
    ("vansh india", "Vansh (India)", "vansh", (True, "india")),
    ("vansh sharma", "vansh sharma", "vansh", (False, "sharma")),
    ("vansh", "vansh", "vansh", None),
    ("vanshika", "vanshika", "vansh", None),       # token-wise, never a string prefix
    ("dev vansh", "dev vansh", "vansh", None),
])
def test_head_match(norm, raw, known, want) -> None:
    assert head_match(norm, raw, known) == want


@pytest.mark.parametrize("m,known,hit", [
    ("vs", "vansh sharma", "initials"),
    ("vs", "vansh", "names not on record"),
    ("vsh", "vansh", "abbreviation"),
    ("vansh", "vansh sharma", "first name"),
    ("vs", "vikram shah", "initials"),
    ("xs", "vansh", None),
    ("sv", "vansh sharma", None),
    ("vanshik", "vansh", None),       # too long to be an abbreviation
    ("v", "vansh", None),             # one letter is not evidence
    ("vansh", "vansh", None),
])
def test_abbreviation_of(m, known, hit) -> None:
    got = abbreviation_of(m, known)
    assert (got is None) if hit is None else (hit in got)


# ------------------------------------------------------------ resolve
def test_first_mention_creates_an_entity_and_repeat_finds_it(store) -> None:
    r1 = store.resolve_entity(ALICE, "Vansh")
    assert (r1.status, r1.created_new, r1.canonical) == ("confirmed", True, "Vansh")
    r2 = store.resolve_entity(ALICE, "  VANSH ")
    assert (r2.entity_id, r2.created_new) == (r1.entity_id, False)


def test_head_with_a_qualifier_is_confirmed_with_evidence(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    r = store.resolve_entity(ALICE, "vansh from india")
    assert (r.entity_id, r.status, r.created_new) == (v.entity_id, "confirmed", False)
    [ent] = store.entities(ALICE)
    assert {"alias": "vansh from india", "status": "confirmed", "scope": str(ALICE),
            "evidence": "from india"} in ent.aliases


def test_head_without_a_qualifier_is_only_possible(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    r = store.resolve_entity(ALICE, "vansh sharma")
    assert (r.status, r.entity_id, r.possible) == ("possible", None, [v.entity_id])
    assert len(store.entities(ALICE)) == 1


def test_initials_are_possible_for_every_plausible_entity_and_merge_nothing(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    k = store.resolve_entity(ALICE, "Vikram Shah")
    store.resolve_entity(ALICE, "Priya")
    r = store.resolve_entity(ALICE, "VS")
    assert r.status == "possible" and r.entity_id is None and not r.created_new
    assert set(r.possible) == {v.entity_id, k.entity_id}
    assert len(store.entities(ALICE)) == 3
    # Asking again gives the same answer and does not duplicate links.
    again = store.resolve_entity(ALICE, "v.s.")
    assert set(again.possible) == set(r.possible)
    vs_links = [a for e in store.entities(ALICE) for a in e.aliases if a["alias"] == "vs"]
    assert len(vs_links) == 2


def test_confirm_promotes_and_drops_rivals(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    k = store.resolve_entity(ALICE, "Vikram Shah")
    store.resolve_entity(ALICE, "VS")
    c = store.confirm_alias(ALICE, "VS", k.entity_id)
    assert (c.entity_id, c.status) == (k.entity_id, "confirmed")
    r = store.resolve_entity(ALICE, "vs")
    assert (r.entity_id, r.status) == (k.entity_id, "confirmed")
    vs_links = [(e.id, a["status"]) for e in store.entities(ALICE) for a in e.aliases
                if a["alias"] == "vs"]
    assert vs_links == [(k.entity_id, "confirmed")]
    assert v.entity_id != k.entity_id


def test_reject_is_remembered_so_the_link_is_not_reproposed(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    k = store.resolve_entity(ALICE, "Vikram Shah")
    store.resolve_entity(ALICE, "VS")
    assert store.reject_alias(ALICE, "VS", v.entity_id) is True
    r = store.resolve_entity(ALICE, "VS")
    assert (r.status, r.possible) == ("possible", [k.entity_id])
    assert store.reject_alias(ALICE, "VS", k.entity_id) is True
    # Every plausible link refused: VS is somebody new.
    new = store.resolve_entity(ALICE, "VS")
    assert new.created_new and new.status == "confirmed"
    assert new.entity_id not in (v.entity_id, k.entity_id)


def test_reject_of_an_unknown_link_returns_false(store) -> None:
    v = store.resolve_entity(ALICE, "vansh")
    assert store.reject_alias(ALICE, "zed", v.entity_id) is False


def test_me_is_the_scope_owner_across_projects(store) -> None:
    a = store.resolve_entity(PROJ, "me")
    assert (a.canonical, a.status, a.created_new) == ("alice", "confirmed", True)
    b = store.resolve_entity(Scope("acme", "alice", "other"), "Myself")
    c = store.resolve_entity(ALICE, "I")
    assert a.entity_id == b.entity_id == c.entity_id
    # The owner's name resolves to the owner, not to a new "alice".
    assert store.resolve_entity(PROJ, "Alice").entity_id == a.entity_id
    [owner] = store.entities(ALICE)
    assert owner.scope == str(ALICE)


def test_me_without_a_named_user_is_refused(store) -> None:
    for sc in (Scope("acme"), Scope("platform")):
        with pytest.raises(ScopeError):
            store.resolve_entity(sc, "me")


def test_a_mention_with_no_name_is_refused(store) -> None:
    with pytest.raises(ScopeError):
        store.resolve_entity(ALICE, " ?! ")
    with pytest.raises(ScopeError):
        store.resolve_entity("acme:alice:*", "vansh")  # type: ignore[arg-type]


def test_ancestor_entities_are_visible_and_links_land_at_the_callers_scope(store) -> None:
    v = store.resolve_entity(Scope("acme"), "vansh")
    r = store.resolve_entity(PROJ, "vansh from india")
    assert (r.entity_id, r.status) == (v.entity_id, "confirmed")
    # The tenant does not see the project's alias: a write lands at one scope.
    tenant_aliases = [a["alias"] for e in store.entities(Scope("acme")) for a in e.aliases]
    assert tenant_aliases == ["vansh"]
    assert "vansh from india" in [a["alias"] for e in store.entities(PROJ)
                                  for a in e.aliases]


def test_sibling_entities_never_resolve_or_list(store) -> None:
    bob = Scope("acme", "bob")
    b = store.resolve_entity(bob, "vansh")
    store.resolve_entity(Scope("acmecorp"), "vansh")
    r = store.resolve_entity(ALICE, "vansh")
    assert r.created_new and r.entity_id != b.entity_id
    assert store.resolve_entity(Scope("acme", "carol"), "VS").status == "confirmed"
    assert [e.id for e in store.entities(Scope("acme", "dave"))] == []
    assert [e.id for e in store.entities(ALICE)] == [r.entity_id]


def test_confirm_and_reject_refuse_an_invisible_entity(store) -> None:
    b = store.resolve_entity(Scope("acme", "bob"), "vansh")
    with pytest.raises(ScopeError, match="no entity"):
        store.confirm_alias(ALICE, "vs", b.entity_id)
    with pytest.raises(ScopeError, match="no entity"):
        store.reject_alias(ALICE, "vs", b.entity_id)
    with pytest.raises(ScopeError, match="no entity"):
        store.confirm_alias(ALICE, "vs", 999)
