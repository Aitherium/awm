"""Round-6 review regressions (awm): a stale compat handle after a peer's migrate,
killed-migration leftovers, purge leaks (digests, pending log), a descendant merge
vetoing its ancestor, the non-ASCII slug collision, an LLM rewriting another
subject's slot, the cross-prefix marginal, as_of parsing, and doctor's version line.

Each test failed before its fix (verified by reverting the fixed module).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from awm import MemoryStore, Scope, world
from awm.cli import check_ts, parse_as_of
from awm.pending import slug
from awm.reconcile import LLMReconciler, ReconcileError
from awm.scope import ScopeError

SC = Scope.parse("acme:alice:proj")
PKG = Path(__file__).resolve().parent.parent

_V1 = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact', created REAL NOT NULL,
    updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}', UNIQUE(scope, key));
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
"""


@pytest.fixture(autouse=True)
def _no_auto(monkeypatch):
    monkeypatch.delenv("AWM_AUTO_MIGRATE", raising=False)


def _v1(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    con.executescript(_V1)
    con.commit()
    con.close()
    return path


def _clocked(path: Path) -> MemoryStore:
    st = MemoryStore(path)
    t = [1000.0]

    def clk() -> float:
        t[0] += 1.0
        return t[0]
    st._clock = clk
    return st


# -- F1: a compat handle kept writing v1 rows into a file a peer migrated -------------
def test_compat_handle_leaves_compat_when_a_peer_migrates(tmp_path):
    db = _v1(tmp_path / "m.db")
    a = MemoryStore(db)                          # long-lived holder, opened on v1
    assert a.compat and a.file_version == 1
    a.remember(SC, "k", "OLD")
    peer = MemoryStore(db)
    peer.migrate(backup=False)                   # another process migrates the file
    peer.close()
    a.remember(SC, "k", "NEW")                   # must supersede the v3 way
    assert a.file_version == 3 and not a.compat
    a.close()
    with MemoryStore(db) as b:
        assert [h.value for h in b.history(SC, "k")] == ["OLD", "NEW"]
    con = sqlite3.connect(str(db))
    since = con.execute("SELECT since FROM memories WHERE key='k'").fetchone()[0]
    con.close()
    assert since is not None


def test_compat_handle_features_unlock_after_a_peer_migrates(tmp_path):
    db = _v1(tmp_path / "m.db")
    a = MemoryStore(db)
    peer = MemoryStore(db)
    peer.migrate(backup=False)
    peer.close()
    # reconcile needs v3: the stale handle must see the migration, not raise
    d = a.reconcile_and_remember(SC, "blue", subject="ui.color")
    assert d.action == "add"
    a.close()


# -- F2: a hard-killed migrate left a backup / .partial that nothing reaped -----------
def test_next_migrate_reaps_a_killed_migrations_leftovers(tmp_path):
    db = _v1(tmp_path / "m.db")
    with MemoryStore(db) as st:
        st.remember(SC, "k", "v")
    raw = db.read_bytes()
    # what a kill leaves: a full byte-identical backup, and a truncated .partial
    (tmp_path / "m.db.v1-backup-20200101T000000").write_bytes(raw)
    (tmp_path / "m.db.v1-backup-20200101T000001.partial").write_bytes(raw[: len(raw) // 2])
    # a backup of a DIFFERENT state is not a duplicate and must be kept
    (tmp_path / "m.db.v1-backup-20190101T000000").write_bytes(raw + b"x")
    with MemoryStore(db) as st:
        rep = st.migrate(backup=True)
    assert rep["migrated"]
    assert sorted(rep["reaped_backups"]) == ["m.db.v1-backup-20200101T000000",
                                             "m.db.v1-backup-20200101T000001.partial"]
    left = sorted(p.name for p in tmp_path.iterdir() if "backup" in p.name)
    assert left == sorted(["m.db.v1-backup-20190101T000000", Path(rep["backup"]).name])


# -- F3: purge left the value committed in transition digests -------------------------
def test_purge_removes_transitions_whose_digest_holds_the_value(tmp_path):
    st = _clocked(tmp_path / "m.db")
    st.remember(SC, "acct.pin", "4821")
    st.remember(SC, "acct.door", "closed")
    b = st.encode_state(SC, prefix="acct")
    st.remember(SC, "acct.door", "open")          # the step does NOT name the pin
    st.observe_transition(SC, b, "open_door", st.encode_state(SC, prefix="acct"))
    st.remember(SC, "acct.pin", "0000")           # rotate
    st.purge_history(SC, "acct.pin")
    for t in st.transitions(SC):                  # no digest may commit to 4821
        for guess_door in ("closed", "open"):
            slots = {"acct.door": guess_door, "acct.pin": "4821"}
            assert world.state_digest(slots) not in (t.state_digest, t.next_digest)
    old = {"acct.door": "closed", "acct.pin": "4821"}
    ws = world.WorldState(digest=world.state_digest(old), slots=old, scope=str(SC),
                          prefix="acct", as_of=None, ts=0.0)
    assert st.predict_outcome(SC, ws, "open_door").source != "RECALLED"
    st.close()


def test_purge_keeps_a_transition_that_only_saw_the_live_value(tmp_path):
    st = _clocked(tmp_path / "m.db")
    st.remember(SC, "acct.pin", "4821")
    st.remember(SC, "acct.pin", "0000")           # rotated before the step
    st.remember(SC, "acct.door", "closed")
    b = st.encode_state(SC, prefix="acct")
    st.remember(SC, "acct.door", "open")
    st.observe_transition(SC, b, "open_door", st.encode_state(SC, prefix="acct"))
    st.purge_history(SC, "acct.pin")
    assert len(st.transitions(SC)) == 1
    st.close()


# -- F4: purge left the value in entity_pending (surprise_log served it) --------------
def test_purge_removes_the_pending_row_that_carried_the_value(tmp_path):
    st = _clocked(tmp_path / "m.db")
    r0 = st.remember_about(SC, "Vansh Sharma", "Vansh lives at 12 Secret Lane, Delhi")
    st.remember_about(SC, "VS", "Vansh lives at 99 Hidden Rd, Pune")
    st.confirm_alias(SC, "VS", r0.entity_id)
    st.reconcile_and_remember(SC, "Vansh lives somewhere else now", subject=r0.subject)
    st.purge_history(SC, r0.subject)
    leaked = "99 Hidden Rd"
    assert not any(leaked in json.dumps(e.detail) for e in st.surprise_log(SC, 0.0))
    assert not any(leaked in p.fact for p in st.pending_updates(SC, status=None))
    st.close()


def test_purge_keeps_a_still_pending_fact(tmp_path):
    st = _clocked(tmp_path / "m.db")
    r0 = st.remember_about(SC, "Vansh Sharma", "Vansh lives in Delhi")
    st.remember_about(SC, "VS", "Vansh lives in Pune")      # POSSIBLE: held pending
    st.reconcile_and_remember(SC, "Vansh lives in Goa", subject=r0.subject)
    st.purge_history(SC, r0.subject)
    assert [p.fact for p in st.pending_updates(SC)] == ["Vansh lives in Pune"]
    st.close()


# -- F5: a descendant's merge INTO an org entity vetoed the org's own merge ------------
def test_descendant_merge_does_not_veto_the_ancestor(tmp_path):
    st = _clocked(tmp_path / "m.db")
    org, bob = Scope.parse("acme:*:*"), Scope.parse("acme:bob:*")
    a = st.resolve_entity(org, "Dana Whitfield").entity_id
    b = st.resolve_entity(org, "Priya Raman").entity_id
    x = st.resolve_entity(bob, "Zeb Quill").entity_id
    st.merge_entities(bob, keep=a, drop=x)
    st.merge_entities(org, keep=b, drop=a)                  # refused before the fix
    # bob can still split his own merge; his entity comes back
    st.split_entity(bob, x)
    assert any(e.id == x for e in st.entities(bob))
    # and the org's split restores its entity
    st.split_entity(org, a)
    assert any(e.id == a for e in st.entities(org))
    st.close()


def test_org_split_repoints_an_unsplit_descendant_merge(tmp_path):
    st = _clocked(tmp_path / "m.db")
    org, bob = Scope.parse("acme:*:*"), Scope.parse("acme:bob:*")
    a = st.resolve_entity(org, "Dana Whitfield").entity_id
    b = st.resolve_entity(org, "Priya Raman").entity_id
    x = st.resolve_entity(bob, "Zeb Quill").entity_id
    st.merge_entities(bob, keep=a, drop=x)
    st.merge_entities(org, keep=b, drop=a)
    st.split_entity(org, a)
    keep = st._db.execute("SELECT keep_id FROM entity_merges WHERE scope=? AND drop_id=?",
                          (str(bob), x)).fetchone()[0]
    assert keep == a
    st.split_entity(bob, x)
    assert any(e.id == x for e in st.entities(bob))
    st.close()


# -- F6: every non-ASCII name slugged to people.unknown --------------------------------
def test_slug_keeps_every_script_and_ascii_is_unchanged():
    assert slug("Vansh Sharma") == "vansh_sharma"
    assert slug("R2-D2") == "r2_d2"
    assert slug("李雷") != slug("王芳")
    assert slug("Zoë") != slug("Zo")
    assert slug("!!!") != slug("???") and slug("!!!") != "unknown"
    assert slug("") == "unknown"


def test_two_cjk_people_do_not_share_a_fact_slot(tmp_path):
    st = _clocked(tmp_path / "m.db")
    li = st.remember_about(SC, "李雷", "李雷 lives in Beijing")
    st.remember_about(SC, "王芳", "王芳 lives in Shanghai")
    cur = [m.value for m in st.recall_entity(SC, li.entity_id).current]
    assert cur == ["李雷 lives in Beijing"]
    st.close()


# -- F7: an LLM decision rewrote a slot reconciled under ANOTHER subject ---------------
def test_llm_cannot_rewrite_a_slot_owned_by_another_subject(tmp_path):
    st = MemoryStore(tmp_path / "m.db")
    st.reconcile_and_remember(SC, "alice", subject="deploy.approver")
    st.reconcile_and_remember(SC, "green", subject="deploy")
    planted = json.dumps({"action": "update", "key": "deploy.approver",
                          "value": "mallory", "reason": "x"})
    with pytest.raises(ReconcileError, match="reconciled under subject"):
        st.reconcile_and_remember(SC, "rolled back", subject="deploy",
                                  reconciler=LLMReconciler(lambda _p: planted))
    assert [m.value for m in st.recall(SC) if m.key == "deploy.approver"] == ["alice"]
    st.close()


def test_llm_may_still_update_a_sub_slot_of_its_own_subject(tmp_path):
    st = MemoryStore(tmp_path / "m.db")
    add = json.dumps({"action": "add", "key": "user.ui_theme", "value": "dark",
                      "reason": "r"})
    st.reconcile_and_remember(SC, "I like dark mode", subject="user",
                              reconciler=LLMReconciler(lambda _p: add))
    upd = json.dumps({"action": "update", "key": "user.ui_theme", "value": "light",
                      "reason": "r"})
    d = st.reconcile_and_remember(SC, "switched to light", subject="user",
                                  reconciler=LLMReconciler(lambda _p: upd))
    assert d.action == "update"
    assert [h.value for h in st.history(SC, "user.ui_theme")] == ["dark", "light"]
    st.close()


# -- F10: the GENERALIZED marginal crossed prefixes -----------------------------------
def test_marginal_does_not_cross_prefixes(tmp_path):
    st = _clocked(tmp_path / "m.db")
    st.remember(SC, "world.b.n", "0")
    b0 = st.encode_state(SC, prefix="world.b")
    st.remember(SC, "world.b.n", "1")
    st.observe_transition(SC, b0, "inc", st.encode_state(SC, prefix="world.b"))
    st.remember(SC, "world.a.n", "5")
    a0 = st.encode_state(SC, prefix="world.a")
    p = st.predict_outcome(SC, a0, "inc")
    assert p.source == "NONE"
    st.remember(SC, "world.a.n", "6")
    t = st.observe_transition(SC, a0, "inc", st.encode_state(SC, prefix="world.a"))
    assert t.surprise is None                     # no expectation, nothing violated
    st.close()


def test_foreign_noop_is_not_this_prefixes_marginal(tmp_path):
    st = _clocked(tmp_path / "m.db")
    for i in range(3):                            # world.b: 'tick' does nothing
        st.remember(SC, "world.b.n", str(i))
        s = st.encode_state(SC, prefix="world.b")
        st.observe_transition(SC, s, "tick", st.encode_state(SC, prefix="world.b"))
    st.remember(SC, "world.a.n", "0")
    p = st.predict_outcome(SC, st.encode_state(SC, prefix="world.a"), "tick")
    assert p.source == "NONE"
    # its OWN no-op, from a state its own rows reach, still counts
    a0 = st.encode_state(SC, prefix="world.a")
    st.remember(SC, "world.a.n", "1")
    a1 = st.encode_state(SC, prefix="world.a")
    st.observe_transition(SC, a0, "inc", a1)
    st.observe_transition(SC, a1, "tick", st.encode_state(SC, prefix="world.a"))
    st.remember(SC, "world.a.n", "7")
    p = st.predict_outcome(SC, st.encode_state(SC, prefix="world.a"), "tick")
    assert p.source == "GENERALIZED" and p.support == 1
    st.close()


# -- F16/F17: as_of accepted nan/inf, and differed between 3.10 and 3.12 ---------------
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e400", "NaN", "-5",
                                 "20991231", "2026-W39-1", "2026-13-01", "9999999999999"])
def test_parse_as_of_refuses(bad):
    with pytest.raises(ScopeError):
        parse_as_of(bad)


@pytest.mark.parametrize("text,want", [
    ("2026-09-28T10:00:00.5Z", 1790589600.5),
    ("2026-09-28T10:00:00+0000", 1790589600.0),
    ("2026-09-28T10:00:00+00:00", 1790589600.0),
    ("2026-09-28T12:00:00.25+02:00", 1790589600.25),
    ("1790589600", 1790589600.0),
    ("1790589600.5", 1790589600.5),
])
def test_parse_as_of_is_one_grammar_on_every_version(text, want):
    assert parse_as_of(text) == want


def test_check_ts_refuses_non_finite_numbers():
    for bad in (float("nan"), float("inf"), -1.0, True):
        with pytest.raises(ScopeError):
            check_ts(bad)
    assert check_ts(5) == 5.0


def test_mcp_as_of_refuses_nan_as_number_and_text():
    from awm.mcp_server import AwmMcp, ToolError
    for raw in (float("nan"), float("inf"), "nan", "inf"):
        with pytest.raises(ToolError):
            AwmMcp._as_of({"as_of": raw})
    assert AwmMcp._as_of({"as_of": 5}) == 5.0


def test_parse_as_of_same_answers_on_python310():
    """The grammar is the code's, not datetime's: 3.10 must agree with this interpreter."""
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); from awm.cli import parse_as_of\n"
             "out = []\n"
             "for t in sys.argv[2:]:\n"
             "    try: out.append(repr(parse_as_of(t)))\n"
             "    except Exception as e: out.append(type(e).__name__)\n"
             "print('|'.join(out))")
    cases = ["2026-09-28T10:00:00.5Z", "2026-09-28T10:00:00+0000", "2026-W39-1", "20260928"]
    try:
        r310 = subprocess.run(["py", "-3.10", "-c", probe, str(PKG), *cases],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("no Python 3.10 launcher here")
    if r310.returncode != 0:
        pytest.skip(f"Python 3.10 not usable: {r310.stderr[-200:]}")
    here = subprocess.run([sys.executable, "-c", probe, str(PKG), *cases],
                          capture_output=True, text=True, timeout=60)
    assert r310.stdout.strip() == here.stdout.strip()


# -- F19: doctor printed the installed dist's version as if it were the running code --
def test_doctor_reports_the_running_code_version(tmp_path, monkeypatch):
    import awm
    from awm import doctor_local
    monkeypatch.setattr(doctor_local, "DB_PATH", tmp_path / "none.db")
    lines = doctor_local._doctor_local()
    code = [ln for ln in lines if ln.startswith("code ")]
    assert code and f"awm {awm.__version__} at" in code[0]
    assert str(Path(awm.__file__).resolve().parent) in code[0]


def test_doctor_names_a_dist_mismatch(tmp_path, monkeypatch):
    import importlib.metadata as md
    from awm import doctor_local
    monkeypatch.setattr(doctor_local, "DB_PATH", tmp_path / "none.db")
    monkeypatch.setattr(md, "version", lambda _n: "0.0.1-other")
    out = io.StringIO()
    with redirect_stdout(out):
        lines = doctor_local._doctor_local()
    assert any(ln.startswith("dist ") and "MISMATCH" in ln and "0.0.1-other" in ln
               for ln in lines)
    assert os.environ.get("AWM_AUTO_MIGRATE") is None
