"""The dynamics layer (schema v3): encode, predict (tabular first), surprise, migration."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import List, Optional

import pytest
from awm import world
from awm.cli import main
from awm.scope import Scope
from awm.store import SCHEMA_VERSION, MemoryStore
from awm.world import (
    GENERALIZED,
    NONE,
    PREDICTED,
    RECALLED,
    WorldError,
    state_digest,
    surprise_score,
)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def S(text: str) -> Scope:
    return Scope.parse(text)


SC = S("acme:alice:grid")


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: Clock):
    with MemoryStore(tmp_path / "mem.db") as s:
        s._clock = clock
        yield s


# ------------------------------------------------------------ a toy grid world
MOVES = {"right": ("grid.x", 1), "up": ("grid.y", 1)}


def act(st: MemoryStore, clock: Clock, sc: Scope, action: str, *,
        teleport_mid: Optional[tuple] = None):
    """Execute one action as a real step: encode, write, encode, observe."""
    before = st.encode_state(sc, prefix="grid")
    clock.t += 1
    if action == "reset":
        st.remember(sc, "grid.x", "0")
        st.remember(sc, "grid.y", "0")
    else:
        key, d = MOVES[action]
        st.remember(sc, key, str(int(before.slots[key]) + d))
    if teleport_mid is not None:
        st.remember(sc, *teleport_mid)
    clock.t += 1
    after = st.encode_state(sc, prefix="grid")
    return st.observe_transition(sc, before, action, after)


def episode(st, clock, sc, seq: List[str], **kw):
    out = [act(st, clock, sc, "reset")]
    for i, a in enumerate(seq):
        out.append(act(st, clock, sc, a,
                       teleport_mid=kw.get("mid") if kw.get("mid_at") == i else None))
        if kw.get("between_at") == i:
            clock.t += 1
            st.remember(sc, *kw["between"])
    return out


# ------------------------------------------------------------ encode
def test_encode_state_is_stable_nearest_wins_and_prefix_is_segmentwise(store) -> None:
    store.remember(S("acme:alice:*"), "user.theme", "dark")
    store.remember(SC, "user.theme", "light")          # nearer: wins
    store.remember(SC, "username", "alice")            # NOT under prefix "user"
    store.remember(S("acme:bob:*"), "user.lang", "fr")  # sibling: invisible
    store.remember(S("acmecorp:*:*"), "user.x", "leak")  # prefix trap: invisible
    ws = store.encode_state(SC, prefix="user")
    assert ws.slots == {"user.theme": "light"}
    assert ws.digest == state_digest({"user.theme": "light"})
    assert store.encode_state(SC, prefix="user").digest == ws.digest
    assert set(store.encode_state(SC).slots) == {"user.theme", "username"}


def test_encode_state_as_of_is_the_past_state(store, clock) -> None:
    store.remember(SC, "grid.x", "0")
    clock.t = 2000.0
    store.remember(SC, "grid.x", "1")
    assert store.encode_state(SC, as_of=1500.0).slots == {"grid.x": "0"}
    assert store.encode_state(SC).slots == {"grid.x": "1"}
    assert store.encode_state(SC, as_of=1500.0).ts == 1500.0


# ------------------------------------------------------------ predict
def test_fresh_store_predicts_none_not_a_guess(store) -> None:
    p = store.predict_outcome(SC, store.encode_state(SC), "right")
    assert (p.source, p.delta, p.confidence, p.support, p.known) == (NONE, {}, 0.0, 0, False)


def test_observed_step_is_recalled_next_time(store, clock) -> None:
    t0 = act(store, clock, SC, "reset")
    assert t0.surprise is None  # novel: nothing was expected, so nothing violated
    t1 = act(store, clock, SC, "right")
    act(store, clock, SC, "reset")
    s = store.encode_state(SC, prefix="grid")
    p = store.predict_outcome(SC, s, "right")
    assert (p.source, p.delta, p.confidence, p.support) == (RECALLED, {"grid.x": "1"}, 1.0, 1)
    assert p.next_digest == t1.next_digest
    t2 = act(store, clock, SC, "right")
    assert t2.surprise == 0.0 and t2.predicted["source"] == RECALLED


def test_unseen_state_generalizes_and_is_labelled(store, clock) -> None:
    act(store, clock, SC, "reset")
    act(store, clock, SC, "up")
    s = store.encode_state(SC, prefix="grid")  # (0,1): never seen with "up"
    p = store.predict_outcome(SC, s, "up")
    assert p.source == GENERALIZED and p.delta == {"grid.y": "1"} and p.support == 1


def test_agreement_ratio_is_confidence_and_latest_wins(store, clock) -> None:
    s = store.encode_state(SC, prefix="grid")
    for v in ("a", "b"):
        clock.t += 1
        after = world.WorldState(**{**s.to_dict(), "slots": {"grid.x": v},
                                    "digest": state_digest({"grid.x": v}), "ts": clock.t})
        store.observe_transition(SC, s, "flip", after)
    p = store.predict_outcome(SC, s, "flip")
    assert (p.source, p.delta, p.confidence, p.support) == (RECALLED, {"grid.x": "b"}, 0.5, 2)


class DictPredictor:
    name = "toy"

    def __init__(self, out):
        self.out = out
        self.calls = 0

    def predict(self, z, action, **kw):
        self.calls += 1
        return self.out

    def surprise(self, obs, action, next_obs, *a, **kw):
        return None


def test_predictor_only_on_a_miss_and_never_merged(store, clock) -> None:
    pr = DictPredictor({"grid.x": "7"})
    s = store.encode_state(SC, prefix="grid")
    p = store.predict_outcome(SC, s, "jump", predictor=pr)
    assert (p.source, p.delta, p.engine, pr.calls) == (PREDICTED, {"grid.x": "7"}, "toy", 1)
    act(store, clock, SC, "reset")
    act(store, clock, SC, "reset")
    s = store.encode_state(SC, prefix="grid")
    p = store.predict_outcome(SC, s, "reset", predictor=pr)
    assert p.source == RECALLED and pr.calls == 1  # the table answered; model untouched


def test_degraded_predictor_falls_through_and_bad_reply_raises(store) -> None:
    s = store.encode_state(SC)
    p = store.predict_outcome(SC, s, "jump", predictor=DictPredictor(None))
    assert p.source == NONE and "degraded" in p.note
    with pytest.raises(WorldError):
        store.predict_outcome(SC, s, "jump", predictor=DictPredictor([1, 2]))


def test_surprise_score_bounds() -> None:
    assert surprise_score({}, {}) == 0.0
    assert surprise_score({"a": "1"}, {"a": "1"}) == 0.0
    assert surprise_score({"a": "1"}, {"a": "2"}) == 1.0
    assert surprise_score({"a": "1"}, {"a": "1", "b": "x"}) == 0.5
    assert surprise_score({"a": None}, {}) == 1.0  # predicted removal did not happen
    # Judged on the next STATE: predicting "a := 1" when a already is 1 is right.
    assert surprise_score({"a": "1"}, {}, before={"a": "1"}) == 0.0
    assert surprise_score({"a": "1"}, {}, before={"a": "0"}) == 1.0


# ------------------------------------------------------------ violation of expectation
def test_voe_teleport_is_more_surprising_than_baseline_for_every_seed(tmp_path) -> None:
    pairs = []
    for seed in range(8):
        rng = random.Random(seed)
        seq = [rng.choice(("right", "up")) for _ in range(6)]
        mid_at, between_at = rng.randrange(6), rng.randrange(6)
        totals = []
        for perturbed in (False, True):
            clock = Clock()
            with MemoryStore(tmp_path / f"s{seed}-{perturbed}.db") as st:
                st._clock = clock
                episode(st, clock, SC, seq)            # learn
                act(st, clock, SC, "reset")            # learn reset-from-the-end
                start = clock.t + 1
                kw = ({"mid_at": mid_at, "mid": ("grid.door", "open"),
                       "between_at": between_at, "between": ("grid.key", "held")}
                      if perturbed else {})
                episode(st, clock, SC, seq, **kw)
                log = st.surprise_log(SC, start)
                totals.append(sum(e.score for e in log))
                kinds = {e.kind for e in log}
                if perturbed:
                    assert "transition" in kinds and "unexplained" in kinds
                    [u] = st.unexplained_changes(SC, start)
                    assert (u.key, u.new) == ("grid.key", "held")
                else:
                    assert log == [] and st.unexplained_changes(SC, start) == []
        pairs.append(tuple(totals))
    assert all(b == 0.0 for b, _ in pairs)
    assert all(p > b for b, p in pairs), pairs


def test_teleport_at_the_boundary_is_not_claimed_by_the_next_step(store, clock) -> None:
    act(store, clock, SC, "reset")
    start = clock.t
    store.remember(SC, "grid.x", "9")  # same instant the next step's `before` is taken
    act(store, clock, SC, "right")     # 9 -> 10: its delta names 10, not 9
    [u] = store.unexplained_changes(SC, start)
    assert (u.key, u.old, u.new) == ("grid.x", "0", "9")


def test_announced_update_is_explained_silent_one_is_not(store, clock) -> None:
    def said(text: str) -> None:
        before = store.encode_state(SC, prefix="user")
        clock.t += 1
        store.reconcile_and_remember(SC, text, subject="user.ui_theme")
        clock.t += 1
        store.observe_transition(SC, before, f"user says: {text}",
                                 store.encode_state(SC, prefix="user"), source="user")

    said("dark")
    said("light")
    assert store.unexplained_changes(SC, 0.0) == []
    assert [h.value for h in store.history(SC, "user.ui_theme")] == ["dark", "light"]
    clock.t += 1
    store.reconcile_and_remember(SC, "blue", subject="user.ui_theme")  # nothing announced
    [u] = store.unexplained_changes(SC, 0.0)
    assert (u.old, u.new, u.reason) == ("light", "blue", "updated")


def test_forget_without_an_action_is_unexplained(store, clock) -> None:
    act(store, clock, SC, "reset")
    clock.t += 1
    store.forget(SC, "grid.y")
    [u] = store.unexplained_changes(SC, 0.0, prefix="grid")
    assert (u.key, u.new, u.reason) == ("grid.y", None, "forgotten")


# ------------------------------------------------------------ visibility + refusals
def test_transitions_obey_scope_rules(store, clock) -> None:
    parent = S("acme:alice:*")
    act(store, clock, parent, "reset")
    act(store, clock, S("acme:bob:*"), "reset")
    act(store, clock, S("acmecorp:alice:*"), "reset")
    seen = {t.scope for t in store.transitions(SC)}
    assert seen == {"acme:alice:*"}  # ancestor yes; sibling and prefix-trap never
    assert store.transitions(parent) and not store.transitions(S("acme:*:*"))
    rows = store._db.execute("SELECT DISTINCT scope FROM transitions").fetchall()
    assert {r[0] for r in rows} == {"acme:alice:*", "acme:bob:*", "acmecorp:alice:*"}


def test_observe_refuses_what_cannot_be_recorded_honestly(store, clock) -> None:
    s = store.encode_state(SC, prefix="grid")
    with pytest.raises(WorldError, match="imagined"):
        store.observe_transition(SC, s, "x", s.apply({"grid.x": "1"}))
    with pytest.raises(WorldError, match="encoded at"):
        store.observe_transition(S("acme:alice:*"), s, "x", s)
    with pytest.raises(WorldError, match="prefixes"):
        store.observe_transition(SC, s, "x", store.encode_state(SC))
    clock.t -= 10
    with pytest.raises(WorldError, match="precedes"):
        store.observe_transition(SC, s, "x", store.encode_state(SC, prefix="grid"))
    with pytest.raises(WorldError):
        store.observe_transition(SC, s, "  ", s)
    assert store.transitions(SC) == []


def test_apply_and_satisfies_imagine_without_writing(store) -> None:
    s = store.encode_state(SC, prefix="grid")
    s2 = s.apply({"grid.x": "1"}).apply({"grid.x": None, "grid.y": "2"})
    assert s2.slots == {"grid.y": "2"} and s2.imagined
    assert s2.satisfies({"grid.y": "2", "grid.x": None})
    assert store.count() == 0


# ------------------------------------------------------------ migration v2 -> v3
def _as_v2(db: Path) -> None:
    MemoryStore(db).close()
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE transitions")
    con.execute("UPDATE schema_meta SET version = 2")
    con.execute("INSERT INTO memories(scope,key,value,kind,created,updated,meta,since) "
                "VALUES ('acme:*:*','k','v','fact',1.0,1.0,'{}',1.0)")
    con.commit()
    con.close()


def test_v2_file_migrates_to_v3_rows_untouched(tmp_path) -> None:
    db = tmp_path / "v2.db"
    _as_v2(db)
    with MemoryStore(db, auto_migrate=True) as st:
        assert st.recall(S("acme:*:*"))[0].value == "v"
        assert st.transitions(S("acme:*:*")) == []
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchall() == [(SCHEMA_VERSION,)]
    con.close()


def test_a_failed_v3_migration_leaves_a_v2_file(tmp_path, monkeypatch) -> None:
    import awm.store as store_mod
    db = tmp_path / "v2.db"
    _as_v2(db)
    monkeypatch.setattr(store_mod, "_SCHEMA_V3", (store_mod._SCHEMA_V3[0], "NOT SQL"))
    with pytest.raises(sqlite3.OperationalError):
        MemoryStore(db, auto_migrate=True)
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT version FROM schema_meta").fetchone()[0] == 2
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "transitions" not in names


# ------------------------------------------------------------ surfaces
def test_cli_world_state_predict_surprises(tmp_path, capsys) -> None:
    db = tmp_path / "m.db"
    clock = Clock()
    with MemoryStore(db) as st:
        st._clock = clock
        episode(st, clock, SC, ["right"])
        act(st, clock, SC, "reset")
    base = ["--db", str(db), "world"]
    assert main([*base, "state", "--scope", str(SC), "--prefix", "grid", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["slots"] == {"grid.x": "0", "grid.y": "0"}
    assert main([*base, "predict", "right", "--scope", str(SC), "--prefix", "grid",
                 "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["source"], out["delta"]) == (RECALLED, {"grid.x": "1"})
    assert main([*base, "predict", "fly", "--scope", str(SC)]) == 1  # NONE is not success
    capsys.readouterr()
    assert main([*base, "surprises", "--scope", str(SC), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_mcp_world_tools_round_trip(tmp_path) -> None:
    from awm import mcp_server
    srv = mcp_server.AwmMcp(tmp_path / "m.db")

    def call(name, **args):
        res = srv.call(name, args)
        text = res["content"][0]["text"]
        return res["isError"], (text if res["isError"] else json.loads(text))

    sc = "acme:alice:grid"
    err, st = call("awm_world_state", scope=sc, prefix="user")
    assert not err and st["slots"] == {}
    err, _ = call("awm_remember", scope=sc, subject="user.ui_theme", value="dark")
    assert not err
    err, t = call("awm_observe", scope=sc, prefix="user", action="user says dark",
                  before_as_of=st["ts"] - 1e-3)
    assert not err and t["delta"] == {"user.ui_theme": "dark"} and t["surprise"] is None
    err, p = call("awm_predict", scope=sc, prefix="user", action="user says dark")
    assert not err and p["source"] == GENERALIZED  # state changed since: not the same state
    err, msg = call("awm_observe", scope=sc, action="x")
    assert err and "before_as_of" in msg
