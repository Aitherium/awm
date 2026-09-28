"""Facts about an AMBIGUOUS mention: held as pending updates, never silently diverged.

The failure this exists for (live eval, 2026-09-27): "VS relocated to Pune" named a
mention with only a POSSIBLE link to the known entity Vansh. The caller split "VS" off
as a new person, the fact landed there, and "Which city does Vansh live in?" kept
answering the stale "Delhi" -- two entities, each confidently half right.

The rule: a fact about a mention that is only POSSIBLY an existing entity is written
to NO entity. It is recorded once per candidate as a pending update:

* ``MemoryStore.recall_entity(scope, entity_id)`` returns the entity's current facts
  PLUS its pending updates, each flagged with the mention and its evidence;
* ``MemoryStore.surprise_log`` reports each one as an event of kind
  ``ambiguous_entity`` (score 1.0: the model could not place a fact it was given);
* ``MemoryStore.confirm_alias(scope, mention, entity_id)`` applies that candidate's
  pending facts through ``reconcile_and_remember`` -- so an update moves the old value
  to history exactly like any other update -- and drops the rival candidates' rows;
* ``MemoryStore.reject_alias`` drops that candidate's rows; when the last candidate
  of a fact is rejected the fact becomes ``orphaned`` (listed, never written anywhere
  a caller did not choose).

Storage is one table added lazily, like ``entity_merges``: a new table is invisible to
an older v3 reader, so no schema version bump is needed. Stdlib only.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PENDING = "pending"
APPLIED = "applied"
DROPPED = "dropped"
ORPHANED = "orphaned"
STATUSES = (PENDING, APPLIED, DROPPED, ORPHANED)

#: surprise_log event kind for a fact recorded against a POSSIBLE link.
KIND_AMBIGUOUS = "ambiguous_entity"

PENDING_DDL = """CREATE TABLE IF NOT EXISTS entity_pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    mention    TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    entity_id  INTEGER NOT NULL,
    subject    TEXT NOT NULL,
    fact       TEXT NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '',
    group_id   INTEGER NOT NULL,
    created    REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    resolved   REAL,
    outcome    TEXT
)"""
PENDING_INDEX = ("CREATE INDEX IF NOT EXISTS idx_entity_pending_entity "
                 "ON entity_pending(scope, entity_id, status)")


def slug(name: str) -> str:
    """Canonical name -> subject segment: casefolded, runs of non-word chars to '_'.

    Letters and digits of EVERY script are kept (NFKC first, so width variants
    agree). An ASCII-only slug mapped every CJK, Cyrillic or Arabic name to
    'unknown' and stripped accents ('Zoë' == 'Zo'): distinct people shared one
    fact slot, and the second one's fact superseded the first's. A name with no
    letter or digit at all gets a digest of its text, never a shared constant.
    An ASCII name slugs exactly as before.
    """
    text = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"[\W_]+", "_", text.casefold()).strip("_")
    if s:
        return s
    if not text.strip():
        return "unknown"
    return "n" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class PendingUpdate:
    """One fact held against one candidate entity until the link is decided."""

    id: int
    scope: str
    mention: str
    entity_id: int
    subject: str
    fact: str
    evidence: str
    group_id: int
    created: float
    status: str
    resolved: Optional[float] = None
    outcome: Optional[str] = None
    #: The row's in-file id and group (never shown: see _pubid.py). `id` and
    #: `group_id` above are the opaque public forms.
    rid: int = field(default=0, repr=False, compare=False)
    rgroup: int = field(default=0, repr=False, compare=False)

    @property
    def flag(self) -> str:
        return (f"UNCONFIRMED: {self.mention!r} may be this entity ({self.evidence}); "
                f"pending update: {self.fact}")

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "scope": self.scope, "mention": self.mention,
                "entity_id": self.entity_id, "subject": self.subject, "fact": self.fact,
                "evidence": self.evidence, "group_id": self.group_id,
                "created": self.created, "status": self.status,
                "resolved": self.resolved, "outcome": self.outcome, "flag": self.flag}


@dataclass
class AboutResult:
    """What ``remember_about`` did: wrote through reconcile, or held the fact pending."""

    status: str                      # "confirmed" | "possible"
    entity_id: Optional[int]
    canonical: Optional[str]
    subject: Optional[str]
    decision: Any = None             # reconcile Decision when written
    pending: List[PendingUpdate] = field(default_factory=list)
    candidates: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        dec = self.decision.to_dict() if hasattr(self.decision, "to_dict") else self.decision
        return {"status": self.status, "entity_id": self.entity_id,
                "canonical": self.canonical, "subject": self.subject, "decision": dec,
                "pending": [p.to_dict() for p in self.pending],
                "candidates": list(self.candidates)}


@dataclass
class EntityRecall:
    """An entity's current facts plus every fact still pending against it."""

    entity_id: int
    canonical: str
    subject: str
    current: List[Any]
    pending: List[PendingUpdate]

    def to_dict(self) -> Dict[str, Any]:
        return {"entity_id": self.entity_id, "canonical": self.canonical,
                "subject": self.subject,
                "current": [m.to_dict() for m in self.current],
                "pending": [p.to_dict() for p in self.pending]}


def ensure(db: sqlite3.Connection) -> None:
    db.execute(PENDING_DDL)
    db.execute(PENDING_INDEX)


def has_table(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                      "name='entity_pending'").fetchone() is not None


def public_ids(db: sqlite3.Connection, ids: List[int]) -> List[int]:
    """The opaque ids callers see for in-file pending row ids (see _pubid.py)."""
    from . import _pubid  # stdlib-only sibling
    key = _pubid.file_key(db)
    return [_pubid.public_id(key, "entity_pending", i) for i in ids]


def _row(r: sqlite3.Row, key: bytes) -> PendingUpdate:
    from . import _pubid  # stdlib-only sibling
    return PendingUpdate(id=_pubid.public_id(key, "entity_pending", r["id"]),
                         scope=r["scope"], mention=r["mention"],
                         entity_id=r["entity_id"], subject=r["subject"], fact=r["fact"],
                         evidence=r["evidence"],
                         group_id=_pubid.public_id(key, "entity_pending", r["group_id"]),
                         created=r["created"], status=r["status"], resolved=r["resolved"],
                         outcome=r["outcome"], rid=int(r["id"]), rgroup=int(r["group_id"]))


def _key(db: sqlite3.Connection) -> bytes:
    from . import _pubid  # stdlib-only sibling
    return _pubid.file_key(db)


def insert(db: sqlite3.Connection, scope: str, mention: str, alias_norm: str,
           candidates: List[Dict[str, Any]], fact: str, now: float) -> List[PendingUpdate]:
    """One row per candidate, sharing one group id (the fact). Caller holds the tx."""
    ensure(db)
    ids: List[int] = []
    group = None
    for c in candidates:
        cur = db.execute(
            "INSERT INTO entity_pending(scope, mention, alias_norm, entity_id, subject, "
            "fact, evidence, group_id, created, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (scope, mention, alias_norm, int(c["entity_id"]), c["subject"], fact,
             c.get("evidence") or "", group if group is not None else 0, now, PENDING))
        rid = int(cur.lastrowid)
        if group is None:
            group = rid
            db.execute("UPDATE entity_pending SET group_id = ? WHERE id = ?", (group, rid))
        ids.append(rid)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    key = _key(db)
    return [_row(r, key) for r in db.execute(
        f"SELECT * FROM entity_pending WHERE id IN ({marks}) ORDER BY id", ids)]


def rows(db: sqlite3.Connection, names: List[str], *, entity_id: Optional[int] = None,
         status: Optional[str] = PENDING, alias_norm: Optional[str] = None,
         scope_exact: Optional[str] = None, since: Optional[float] = None,
         until: Optional[float] = None) -> List[PendingUpdate]:
    if not has_table(db):
        return []
    sql = f"SELECT * FROM entity_pending WHERE scope IN ({','.join('?' * len(names))})"
    args: List[Any] = list(names)
    if scope_exact is not None:
        sql += " AND scope = ?"
        args.append(scope_exact)
    if entity_id is not None:
        sql += " AND entity_id = ?"
        args.append(int(entity_id))
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    if alias_norm is not None:
        sql += " AND alias_norm = ?"
        args.append(alias_norm)
    if since is not None:
        sql += " AND created >= ?"
        args.append(float(since))
    if until is not None:
        sql += " AND created <= ?"
        args.append(float(until))
    got = db.execute(sql + " ORDER BY created, id", args).fetchall()
    if not got:
        return []
    key = _key(db)
    return [_row(r, key) for r in got]


def settle(db: sqlite3.Connection, row_id: int, status: str, now: float,
           outcome: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown pending status {status!r}")
    db.execute("UPDATE entity_pending SET status = ?, resolved = ?, outcome = ? "
               "WHERE id = ?", (status, now, outcome, int(row_id)))


def orphan_if_last(db: sqlite3.Connection, group_id: int, now: float) -> bool:
    """When no row of a fact's group is pending or applied, mark the group orphaned."""
    live = db.execute("SELECT 1 FROM entity_pending WHERE group_id = ? AND status IN (?, ?)",
                      (int(group_id), PENDING, APPLIED)).fetchone()
    if live is not None:
        return False
    db.execute("UPDATE entity_pending SET status = ?, outcome = ? WHERE group_id = ? "
               "AND id = (SELECT MIN(id) FROM entity_pending WHERE group_id = ?)",
               (ORPHANED, "every candidate rejected; the fact is written nowhere", int(group_id),
                int(group_id)))
    return True


__all__ = ["APPLIED", "AboutResult", "DROPPED", "EntityRecall", "KIND_AMBIGUOUS",
           "ORPHANED", "PENDING", "PendingUpdate", "slug"]
