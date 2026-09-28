"""Write-time reconcile: the decision is validated before it touches the store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest
from awm.reconcile import (
    Decision,
    LLMReconciler,
    ReconcileError,
    Reconciler,
    SlotReconciler,
    validate,
)
from awm.scope import Scope, ScopeError
from awm.store import Memory, MemoryStore

ALICE = Scope("acme", "alice")


@pytest.fixture()
def store(tmp_path: Path):
    with MemoryStore(tmp_path / "mem.db") as s:
        yield s


class Fixed:
    """A reconciler that answers whatever it is told to, and records what it saw."""

    def __init__(self, decision: Decision):
        self.decision = decision
        self.seen: List[Memory] = []

    def decide(self, new_fact: str, candidates: List[Memory]) -> Decision:
        self.seen = list(candidates)
        return self.decision


def _mem(key: str, value: str) -> Memory:
    return Memory("acme:alice:*", key, value, "fact", 0.0, 0.0, 0, {})


def test_shipped_reconcilers_satisfy_the_protocol() -> None:
    assert isinstance(SlotReconciler(), Reconciler)
    assert isinstance(LLMReconciler(lambda p: ""), Reconciler)
    assert isinstance(Fixed(Decision("ignore", "k", "", "")), Reconciler)
    assert not isinstance(object(), Reconciler)


# ------------------------------------------------------------ SlotReconciler
def test_slot_adds_when_nothing_is_held(store) -> None:
    d = store.reconcile_and_remember(ALICE, "dark mode", subject="user.ui_theme")
    assert (d.action, d.key) == ("add", "user.ui_theme")
    m = store.recall(ALICE)[0]
    assert (m.key, m.value, m.meta["subject"]) == ("user.ui_theme", "dark mode",
                                                   "user.ui_theme")


def test_slot_updates_and_history_keeps_the_old(store) -> None:
    store.reconcile_and_remember(ALICE, "dark mode", subject="user.ui_theme")
    d = store.reconcile_and_remember(ALICE, "light mode", subject="user.ui_theme")
    assert d.action == "update"
    assert [m.value for m in store.recall(ALICE)] == ["light mode"]
    assert [h.value for h in store.history(ALICE, "user.ui_theme")] == ["dark mode",
                                                                        "light mode"]


def test_slot_ignores_an_identical_value(store) -> None:
    store.reconcile_and_remember(ALICE, "dark mode", subject="user.ui_theme")
    d = store.reconcile_and_remember(ALICE, "  dark mode ", subject="user.ui_theme")
    assert d.action == "ignore"
    assert len(store.history(ALICE, "user.ui_theme")) == 1


def test_slot_without_a_subject_refuses() -> None:
    with pytest.raises(ReconcileError):
        SlotReconciler().decide("x", [])


# ------------------------------------------------------------ candidates
def test_candidates_are_the_subject_and_its_dotted_children_at_the_exact_scope(store) -> None:
    store.remember(ALICE, "user.ui_theme", "dark")
    store.remember(ALICE, "user.ui_theme.contrast", "high")
    store.remember(ALICE, "user.ui_themes", "not a child")      # prefix, not a child
    store.remember(ALICE, "user_ui_theme", "LIKE-trap")          # `_` wildcard trap
    store.remember(Scope("acme"), "user.ui_theme", "ancestor")  # not the write scope
    store.remember(Scope("acme", "bob"), "user.ui_theme", "sibling")
    rec = Fixed(Decision("ignore", "user.ui_theme", "", "test"))
    store.reconcile_and_remember(ALICE, "x", subject="user.ui_theme", reconciler=rec)
    assert [(m.key, m.value) for m in rec.seen] == [
        ("user.ui_theme", "dark"), ("user.ui_theme.contrast", "high")]


# ------------------------------------------------------------ validation
@pytest.mark.parametrize("decision,msg", [
    (Decision("merge", "s", "v", ""), "unknown action"),
    (Decision("add", "", "v", ""), "non-empty"),
    (Decision("update", "s.other", "v", ""), "not a candidate"),
    (Decision("add", "s", "v", ""), "use update"),
    (Decision("add", "elsewhere", "v", ""), "outside subject"),
    (Decision("add", "sx", "v", ""), "outside subject"),
    (Decision("update", "s", "  ", ""), "non-empty value"),
])
def test_validate_refuses_unsafe_decisions(decision, msg) -> None:
    with pytest.raises(ReconcileError, match=msg):
        validate(decision, [_mem("s", "old")], "s")


def test_validate_refuses_a_non_decision() -> None:
    with pytest.raises(ReconcileError):
        validate({"action": "add"}, [], "s")


def test_a_refused_decision_writes_nothing(store) -> None:
    rec = Fixed(Decision("update", "user.name", "Mallory", "model said so"))
    with pytest.raises(ReconcileError):
        store.reconcile_and_remember(ALICE, "Mallory", subject="user.name", reconciler=rec)
    assert store.count() == 0


def test_add_at_a_dotted_child_key(store) -> None:
    store.remember(ALICE, "user.ui_theme", "dark")
    rec = Fixed(Decision("add", "user.ui_theme.contrast", "high", "new attribute"))
    store.reconcile_and_remember(ALICE, "high contrast", subject="user.ui_theme",
                                 reconciler=rec)
    assert {m.key for m in store.recall(ALICE)} == {"user.ui_theme", "user.ui_theme.contrast"}


def test_reconcile_refuses_bad_inputs(store) -> None:
    with pytest.raises(ScopeError):
        store.reconcile_and_remember(ALICE, "x", subject=" ")
    with pytest.raises(ScopeError):
        store.reconcile_and_remember(ALICE, " ", subject="s")
    with pytest.raises(ScopeError):
        store.reconcile_and_remember("acme:*:*", "x", subject="s")  # type: ignore[arg-type]
    with pytest.raises(ReconcileError):
        store.reconcile_and_remember(ALICE, "x", subject="s", reconciler=object())


# ------------------------------------------------------------ LLMReconciler
def _reply(**kw) -> str:
    base = {"action": "update", "key": "user.ui_theme", "value": "light mode",
            "reason": "the user switched"}
    base.update(kw)
    return json.dumps(base)


def test_llm_reconciler_prompt_lists_candidates_and_applies_a_valid_reply(store) -> None:
    # Seeded through a reconcile: an update may only supersede a value the
    # reconcile path owns (a key-written one needs overwrite=True).
    store.reconcile_and_remember(ALICE, "dark mode", subject="user.ui_theme")
    prompts: List[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return _reply()

    d = store.reconcile_and_remember(ALICE, "I switched to light mode",
                                     subject="user.ui_theme",
                                     reconciler=LLMReconciler(complete))
    assert d.action == "update"
    assert "dark mode" in prompts[0] and "I switched to light mode" in prompts[0]
    assert "'user.ui_theme.'" in prompts[0]
    assert [m.value for m in store.recall(ALICE)] == ["light mode"]


@pytest.mark.parametrize("reply", [
    "sure! " + _reply(),                           # prose around the JSON
    "```json\n" + _reply() + "\n```",              # a fence is interpretation
    "[]",
    json.dumps({"action": "update", "key": "k", "value": "v"}),         # missing reason
    json.dumps({"action": "update", "key": "k", "value": "v", "reason": "r", "x": 1}),
    json.dumps({"action": "update", "key": 3, "value": "v", "reason": "r"}),
    "",
])
def test_llm_reconciler_never_guesses_a_malformed_reply(reply) -> None:
    with pytest.raises(ReconcileError):
        LLMReconciler(lambda p: reply, subject="user.ui_theme").decide("x", [])


def test_llm_reconciler_refuses_a_non_text_reply() -> None:
    with pytest.raises(ReconcileError):
        LLMReconciler(lambda p: None, subject="s").decide("x", [])  # type: ignore[arg-type]


def test_llm_reconciler_needs_a_callable() -> None:
    with pytest.raises(TypeError):
        LLMReconciler("not callable")  # type: ignore[arg-type]
