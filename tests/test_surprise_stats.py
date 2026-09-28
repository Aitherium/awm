"""surprise_stats: the distribution of surprise, and whether stated confidence meant anything."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import pytest
from awm import mcp_server
from awm.cli import main
from awm.scope import Scope
from awm.store import MemoryStore
from awm.world import (
    NONE,
    RECALLED,
    Prediction,
    WorldError,
    WorldState,
    quantile,
    state_digest,
)

SC = Scope("acme", "alice", "grid")
SIB = Scope("acme", "bob", "grid")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: Clock):
    with MemoryStore(tmp_path / "mem.db") as s:
        s._clock = clock
        yield s


def _state(sc: Scope, slots: Dict[str, str], ts: float) -> WorldState:
    return WorldState(digest=state_digest(slots), slots=dict(slots), scope=str(sc),
                      prefix=None, as_of=ts, ts=ts)


def _step(st: MemoryStore, sc: Scope, t: float, observed: Dict[str, str],
          predicted: Optional[Dict[str, str]], conf: float) -> float:
    """One transition from {} to `observed`, scored against `predicted` at `conf`."""
    before, after = _state(sc, {}, t), _state(sc, observed, t + 1)
    pred = (Prediction(delta={}, source=NONE, confidence=0.0, support=0) if predicted is None
            else Prediction(delta=predicted, source=RECALLED, confidence=conf, support=1))
    return st.observe_transition(sc, before, "act", after, pred).surprise


def _world(st: MemoryStore) -> None:
    # scores: 0.0, 1.0 at confidence 0.9; 0.0, 0.5 at confidence 0.3; one novel.
    assert _step(st, SC, 100, {"a": "1"}, {"a": "1"}, 0.9) == 0.0
    assert _step(st, SC, 110, {"a": "1"}, {"a": "2"}, 0.9) == 1.0
    assert _step(st, SC, 120, {"a": "1"}, {"a": "1"}, 0.3) == 0.0
    assert _step(st, SC, 130, {"a": "1", "b": "1"}, {"a": "1", "b": "9"}, 0.3) == 0.5
    assert _step(st, SC, 140, {"a": "1"}, None, 0.0) is None


def test_quantile_is_linear_interpolation() -> None:
    xs = [0.0, 0.0, 0.5, 1.0]
    assert quantile(xs, 0.5) == pytest.approx(0.25)
    assert quantile(xs, 0.9) == pytest.approx(0.85)
    assert quantile(xs, 0.0) == 0.0 and quantile(xs, 1.0) == 1.0
    assert quantile([], 0.5) is None
    assert quantile([0.7], 0.9) == 0.7
    with pytest.raises(WorldError):
        quantile(xs, 1.5)


def test_stats_numbers_and_calibration_table(store) -> None:
    _world(store)
    s = store.surprise_stats(SC)
    assert (s.transitions, s.count, s.novel) == (5, 4, 1)
    assert s.mean == pytest.approx(0.375)
    assert s.p50 == pytest.approx(0.25) and s.p90 == pytest.approx(0.85)
    assert s.max == 1.0
    assert s.by_source == {RECALLED: 4}
    rows = {(round(r["lo"], 2), round(r["hi"], 2)): r for r in s.calibration}
    assert len(s.calibration) == 5
    low, high = rows[(0.2, 0.4)], rows[(0.8, 1.0)]
    assert (low["n"], low["mean_confidence"], low["match_rate"]) == (2, pytest.approx(0.3), 0.5)
    assert (high["n"], high["mean_confidence"], high["match_rate"]) == (
        2, pytest.approx(0.9), 0.5)
    assert rows[(0.0, 0.2)]["n"] == 0 and rows[(0.0, 0.2)]["match_rate"] is None
    # ece = (2*|0.5-0.3| + 2*|0.5-0.9|) / 4
    assert s.ece == pytest.approx(0.3)
    assert s.uncalibrated == 0


def test_confidence_one_lands_in_the_last_bucket(store) -> None:
    _step(store, SC, 100, {"a": "1"}, {"a": "1"}, 1.0)
    s = store.surprise_stats(SC, buckets=4)
    assert [r["n"] for r in s.calibration] == [0, 0, 0, 1]
    assert s.calibration[-1]["match_rate"] == 1.0


def test_since_until_and_unexplained(store, clock) -> None:
    _world(store)
    clock.t = 200.0
    store.remember(SC, "door", "open")            # a change no transition explains
    s = store.surprise_stats(SC, since=125)
    assert (s.count, s.novel) == (1, 1)           # the 0.5 step (ts 131) and the novel one
    assert s.mean == pytest.approx(0.5)
    assert s.unexplained == 1
    assert store.surprise_stats(SC, since=125, prefix="window").unexplained == 0
    assert store.surprise_stats(SC, since=0, until=115).count == 2


def test_empty_is_none_not_zero(store) -> None:
    s = store.surprise_stats(SC)
    assert (s.count, s.mean, s.p50, s.p90, s.ece) == (0, None, None, None, None)
    assert all(r["n"] == 0 and r["match_rate"] is None for r in s.calibration)


def test_a_sibling_s_transitions_are_not_counted(store) -> None:
    _step(store, SIB, 100, {"a": "1"}, {"a": "2"}, 0.9)
    assert store.surprise_stats(SC).count == 0
    assert store.surprise_stats(SIB).count == 1


def test_a_prediction_without_confidence_is_uncalibrated(store) -> None:
    store._db.execute(
        "INSERT INTO transitions(scope,state_digest,action,outcome_json,next_digest,ts,"
        "before_ts,source,predicted_json,surprise) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(SC), "d0", "act", "{}", "d1", 10.0, 9.0, "observed",
         json.dumps({"delta": {}, "source": "PREDICTED"}), 0.0))
    store._db.commit()
    s = store.surprise_stats(SC)
    assert (s.count, s.uncalibrated) == (1, 1)
    assert all(r["n"] == 0 for r in s.calibration) and s.ece is None


@pytest.mark.parametrize("bad", [0, 101, True, 2.5])
def test_bucket_count_is_validated(store, bad) -> None:
    with pytest.raises(WorldError):
        store.surprise_stats(SC, buckets=bad)


def test_cli_and_mcp(tmp_path: Path, capsys) -> None:
    db = tmp_path / "m.db"
    with MemoryStore(db) as st:
        _world(st)
    assert main(["--db", str(db), "world", "stats", "--scope", str(SC), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 4 and out["ece"] == pytest.approx(0.3)
    assert main(["--db", str(db), "world", "stats", "--scope", str(SC)]) == 0
    text = capsys.readouterr().out
    assert "4 scored transition(s), 1 novel" in text and "p90=0.850" in text
    srv = mcp_server.AwmMcp(db, cwd=tmp_path, user="alice")
    res = srv.call("awm_surprise_stats", {"scope": str(SC), "buckets": 2})
    body = json.loads(res["content"][0]["text"])
    assert not res["isError"] and len(body["calibration"]) == 2
    assert body["p50"] == pytest.approx(0.25)
