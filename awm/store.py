"""A portable, scoped agent memory. SQLite, no service, no network.

`remember` writes at exactly one scope. `recall` reads that scope and its
ancestors, weighted by distance, and nothing else.

WHY SQLITE AND WHY THE FILTERING IS IN PYTHON

The scope check is the security boundary, and it is done in `Scope.covers` —
segment-wise — rather than in SQL. A `LIKE 'acme:%'` is one keystroke from
matching `acmecorp:...`, and the failure is silent: the query returns rows, the
agent answers, and one customer's memory has entered another's context. So SQL
narrows by an exact set of scope strings computed in Python, and never by a
pattern.

That set is small by construction — a scope has at most three ancestors — so
"filter in Python" costs an `IN (?,?,?,?)` and buys a boundary that can be
read, tested and reasoned about in one function.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scope import Scope, ScopeError, visible_scopes

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scope     TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'fact',
    created   REAL NOT NULL,
    updated   REAL NOT NULL,
    hits      INTEGER NOT NULL DEFAULT 0,
    meta      TEXT NOT NULL DEFAULT '{}',
    UNIQUE(scope, key)
);
CREATE INDEX IF NOT EXISTS idx_scope ON memories(scope);
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
"""


@dataclass
class Memory:
    scope: str
    key: str
    value: str
    kind: str
    created: float
    updated: float
    hits: int
    meta: Dict[str, Any]
    weight: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        d = {"scope": self.scope, "key": self.key, "value": self.value,
             "kind": self.kind, "created": self.created,
             "updated": self.updated, "hits": self.hits, "meta": self.meta}
        d["weight"] = self.weight
        return d


class MemoryStore:
    """Scoped memory backed by one SQLite file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        row = self._db.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            self._db.execute("INSERT INTO schema_meta(version) VALUES (?)",
                             (SCHEMA_VERSION,))
            self._db.commit()
        elif row["version"] != SCHEMA_VERSION:
            raise ScopeError(
                f"{self.path} is schema version {row['version']}, this is "
                f"{SCHEMA_VERSION}. Refusing to read it — a misread row here is "
                f"a memory attributed to the wrong scope")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def remember(self, scope: Scope, key: str, value: str, *,
                 kind: str = "fact", meta: Optional[Dict[str, Any]] = None) -> Memory:
        """Write at EXACTLY this scope. Upserts on (scope, key)."""
        if not isinstance(scope, Scope):
            raise ScopeError(f"expected a Scope, got {type(scope).__name__}")
        if not key or not isinstance(key, str):
            raise ScopeError("key must be a non-empty string")
        now = time.time()
        s = str(scope)
        payload = json.dumps(meta or {}, sort_keys=True)
        self._db.execute(
            "INSERT INTO memories(scope,key,value,kind,created,updated,meta) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value, "
            "kind=excluded.kind, updated=excluded.updated, meta=excluded.meta",
            (s, key, value, kind, now, now, payload))
        self._db.commit()
        return Memory(scope=s, key=key, value=value, kind=kind, created=now,
                      updated=now, hits=0, meta=dict(meta or {}))

    def recall(self, scope: Scope, *, query: Optional[str] = None,
               limit: int = 20, kind: Optional[str] = None) -> List[Memory]:
        """Read this scope and its ancestors, nearest-weighted. Nothing else."""
        if not isinstance(scope, Scope):
            raise ScopeError(f"expected a Scope, got {type(scope).__name__}")
        wanted = visible_scopes(scope)
        names = [str(s) for s in wanted]
        # An exact IN over a computed set. Never a LIKE: `LIKE 'acme:%'` also
        # matches `acmecorp:...`, and the leak is silent.
        placeholders = ",".join("?" * len(names))
        sql = f"SELECT * FROM memories WHERE scope IN ({placeholders})"
        args: List[Any] = list(names)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = self._db.execute(sql, args).fetchall()

        out: List[Memory] = []
        for r in rows:
            src = Scope.parse(r["scope"])
            w = src.weight_for(scope)
            if w <= 0.0:
                # Belt and braces. The IN clause should make this unreachable;
                # if it ever is reachable, dropping the row is the safe answer
                # and a leak is not.
                continue
            if query and query.lower() not in (r["value"] or "").lower() \
                    and query.lower() not in (r["key"] or "").lower():
                continue
            out.append(Memory(scope=r["scope"], key=r["key"], value=r["value"],
                              kind=r["kind"], created=r["created"],
                              updated=r["updated"], hits=r["hits"],
                              meta=json.loads(r["meta"] or "{}"), weight=w))
        # Nearest scope first, then most recently updated. A platform fact must
        # not outrank a project fact just because it was written first.
        out.sort(key=lambda m: (-m.weight, -m.updated))
        return out[:limit]

    def forget(self, scope: Scope, key: str) -> bool:
        """Delete one memory at EXACTLY this scope. Never cascades.

        A delete that walked descendants would let a tenant-level forget silently
        remove a project's memories — destructive, invisible, and impossible to
        undo from here.
        """
        cur = self._db.execute("DELETE FROM memories WHERE scope=? AND key=?",
                               (str(scope), key))
        self._db.commit()
        return cur.rowcount > 0

    def count(self, scope: Optional[Scope] = None) -> int:
        if scope is None:
            return self._db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        return self._db.execute("SELECT COUNT(*) c FROM memories WHERE scope=?",
                                (str(scope),)).fetchone()["c"]
