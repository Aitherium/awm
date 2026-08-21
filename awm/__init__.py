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
from .store import SCHEMA_VERSION, Memory, MemoryStore

__version__ = "0.1.0"

__all__ = [
    "ANCESTOR_DECAY",
    "PLATFORM",
    "SCHEMA_VERSION",
    "Memory",
    "MemoryStore",
    "Scope",
    "ScopeError",
    "WILDCARD",
    "visible_scopes",
]
