"""awm — a portable, scoped agent memory. SQLite, no service, no network.

Extracted from AitherOS's ScopedMemory. The service plumbing does not
generalise; the semantics do:

    platform:*:*               everyone
    {tenant}:*:*               one org
    {tenant}:{user}:*          one person
    {tenant}:{user}:{project}  one piece of work

    import awm
    st = awm.MemoryStore(Path("memory.db"))
    st.remember(awm.Scope("acme", "alice", "orchestrator"), "recipe",
                "rank 16, lr 2e-5")
    st.recall(awm.Scope("acme", "alice", "orchestrator"))

Two rules, deliberately asymmetric:

- **A write lands at exactly one scope.** Writing "somewhere in this subtree" is
  how a memory becomes visible to a scope its author never considered.
- **A read includes ancestors, weighted by distance.** A project query should
  surface the user's preferences and the platform's conventions — but a
  platform fact must not outrank a project fact just because it was older.

And one property that is security, not tidiness: **siblings never see each
other.** The scope check is segment-wise, never a string prefix, because
`"acmecorp:...".startswith("acme")` is True — that is one customer's memory
entering another's context, silently, with the answer still looking like an
answer. SQL narrows by an exact computed set, never by a LIKE.
"""

from __future__ import annotations

from .scope import (
    ANCESTOR_DECAY,
    PLATFORM,
    WILDCARD,
    Scope,
    ScopeError,
    visible_scopes,
)
from .entities import Entity, Resolution, normalize
from .pending import KIND_AMBIGUOUS, AboutResult, EntityRecall, PendingUpdate
from .reconcile import (
    Decision,
    LLMReconciler,
    ReconcileError,
    Reconciler,
    SlotReconciler,
)
from .store import (
    SCHEMA_VERSION,
    HistoryEntry,
    Memory,
    MemoryStore,
    MigrationError,
    NeedsMigration,
    probe_schema,
)
from .world import (
    GENERALIZED,
    NONE,
    PREDICTED,
    RECALLED,
    SlotChange,
    SurpriseEvent,
    SurpriseStats,
    Transition,
    WorldError,
    WorldState,
    encode_state,
    state_digest,
)
from .world import Prediction as WorldPrediction

__version__ = "0.6.1"

__all__ = [
    "ANCESTOR_DECAY",
    "AboutResult",
    "EntityRecall",
    "KIND_AMBIGUOUS",
    "PendingUpdate",
    "GENERALIZED",
    "NONE",
    "PREDICTED",
    "RECALLED",
    "MigrationError",
    "NeedsMigration",
    "SlotChange",
    "SurpriseEvent",
    "SurpriseStats",
    "Transition",
    "WorldError",
    "WorldPrediction",
    "WorldState",
    "encode_state",
    "state_digest",
    "PLATFORM",
    "SCHEMA_VERSION",
    "Decision",
    "Entity",
    "HistoryEntry",
    "LLMReconciler",
    "Memory",
    "MemoryStore",
    "ReconcileError",
    "Reconciler",
    "Resolution",
    "Scope",
    "ScopeError",
    "SlotReconciler",
    "WILDCARD",
    "normalize",
    "probe_schema",
    "visible_scopes",
]
