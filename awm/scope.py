"""Hierarchical memory scopes: what an agent may see, and what it may not.

Extracted from AitherOS's ScopedMemory, which wraps a memory service and a
knowledge graph with project-aware scoping. The service plumbing does not
generalise; the SEMANTICS do, and they are the whole value:

    platform:*:*              everyone
    {tenant}:*:*              one org
    {tenant}:{user}:*         one person
    {tenant}:{user}:{project} one piece of work

Two rules, and they are deliberately asymmetric:

- **A write lands at EXACTLY ONE scope.** Writing "somewhere in this subtree" is
  how a memory ends up visible to a scope its author never considered.
- **A read includes ANCESTORS, with diminishing weight.** Asking at project
  level should surface the user's preferences and the platform's conventions —
  that is the point of a hierarchy — but a platform fact must never outrank a
  project fact simply because it was written first.

WHAT MUST NEVER HAPPEN, AND WHY IT IS A SECURITY PROPERTY RATHER THAN A BUG

Siblings must not see each other. `acme:alice:*` and `acme:bob:*` share an
ancestor and are otherwise unrelated; `acme:*:*` and `globex:*:*` share only the
root. A retrieval that walks the tree loosely — a `LIKE 'acme:%'` here, a
prefix compare there — leaks one customer's memory into another's context, and
it leaks SILENTLY: the answer still looks like an answer.

Prefix matching is the specific trap and it is not hypothetical: `acme` is a
prefix of `acmecorp`, so `scope.startswith(ancestor)` returns True for two
unrelated tenants. Scopes here are compared SEGMENT-WISE, never as strings, and
the self-test carries that exact pair as a case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

SEP = ":"
WILDCARD = "*"
PLATFORM = "platform"

#: How much an ancestor's memories are discounted per level of distance. A
#: project fact and a platform fact are both relevant; the nearer one should
#: win. Applied multiplicatively, so two levels up is 0.25 rather than 0.
ANCESTOR_DECAY = 0.5


class ScopeError(ValueError):
    """A malformed or unsafe scope. Raised rather than normalised.

    "Fixing" a bad scope quietly is how a memory lands somewhere its author did
    not intend — and the write succeeds, so nothing looks wrong.
    """


@dataclass(frozen=True)
class Scope:
    """`tenant:user:project`, where `*` means "not narrowed at this level"."""

    tenant: str
    user: str = WILDCARD
    project: str = WILDCARD

    def __post_init__(self) -> None:
        for name, part in (("tenant", self.tenant), ("user", self.user),
                           ("project", self.project)):
            if not part:
                raise ScopeError(f"{name} must not be empty (use {WILDCARD!r})")
            if SEP in part:
                raise ScopeError(
                    f"{name}={part!r} contains {SEP!r}, the scope separator — "
                    f"that would silently re-partition the scope")
        # A narrower level under a wildcard is meaningless and, worse,
        # ambiguous: `acme:*:secret` reads as "that project for every user",
        # which no ancestor walk can express consistently.
        if self.user == WILDCARD and self.project != WILDCARD:
            raise ScopeError(
                f"project={self.project!r} under a wildcard user is ambiguous — "
                f"name the user, or widen the project to {WILDCARD!r}")

    def __str__(self) -> str:
        return SEP.join((self.tenant, self.user, self.project))

    @property
    def parts(self) -> Tuple[str, str, str]:
        return (self.tenant, self.user, self.project)

    @classmethod
    def parse(cls, text: str) -> "Scope":
        if not isinstance(text, str) or not text.strip():
            raise ScopeError("scope must be a non-empty string")
        bits = text.strip().split(SEP)
        if len(bits) != 3:
            raise ScopeError(
                f"scope {text!r} must have exactly three segments "
                f"(tenant{SEP}user{SEP}project); got {len(bits)}")
        return cls(*bits)

    @property
    def is_platform(self) -> bool:
        """`platform` is a SENTINEL tenant meaning "everyone", not a tenant.

        Reserved: a real organisation cannot be called `platform` here. The
        alternative — resolving the ambiguity at read time — means a tenant
        could name itself into the root of the hierarchy and see every other
        tenant's memories.
        """
        return self.tenant == PLATFORM

    @property
    def depth(self) -> int:
        """How specific this scope is: 0 platform-wide … 3 one project.

        The platform scope is depth 0, not 1. It names no organisation; it is
        the root every other scope descends from, and counting its sentinel
        tenant as a narrowing put it at the same distance as a real tenant —
        which is how it came to be invisible from everywhere.
        """
        if self.is_platform:
            return 0
        return sum(1 for p in self.parts if p != WILDCARD)

    def ancestors(self) -> List["Scope"]:
        """Every scope a read at this scope may ALSO see, nearest first.

        Never includes siblings. Widening happens strictly right-to-left, which
        is what keeps `acme:alice:*` from ever reaching `acme:bob:*`.
        """
        out: List[Scope] = []
        tenant, user, project = self.parts
        if project != WILDCARD:
            out.append(Scope(tenant, user, WILDCARD))
        if user != WILDCARD:
            out.append(Scope(tenant, WILDCARD, WILDCARD))
        if tenant != PLATFORM:
            out.append(Scope(PLATFORM, WILDCARD, WILDCARD))
        return out

    def covers(self, other: "Scope") -> bool:
        """True when a read at `other` may see memories written at `self`.

        Segment-wise, never a string prefix. `Scope("acme").covers(...)` must
        not match tenant `acmecorp`, and `"acmecorp:...".startswith("acme")` is
        True — which is precisely how one customer's memory reaches another's
        context, silently, with the answer still looking like an answer.
        """
        if not isinstance(other, Scope):
            raise ScopeError(f"expected a Scope, got {type(other).__name__}")
        # The platform scope is the root of the hierarchy and covers every
        # scope. Comparing its sentinel tenant literally made `platform:*:*`
        # cover NOTHING — so platform-wide conventions reached no agent, and
        # the failure was a silence: every recall still returned results, just
        # never that one.
        if self.is_platform:
            return True
        for mine, theirs in zip(self.parts, other.parts):
            if mine == WILDCARD:
                continue
            if mine != theirs:
                return False
        return True

    def weight_for(self, query: "Scope") -> float:
        """Relevance multiplier for a memory at `self` read from `query`.

        1.0 at the query's own scope, decaying per level of distance. Returns
        0.0 when this scope is not visible from `query` at all — a caller that
        forgets to filter still gets a zero rather than a leak.
        """
        if not self.covers(query):
            return 0.0
        distance = query.depth - self.depth
        if distance < 0:
            return 0.0
        return ANCESTOR_DECAY ** distance


def visible_scopes(query: Scope) -> List[Scope]:
    """The query scope plus its ancestors, nearest first."""
    return [query, *query.ancestors()]
