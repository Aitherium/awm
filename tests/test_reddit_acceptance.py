"""The public test every agent-memory SDK failed, run against awm across real sessions.

1. Contradiction across sessions: session 1 "I prefer dark mode", session 5 "I
   switched to light mode", session 8 "which mode do I use?" -> ONE answer, light,
   with dark still visible as history.
2. Entity resolution: "vansh", "vansh from india", "VS" -> the first two are one
   person; "VS" is flagged as possibly him, never silently merged.

Each session opens and closes its OWN MemoryStore on the same file: state that only
survives inside one process object is not memory.
"""

from __future__ import annotations

from pathlib import Path

from awm.scope import Scope
from awm.store import MemoryStore

USER = Scope("acme", "vansh", "chat")
SUBJECT = "user.ui_theme"


def _session(db: Path, at: float) -> MemoryStore:
    st = MemoryStore(db)
    st._clock = lambda: at
    return st


def test_contradiction_across_sessions_resolves_to_the_latest(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with _session(db, 1_000.0) as s1:
        d = s1.reconcile_and_remember(USER, "prefers dark mode", subject=SUBJECT)
        assert d.action == "add"
    with _session(db, 5_000.0) as s5:
        d = s5.reconcile_and_remember(USER, "switched to light mode", subject=SUBJECT)
        assert d.action == "update"
    with _session(db, 8_000.0) as s8:
        now = s8.recall(USER, query="mode")
        assert [(m.key, m.value) for m in now] == [(SUBJECT, "switched to light mode")]

        hist = s8.history(USER, SUBJECT)
        assert [h.value for h in hist] == ["prefers dark mode", "switched to light mode"]
        assert hist[0].valid_to == 5_000.0 and hist[0].reason == "superseded"
        assert hist[1].valid_to is None

        then = s8.recall(USER, as_of=3_000.0)
        assert [m.value for m in then] == ["prefers dark mode"]


def test_entity_resolution_across_sessions(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    with _session(db, 1_000.0) as s1:
        vansh = s1.resolve_entity(USER, "vansh")
        assert vansh.created_new
    with _session(db, 5_000.0) as s5:
        r = s5.resolve_entity(USER, "vansh from india")
        assert (r.entity_id, r.status, r.created_new) == (vansh.entity_id, "confirmed", False)
    with _session(db, 8_000.0) as s8:
        vs = s8.resolve_entity(USER, "VS")
        assert vs.status == "possible" and vs.entity_id is None
        assert vansh.entity_id in vs.possible
        assert len(s8.entities(USER)) == 1          # nothing new, nothing merged
    with _session(db, 9_000.0) as s9:
        s9.confirm_alias(USER, "VS", vansh.entity_id)
    with _session(db, 10_000.0) as s10:
        vs = s10.resolve_entity(USER, "VS")
        assert (vs.entity_id, vs.status) == (vansh.entity_id, "confirmed")


def test_a_sibling_never_sees_history_as_of_or_entities(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    other = Scope("acme", "priya", "chat")
    prefix_tenant = Scope("acmecorp", "vansh", "chat")
    with _session(db, 1_000.0) as s:
        s.reconcile_and_remember(USER, "prefers dark mode", subject=SUBJECT)
        s.resolve_entity(USER, "vansh")
    with _session(db, 5_000.0) as s:
        s.reconcile_and_remember(USER, "switched to light mode", subject=SUBJECT)
    with _session(db, 8_000.0) as s:
        for spy in (other, prefix_tenant, Scope("acme", "vansh", "work")):
            assert s.history(spy, SUBJECT) == []
            assert s.recall(spy, as_of=3_000.0) == []
            assert s.recall(spy) == []
            assert s.entities(spy) == []
            r = s.resolve_entity(spy, "VS")
            assert r.created_new and r.status == "confirmed"
