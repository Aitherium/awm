"""The dynamics layer: what the world IS, what an action DOES, and when it surprised us.

awm already answers "what is true now" (`recall`) and "what was true then"
(`recall(as_of=...)`). A world model needs two more answers, and this module is
both of them, still stdlib-only:

- **ENCODE.** The world state s_t is the set of CURRENT slot values visible from a
  scope (optionally only keys under one subject prefix), nearest scope winning a
  key. Its canonical form is a stable digest of the sorted (key, value) pairs, so
  two states are the same state exactly when their slots are equal.
- **PREDICT, tabular first.** A transition (s, action) -> delta is recorded every
  time an agent acts. `predict_outcome` answers from that table: the majority
  outcome for the exact (state, action), with its support and agreement ratio as
  confidence -- RECALLED. Only on a miss is an injected predictor consulted --
  PREDICTED. Failing both, the action's marginal over every state -- GENERALIZED.
  Nothing at all -- NONE, never a guess. The four are never merged: a caller can
  always tell which it got (the same rule `predict.py` states for recall).

  Why a table and not a model first, measured on real transitions
  (`check_world_model_floor.py`): online last-outcome 0.972 next-state accuracy
  against a trained MLP's 0.936. In a small discrete world a lookup table that
  updates on every step IS the best model, and a learned one is the tail.

- **SURPRISE (violation of expectation).** `observe_transition` compares the
  prediction the store WOULD have made with what happened and records a score in
  [0, 1]: the fraction of slots either delta touches whose predicted next value
  differs from the observed one. No prediction means no expectation, so the score is NULL -- novelty is
  not surprise. A slot that changed with no recorded transition covering it is an
  UNEXPLAINED change ("teleport"): `unexplained_changes` finds them from history,
  and `surprise_log` reports them beside the scored transitions.

Visibility is the store's, unchanged: a transition is written at EXACTLY one
scope and read from the query scope and its ancestors by an exact `IN`, never a
sibling, never a `LIKE`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from .scope import Scope, ScopeError

if TYPE_CHECKING:  # pragma: no cover
    from .store import MemoryStore

__all__ = [
    "RECALLED", "GENERALIZED", "PREDICTED", "NONE", "SOURCES",
    "WorldError", "WorldState", "Prediction", "Transition", "SlotChange",
    "SurpriseEvent", "state_digest", "apply_delta", "diff_slots", "surprise_score",
    "action_key", "encode_state", "observe_transition", "predict_outcome",
    "transitions", "unexplained_changes", "surprise_log", "SurpriseStats", "surprise_stats",
    "quantile", "KIND_TRANSITION", "KIND_UNEXPLAINED", "KIND_AMBIGUOUS",
]

#: Exact (state, action) seen before: the table's own answer.
RECALLED = "RECALLED"
#: The state was never seen with this action; the action's outcome over ALL states.
GENERALIZED = "GENERALIZED"
#: An injected predictor answered a miss. Never mixed with RECALLED.
PREDICTED = "PREDICTED"
#: Nothing to go on. Reported, never guessed.
NONE = "NONE"
SOURCES = (RECALLED, GENERALIZED, PREDICTED, NONE)

#: `surprise_log` event kinds.
KIND_TRANSITION = "transition"
KIND_UNEXPLAINED = "unexplained"
#: A fact about a mention only POSSIBLY naming a known entity (awm/pending.py).
KIND_AMBIGUOUS = "ambiguous_entity"

#: Confidence stamped on a predictor's answer that states none. Deliberately
#: below any RECALLED agreement a deterministic world produces: a model's guess
#: must not read as surer than what the table actually saw.
PREDICTOR_DEFAULT_CONFIDENCE = 0.5


class WorldError(ValueError):
    """A transition or state that cannot be recorded or read honestly. Raised, never repaired."""


# ------------------------------------------------------------ pure helpers
def state_digest(slots: Dict[str, str]) -> str:
    """Stable digest of a slot dict: sha256 over the sorted (key, value) pairs."""
    canon = json.dumps(sorted(slots.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def diff_slots(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, Optional[str]]:
    """The delta that takes `before` to `after`: changed/added -> new value, removed -> None."""
    delta: Dict[str, Optional[str]] = {}
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            delta[k] = after.get(k)
    return delta


def apply_delta(slots: Dict[str, str], delta: Dict[str, Optional[str]]) -> Dict[str, str]:
    """`slots` with `delta` applied (a None value removes the slot). Pure."""
    out = dict(slots)
    for k, v in delta.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


_ABSENT = object()


def surprise_score(predicted: Dict[str, Optional[str]],
                   observed: Dict[str, Optional[str]],
                   before: Optional[Dict[str, str]] = None) -> float:
    """Fraction of touched slots whose predicted NEXT value differs from the observed one.

    The slots judged are those either delta names. With `before`, each is
    compared as the value it would hold after the predicted delta against the
    value it holds after the observed one -- so "reset x to 0" predicted from a
    state where x already was 0 is right, not a miss. Without `before`, the
    deltas are compared directly. 0.0 = exactly as expected, 1.0 = every
    touched slot wrong.
    """
    keys = set(predicted) | set(observed)
    if not keys:
        return 0.0
    if before is not None:
        want, got = apply_delta(before, predicted), apply_delta(before, observed)
        wrong = sum(1 for k in keys if want.get(k) != got.get(k))
    else:
        wrong = sum(1 for k in keys
                    if predicted.get(k, _ABSENT) != observed.get(k, _ABSENT))
    return wrong / len(keys)


def action_key(action: Any) -> str:
    """Canonical text for an action: a string as given, anything else as sorted JSON."""
    if isinstance(action, str):
        if not action.strip():
            raise WorldError("action must be a non-empty string")
        return action
    try:
        return json.dumps(action, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise WorldError(f"action {action!r} is neither text nor JSON-serialisable") from exc


def _in_prefix(key: str, prefix: Optional[str]) -> bool:
    # Segment-wise, the reconcile rule: prefix "user" covers "user" and
    # "user.theme", never "username".
    return prefix is None or key == prefix or key.startswith(prefix + ".")


def _clean_delta(raw: Any) -> Dict[str, Optional[str]]:
    if not isinstance(raw, dict):
        raise WorldError(f"a delta must be a dict of slot -> value|None, got {type(raw).__name__}")
    out: Dict[str, Optional[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            raise WorldError(f"delta key {k!r} must be a non-empty string")
        if v is not None and not isinstance(v, str):
            raise WorldError(f"delta value for {k!r} must be a string or None, "
                             f"got {type(v).__name__}")
        out[k] = v
    return out


# ------------------------------------------------------------ value types
@dataclass(frozen=True)
class WorldState:
    """s_t: the slots visible from `scope` (under `prefix`) at instant `ts`.

    `as_of` is the instant that was ASKED for (None = now); `ts` is the instant
    the state reflects -- never earlier than the newest slot it contains. An
    `imagined` state (from `apply`) was never observed and cannot be recorded
    as one.
    """

    digest: str
    slots: Dict[str, str]
    scope: str
    prefix: Optional[str]
    as_of: Optional[float]
    ts: float
    imagined: bool = False

    def apply(self, delta: Dict[str, Optional[str]]) -> "WorldState":
        """The state `delta` would produce -- for planning in imagination."""
        slots = apply_delta(self.slots, _clean_delta(delta))
        return replace(self, slots=slots, digest=state_digest(slots), imagined=True)

    def satisfies(self, goal: Dict[str, Optional[str]]) -> bool:
        """Every goal slot holds its target (None = the slot is absent)."""
        return all(self.slots.get(k) == v for k, v in goal.items())

    def to_dict(self) -> Dict[str, Any]:
        return {"digest": self.digest, "slots": dict(self.slots), "scope": self.scope,
                "prefix": self.prefix, "as_of": self.as_of, "ts": self.ts,
                "imagined": self.imagined}


@dataclass(frozen=True)
class Prediction:
    """What an action is expected to do, and where that expectation came from.

    `source` is one of RECALLED | GENERALIZED | PREDICTED | NONE. A NONE
    prediction has an empty delta and zero confidence and must be read as
    "no model", never as "nothing changes".
    """

    delta: Dict[str, Optional[str]]
    source: str
    confidence: float
    support: int
    next_digest: Optional[str] = None
    engine: Optional[str] = None
    note: str = ""

    @property
    def known(self) -> bool:
        return self.source != NONE

    def to_dict(self) -> Dict[str, Any]:
        return {"delta": dict(self.delta), "source": self.source,
                "confidence": self.confidence, "support": self.support,
                "next_digest": self.next_digest, "engine": self.engine, "note": self.note}


@dataclass(frozen=True)
class Transition:
    """One recorded step: (state_digest, action) -> delta, and how surprising it was."""

    id: int
    scope: str
    state_digest: str
    action: str
    delta: Dict[str, Optional[str]]
    next_digest: str
    ts: float
    before_ts: float
    source: str
    predicted: Optional[Dict[str, Any]]
    surprise: Optional[float]
    #: The in-file row id (never shown). `id` is the opaque public id: the
    #: table is shared by every tenant, see _pubid.py.
    rid: int = field(default=0, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "state_digest": self.state_digest,
                "action": self.action, "delta": dict(self.delta),
                "next_digest": self.next_digest, "ts": self.ts, "before_ts": self.before_ts,
                "source": self.source, "predicted": self.predicted, "surprise": self.surprise}


@dataclass(frozen=True)
class SlotChange:
    """One change a slot underwent, read from history. `new` None = forgotten."""

    scope: str
    key: str
    old: Optional[str]
    new: Optional[str]
    ts: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"scope": self.scope, "key": self.key, "old": self.old, "new": self.new,
                "ts": self.ts, "reason": self.reason}


@dataclass(frozen=True)
class SurpriseEvent:
    """A scored transition, or an unexplained change (score 1.0)."""

    kind: str
    ts: float
    score: float
    scope: str
    action: Optional[str] = None
    key: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, "score": self.score, "scope": self.scope,
                "action": self.action, "key": self.key, "detail": dict(self.detail)}


# ------------------------------------------------------------ encode
def encode_state(store: "MemoryStore", scope: Scope, *, prefix: Optional[str] = None,
                 as_of: Optional[float] = None) -> WorldState:
    """s_t for `scope`: current (or `as_of`) slot values, nearest scope winning a key."""
    from .store import _visible_names

    names = _visible_names(scope)
    if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
        raise WorldError("prefix must be a non-empty string or None")
    rank = {n: i for i, n in enumerate(names)}
    with store._snapshot():
        now = float(store._clock())
        if as_of is None:
            rows = store._db.execute(
                f"SELECT scope, key, value, since, updated FROM memories "
                f"WHERE scope IN ({','.join('?' * len(names))})", names).fetchall()
            items = [(r["scope"], r["key"], r["value"], store._start(r)) for r in rows]
        else:
            items = [(m.scope, m.key, m.value, m.created)
                     for m in store._rows_as_of(names, None, float(as_of))]
    slots: Dict[str, str] = {}
    best: Dict[str, int] = {}
    newest = float("-inf")
    for sc, key, value, start in items:
        if not _in_prefix(key, prefix):
            continue
        r = rank.get(sc)
        if r is None:  # belt and braces: the IN makes this unreachable
            continue
        newest = max(newest, float(start))
        if key not in best or r < best[key]:
            best[key] = r
            slots[key] = value
    if as_of is not None:
        ts = float(as_of)
    else:
        # A write clamped past the clock (store._now) must still fall inside the
        # state that contains it, or it would read as happening after it.
        ts = max(now, newest)
    return WorldState(digest=state_digest(slots), slots=slots, scope=str(scope),
                      prefix=prefix, as_of=None if as_of is None else float(as_of), ts=ts)


# ------------------------------------------------------------ read transitions
def _transition(r: Any, key: bytes) -> Transition:
    from . import _pubid  # stdlib-only sibling
    return Transition(
        id=_pubid.public_id(key, "transitions", r["id"]), rid=int(r["id"]), scope=r["scope"], state_digest=r["state_digest"], action=r["action"],
        delta=json.loads(r["outcome_json"]), next_digest=r["next_digest"], ts=r["ts"],
        before_ts=r["before_ts"], source=r["source"],
        predicted=json.loads(r["predicted_json"]) if r["predicted_json"] else None,
        surprise=r["surprise"])


def transitions(store: "MemoryStore", scope: Scope, *, since: Optional[float] = None,
                until: Optional[float] = None, state: Optional[str] = None,
                action: Optional[Any] = None) -> List[Transition]:
    """Transitions visible from `scope` (it and its ancestors), oldest first."""
    from .store import _visible_names

    names = _visible_names(scope)
    where = [f"scope IN ({','.join('?' * len(names))})"]
    args: List[Any] = list(names)
    if state is not None:
        where.append("state_digest = ?")
        args.append(state)
    if action is not None:
        where.append("action = ?")
        args.append(action_key(action))
    if since is not None:
        where.append("ts >= ?")
        args.append(float(since))
    if until is not None:
        where.append("ts <= ?")
        args.append(float(until))
    rows = store._db.execute(
        f"SELECT * FROM transitions WHERE {' AND '.join(where)} ORDER BY ts, id",
        args).fetchall()
    if not rows:
        return []
    from . import _pubid  # stdlib-only sibling
    key = _pubid.file_key(store._db)
    return [_transition(r, key) for r in rows]


# ------------------------------------------------------------ predict
def _canon(delta: Dict[str, Optional[str]]) -> str:
    return json.dumps(delta, sort_keys=True, ensure_ascii=False)


def _majority(rows: List[Transition]) -> Tuple[Dict[str, Optional[str]], int]:
    """The most frequent delta among `rows` (oldest first), ties to the most recent."""
    tally: Dict[str, Tuple[int, int]] = {}  # canon -> (count, last index)
    for i, t in enumerate(rows):
        c, _ = tally.get(_canon(t.delta), (0, -1))
        tally[_canon(t.delta)] = (c + 1, i)
    best = max(tally, key=lambda k: tally[k])
    return json.loads(best), tally[best][0]


def _from_predictor(predictor: Any, state: WorldState, act: str) -> Optional[Prediction]:
    """Ask an injected predictor. None when it is degraded (returns None).

    Accepts a delta dict, or an object with a `delta` (and optional
    `confidence`) attribute. Any other reply raises: a reply that has to be
    interpreted can be misinterpreted into a plan.
    """
    engine = str(getattr(predictor, "name", None) or type(predictor).__name__)
    out = predictor.predict(state, act)
    if out is None:
        return None
    conf = PREDICTOR_DEFAULT_CONFIDENCE
    if isinstance(out, dict):
        delta = _clean_delta(out)
    elif isinstance(getattr(out, "delta", None), dict):
        delta = _clean_delta(out.delta)
        c = getattr(out, "confidence", None)
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            conf = min(1.0, max(0.0, float(c)))
    else:
        raise WorldError(f"predictor {engine} returned {type(out).__name__}; "
                         f"expected a delta dict, an object with .delta, or None")
    return Prediction(delta=delta, source=PREDICTED, confidence=conf, support=0,
                      next_digest=state_digest(apply_delta(state.slots, delta)),
                      engine=engine)


def prefix_rows(rows: List[Transition], prefix: Optional[str],
                known: Callable[[], Set[str]]) -> List[Transition]:
    """The rows of `rows` that belong to the slot namespace `prefix`.

    Transitions carry no prefix, and one scope holds several (one per agent or
    domain). A row with a delta belongs when every key it names is in `prefix`.
    An EMPTY delta (a no-op) names no key, so `all()` over it is vacuously true
    and every other prefix's no-ops would enter this one's marginal: an action
    never tried here would read as GENERALIZED. An empty-delta row belongs only
    when its state digest is one this prefix is known to produce -- `known()`
    returns them (computed only when an empty row is met). Unattributable no-ops
    are left out: a missing observation reads as "no model", a foreign one as a
    claim about this world.
    """
    if prefix is None:
        return list(rows)
    out: List[Transition] = []
    digests: Optional[Set[str]] = None
    for t in rows:
        if t.delta:
            if all(_in_prefix(k, prefix) for k in t.delta):
                out.append(t)
            continue
        if digests is None:
            digests = known()
        if t.state_digest in digests:
            out.append(t)
    return out


def prefix_digests(rows: List[Transition], prefix: Optional[str]) -> Set[str]:
    """State digests a prefix is known to produce: both ends of its non-empty rows."""
    got: Set[str] = set()
    for t in rows:
        if t.delta and all(_in_prefix(k, prefix) for k in t.delta):
            got.add(t.state_digest)
            got.add(t.next_digest)
    return got


#: The most a STATE-BLIND marginal may claim: one whose outcomes disagree, or that was
#: seen in fewer than GENERALIZE_MIN_STATES distinct states. Ten observations of an
#: action in ONE state are that state's outcome repeated, not evidence it holds
#: elsewhere -- they must not read as 0.909. adk's world model applies the same cap
#: (adk.world.STATE_BLIND_CAP); the two must give one answer for one table.
STATE_BLIND_CAP = 0.5
#: Distinct before-states an agreeing marginal needs before its shrunk agreement is
#: reported uncapped (adk.world.GENERALIZE_MIN_SUPPORT).
GENERALIZE_MIN_STATES = 2


def predict_outcome(store: "MemoryStore", scope: Scope, state: WorldState, action: Any,
                    predictor: Any = None) -> Prediction:
    """What `action` does from `state`: RECALLED, else PREDICTED, else GENERALIZED, else NONE.

    RECALLED is the MAJORITY observed outcome for the exact (state, action),
    ties to the most recent; confidence is the share of those observations that
    agree with it -- one anomaly after five consistent observations does not
    become the prediction. PREDICTED comes from `predictor` only on a miss.
    GENERALIZED is the action's most frequent outcome over every state (ties to
    the most recent) -- state-blind, so labelled as such, and its confidence is
    the agreement shrunk by n / (n + 1): one observation of an action, in some
    other state, is never certainty -- and capped at STATE_BLIND_CAP unless every
    observation agrees AND they span at least GENERALIZE_MIN_STATES distinct
    states (the note says which). adk's world model reads the same table with
    the same RECALLED and GENERALIZED rules but ONE different order: a marginal
    whose observations all agree across at least two DISTINCT states is taken
    ahead of its learner (state-independent evidence beats a model's guess). With
    no predictor, or a marginal that disagrees or was seen in one state only, the
    two give one answer for one table.
    """
    act = action_key(action)
    exact = transitions(store, scope, state=state.digest, action=act)
    if exact:
        best, count = _majority(exact)
        return Prediction(delta=best, source=RECALLED,
                          confidence=count / len(exact), support=len(exact),
                          next_digest=state_digest(apply_delta(state.slots, best)))
    note = ""
    if predictor is not None:
        got = _from_predictor(predictor, state, act)
        if got is not None:
            return got
        note = "predictor degraded (returned None)"
    # Only this state's prefix (`prefix_rows`): another agent's delta names slots
    # this state does not hold, and its no-ops are not evidence about this world.
    marg = prefix_rows(transitions(store, scope, action=act), state.prefix,
                       lambda: prefix_digests(transitions(store, scope), state.prefix)
                       | {state.digest})
    if marg:
        delta, count = _majority(marg)
        n = len(marg)
        conf = (count / n) * n / (n + 1.0)
        states = len({t.state_digest for t in marg})
        if count < n or states < GENERALIZE_MIN_STATES:
            conf = min(STATE_BLIND_CAP, conf)
            why = ("state-blind: this action's outcomes disagree across states"
                   if count < n else
                   f"state-blind: seen in {states} distinct state(s), fewer than "
                   f"{GENERALIZE_MIN_STATES}")
            note = f"{note}; {why}" if note else why
        return Prediction(delta=delta, source=GENERALIZED,
                          confidence=conf, support=n,
                          next_digest=state_digest(apply_delta(state.slots, delta)),
                          note=note)
    return Prediction(delta={}, source=NONE, confidence=0.0, support=0,
                      note=note or "no transition recorded for this action")


# ------------------------------------------------------------ observe
def observe_transition(store: "MemoryStore", scope: Scope, before: WorldState, action: Any,
                       after: WorldState, predicted: Any = None, *,
                       predictor: Any = None, source: str = "observed") -> Transition:
    """Record (before, action) -> after at EXACTLY `scope`, scored against a prediction.

    `predicted` may be a `Prediction`, a delta dict, or None -- in which case
    the store's own prediction (made BEFORE this row exists) is used, so the
    score measures what the table expected. A NONE prediction leaves surprise
    NULL: with no expectation there is nothing to violate.
    """
    if not isinstance(scope, Scope):
        raise ScopeError(f"expected a Scope, got {type(scope).__name__}")
    for name, st in (("before", before), ("after", after)):
        if not isinstance(st, WorldState):
            raise WorldError(f"{name} must be a WorldState, got {type(st).__name__}")
        if st.imagined:
            raise WorldError(f"{name} is an imagined state; only observed states are recorded")
        if st.scope != str(scope):
            raise WorldError(f"{name} was encoded at {st.scope}, not {scope}: "
                             f"its slots are another scope's view")
    if before.prefix != after.prefix:
        raise WorldError(f"before/after prefixes differ ({before.prefix!r} vs "
                         f"{after.prefix!r}); the delta would be an artefact")
    if after.ts < before.ts:
        raise WorldError(f"after.ts {after.ts} precedes before.ts {before.ts}")
    if not isinstance(source, str) or not source.strip():
        raise WorldError("source must be a non-empty string")
    act = action_key(action)
    observed = diff_slots(before.slots, after.slots)

    if predicted is None:
        pred: Optional[Prediction] = predict_outcome(store, scope, before, act, predictor)
    elif isinstance(predicted, Prediction):
        pred = predicted
    elif isinstance(predicted, dict):
        pred = Prediction(delta=_clean_delta(predicted), source=PREDICTED,
                          confidence=PREDICTOR_DEFAULT_CONFIDENCE, support=0,
                          engine="caller")
    else:
        raise WorldError(f"predicted must be a Prediction, a delta dict or None, "
                         f"got {type(predicted).__name__}")
    surprise = (None if pred.source == NONE
                else surprise_score(pred.delta, observed, before.slots))
    pjson = None if pred.source == NONE else json.dumps(pred.to_dict(), ensure_ascii=False)
    with store._tx():
        cur = store._db.execute(
            "INSERT INTO transitions(scope,state_digest,action,outcome_json,next_digest,"
            "ts,before_ts,source,predicted_json,surprise) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(scope), before.digest, act, json.dumps(observed, ensure_ascii=False),
             after.digest, after.ts, before.ts, source, pjson, surprise))
        tid = int(cur.lastrowid or 0)
        from . import _pubid  # stdlib-only sibling
        pub = _pubid.public_id(_pubid.file_key(store._db), "transitions", tid)
    return Transition(id=pub, rid=tid, scope=str(scope), state_digest=before.digest, action=act,
                      delta=observed, next_digest=after.digest, ts=after.ts,
                      before_ts=before.ts, source=source,
                      predicted=None if pjson is None else json.loads(pjson),
                      surprise=surprise)


# ------------------------------------------------------------ surprise
def _slot_changes(store: "MemoryStore", scope: Scope, since: float, until: float,
                  prefix: Optional[str]) -> List[SlotChange]:
    from .store import FORGOTTEN, _visible_names

    names = _visible_names(scope)
    with store._snapshot():
        hist = store._history_rows(names, None)
    by_key: Dict[Tuple[str, str], List[Any]] = {}
    for h in hist:
        if _in_prefix(h.key, prefix):
            by_key.setdefault((h.scope, h.key), []).append(h)
    out: List[SlotChange] = []
    for (sc, key), seq in by_key.items():
        prev = None
        for h in seq:
            # Contiguous with the previous interval = a replacement; a gap = the
            # key was absent (forgotten) and came back.
            old = prev.value if prev is not None and prev.valid_to == h.valid_from else None
            if since <= h.valid_from <= until:
                out.append(SlotChange(sc, key, old, h.value, h.valid_from,
                                      "added" if old is None else "updated"))
            if h.reason == FORGOTTEN and h.valid_to is not None \
                    and since <= h.valid_to <= until:
                out.append(SlotChange(sc, key, h.value, None, h.valid_to, FORGOTTEN))
            prev = h
    out.sort(key=lambda c: (c.ts, c.key))
    return out


def unexplained_changes(store: "MemoryStore", scope: Scope, since: float,
                        until: Optional[float] = None, *,
                        prefix: Optional[str] = None) -> List[SlotChange]:
    """Slot changes in [since, until] that no recorded transition accounts for.

    A change is EXPLAINED when a transition visible from `scope` spans its
    instant (before_ts <= ts <= after ts) and its observed delta took the key
    to exactly the changed value. The value check is what keeps a timestamp
    tie honest: a slot teleported at the instant the next step's `before` was
    encoded is already IN that state, so the step's delta names a different
    value and cannot claim the teleport. "I switched to light mode" recorded as an action explains dark ->
    light; the same write with nothing announcing it is a teleport.
    """
    hi = float("inf") if until is None else float(until)
    changes = _slot_changes(store, scope, float(since), hi, prefix)
    if not changes:
        return []
    # A covering transition ends at or after the change, so none ending before
    # `since` can cover anything in the window.
    covering = transitions(store, scope, since=float(since))
    out = []
    for c in changes:
        if not any(t.before_ts <= c.ts <= t.ts and c.key in t.delta
                   and t.delta[c.key] == c.new for t in covering):
            out.append(c)
    return out


def surprise_log(store: "MemoryStore", scope: Scope, since: float,
                 until: Optional[float] = None, *, prefix: Optional[str] = None,
                 include_zero: bool = False) -> List[SurpriseEvent]:
    """Scored transitions and unexplained changes in [since, until], oldest first.

    Transitions with a NULL score (novel: nothing was predicted) are not
    surprises and are left out; zero scores only with `include_zero`.
    """
    events: List[SurpriseEvent] = []
    for t in transitions(store, scope, since=since, until=until):
        if t.surprise is None or (t.surprise <= 0.0 and not include_zero):
            continue
        events.append(SurpriseEvent(
            kind=KIND_TRANSITION, ts=t.ts, score=float(t.surprise), scope=t.scope,
            action=t.action,
            detail={"transition_id": t.id, "observed": t.delta,
                    "predicted": (t.predicted or {}).get("delta"),
                    "source": (t.predicted or {}).get("source")}))
    for c in unexplained_changes(store, scope, since, until, prefix=prefix):
        events.append(SurpriseEvent(kind=KIND_UNEXPLAINED, ts=c.ts, score=1.0,
                                    scope=c.scope, key=c.key, detail=c.to_dict()))
    # A fact the model could not place on one entity: surprising by construction,
    # reported once per candidate (each is a subject that may be stale).
    from . import pending as _pend
    from .store import _visible_names
    for p in _pend.rows(store._db, _visible_names(scope), status=None, since=since,
                        until=until):
        if prefix is not None and not _in_prefix(p.subject, prefix):
            continue
        events.append(SurpriseEvent(kind=KIND_AMBIGUOUS, ts=p.created, score=1.0,
                                    scope=p.scope, key=p.subject, detail=p.to_dict()))
    events.sort(key=lambda e: e.ts)
    return events


# ------------------------------------------------------------ surprise statistics
@dataclass(frozen=True)
class SurpriseStats:
    """How surprising the world has been, and whether the confidence meant anything.

    `count` is the SCORED transitions (a prediction existed); `novel` the ones
    with nothing to compare against (NULL surprise, left out of every number).
    `calibration` has one row per confidence bucket: `n` scored transitions
    whose prediction stated a confidence in [lo, hi) (the last bucket includes
    1.0), their mean stated confidence, and `match_rate` -- the share whose
    prediction was exactly right (surprise 0). A calibrated model has
    match_rate close to mean_confidence in every bucket; `ece` is the
    n-weighted mean gap between the two. A bucket with n == 0 reports None, not 0.
    """

    scope: str
    since: float
    until: Optional[float]
    transitions: int
    count: int
    novel: int
    mean: Optional[float]
    p50: Optional[float]
    p90: Optional[float]
    max: Optional[float]
    unexplained: int
    calibration: List[Dict[str, Any]]
    ece: Optional[float]
    uncalibrated: int
    by_source: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {"scope": self.scope, "since": self.since, "until": self.until,
                "transitions": self.transitions, "count": self.count, "novel": self.novel,
                "mean": self.mean, "p50": self.p50, "p90": self.p90, "max": self.max,
                "unexplained": self.unexplained,
                "calibration": [dict(r) for r in self.calibration], "ece": self.ece,
                "uncalibrated": self.uncalibrated, "by_source": dict(self.by_source)}


def quantile(sorted_values: List[float], q: float) -> Optional[float]:
    """Linear-interpolated quantile of an ascending list (numpy's default). None if empty."""
    if not sorted_values:
        return None
    if not 0.0 <= q <= 1.0:
        raise WorldError(f"quantile {q} is outside [0, 1]")
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def surprise_stats(store: "MemoryStore", scope: Scope, since: float = 0.0,
                   until: Optional[float] = None, *, prefix: Optional[str] = None,
                   buckets: int = 5) -> SurpriseStats:
    """Distribution of transition surprise in [since, until] plus a calibration table.

    Computed from the recorded transitions only (their stored prediction and
    score), never re-predicted: the question is how the predictions MADE at
    the time held up. `prefix` narrows the unexplained-change count only; a
    transition is not keyed by slot. A prediction stored without a numeric
    confidence is scored but counted in `uncalibrated`, not in a bucket.
    """
    if isinstance(buckets, bool) or not isinstance(buckets, int) or not 1 <= buckets <= 100:
        raise WorldError("buckets must be an integer in [1, 100]")
    rows = transitions(store, scope, since=since, until=until)
    scored = [t for t in rows if t.surprise is not None]
    scores = sorted(float(t.surprise) for t in scored)  # type: ignore[arg-type]
    table = [{"lo": i / buckets, "hi": (i + 1) / buckets, "n": 0,
              "mean_confidence": None, "match_rate": None} for i in range(buckets)]
    sums = [[0.0, 0] for _ in range(buckets)]  # confidence sum, exact matches
    uncalibrated = 0
    by_source: Dict[str, int] = {}
    for t in scored:
        pred = t.predicted or {}
        src = str(pred.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1
        conf = pred.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
                or not 0.0 <= float(conf) <= 1.0:
            uncalibrated += 1
            continue
        i = min(int(float(conf) * buckets), buckets - 1)
        table[i]["n"] += 1
        sums[i][0] += float(conf)
        sums[i][1] += 1 if float(t.surprise or 0.0) == 0.0 else 0
    judged = 0
    gap = 0.0
    for row, (csum, hits) in zip(table, sums):
        if row["n"]:
            row["mean_confidence"] = csum / row["n"]
            row["match_rate"] = hits / row["n"]
            judged += row["n"]
            gap += row["n"] * abs(row["match_rate"] - row["mean_confidence"])
    unexplained = len(unexplained_changes(store, scope, since, until, prefix=prefix))
    return SurpriseStats(
        scope=str(scope), since=float(since), until=None if until is None else float(until),
        transitions=len(rows), count=len(scored), novel=len(rows) - len(scored),
        mean=(sum(scores) / len(scores)) if scores else None,
        p50=quantile(scores, 0.5), p90=quantile(scores, 0.9),
        max=scores[-1] if scores else None, unexplained=unexplained,
        calibration=table, ece=(gap / judged) if judged else None,
        uncalibrated=uncalibrated, by_source=by_source)
