"""Entity resolution: is "vansh from india" the same person as "vansh"? Is "VS"?

Two failure modes, and every memory tool picks one of them:

- **Split everything.** "vansh", "vansh from india" and "VS" become three people,
  and a fact about one never reaches a question about the others.
- **Merge everything.** A fuzzy matcher decides "VS" is Vansh, and when it is
  actually Vikram Shah the two people's memories are now one — silently, and no
  later read can tell which fact belonged to whom.

The second is worse: a split can be joined later, an over-merge cannot be
un-merged from the data alone. So the rules here are ordered from certain to
uncertain, and only the certain ones link anything:

1. An exact CONFIRMED alias names that entity.
2. A HEAD match — the mention starts with a known name and the rest is a
   qualifier ("vansh from india", "vansh (india)") — is confirmed, and the
   qualifier is kept as evidence. The qualifier must start RIGHT AFTER the head:
   "Vansh Kumar, the designer" has a comma, but after "Kumar", so it describes
   Vansh Kumar, who may be a different Vansh. A head followed by something that
   is NOT a qualifier ("vansh sharma", "vansh a. sharma") is only POSSIBLE.
   The rule is order-free: a first mention "vansh from india" creates the
   entity "vansh" with both names confirmed, so a later bare "vansh" finds it.
3. Initials, abbreviations and bare first names ("VS", "vsh", "vansh" for
   "vansh sharma") are POSSIBLE links to every plausible entity. Nothing is
   merged; the caller confirms or rejects. A mention written as initials
   ("VS", "V.S.") ranks the entities whose multi-word name it is the exact
   initials of FIRST: that is the reading its writer most likely meant.
   A near-miss spelling ("Vanhs" for "vansh") is POSSIBLE too, never
   confirmed: within edit distance 1 of a known name when both are at least
   5 characters, within 2 when both are at least 8, and never when the two
   differ in their digits ("server01" is not a typo of "server02").
4. Nothing matched: a new entity.

A human who later decides two entities ARE one merges them (`merge`); the
merge records the prior alias sets so `split` restores them exactly. Merging is
still never done on a guess — only by an explicit call.

A rejected link is kept as a tombstone rather than deleted. Deleting it would let
rule 3 re-propose, on the very next read, the link a human just refused.

Visibility is the store's: entities and aliases are read from the query scope
and its ancestors by an exact `IN`, never from a sibling, and every write lands
at exactly the caller's scope (the one exception, the memory owner, is below).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .scope import PLATFORM, WILDCARD, Scope, ScopeError, visible_scopes

CONFIRMED = "confirmed"
POSSIBLE = "possible"
#: Internal tombstone. Never returned as a resolution status.
REJECTED = "rejected"

#: Mentions that name the memory's owner. Resolved from the scope, never matched:
#: "I" is not an abbreviation of Isabel.
RESERVED_SELF = frozenset({"me", "i", "myself"})

#: A remainder that starts with one of these describes the head rather than
#: extending it: "vansh FROM india" is Vansh, "vansh SHARMA" might not be.
#: No single letters: "Vansh A. Sharma" has a middle INITIAL, and "a" here
#: would read it as the article and merge a different person.
QUALIFIER_WORDS = frozenset({
    "from", "of", "at", "in", "the", "who", "with", "on", "for", "my", "our",
    "an", "aka", "based", "works", "living", "lives",
})
#: Words that set a qualifier off inside a FIRST mention, where there is no
#: known head to anchor on. Narrower than QUALIFIER_WORDS on purpose: "Bank of
#: America" and "The Head of State" are names, "vansh from india" is not.
SPLIT_WORDS = frozenset({"from", "who", "aka", "based", "works", "living", "lives"})
#: Punctuation that sets a qualifier off, immediately after the head:
#: "vansh, the designer". Not "/" -- "AC/DC" is one name.
QUALIFIER_DELIMITERS = (",", "(", "[", "—", "–")

_ID_SPACE = 2 ** 53 - 1
#: A mention is a name plus a qualifier, not a document; the head scan is quadratic.
MAX_MENTION = 256

#: Longest mention treated as initials or an abbreviation. Longer single words
#: are names, and a name that merely contains another's letters is not it.
MAX_ABBREVIATION = 4

#: (minimum length of BOTH names, largest edit distance still read as a typo).
#: Checked longest first. Shorter names get no typo tolerance: "ann" and "ian"
#: are one edit from a dozen other people.
TYPO_BANDS = ((8, 2), (5, 1))

#: Reversible merges (schema v3). `snapshot` is JSON: the dropped entity's row
#: and every alias row of both entities at the merge scope, as they were.
MERGES_DDL = """CREATE TABLE IF NOT EXISTS entity_merges (
    id        INTEGER PRIMARY KEY,
    scope     TEXT NOT NULL,
    keep_id   INTEGER NOT NULL,
    drop_id   INTEGER NOT NULL,
    snapshot  TEXT NOT NULL,
    created   REAL NOT NULL,
    split_at  REAL
)"""
MERGES_INDEX = ("CREATE INDEX IF NOT EXISTS idx_entity_merges_drop "
                "ON entity_merges(scope, drop_id)")

#: Status precedence when a merge folds two alias rows for one name into one.
_STATUS_RANK = {POSSIBLE: 0, CONFIRMED: 1}


@dataclass
class Resolution:
    """The answer to "who is this mention?".

    `status` is `confirmed` (entity_id is set) or `possible` (entity_id is None
    and `possible` lists the candidates — nothing was merged).
    """

    entity_id: Optional[int]
    canonical: Optional[str]
    status: str
    created_new: bool
    possible: List[int] = field(default_factory=list)
    #: Pending facts `confirm_alias` applied (see pending.py); empty otherwise.
    applied: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out = {"entity_id": self.entity_id, "canonical": self.canonical,
               "status": self.status, "created_new": self.created_new,
               "possible": list(self.possible)}
        if self.applied:
            out["applied"] = list(self.applied)
        return out


@dataclass
class Entity:
    id: int
    scope: str
    canonical: str
    created: float
    aliases: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "canonical": self.canonical,
                "created": self.created, "aliases": list(self.aliases)}


# ------------------------------------------------------------ pure matching
def normalize(mention: str) -> str:
    """Casefold, punctuation to spaces, whitespace collapsed.

    Punctuation becomes a SPACE rather than vanishing so "V.S." and "v s" meet;
    initials are then compacted by `compact`.
    """
    if not isinstance(mention, str):
        raise ScopeError(f"mention must be a string, got {type(mention).__name__}")
    out = []
    for ch in mention.casefold():
        cat = unicodedata.category(ch)
        out.append(" " if cat[0] in ("P", "S") or ch.isspace() else ch)
    return " ".join("".join(out).split())


def compact(norm: str) -> str:
    """"v s" -> "vs": a run of single letters is one set of initials."""
    toks = norm.split()
    if len(toks) > 1 and all(len(t) == 1 for t in toks):
        return "".join(toks)
    return norm


def _raw_after(raw: str, ntoks: int) -> Optional[str]:
    """The raw text after the first `ntoks` normalized tokens of `raw`, or None."""
    for i in range(1, len(raw) + 1):
        toks = normalize(raw[:i]).split()
        if len(toks) > ntoks:
            break
        if len(toks) == ntoks and (
                i == len(raw) or normalize(raw[:i + 1]).split()[:ntoks] == toks):
            return raw[i:]
    return None


def _delimited(after: Optional[str]) -> bool:
    """True when `after` (the raw text right after a head) opens with a delimiter."""
    if after is None:
        return False
    stripped = after.lstrip()
    return stripped.startswith(QUALIFIER_DELIMITERS) or after.startswith(" - ")


def head_match(norm: str, raw: str, known: str) -> Optional[Tuple[bool, str]]:
    """(is_qualifier, remainder) when `norm` starts with the name `known`.

    None when it does not, or when it IS `known` (that is an exact match). A
    delimiter counts only where it follows the head, never later in the mention.
    """
    toks, head = norm.split(), known.split()
    if len(toks) <= len(head) or toks[:len(head)] != head:
        return None
    rest = toks[len(head):]
    qualifier = rest[0] in QUALIFIER_WORDS or _delimited(_raw_after(raw, len(head)))
    return qualifier, " ".join(rest)


def split_qualifier(norm: str, raw: str) -> Optional[Tuple[str, str, str]]:
    """(head_norm, head_raw, qualifier) when a first mention is "<name> <qualifier>".

    "vansh from india" -> ("vansh", "vansh", "from india"). None when no head
    can be told apart from its qualifier, which leaves the mention whole.
    """
    toks = norm.split()
    for k in range(1, len(toks)):
        after = _raw_after(raw, k)
        if toks[k] in SPLIT_WORDS or _delimited(after):
            head_raw = raw[:len(raw) - len(after)] if after is not None else " ".join(toks[:k])
            head_raw = " ".join(head_raw.split()).strip(" ,([—–-")
            head = compact(" ".join(toks[:k]))
            if head in RESERVED_SELF or not head_raw:
                return None
            return head, head_raw, " ".join(toks[k:])
    return None


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute, swap neighbours.

    The swap matters: "vanhs" is ONE slip from "vansh", but two by plain
    Levenshtein, which would put the commonest typo out of reach.
    """
    if a == b:
        return 0
    prev2: List[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def typo_of(norm: str, known: str) -> Optional[str]:
    """Evidence text when `norm` is a near-miss spelling of the name `known`.

    Only ever a POSSIBLE link: "vansh" and "vanshi" may be two people.
    """
    if norm == known or not norm or not known:
        return None
    if [c for c in norm if c.isdigit()] != [c for c in known if c.isdigit()]:
        return None
    shortest = min(len(norm), len(known))
    limit = next((d for n, d in TYPO_BANDS if shortest >= n), 0)
    if limit == 0 or abs(len(norm) - len(known)) > limit:
        return None
    d = edit_distance(norm, known)
    if 0 < d <= limit:
        return f"within edit distance {d} of {known!r} (possible typo)"
    return None


def written_as_initials(raw: str) -> bool:
    """True for "VS", "V.S.", "v s": letters meant as initials, not a word.

    All capitals, or single letters set apart by punctuation or spaces. A
    lowercase run ("vs") stays ambiguous between initials and an abbreviation.
    """
    letters = [c for c in raw if c.isalpha()]
    if not 2 <= len(letters) <= MAX_ABBREVIATION:
        return False
    if any(not (c.isalpha() or c.isspace() or c in ".-_") for c in raw.strip()):
        return False
    if all(c.isupper() for c in letters):
        return True
    toks = normalize(raw).split()
    return len(toks) > 1 and all(len(t) == 1 for t in toks)


def initials_of(norm: str, known: str) -> bool:
    """`norm` is exactly the initials of the multi-word name `known`."""
    toks = known.split()
    return len(toks) >= 2 and compact(norm) == "".join(t[0] for t in toks)


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def abbreviation_of(norm: str, known: str) -> Optional[str]:
    """Evidence text when `norm` could plausibly abbreviate the name `known`.

    Plausible, never proof: "vs" fits Vansh S-something and Vikram Shah alike,
    which is exactly why this only ever produces a POSSIBLE link.
    """
    m, toks = compact(norm), known.split()
    if not toks or m == known:
        return None
    mtoks = m.split()
    if len(mtoks) < len(toks) and toks[:len(mtoks)] == mtoks:
        return f"first name(s) of {known!r}"
    if " " in m or not m.isalpha() or not 2 <= len(m) <= MAX_ABBREVIATION:
        return None
    initials = "".join(t[0] for t in toks)
    why = []
    if len(toks) >= 2 and m == initials:
        why.append(f"initials of {known!r}")
    if len(m) > len(initials) and m.startswith(initials):
        why.append(f"initials of {known!r} plus names not on record")
    if m[0] == known[0] and _is_subsequence(m, known.replace(" ", "")):
        why.append(f"abbreviation of {known!r}")
    # Every explanation is kept: the human confirming the link should see why.
    return " or ".join(why) or None


# ------------------------------------------------------------ database
def _names(scope: Scope) -> List[str]:
    if not isinstance(scope, Scope):
        raise ScopeError(f"expected a Scope, got {type(scope).__name__}")
    return [str(s) for s in visible_scopes(scope)]


def _in(names: List[str]) -> str:
    # An exact IN over a computed set. Never a LIKE — see store.py.
    return ",".join("?" * len(names))


def _weight(row_scope: str, query: Scope) -> float:
    return Scope.parse(row_scope).weight_for(query)


def _visible_links(db: sqlite3.Connection, scope: Scope, norm: str,
                   status: Optional[str] = None) -> List[sqlite3.Row]:
    """Alias links for `norm` whose alias AND entity are both visible, nearest first."""
    names = _names(scope)
    sql = (f"SELECT a.scope AS ascope, a.entity_id, a.status, a.evidence, a.created, "
           f"e.canonical, e.scope AS escope FROM entity_aliases a "
           f"JOIN entities e ON e.id = a.entity_id "
           f"WHERE a.alias_norm = ? AND a.scope IN ({_in(names)}) "
           f"AND e.scope IN ({_in(names)})")
    args: List[Any] = [norm, *names, *names]
    if status:
        sql += " AND a.status = ?"
        args.append(status)
    rows = [r for r in db.execute(sql, args).fetchall()
            if _weight(r["ascope"], scope) > 0 and _weight(r["escope"], scope) > 0]
    rows.sort(key=lambda r: (-_weight(r["ascope"], scope), r["created"], r["entity_id"]))
    return rows


def _visible_names_known(db: sqlite3.Connection, scope: Scope) -> List[sqlite3.Row]:
    """Every confirmed name (alias) visible from `scope`, nearest first."""
    names = _names(scope)
    rows = db.execute(
        f"SELECT a.alias_norm, a.entity_id, a.scope AS ascope, e.canonical, "
        f"e.scope AS escope, e.created AS ecreated "
        f"FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        f"WHERE a.status = ? AND a.scope IN ({_in(names)}) AND e.scope IN ({_in(names)})",
        [CONFIRMED, *names, *names]).fetchall()
    rows = [r for r in rows
            if _weight(r["ascope"], scope) > 0 and _weight(r["escope"], scope) > 0]
    rows.sort(key=lambda r: (-_weight(r["ascope"], scope), r["ecreated"], r["entity_id"]))
    return rows


def _link(db: sqlite3.Connection, scope: Scope, norm: str, entity_id: int,
          status: str, evidence: str, now: float) -> None:
    db.execute(
        "INSERT INTO entity_aliases(scope, alias_norm, entity_id, status, evidence, created) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(scope, alias_norm, entity_id) "
        "DO UPDATE SET status = excluded.status",
        (str(scope), norm, entity_id, status, evidence, now))


def _new_entity(db: sqlite3.Connection, scope: Scope, canonical: str, norm: str,
                evidence: str, now: float) -> int:
    # A random id, not AUTOINCREMENT: the table is shared by every tenant, so a
    # sequential id tells a caller how many entities OTHER scopes created
    # between two of its own. 53 bits so the id survives a JSON round trip.
    for _ in range(8):
        eid = secrets.randbelow(_ID_SPACE) + 1
        try:
            db.execute("INSERT INTO entities(id, scope, canonical, created) VALUES (?,?,?,?)",
                       (eid, str(scope), canonical, now))
        except sqlite3.IntegrityError:
            continue
        _link(db, scope, norm, eid, CONFIRMED, evidence, now)
        return eid
    raise ScopeError("could not allocate an entity id")


def _entity_visible(db: sqlite3.Connection, scope: Scope, entity_id: int) -> sqlite3.Row:
    names = _names(scope)
    row = db.execute(f"SELECT * FROM entities WHERE id = ? AND scope IN ({_in(names)})",
                     [int(entity_id), *names]).fetchone()
    if row is None or _weight(row["scope"], scope) <= 0:
        # Same message whether it does not exist or belongs to a sibling: the
        # difference is itself a leak.
        raise ScopeError(f"no entity {entity_id} visible from {scope}")
    return row


def resolve_owner(db: sqlite3.Connection, scope: Scope, now: float) -> Resolution:
    """"me" is the scope's user, recorded once at `tenant:user:*`.

    The one write that does not land at the caller's own scope, deliberately:
    the owner of `acme:alice:proj` and of `acme:alice:other` is one person, and
    recording her per project would split her — rule 1's failure, by design.
    """
    if scope.is_platform or scope.user == WILDCARD or scope.tenant == PLATFORM:
        raise ScopeError(f"'me' has no owner at {scope}: name a user in the scope")
    owner = Scope(scope.tenant, scope.user, WILDCARD)
    norm = compact(normalize(scope.user))
    # The owner's name already names an entity from here ("Alice" mentioned
    # before "me"): that entity IS the owner. A second one would split her, and
    # plain "alice" would keep resolving to the other one.
    named = _visible_links(db, scope, norm, CONFIRMED)
    if named:
        return Resolution(named[0]["entity_id"], named[0]["canonical"], CONFIRMED, False)
    row = db.execute(
        "SELECT e.id, e.canonical FROM entity_aliases a JOIN entities e "
        "ON e.id = a.entity_id WHERE a.scope = ? AND e.scope = ? AND a.alias_norm = ? "
        "AND a.status = ? ORDER BY e.id LIMIT 1",
        (str(owner), str(owner), norm, CONFIRMED)).fetchone()
    if row is not None:
        return Resolution(row["id"], row["canonical"], CONFIRMED, False)
    eid = _new_entity(db, owner, scope.user, norm, "memory owner", now)
    return Resolution(eid, scope.user, CONFIRMED, True)


def resolve(db: sqlite3.Connection, scope: Scope, mention: str, now: float) -> Resolution:
    """Apply the four rules in the module docstring, in order."""
    norm = normalize(mention)
    if not norm:
        raise ScopeError(f"mention {mention!r} contains no name")
    if len(mention) > MAX_MENTION:
        raise ScopeError(f"mention is {len(mention)} characters; a name is at most "
                         f"{MAX_MENTION}")
    if norm in RESERVED_SELF:
        return resolve_owner(db, scope, now)
    norm = compact(norm)

    exact = _visible_links(db, scope, norm, CONFIRMED)
    if exact:
        return Resolution(exact[0]["entity_id"], exact[0]["canonical"], CONFIRMED, False)

    rejected = {r["entity_id"] for r in _visible_links(db, scope, norm, REJECTED)}
    known = [r for r in _visible_names_known(db, scope) if r["entity_id"] not in rejected]

    head_hit = _confirmed_head(norm, mention, known)
    if head_hit is not None:
        r, rest = head_hit
        _link(db, scope, norm, r["entity_id"], CONFIRMED, rest, now)
        return Resolution(r["entity_id"], r["canonical"], CONFIRMED, False)
    plausible = _plausible(db, scope, norm, mention, known, rejected)

    split = None if plausible else split_qualifier(norm, mention)
    if split is not None:
        head, head_raw, _qual = split
        head_rejected = {r["entity_id"] for r in _visible_links(db, scope, head, REJECTED)}
        head_known = [r for r in known if r["entity_id"] not in head_rejected]
        # The head alone is ambiguous ("vansh" vs a known "vansh sharma"): so is
        # the mention. Offer the same candidates; merge nothing.
        plausible = _plausible(db, scope, head, head_raw, head_known,
                               rejected | head_rejected)

    if plausible:
        for eid, ev in plausible.items():
            db.execute(
                "INSERT OR IGNORE INTO entity_aliases"
                "(scope, alias_norm, entity_id, status, evidence, created) "
                "VALUES (?,?,?,?,?,?)", (str(scope), norm, eid, POSSIBLE, ev, now))
        return Resolution(None, None, POSSIBLE, False, list(plausible))

    if split is not None:
        head, head_raw, qual = split
        eid = _new_entity(db, scope, head_raw, head, "first mention", now)
        _link(db, scope, norm, eid, CONFIRMED, qual, now)
        return Resolution(eid, head_raw, CONFIRMED, True)
    canonical = " ".join(mention.split())
    eid = _new_entity(db, scope, canonical, norm, "first mention", now)
    return Resolution(eid, canonical, CONFIRMED, True)


def _confirmed_head(norm: str, raw: str,
                    known: List[sqlite3.Row]) -> Optional[Tuple[sqlite3.Row, str]]:
    """The known name `norm` extends with a qualifier (longest head wins), if any."""
    heads = []
    for r in known:
        hm = head_match(norm, raw, r["alias_norm"])
        if hm is not None:
            heads.append((len(r["alias_norm"].split()), r, hm))
    heads.sort(key=lambda h: -h[0])
    if heads and heads[0][2][0]:
        _n, r, (_q, rest) = heads[0]
        return r, rest
    return None


def _plausible(db: sqlite3.Connection, scope: Scope, norm: str, raw: str,
               known: List[sqlite3.Row], rejected: set) -> Dict[int, str]:
    """entity_id -> evidence for every entity `norm` might name. Links nothing."""
    plausible: Dict[int, str] = {}
    heads = []
    for r in known:
        hm = head_match(norm, raw, r["alias_norm"])
        if hm is not None:
            heads.append((len(r["alias_norm"].split()), r, hm))
    heads.sort(key=lambda h: -h[0])
    for _n, r, (_q, rest) in heads:
        plausible.setdefault(r["entity_id"], f"{r['canonical']!r} followed by {rest!r}")

    names = _candidate_names(known)
    for eid, name in names:
        ev = abbreviation_of(norm, name)
        if ev:
            plausible.setdefault(eid, ev)
    for r in _visible_links(db, scope, norm, POSSIBLE):
        plausible.setdefault(r["entity_id"], r["evidence"])
    for eid, name in names:
        ev = typo_of(norm, name)
        if ev:
            plausible.setdefault(eid, ev)
    for eid in rejected:
        plausible.pop(eid, None)
    if written_as_initials(raw):
        # Stable: among the exact-initials entities, and among the rest, the
        # order above (nearest scope, then oldest) is kept.
        first = {eid for eid, name in names if initials_of(norm, name)}
        plausible = dict(sorted(plausible.items(), key=lambda kv: kv[0] not in first))
    return plausible


def _candidate_names(known: List[sqlite3.Row]) -> List[Tuple[int, str]]:
    """(entity_id, name) for every confirmed alias AND every canonical, deduplicated.

    The canonical is matched too: an entity created as "Vansh Sharma (Delhi)"
    has the alias "vansh sharma delhi" but is still NAMED "vansh sharma"-ish by
    its canonical, and initials are read off the name a person was given.
    """
    out: List[Tuple[int, str]] = []
    seen = set()
    for r in known:
        for name in (r["alias_norm"], compact(normalize(r["canonical"]))):
            if name and (r["entity_id"], name) not in seen:
                seen.add((r["entity_id"], name))
                out.append((r["entity_id"], name))
    return out


def confirm(db: sqlite3.Connection, scope: Scope, alias: str, entity_id: int,
            now: float) -> Resolution:
    """Promote `alias` -> entity to confirmed at exactly `scope`; drop its rivals there."""
    norm = compact(normalize(alias))
    if not norm:
        raise ScopeError(f"alias {alias!r} contains no name")
    ent = _entity_visible(db, scope, entity_id)
    _link(db, scope, norm, ent["id"], CONFIRMED, "confirmed by caller", now)
    db.execute("DELETE FROM entity_aliases WHERE scope = ? AND alias_norm = ? "
               "AND status = ? AND entity_id != ?",
               (str(scope), norm, POSSIBLE, ent["id"]))
    return Resolution(ent["id"], ent["canonical"], CONFIRMED, False)


def reject(db: sqlite3.Connection, scope: Scope, alias: str, entity_id: int,
           now: float) -> bool:
    """Tombstone one alias -> entity link at exactly `scope`. True if one was live."""
    norm = compact(normalize(alias))
    if not norm:
        raise ScopeError(f"alias {alias!r} contains no name")
    ent = _entity_visible(db, scope, entity_id)
    live = db.execute(
        "SELECT 1 FROM entity_aliases WHERE scope = ? AND alias_norm = ? AND entity_id = ? "
        "AND status != ?", (str(scope), norm, ent["id"], REJECTED)).fetchone()
    _link(db, scope, norm, ent["id"], REJECTED, "rejected by caller", now)
    return live is not None


def list_entities(db: sqlite3.Connection, scope: Scope) -> List[Entity]:
    """Entities visible from `scope`, nearest first, with their visible aliases."""
    names = _names(scope)
    ents = [Entity(r["id"], r["scope"], r["canonical"], r["created"])
            for r in db.execute(f"SELECT * FROM entities WHERE scope IN ({_in(names)})",
                                names).fetchall()
            if _weight(r["scope"], scope) > 0]
    by_id = {e.id: e for e in ents}
    if by_id:
        rows = db.execute(
            f"SELECT * FROM entity_aliases WHERE scope IN ({_in(names)}) "
            f"AND status != ? ORDER BY created, alias_norm", [*names, REJECTED]).fetchall()
        for r in rows:
            e = by_id.get(r["entity_id"])
            if e is not None:
                e.aliases.append({"alias": r["alias_norm"], "status": r["status"],
                                  "scope": r["scope"], "evidence": r["evidence"]})
    ents.sort(key=lambda e: (-_weight(e.scope, scope), e.created, e.id))
    return ents


# ------------------------------------------------------------ merge / split
def _alias_rows(db: sqlite3.Connection, scope: Scope, entity_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.execute(
        "SELECT scope, alias_norm, entity_id, status, evidence, created FROM entity_aliases "
        "WHERE scope = ? AND entity_id = ? ORDER BY alias_norm", (str(scope), entity_id))]


def _insert_alias(db: sqlite3.Connection, row: Dict[str, Any], entity_id: int) -> None:
    db.execute("INSERT INTO entity_aliases(scope, alias_norm, entity_id, status, evidence, "
               "created) VALUES (?,?,?,?,?,?)",
               (row["scope"], row["alias_norm"], entity_id, row["status"], row["evidence"],
                row["created"]))


def _pending_subject(subject: str, old_canonical: str, new_canonical: str) -> str:
    """``<prefix>.<slug(old)>`` -> ``<prefix>.<slug(new)>``; any other subject unchanged."""
    from . import pending as _pend  # stdlib-only sibling; imported late, no cycle
    tail = "." + _pend.slug(old_canonical)
    if subject.endswith(tail):
        return subject[: -len(tail)] + "." + _pend.slug(new_canonical)
    return subject


def _retarget_pending(db: sqlite3.Connection, scope: Scope, k: Dict[str, Any],
                      d: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Facts still PENDING against the dropped entity follow it into `keep`.

    Left alone they point at an entity that no longer exists: `confirm_alias` for
    `keep` skips them (wrong id), `recall_entity(keep)` does not list them, and
    `confirm_alias` for the dropped id is refused -- pending forever. A row whose
    group already holds a pending row for `keep` (both were candidates for the one
    mention) is a duplicate after the merge and is settled `dropped`, or confirming
    the alias would apply the fact twice. Only rows at `scope` or a descendant it
    covers are touched (where the entity was visible). Returns the prior state of
    every touched row, which `split` restores.
    """
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_pending'"
                  ).fetchone() is None:
        return []
    moved: List[Dict[str, Any]] = []
    for r in db.execute("SELECT id, scope, subject, group_id FROM entity_pending "
                        "WHERE entity_id = ? AND status = 'pending' ORDER BY id",
                        (d["id"],)).fetchall():
        try:
            if not scope.covers(Scope.parse(r["scope"])):
                continue
        except ScopeError:
            continue
        dup = db.execute("SELECT 1 FROM entity_pending WHERE group_id = ? AND entity_id = ? "
                         "AND status = 'pending'", (r["group_id"], k["id"])).fetchone()
        prior = {"id": r["id"], "entity_id": d["id"], "subject": r["subject"],
                 "scope": r["scope"]}
        if dup is not None:
            db.execute("UPDATE entity_pending SET status = 'dropped', resolved = ?, "
                       "outcome = ? WHERE id = ?",
                       (now, f"merged into entity {k['id']} (duplicate)", r["id"]))
            prior["action"] = "dropped"
        else:
            db.execute("UPDATE entity_pending SET entity_id = ?, subject = ? WHERE id = ?",
                       (k["id"], _pending_subject(r["subject"], d["canonical"],
                                                  k["canonical"]), r["id"]))
            prior["action"] = "retargeted"
        moved.append(prior)
    return moved


def _restore_pending(db: sqlite3.Connection, keep_id: int,
                     moved: List[Dict[str, Any]]) -> None:
    """Undo `_retarget_pending` for every row still exactly as the merge left it.

    A row settled since (applied, dropped by a later confirm) stays settled: the
    split cannot un-apply a fact, and it does not guess."""
    for m in moved:
        if m.get("action") == "retargeted":
            db.execute("UPDATE entity_pending SET entity_id = ?, subject = ? WHERE id = ? "
                       "AND entity_id = ? AND status = 'pending'",
                       (m["entity_id"], m["subject"], m["id"], keep_id))
        else:
            db.execute("UPDATE entity_pending SET status = 'pending', resolved = NULL, "
                       "outcome = NULL WHERE id = ? AND status = 'dropped' AND outcome = ?",
                       (m["id"], f"merged into entity {keep_id} (duplicate)"))


def merge(db: sqlite3.Connection, scope: Scope, keep: int, drop: int,
          now: float) -> Dict[str, Any]:
    """Fold entity `drop` into `keep` at exactly `scope`. Reversible with `split`.

    Refused, never guessed around:
    - either id not visible from `scope` (same message as "does not exist");
    - `drop` not living at exactly `scope`: deleting an ancestor's entity is a
      write at a scope the caller did not name;
    - `drop` named by an alias at a scope that is neither `scope` nor one of
      its descendants (unreachable while visibility holds; refused if it ever
      is). An alias a DESCENDANT confirmed for this scope's entity is
      retargeted to `keep` -- the descendant named this scope's entity, which
      is now `keep` -- and `split` puts it back. Refusing instead would let any
      descendant veto the ancestor's merge, and the refusal would tell the
      ancestor what a scope it cannot read had written;
    - `drop` is the survivor of an earlier merge AT THIS SCOPE that is not split
      yet: the later split could not put that earlier merge's aliases back. A
      DESCENDANT's unsplit merge into `drop` is not a refusal (that would be the
      veto and the disclosure ruled out above): its record is re-pointed at
      `keep` -- the descendant's aliases move to `keep` with this merge anyway --
      so the descendant can still split it, and this merge's split points it
      back;
    - one name confirmed for one entity and rejected for the other: a human
      said both "is" and "is not"; settle it first.
    """
    if keep == drop:
        raise ScopeError("an entity cannot be merged into itself")
    k = _entity_visible(db, scope, keep)
    d = _entity_visible(db, scope, drop)
    if d["scope"] != str(scope):
        raise ScopeError(f"entity {drop} lives at {d['scope']}, not {scope}: merge it "
                         f"from there")
    desc_drop: List[Dict[str, Any]] = []
    for r in db.execute("SELECT scope, alias_norm, entity_id, status, evidence, created "
                        "FROM entity_aliases WHERE entity_id = ? AND scope != ? "
                        "ORDER BY scope, alias_norm", (d["id"], str(scope))):
        try:
            under = scope.covers(Scope.parse(r["scope"]))
        except ScopeError:
            under = False
        if not under:
            raise ScopeError(f"entity {drop} is named at a scope {scope} does not cover; "
                             f"refusing to rewrite it")
        desc_drop.append(dict(r))
    desc_keep = [dict(r) for x in desc_drop for r in db.execute(
        "SELECT scope, alias_norm, entity_id, status, evidence, created FROM entity_aliases "
        "WHERE scope = ? AND alias_norm = ? AND entity_id = ?",
        (x["scope"], x["alias_norm"], k["id"]))]
    if db.execute("SELECT 1 FROM entity_merges WHERE keep_id = ? AND scope = ? "
                  "AND split_at IS NULL", (d["id"], str(scope))).fetchone():
        raise ScopeError(f"entity {drop} already absorbed another entity; split that "
                         f"merge before merging {drop} away")
    keep_rows = _alias_rows(db, scope, k["id"])
    drop_rows = _alias_rows(db, scope, d["id"])
    by_norm = {r["alias_norm"]: r for r in keep_rows}
    for r in drop_rows:
        other = by_norm.get(r["alias_norm"])
        if other is not None and {other["status"], r["status"]} == {CONFIRMED, REJECTED}:
            raise ScopeError(f"alias {r['alias_norm']!r} is confirmed for one entity and "
                             f"rejected for the other; settle it before merging")
    snapshot = {"drop_entity": dict(d), "keep_aliases": keep_rows, "drop_aliases": drop_rows,
                "desc_drop_aliases": desc_drop, "desc_keep_aliases": desc_keep}
    held_keep = {(r["scope"], r["alias_norm"]): r for r in desc_keep}
    for r in desc_drop:
        other = held_keep.get((r["scope"], r["alias_norm"]))
        if other is None:
            db.execute("UPDATE entity_aliases SET entity_id = ? WHERE scope = ? AND "
                       "alias_norm = ? AND entity_id = ?",
                       (k["id"], r["scope"], r["alias_norm"], d["id"]))
            continue
        status = (REJECTED if REJECTED in (other["status"], r["status"]) else
                  max((other["status"], r["status"]), key=lambda x: _STATUS_RANK[x]))
        db.execute("UPDATE entity_aliases SET status = ? WHERE scope = ? AND alias_norm = ? "
                   "AND entity_id = ?", (status, r["scope"], r["alias_norm"], k["id"]))
        db.execute("DELETE FROM entity_aliases WHERE scope = ? AND alias_norm = ? AND "
                   "entity_id = ?", (r["scope"], r["alias_norm"], d["id"]))
    moved = 0
    for r in drop_rows:
        other = by_norm.get(r["alias_norm"])
        if other is None:
            _insert_alias(db, r, k["id"])
            moved += 1
            continue
        if REJECTED in (other["status"], r["status"]):
            status = REJECTED  # a tombstone survives: the refusal still stands
        else:
            status = max((other["status"], r["status"]), key=lambda x: _STATUS_RANK[x])
        db.execute("UPDATE entity_aliases SET status = ? WHERE scope = ? AND alias_norm = ? "
                   "AND entity_id = ?", (status, str(scope), r["alias_norm"], k["id"]))
    db.execute("DELETE FROM entity_aliases WHERE scope = ? AND entity_id = ?",
               (str(scope), d["id"]))
    snapshot["pending"] = _retarget_pending(db, scope, k, d, now)
    # A descendant's unsplit merge INTO `drop`: its survivor is now `keep`.
    desc_merges: List[int] = []
    for r in db.execute("SELECT id, scope FROM entity_merges WHERE keep_id = ? AND "
                        "scope != ? AND split_at IS NULL", (d["id"], str(scope))).fetchall():
        try:
            under = scope.covers(Scope.parse(r["scope"]))
        except ScopeError:
            under = False
        if under:
            desc_merges.append(int(r["id"]))
    for mid_ in desc_merges:
        db.execute("UPDATE entity_merges SET keep_id = ? WHERE id = ?", (k["id"], mid_))
    snapshot["desc_merges"] = desc_merges
    db.execute("DELETE FROM entities WHERE id = ?", (d["id"],))
    for _ in range(8):
        mid = secrets.randbelow(_ID_SPACE) + 1
        try:
            db.execute("INSERT INTO entity_merges(id, scope, keep_id, drop_id, snapshot, "
                       "created) VALUES (?,?,?,?,?,?)",
                       (mid, str(scope), k["id"], d["id"], json.dumps(snapshot), now))
        except sqlite3.IntegrityError:
            continue
        # Only rows the caller can READ are reported, and by their public id: a
        # pending row at a descendant scope moved too, but naming it would tell the
        # ancestor that a scope it cannot read holds a private fact about this
        # entity -- the disclosure the no-refusal rule above exists to avoid.
        own = [m["id"] for m in snapshot["pending"] if m.get("scope") == str(scope)]
        from . import pending as _pend  # stdlib-only sibling; imported late, no cycle
        return {"merge_id": mid, "merged_id": d["id"], "keep_id": k["id"],
                "canonical": k["canonical"], "scope": str(scope), "moved": moved,
                "folded": len(drop_rows) - moved,
                "pending_moved": _pend.public_ids(db, own) if own else []}
    raise ScopeError("could not allocate a merge id")


def split(db: sqlite3.Connection, scope: Scope, merged_id: int, now: float) -> Dict[str, Any]:
    """Undo the latest unsplit merge of entity `merged_id` at exactly `scope`.

    Both entities' alias rows at `scope` go back to exactly what they were
    before the merge. An alias the survivor gained AFTER the merge, under a
    name neither had then, stays with the survivor and is listed in
    `kept_on_survivor` — it was never the dropped entity's to take back.
    """
    row = db.execute("SELECT * FROM entity_merges WHERE scope = ? AND drop_id = ? "
                     "AND split_at IS NULL ORDER BY created DESC LIMIT 1",
                     (str(scope), int(merged_id))).fetchone()
    if row is None:
        # The same answer for "never merged" and "merged at a sibling scope".
        raise ScopeError(f"no merge of entity {merged_id} at {scope}")
    later = db.execute("SELECT drop_id FROM entity_merges WHERE scope = ? AND keep_id = ? "
                       "AND split_at IS NULL AND created > ? ORDER BY created DESC LIMIT 1",
                       (str(scope), row["keep_id"], row["created"])).fetchone()
    if later is not None:
        raise ScopeError(f"entity {row['keep_id']} absorbed entity {later['drop_id']} "
                         f"after this merge; split that one first")
    keep = _entity_visible(db, scope, row["keep_id"])
    if db.execute("SELECT 1 FROM entities WHERE id = ?", (int(merged_id),)).fetchone():
        raise ScopeError(f"entity id {merged_id} is in use again; cannot restore it")
    snap = json.loads(row["snapshot"])
    before = {r["alias_norm"] for r in snap["keep_aliases"]} |         {r["alias_norm"] for r in snap["drop_aliases"]}
    extra = [r for r in _alias_rows(db, scope, keep["id"]) if r["alias_norm"] not in before]
    de = snap["drop_entity"]
    db.execute("INSERT INTO entities(id, scope, canonical, created) VALUES (?,?,?,?)",
               (de["id"], de["scope"], de["canonical"], de["created"]))
    db.execute("DELETE FROM entity_aliases WHERE scope = ? AND entity_id = ?",
               (str(scope), keep["id"]))
    for r in snap["keep_aliases"]:
        _insert_alias(db, r, keep["id"])
    for r in snap["drop_aliases"]:
        _insert_alias(db, r, de["id"])
    for r in extra:
        _insert_alias(db, r, keep["id"])
    # Descendant aliases the merge retargeted go back to what they were -- but
    # only while each is still exactly what the merge left. A descendant that
    # decided about the name AFTER the merge (a reject_alias tombstone, a
    # confirm) made its own call on the survivor: the ancestor's split must not
    # erase it, and a refused link must never come back as confirmed.
    desc_keep = {(r["scope"], r["alias_norm"]): r for r in snap.get("desc_keep_aliases", [])}
    for r in snap.get("desc_drop_aliases", []):
        pair = (r["scope"], r["alias_norm"])
        other = desc_keep.get(pair)
        if other is None:
            left = r["status"]
        elif REJECTED in (other["status"], r["status"]):
            left = REJECTED
        else:
            left = max((other["status"], r["status"]), key=lambda x: _STATUS_RANK[x])
        now_row = db.execute("SELECT status FROM entity_aliases WHERE scope = ? AND "
                             "alias_norm = ? AND entity_id = ?",
                             (r["scope"], r["alias_norm"], keep["id"])).fetchone()
        if now_row is None or now_row["status"] != left:
            continue  # the descendant's later decision stands
        db.execute("DELETE FROM entity_aliases WHERE scope = ? AND alias_norm = ? AND "
                   "entity_id = ?", (r["scope"], r["alias_norm"], keep["id"]))
        if other is not None:
            _insert_alias(db, other, keep["id"])
        _insert_alias(db, r, de["id"])
    _restore_pending(db, keep["id"], snap.get("pending", []))
    # A descendant merge this merge re-pointed at the survivor, still unsplit,
    # points back at the restored entity.
    for mid_ in snap.get("desc_merges", []):
        db.execute("UPDATE entity_merges SET keep_id = ? WHERE id = ? AND keep_id = ? "
                   "AND split_at IS NULL", (de["id"], int(mid_), keep["id"]))
    db.execute("UPDATE entity_merges SET split_at = ? WHERE id = ?", (now, row["id"]))
    return {"merge_id": row["id"], "merged_id": de["id"], "keep_id": keep["id"],
            "scope": str(scope), "restored": len(snap["drop_aliases"]),
            "kept_on_survivor": [r["alias_norm"] for r in extra]}
