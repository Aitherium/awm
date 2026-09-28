"""A fact about an AMBIGUOUS mention never silently diverges (live-eval defect 2026-09-27).

"VS relocated to Pune" was split off to a new entity "VS" and "Which city does Vansh
live in?" kept answering Delhi. Now: nothing is written to any entity; the candidate's
recall carries its current value PLUS a flagged pending update, surprise_log reports an
``ambiguous_entity`` event, and confirm_alias applies it through reconcile (history kept).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from awm import KIND_AMBIGUOUS, MemoryStore, Scope

SC = Scope.parse("evalco:dana:people")


@pytest.fixture()
def st(tmp_path: Path):
    t = [1_000.0]

    def clock() -> float:
        t[0] += 1.0
        return t[0]

    s = MemoryStore(tmp_path / "m.db")
    s._clock = clock
    yield s
    s.close()


def test_a_possible_link_holds_the_fact_pending_on_the_candidate(st: MemoryStore) -> None:
    vansh = st.remember_about(SC, "Vansh", "Vansh lives in Delhi")
    assert vansh.status == "confirmed" and vansh.subject == "people.vansh"
    before = {e.id for e in st.entities(SC)}

    held = st.remember_about(SC, "VS", "VS just told me he relocated to Pune")

    assert held.status == "possible" and held.candidates == [vansh.entity_id]
    assert held.decision is None and held.subject is None
    # nothing diverged: no new entity, no row under any other subject
    assert {e.id for e in st.entities(SC)} == before
    assert [(m.key, m.value) for m in st.recall(SC)] == [("people.vansh",
                                                         "Vansh lives in Delhi")]
    view = st.recall_entity(SC, vansh.entity_id)
    assert [m.value for m in view.current] == ["Vansh lives in Delhi"]
    assert [p.fact for p in view.pending] == ["VS just told me he relocated to Pune"]
    assert view.pending[0].flag.startswith("UNCONFIRMED: 'VS' may be this entity")
    events = [e for e in st.surprise_log(SC, 0.0) if e.kind == KIND_AMBIGUOUS]
    assert len(events) == 1 and events[0].key == "people.vansh"
    assert events[0].detail["mention"] == "VS" and events[0].score == 1.0


def test_confirm_alias_applies_the_pending_fact_through_reconcile_with_history(
        st: MemoryStore) -> None:
    vansh = st.remember_about(SC, "Vansh", "Vansh lives in Delhi")
    st.remember_about(SC, "VS", "VS just told me he relocated to Pune")

    res = st.confirm_alias(SC, "VS", vansh.entity_id)

    assert [a["decision"]["action"] for a in res.applied] == ["update"]
    view = st.recall_entity(SC, vansh.entity_id)
    assert [m.value for m in view.current] == ["VS just told me he relocated to Pune"]
    assert view.pending == []
    assert [h.value for h in st.history(SC, "people.vansh")] == [
        "Vansh lives in Delhi", "VS just told me he relocated to Pune"]
    assert [p.status for p in st.pending_updates(SC, status=None)] == ["applied"]
    # the alias now resolves: the next fact about "VS" is written directly
    again = st.remember_about(SC, "VS", "VS moved again, to Mumbai")
    assert again.status == "confirmed" and again.entity_id == vansh.entity_id


def test_two_candidates_confirming_one_drops_the_other(st: MemoryStore) -> None:
    a = st.remember_about(SC, "Vansh Sharma", "lives in Delhi")
    b = st.remember_about(SC, "Vikram Singh", "lives in Goa")
    held = st.remember_about(SC, "VS", "relocated to Pune")
    assert held.status == "possible" and set(held.candidates) == {a.entity_id, b.entity_id}
    assert len(st.recall_entity(SC, a.entity_id).pending) == 1
    assert len(st.recall_entity(SC, b.entity_id).pending) == 1

    st.confirm_alias(SC, "VS", b.entity_id)

    assert st.recall_entity(SC, a.entity_id).pending == []
    assert [m.value for m in st.recall_entity(SC, a.entity_id).current] == ["lives in Delhi"]
    assert [m.value for m in st.recall_entity(SC, b.entity_id).current] == [
        "relocated to Pune"]
    statuses = sorted((p.entity_id == a.entity_id, p.status)
                      for p in st.pending_updates(SC, status=None))
    assert statuses == [(False, "applied"), (True, "dropped")]


def test_rejecting_every_candidate_orphans_the_fact_and_writes_nothing(
        st: MemoryStore) -> None:
    vansh = st.remember_about(SC, "Vansh", "Vansh lives in Delhi")
    st.remember_about(SC, "VS", "VS relocated to Pune")

    assert st.reject_alias(SC, "VS", vansh.entity_id) is True

    assert st.recall_entity(SC, vansh.entity_id).pending == []
    assert [m.value for m in st.recall(SC)] == ["Vansh lives in Delhi"]
    assert [p.status for p in st.pending_updates(SC, status=None)] == ["orphaned"]


def test_the_split_off_pipeline_the_eval_used_is_what_diverges(st: MemoryStore) -> None:
    """The contrast, pinned: reject + resolve-as-new writes Pune to a SECOND entity."""
    vansh = st.remember_about(SC, "Vansh", "Vansh lives in Delhi")
    res = st.resolve_entity(SC, "VS")
    for i in res.possible:
        st.reject_alias(SC, "VS", i)
    split = st.remember_about(SC, "VS", "VS relocated to Pune")
    assert split.status == "confirmed" and split.entity_id != vansh.entity_id
    assert [m.value for m in st.recall_entity(SC, vansh.entity_id).current] == [
        "Vansh lives in Delhi"]
