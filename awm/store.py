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

WHY A VALUE IS NEVER DESTROYED (schema v2)

"I prefer dark mode" in session 1 and "I switched to light mode" in session 5
are not two facts to return side by side, and not one fact to overwrite: the
second SUPERSEDES the first. `recall` returns only the current value; the old
one moves to `memory_history` with the interval it was true for, so `history`
can show the change and `recall(as_of=...)` can answer "what was true then".
`forget` records a deletion there too; only `purge_history`, at exactly one
scope, removes the record.

WHY THE WORLD MODEL LIVES IN THE SAME FILE (schema v3)

`transitions` records what an ACTION did to the slots (see `world.py`): the
state before, the action, the observed delta, the prediction it was scored
against. It is written and read under exactly the scope rules above, so a
sibling's dynamics are as private as its facts, and it sits beside
`memory_history` because "was this change caused by an action?" is a join of
the two.

WHY AN OLDER FILE IS NOT MIGRATED ON OPEN (0.6.0)

Until 0.5.0 an older file was migrated the moment it was opened, and the bump of
`schema_meta` locked every installed older reader out of it: awm 0.3.x refuses
any file that is not v1. One `awm recall` from a newer install was enough to
break every other tool on the machine that shared the file. So an older file
now opens in COMPAT mode: `remember`/`recall`/`forget`/`count` do exactly what
the file's own version does (v1: an in-place upsert, no history), nothing is
written on open, and every newer feature raises `NeedsMigration` naming the
command. `migrate()` / `awm migrate` is the one way forward: a verified byte
backup first, one transaction, row counts and a content digest compared before
and after. `AWM_AUTO_MIGRATE=1` restores the old migrate-on-open behaviour
(still with the backup). A HARD kill (no Python exception) between the backup and
COMMIT leaves the file unmigrated and the backup (or a `.partial`) behind; the next
`migrate` of that file reaps them under its write lock (see `_reap_stale_backups`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import entities as _ent
from . import pending as _pend
from .reconcile import ReconcileError as _ReconcileError
from .scope import Scope, ScopeError, visible_scopes

SCHEMA_VERSION = 3

_SCHEMA_V1 = """
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

#: Added by v2. One statement per entry so the migration can run them inside a
#: single explicit transaction (`executescript` commits as it goes).
_SCHEMA_V2 = (
    """CREATE TABLE IF NOT EXISTS memory_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        scope      TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'fact',
        meta       TEXT NOT NULL DEFAULT '{}',
        valid_from REAL NOT NULL,
        valid_to   REAL NOT NULL,
        reason     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_history_scope_key ON memory_history(scope, key)",
    """CREATE TABLE IF NOT EXISTS entities (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        scope     TEXT NOT NULL,
        canonical TEXT NOT NULL,
        created   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_entities_scope ON entities(scope)",
    """CREATE TABLE IF NOT EXISTS entity_aliases (
        scope      TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        entity_id  INTEGER NOT NULL REFERENCES entities(id),
        status     TEXT NOT NULL,
        evidence   TEXT NOT NULL DEFAULT '',
        created    REAL NOT NULL,
        UNIQUE(scope, alias_norm, entity_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aliases_norm ON entity_aliases(alias_norm)",
)

#: Added by v3: the dynamics table (`world.py`). One statement per entry, run
#: inside the migration's single transaction like v2.
_SCHEMA_V3 = (
    """CREATE TABLE IF NOT EXISTS transitions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        scope          TEXT NOT NULL,
        state_digest   TEXT NOT NULL,
        action         TEXT NOT NULL,
        outcome_json   TEXT NOT NULL,
        next_digest    TEXT NOT NULL,
        ts             REAL NOT NULL,
        before_ts      REAL NOT NULL,
        source         TEXT NOT NULL,
        predicted_json TEXT,
        surprise       REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transitions_sda "
    "ON transitions(scope, state_digest, action)",
    "CREATE INDEX IF NOT EXISTS idx_transitions_action ON transitions(scope, action)",
    "CREATE INDEX IF NOT EXISTS idx_transitions_ts ON transitions(scope, ts)",
    _ent.MERGES_DDL,
    _ent.MERGES_INDEX,
)

#: The minimum file version each feature needs. A feature missing here works on
#: every version. Read by `_require`; the names are what `NeedsMigration` prints.
FEATURE_VERSION: Dict[str, int] = {
    "history": 2, "recall(as_of=...)": 2, "purge_history": 2,
    "reconcile_and_remember": 2, "entities": 2,
    "world model (encode/observe/predict/transitions/surprise)": 3,
    "surprise_stats": 3, "merge_entities / split_entity": 3,
}

#: Environment switch back to migrate-on-open. Anything but "1" means off.
AUTO_MIGRATE_ENV = "AWM_AUTO_MIGRATE"

#: Tables whose row counts `migrate` compares before and after.
_COUNTED_TABLES = ("memories", "memory_history", "entities", "entity_aliases",
                   "transitions", "entity_merges")

#: When the live value became true. A column on `memories`, not derived from
#: `created` or from history: `created` is the key's FIRST write and history can
#: be purged, so either source back-dates the live value over an interval when a
#: different value held. NULL on a row migrated from v1, where the start is
#: unknown: v1 overwrote in place, so the value is only known to hold from
#: `updated` on (see `_start`).
_SINCE_COLUMN = "since"

SUPERSEDED = "superseded"
FORGOTTEN = "forgotten"
CURRENT = "current"


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


@dataclass
class HistoryEntry:
    """One interval a value was true for. `valid_to` is None for the live value."""

    scope: str
    key: str
    value: str
    kind: str
    meta: Dict[str, Any]
    valid_from: float
    valid_to: Optional[float]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"scope": self.scope, "key": self.key, "value": self.value,
                "kind": self.kind, "meta": self.meta, "valid_from": self.valid_from,
                "valid_to": self.valid_to, "reason": self.reason}


class NeedsMigration(RuntimeError):
    """A feature newer than the file was asked for. The file was NOT changed.

    Raised instead of migrating silently: a migration bumps the file past what
    installed older awm versions can read (see the module docstring).
    """

    def __init__(self, feature: str, need: int, found: int, path: Path):
        self.feature, self.need, self.found, self.path = feature, need, found, path
        super().__init__(
            f"{feature} needs memory schema v{need}, but {path} is schema v{found}, "
            f"opened in COMPAT mode so installed awm readers of v{found} keep working. "
            f"Run `awm migrate --db {path}` (writes a verified byte backup first), "
            f"or set {AUTO_MIGRATE_ENV}=1 to migrate on open.")


class MigrationError(RuntimeError):
    """A migration whose before/after verification failed. It was rolled back."""


def _auto_migrate_default() -> bool:
    return os.environ.get(AUTO_MIGRATE_ENV, "").strip() == "1"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_schema(path: Path) -> Dict[str, Any]:
    """The file's schema version, read-only. Never creates, migrates or writes.

    For `awm doctor`: opening a `MemoryStore` on a missing path creates a file,
    and a diagnostic must not change what it diagnoses.
    """
    p = Path(path)
    out: Dict[str, Any] = {"path": str(p), "exists": p.is_file(), "file_version": None,
                           "code_version": SCHEMA_VERSION, "compat": False,
                           "newer": False, "error": None,
                           "auto_migrate": _auto_migrate_default()}
    if not out["exists"]:
        return out
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='schema_meta'").fetchone()
            row = con.execute("SELECT version FROM schema_meta").fetchone() if has else None
        finally:
            con.close()
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if row is None:
        out["error"] = "no schema_meta row: not an awm file, or never initialised"
        return out
    v = int(row[0])
    out["file_version"] = v
    out["compat"] = v < SCHEMA_VERSION
    out["newer"] = v > SCHEMA_VERSION
    return out


def _check_scope(scope: Any) -> Scope:
    if not isinstance(scope, Scope):
        raise ScopeError(f"expected a Scope, got {type(scope).__name__}")
    return scope


def _visible_names(scope: Scope) -> List[str]:
    return [str(s) for s in visible_scopes(_check_scope(scope))]


def _in(names: List[str]) -> str:
    # An exact IN over a computed set. Never a LIKE: `LIKE 'acme:%'` also
    # matches `acmecorp:...`, and the leak is silent.
    return ",".join("?" * len(names))


def _canon_meta(raw: Optional[str]) -> str:
    """Stored meta in the form `remember` writes, so equal dicts compare equal."""
    return json.dumps(json.loads(raw or "{}"), sort_keys=True)


def _memory(r: sqlite3.Row) -> Memory:
    return Memory(scope=r["scope"], key=r["key"], value=r["value"], kind=r["kind"],
                  created=r["created"], updated=r["updated"], hits=r["hits"],
                  meta=json.loads(r["meta"] or "{}"))


class MemoryStore:
    """Scoped memory backed by one SQLite file."""

    def __init__(self, path: Path, *, auto_migrate: Optional[bool] = None,
                 check_same_thread: bool = True, create: bool = True):
        """Open (or create, at the current schema) the file at `path`.

        An OLDER file opens in compat mode and is not written on open; see the
        module docstring. `auto_migrate` (default: env AWM_AUTO_MIGRATE == "1")
        migrates it instead, with a byte backup. `check_same_thread=False` lets a
        host that SERIALISES its calls (one lock around every use) share the
        store across threads; sqlite3's own check refuses that by default.
        `create=False` (every READ path) never creates a file: a missing `path`
        opens an empty in-memory store (`ephemeral` is True) and nothing reaches
        disk. A read that created the file at this schema would lock older
        installed readers out of it for good.
        """
        self.path = Path(path)
        #: True when `create=False` met a missing (or never-initialised, e.g.
        #: 0-byte) file: an empty, in-memory store.
        self.ephemeral = not create and not self.path.exists()
        if not self.ephemeral:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        #: The clock every interval is stamped with. An attribute so a test can
        #: pin it: `as_of` is only testable if "between" is a known instant.
        self._clock: Callable[[], float] = time.time
        #: The schema version of the FILE (not of this code). Below
        #: SCHEMA_VERSION = compat mode.
        self.file_version: int = SCHEMA_VERSION
        self._has_since = True
        self._db = sqlite3.connect(":memory:" if self.ephemeral else str(self.path),
                                   check_same_thread=bool(check_same_thread))
        self._db.row_factory = sqlite3.Row
        if not create and not self.ephemeral and self._uninitialised():
            # An EXISTING file with no awm tables (0 bytes: `sqlite3.connect` on a
            # missing path leaves one, and so does 0.3.x dying between connect and
            # its schema script). A read must not stamp it: stamped at this
            # version, installed 0.3.x refuses it for good, where untouched 0.3.x
            # initialises it v1. Same as a missing file: an empty in-memory store.
            self._db.close()
            self.ephemeral = True
            self._db = sqlite3.connect(":memory:", check_same_thread=bool(check_same_thread))
            self._db.row_factory = sqlite3.Row
        try:
            self._open_schema(_auto_migrate_default() if auto_migrate is None
                              else bool(auto_migrate))
        except BaseException:
            self._db.close()
            raise

    # ------------------------------------------------------------ schema
    @property
    def compat(self) -> bool:
        """True when the file is older than this code and was left as it is.

        Re-read from the file: another process may have migrated it since open.
        """
        self._refresh_version()
        return self.file_version < SCHEMA_VERSION

    def _require(self, feature: str) -> None:
        need = FEATURE_VERSION[feature]
        if self.file_version < need:
            self._refresh_version()
        if self.file_version < need:
            raise NeedsMigration(feature, need, self.file_version, self.path)

    def _refresh_version(self) -> None:
        """Leave compat mode when another process migrated the file under this handle.

        `file_version` was read once, at open. A long-lived handle opened on a v1
        file that kept writing the v1 way after `awm migrate` ran elsewhere would
        overwrite in place in a v3 file: the superseded value never reaches
        `memory_history` and the new row gets no `since`. Every write transaction
        (and every feature check) re-reads the version while the handle is in
        compat mode; a current handle pays nothing. A file that went NEWER than
        this code is refused exactly as at open.
        """
        if self.ephemeral or self.file_version >= SCHEMA_VERSION:
            return
        found = self._file_version()
        if found is None or found <= self.file_version:
            return
        if found > SCHEMA_VERSION:
            raise ScopeError(
                f"{self.path} became schema version {found} while open; this is "
                f"{SCHEMA_VERSION}. Refusing to write it")
        self.file_version = found
        self._has_since = self._has_column("memories", _SINCE_COLUMN)

    def _uninitialised(self) -> bool:
        """True when the open file holds no version row and no awm table (a READ only)."""
        return self._file_version() is None and self._inferred_version() is None

    def _file_version(self) -> Optional[int]:
        if not self._has_table("schema_meta"):
            return None
        row = self._db.execute("SELECT version FROM schema_meta").fetchone()
        return None if row is None else int(row["version"])

    def _inferred_version(self) -> Optional[int]:
        """The version of a file that HOLDS data but has no version row. None = fresh.

        awm 0.3.x creates its tables, then INSERTs the version row: a crash between
        the two leaves `schema_meta` empty over real memories. Such a file is an
        existing file of the version its tables say, never a new one -- stamping it
        at the current version would lock 0.3.x (which re-stamps it v1) out of it.
        A v1-shaped file is v1 even with no rows: 0.3.x re-stamps it v1 on its next
        open, and stamping it v3 here (from a read) would lock 0.3.x out of it. This
        code creates its tables and version row in ONE transaction, so only 0.3.x
        ever leaves such a file; only a file with no `memories` table is new.
        """
        if self._has_table("transitions") or self._has_table("entity_merges"):
            return 3
        if self._has_table("memory_history"):
            return 2
        if self._has_table("memories"):
            return 1
        return None

    def _open_schema(self, auto_migrate: bool) -> None:
        found = self._file_version()
        if found is None:
            found = self._inferred_version()
            if found == SCHEMA_VERSION:
                # Only this code creates the v3 tables, and it stamps them in the
                # same transaction; a lost row is restored, nothing else is written.
                with self._tx():
                    if self._db.execute("SELECT 1 FROM schema_meta").fetchone() is None:
                        self._db.execute("INSERT INTO schema_meta(version) VALUES (?)",
                                         (SCHEMA_VERSION,))
        if found is None:
            # A new (or never-initialised) file: created at the current version,
            # every table and the version row in ONE transaction. Not
            # `executescript`, which commits as it goes: a crash after it would
            # leave a v1-shaped file that `_inferred_version` reads as 0.3.x's.
            with self._tx():
                for stmt in _SCHEMA_V1.split(";"):
                    if stmt.strip():
                        self._db.execute(stmt)
                # Re-read under the write lock: two processes creating the same
                # file must not both stamp a version row.
                if self._db.execute("SELECT 1 FROM schema_meta").fetchone() is None:
                    self._create_v2()
                    self._create_v3()
                    self._db.execute("INSERT INTO schema_meta(version) VALUES (?)",
                                     (SCHEMA_VERSION,))
            found = self._file_version() or SCHEMA_VERSION
        if found > SCHEMA_VERSION:
            raise ScopeError(
                f"{self.path} is schema version {found}, this is "
                f"{SCHEMA_VERSION}. Refusing to read it — a misread row here is "
                f"a memory attributed to the wrong scope")
        self.file_version = found
        if found == SCHEMA_VERSION:
            with self._tx():
                self._ensure_since()
            return
        # Older. Nothing is written here unless the caller opted in.
        self._has_since = self._has_column("memories", _SINCE_COLUMN)
        if auto_migrate:
            self.migrate(backup=True)

    def _has_column(self, table: str, col: str) -> bool:
        return col in {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}

    def _has_table(self, name: str) -> bool:
        return self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (name,)).fetchone() is not None

    def _create_v2(self) -> None:
        for stmt in _SCHEMA_V2:
            self._db.execute(stmt)
        self._ensure_since()

    def _ensure_since(self) -> None:
        if not self._has_column("memories", _SINCE_COLUMN):
            # NULL for every existing row: their values are not touched, and a
            # NULL start reads as "known since `updated`", never earlier.
            self._db.execute(f"ALTER TABLE memories ADD COLUMN {_SINCE_COLUMN} REAL")
        self._has_since = True

    def _create_v3(self) -> None:
        for stmt in _SCHEMA_V3:
            self._db.execute(stmt)

    def _migrate_statements(self, found: int) -> None:
        """Add every table newer than `found`. Runs inside the caller's transaction.

        Existing rows are not touched: v2 and v3 only add tables (and v2 a
        nullable column), so an older row means exactly what it meant before.
        """
        if found < 2:
            self._create_v2()
        else:
            self._ensure_since()
        self._create_v3()
        # DELETE + INSERT, not an UPDATE keyed on `found`: a file whose version was
        # inferred (no row, see `_inferred_version`) must end with exactly one row.
        self._db.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        self._db.execute("DELETE FROM schema_meta")
        self._db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))

    def _table_counts(self, db: Optional[sqlite3.Connection] = None
                      ) -> Dict[str, Optional[int]]:
        """Rows per known table; None for a table the file does not have."""
        db = self._db if db is None else db

        def has(t: str) -> bool:
            return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (t,)).fetchone() is not None
        return {t: (db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    if has(t) else None) for t in _COUNTED_TABLES}

    def _memories_digest(self, db: Optional[sqlite3.Connection] = None) -> str:
        """sha256 over every v1 column of every memory, in id order.

        The v1 columns only: the migration adds `since` (NULL), which is not a
        change to any row's content.
        """
        db = self._db if db is None else db
        h = hashlib.sha256()
        for r in db.execute("SELECT id,scope,key,value,kind,created,updated,hits,meta "
                            "FROM memories ORDER BY id"):
            h.update(json.dumps(list(r), ensure_ascii=False).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def _backup_path(self, found: int) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        base = self.path.with_name(f"{self.path.name}.v{found}-backup-{stamp}")
        cand, n = base, 1
        while cand.exists():
            cand = base.with_name(f"{base.name}-{n}")
            n += 1
        return cand

    def _reap_stale_backups(self, found: int, keep: Optional[Path],
                            keep_sha: Optional[str]) -> List[str]:
        """Remove what a HARD-killed earlier migration of this file left. Returns names.

        A kill gives Python no chance to run the rollback cleanup, so a kill mid-copy
        leaves `<backup>.partial` and a kill after the rename but before COMMIT leaves
        a full backup of a file that was never migrated. Called under the write lock:
        no other migration of this file can be writing a `.partial` now, so every one
        found is a dead process's and is removed. A full backup is removed only when
        its sha256 equals `keep_sha`, the backup just written of the file as it is
        now: a byte-identical duplicate. Anything else (a different state of the file)
        is kept -- this never guesses which backup the owner still wants.
        """
        reaped: List[str] = []
        prefix = f"{self.path.name}.v{found}-backup-"
        for cand in sorted(c for c in self.path.parent.iterdir()
                           if c.name.startswith(prefix)):
            if keep is not None and cand == keep:
                continue
            try:
                if cand.name.endswith(".partial"):
                    cand.unlink()
                elif keep_sha is not None and _sha256_file(cand) == keep_sha:
                    cand.unlink()
                else:
                    continue
            except OSError:
                continue
            reaped.append(cand.name)
        return reaped

    def _write_backup(self, dest: Path, before: Dict[str, Optional[int]],
                      digest_before: str, wal: bool) -> str:
        """Copy the file to ``dest`` atomically and prove it holds the same rows.

        Called under the migration's write lock. A rollback-journal file keeps every
        committed row in the main file, so it is byte-copied and hash-compared. A WAL
        file may hold committed rows only in ``-wal`` (a reader pinned to an older
        snapshot stops a checkpoint from moving them), so a byte copy of the main file
        would silently miss them: it is copied with the SQLite backup API through a
        second connection (which sees every committed frame), and the copy is made a
        self-contained rollback-journal file. Either way the copy is then OPENED and
        its row counts and memories digest compared with ``before``/``digest_before``
        (measured under the same lock): a content check, not main file to main file.

        The copy goes to ``<dest>.partial``, is fsynced and verified, and only then
        renamed onto ``dest`` (``os.replace``). A kill mid-copy therefore leaves at
        most a ``.partial`` file -- never a truncated file under a backup name that
        sorts before a later good one. The partial is unlinked on any exception.
        Returns the backup file's sha256.
        """
        partial = dest.with_name(dest.name + ".partial")
        try:
            if wal:
                src = sqlite3.connect(str(self.path))
                dst = sqlite3.connect(str(partial))
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
                finally:
                    dst.close()
                    src.close()
            else:
                shutil.copyfile(self.path, partial)
            with open(partial, "rb+") as fh:
                os.fsync(fh.fileno())
            dst_sha = _sha256_file(partial)
            if not wal:
                src_sha = _sha256_file(self.path)
                if src_sha != dst_sha:
                    raise MigrationError(f"backup {dest} does not match {self.path} "
                                         f"byte for byte ({dst_sha} != {src_sha})")
            chk = sqlite3.connect(partial.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                got_counts, got_digest = self._table_counts(chk), self._memories_digest(chk)
            finally:
                chk.close()
            if got_counts != before or got_digest != digest_before:
                raise MigrationError(
                    f"backup {dest} does not hold the rows of {self.path} (counts "
                    f"{got_counts} vs {before}, or the memories digest differs); "
                    f"nothing was migrated")
            os.replace(partial, dest)
        except BaseException:
            try:
                partial.unlink()
            except OSError:
                pass
            raise
        return dst_sha

    def migrate(self, *, backup: bool = True, dry_run: bool = False) -> Dict[str, Any]:
        """Bring an older file to the current schema. The ONLY path that does.

        Order: take the write lock; byte-copy the file next to itself and
        compare hashes (`backup`); run every schema step in that same
        transaction; compare table row counts and a digest of every memory
        before and after. Any mismatch raises `MigrationError` and rolls back,
        so the file is either wholly migrated or exactly as it was. `dry_run`
        reports what would happen and writes nothing (no backup either).

        Returns a report dict: path, from, to, migrated, dry_run, backup,
        backup_sha256, before, after, digest_before, digest_after (and a note).
        """
        self._refresh_version()  # a peer may have migrated it since this handle opened
        found = self.file_version
        rep: Dict[str, Any] = {"path": str(self.path), "from": found, "to": SCHEMA_VERSION,
                               "migrated": False, "dry_run": bool(dry_run),
                               "backup": None, "backup_sha256": None,
                               "before": self._table_counts(), "after": None,
                               "digest_before": None, "digest_after": None}
        if found >= SCHEMA_VERSION:
            rep["note"] = "already at the current schema; nothing to do"
            return rep
        if dry_run:
            rep["digest_before"] = self._memories_digest()
            rep["note"] = (f"would back up {self.path.name}, then add the schema "
                           f"v{found + 1}..v{SCHEMA_VERSION} tables in one transaction")
            return rep
        # A byte copy of a WAL file's main file alone would miss committed frames
        # (see `_write_backup`); a checkpoint cannot be relied on to move them.
        wal = str(self._db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(f"migration of {self.path} could not take the write "
                                 f"lock ({exc}); nothing was written") from exc
        committing = False
        try:
            # Counted under the write lock: no peer can change them from here on.
            before = self._table_counts()
            digest_before = self._memories_digest()
            rep["before"], rep["digest_before"] = before, digest_before
            if backup:
                dest = self._backup_path(found)
                rep["backup"], rep["backup_sha256"] = str(dest), self._write_backup(
                    dest, before, digest_before, wal)
                rep["backup_method"] = "sqlite-backup-api" if wal else "byte-copy"
                rep["reaped_backups"] = self._reap_stale_backups(
                    found, dest, rep["backup_sha256"])
            else:
                rep["reaped_backups"] = self._reap_stale_backups(found, None, None)
            self._migrate_statements(found)
            after = self._table_counts()
            digest_after = self._memories_digest()
            rep["after"], rep["digest_after"] = after, digest_after
            changed = [t for t, n in before.items() if n is not None and after.get(t) != n]
            if changed or digest_after != digest_before:
                raise MigrationError(
                    f"migration changed existing rows "
                    f"({', '.join(changed) or 'memories digest'}); rolled back, "
                    f"{self.path} is still schema v{found}")
            # Inside the try: a COMMIT refused by a peer's read lock ("database is
            # locked") must roll back too, or this connection keeps an open
            # transaction that sees v3 tables the file does not have.
            committing = True
            self._db.commit()
        except BaseException as exc:
            self._db.rollback()
            # The file is unchanged, so the backup is a duplicate: remove it rather
            # than leave a "backup" of a migration that never happened. Only an
            # exception reaches here; a hard kill leaves it for the next migrate's
            # `_reap_stale_backups`.
            if rep.get("backup"):
                try:
                    Path(rep["backup"]).unlink()
                except OSError:
                    pass
                rep["backup"] = rep["backup_sha256"] = None
                rep.pop("backup_method", None)
            if committing and isinstance(exc, sqlite3.OperationalError):
                raise MigrationError(
                    f"migration of {self.path} could not commit ({exc}); rolled back, "
                    f"the file is still schema v{found}. Close other readers and retry"
                ) from exc
            raise
        self.file_version = SCHEMA_VERSION
        self._has_since = True
        rep["migrated"] = True
        return rep

    @contextmanager
    def _snapshot(self) -> Iterator[None]:
        """BEGIN (deferred) .. COMMIT: several SELECTs that see ONE state of the file.

        History is read from two tables; without a read transaction a peer's
        write between the two SELECTs yields a view in which a superseded value
        is in neither (it left `memories` after the history SELECT ran).
        """
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN")
        try:
            yield
        finally:
            self._db.commit()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """BEGIN IMMEDIATE .. COMMIT: read-then-write with no other session between.

        `remember` reads the old value before moving it to history; without the
        write lock taken up front, two sessions could both read the same old
        value and one supersession would be recorded twice, the other never.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            # Under the write lock: a migration cannot land between this read and
            # the write that depends on it.
            self._refresh_version()
            yield
        except BaseException:
            self._db.rollback()
            raise
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ intervals
    @staticmethod
    def _start(row: sqlite3.Row) -> float:
        """When the live value in `row` became true.

        `since`, stamped when the value (or its kind/meta) last CHANGED. Not
        `updated`: rewriting the same value bumps `updated` for recency ranking
        without anything changing. Not `created`: that is the key's first write,
        and after a supersede plus `purge_history` it would back-date the live
        value over the purged one's interval. A v1 row has no `since`; v1
        overwrote in place, so its value is only known to hold from `updated`.
        """
        since = row["since"] if "since" in row.keys() else None
        return float(since) if since is not None else float(row["updated"])

    def _now(self, old: Optional[sqlite3.Row]) -> float:
        """The write's timestamp: read INSIDE the write lock, never before `old`'s start.

        Stamped before BEGIN IMMEDIATE, a write that waited for a peer's lock
        commits LAST with an EARLIER time and records an inverted interval that
        no `as_of` can reach. Under the lock the clock only has cross-process
        skew left, and the clamp absorbs it: an interval always has positive
        length.
        """
        now = float(self._clock())
        if old is not None:
            now = max(now, math.nextafter(self._start(old), math.inf))
        return now

    def _archive(self, row: sqlite3.Row, now: float, reason: str) -> None:
        self._db.execute(
            "INSERT INTO memory_history(scope,key,value,kind,meta,valid_from,valid_to,reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (row["scope"], row["key"], row["value"], row["kind"], row["meta"],
             self._start(row), now, reason))

    # ------------------------------------------------------------ writes
    def remember(self, scope: Scope, key: str, value: str, *,
                 kind: str = "fact", meta: Optional[Dict[str, Any]] = None) -> Memory:
        """Write at EXACTLY this scope. Upserts on (scope, key).

        A changed value, kind or meta moves the old version to history first:
        the past's kind and meta are part of what was recorded then, and an
        in-place overwrite would rewrite them. The identical version rewritten
        records nothing (nothing stopped being true) and keeps its start.
        """
        _check_scope(scope)
        if not key or not isinstance(key, str):
            raise ScopeError("key must be a non-empty string")
        s = str(scope)
        payload = json.dumps(meta or {}, sort_keys=True)
        with self._tx():
            # Decided INSIDE the write lock, after `_tx` re-read the version: a
            # peer's migration between open and now switches this write to v3.
            if self.file_version < 2 or not self._has_since:
                return self._remember_compat(s, key, value, kind, meta, payload)
            old = self._db.execute("SELECT * FROM memories WHERE scope=? AND key=?",
                                   (s, key)).fetchone()
            now = self._now(old)
            if old is None:
                self._db.execute(
                    "INSERT INTO memories(scope,key,value,kind,created,updated,meta,since) "
                    "VALUES (?,?,?,?,?,?,?,?)", (s, key, value, kind, now, now, payload, now))
            else:
                changed = ((old["value"], old["kind"], _canon_meta(old["meta"]))
                           != (value, kind, payload))
                if changed:
                    self._archive(old, now, SUPERSEDED)
                since = now if changed else self._start(old)
                self._db.execute(
                    "UPDATE memories SET value=?, kind=?, updated=?, meta=?, since=? "
                    "WHERE scope=? AND key=?", (value, kind, now, payload, since, s, key))
        return Memory(scope=s, key=key, value=value, kind=kind,
                      created=old["created"] if old is not None else now,
                      updated=now, hits=0, meta=dict(meta or {}))

    def _remember_compat(self, s: str, key: str, value: str, kind: str,
                         meta: Optional[Dict[str, Any]], payload: str) -> Memory:
        """The write an older file's own version makes, and nothing more.

        v1 (awm 0.3.x): an in-place upsert, no history, no `since` column. A v2
        file whose `memories` never got `since` keeps its history but writes no
        `since` either: adding the column is a schema change, which is
        `migrate`'s job, not a write's. Runs inside the caller's `_tx`.
        """
        old = self._db.execute("SELECT * FROM memories WHERE scope=? AND key=?",
                               (s, key)).fetchone()
        now = self._now(old)
        if old is not None and self.file_version >= 2 and (
                (old["value"], old["kind"], _canon_meta(old["meta"]))
                != (value, kind, payload)):
            self._archive(old, now, SUPERSEDED)
        self._db.execute(
            "INSERT INTO memories(scope,key,value,kind,created,updated,meta) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value, "
            "kind=excluded.kind, updated=excluded.updated, meta=excluded.meta",
            (s, key, value, kind, now, now, payload))
        return Memory(scope=s, key=key, value=value, kind=kind,
                      created=old["created"] if old is not None else now,
                      updated=now, hits=0, meta=dict(meta or {}))

    def forget(self, scope: Scope, key: str) -> bool:
        """Delete one memory at EXACTLY this scope. Never cascades.

        A delete that walked descendants would let a tenant-level forget silently
        remove a project's memories — destructive, invisible, and impossible to
        undo from here. The deleted value is kept in history (reason
        `forgotten`) so a forget is visible afterwards; `purge_history` erases it.
        """
        _check_scope(scope)
        with self._tx():
            old = self._db.execute("SELECT * FROM memories WHERE scope=? AND key=?",
                                   (str(scope), key)).fetchone()
            if old is None:
                return False
            if self.file_version >= 2:  # v1 has no history: a plain delete, as 0.3.x
                self._archive(old, self._now(old), FORGOTTEN)
            self._db.execute("DELETE FROM memories WHERE scope=? AND key=?",
                             (str(scope), key))
        return True

    def purge_history(self, scope: Scope, key: str) -> int:
        """Erase the past values of one key at EXACTLY this scope. Returns rows removed.

        The only way history is destroyed, and deliberately narrow: an explicit
        call naming the one scope, never a pattern, never descendants.

        The world model holds values too: a transition whose observed delta or
        stored prediction names `key` carries a value of the key verbatim, and
        `predict_outcome` would keep serving it. Every such transition recording a
        value OTHER than the key's live value at this scope is deleted with the
        history (counted in the return value); a purge that left the value one
        table over would not be a purge. That holds while the key is still live --
        rotate-then-purge is the normal case, and the old value sits in the
        transition recorded before the rotation -- and it covers DESCENDANT scopes:
        a descendant's `encode_state` includes this scope's slots, so its
        transitions captured this key's values. A descendant transition naming the
        key for its own shadowing value goes too: a lost learned transition is
        re-learned, a purged value that is still served is a leak. A transition
        whose only value for `key` is the current live one is kept: that value is
        not past, and the purge does not erase what is current.

        A transition need not NAME the key to hold its value: `state_digest` and
        `next_digest` are sha256 over every slot of the state, so a step recorded
        while the key sat unchanged at a past value commits to that value -- a
        low-entropy value (a PIN) is brute-forced from the digest, and
        `predict_outcome` answers RECALLED for a guessed state, confirming it. So a
        transition whose before- or after-state time falls inside a purged
        interval (closed at both ends: a boundary step is ambiguous, and dropping a
        learned step is the safe side) of a value other than the live one goes too
        -- UNLESS its digests are proven not to commit to the key: the state at
        that instant is replayed and a digest reproduced under a prefix that
        excludes the key (or with the key at a non-purged value) keeps the row. A
        digest reproduced WITH the purged value, or reproduced by nothing (not an
        `encode_state` digest), is deleted: the safe side.

        The return value counts the rows removed at EXACTLY this scope. Rows
        removed at a descendant scope are not counted: that count would tell the
        caller how many transitions a scope it cannot read recorded while the value
        held (the disclosure `merge` rules out for the same reason).

        The pending-update log (`entity_pending`) stores the fact text a
        `remember_about` wrote to this key; every such row at this scope whose
        text is not the live value is deleted as well.
        """
        _check_scope(scope)
        self._require("purge_history")
        with self._tx():
            row = self._db.execute("SELECT value FROM memories WHERE scope=? AND key=?",
                                   (str(scope), key)).fetchone()
            live = None if row is None else row[0]
            # Read BEFORE the delete: the intervals say when each past value held.
            spans = [(float(r[0]), float(r[1])) for r in self._db.execute(
                "SELECT valid_from, valid_to, value FROM memory_history "
                "WHERE scope=? AND key=?", (str(scope), key)) if r[2] != live]
            # Transitions FIRST: telling which digests commit to the key replays the
            # state `as_of` their timestamps, which needs the history still present.
            n_tr = (self._purge_transitions(scope, key, live, spans)
                    if self._has_table("transitions") else 0)
            cur = self._db.execute("DELETE FROM memory_history WHERE scope=? AND key=?",
                                   (str(scope), key))
            n = cur.rowcount + n_tr
            if self._has_table("entity_pending"):
                n += self._purge_pending(scope, key, live)
        return n

    def _purge_pending(self, scope: Scope, key: str, live: Optional[str]) -> int:
        """Delete the pending-update rows that carried a past value of `key` here.

        A row APPLIED at exactly this scope under `key`'s subject (the key itself, or
        a namespace it sits under: a reconciler may write a sub-slot) wrote its fact
        text to the key; unless that text is the live value it is a past value, and
        `surprise_log` / `pending_updates(status=None)` would keep serving it. The
        whole fact group goes (rival candidates' `dropped` rows hold the same text).
        Rows still PENDING never reached the key and are left alone.
        """
        groups = [int(r[0]) for r in self._db.execute(
            "SELECT DISTINCT group_id FROM entity_pending WHERE scope=? AND status=? "
            "AND (subject=? OR substr(?, 1, length(subject) + 1) = subject || '.') "
            "AND (? IS NULL OR fact IS NOT ?)",
            (str(scope), _pend.APPLIED, key, key, live, live))]
        n = 0
        for i in range(0, len(groups), 500):
            chunk = groups[i:i + 500]
            n += self._db.execute(
                f"DELETE FROM entity_pending WHERE group_id IN ({_in(chunk)})",
                chunk).rowcount
        return n

    def _purge_transitions(self, scope: Scope, key: str, live: Optional[str],
                           spans: Sequence[Tuple[float, float]] = ()) -> int:
        """Delete transitions at `scope` or a descendant whose delta or prediction holds
        a value of `key` other than `live`. Descendants are matched segment-wise
        (`visible_scopes`), never by string prefix."""
        mine = str(scope)
        scopes: List[str] = []
        for (name,) in self._db.execute("SELECT DISTINCT scope FROM transitions"):
            try:
                if mine in _visible_names(Scope.parse(name)):
                    scopes.append(name)
            except ScopeError:
                continue
        doomed: List[Tuple[int, str]] = []
        for i in range(0, len(scopes), 500):
            doomed += self._doomed_transitions(scopes[i:i + 500], key, live, spans)
        ids = [rid for rid, _ in doomed]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self._db.execute(f"DELETE FROM transitions WHERE id IN ({_in(chunk)})", chunk)
        # Only rows at the caller's own scope are reported (see `purge_history`).
        return sum(1 for _, sc in doomed if sc == mine)

    def _digest_commits_to(self, scope: str, t: float, digest: str, key: str,
                           live: Optional[str]) -> bool:
        """False only when `digest` is PROVEN not to hold a past value of `key`.

        Replays the slots visible from `scope` as of `t` and tries every prefix
        `encode_state` could have been called with (None and every dotted prefix of
        a visible key). A reproduction under a prefix excluding `key`, or with
        `key` at the live value, proves the digest never saw a purged value.
        Nothing reproduced = unknown = True (delete: the safe side).
        """
        from . import world
        slots = world.encode_state(self, Scope.parse(scope), as_of=float(t)).slots
        if digest == world.state_digest({}):
            return False  # a prefix nothing matched yet: the state held no slot at all
        prefixes: set = {None}
        for k in slots:
            parts = k.split(".")
            prefixes.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        for pre in prefixes:
            sub = {k: v for k, v in slots.items() if world._in_prefix(k, pre)}
            if world.state_digest(sub) != digest:
                continue
            return key in sub and sub[key] != live
        return True

    def _doomed_transitions(self, scopes: List[str], key: str, live: Optional[str],
                            spans: Sequence[Tuple[float, float]] = ()
                            ) -> List[Tuple[int, str]]:
        doomed: List[Tuple[int, str]] = []
        for r in self._db.execute("SELECT id, scope, outcome_json, predicted_json, ts, "
                                  "before_ts, state_digest, next_digest "
                                  f"FROM transitions WHERE scope IN ({_in(scopes)})", scopes):
            # The digests commit to every slot of the state: a step taken while a
            # purged value held carries it, named in the delta or not -- unless the
            # state it digested is proven to exclude the key.
            if any(self._digest_commits_to(r["scope"], float(t), dg, key, live)
                   for t, dg in ((r["before_ts"], r["state_digest"]),
                                 (r["ts"], r["next_digest"]))
                   if any(lo <= float(t) <= hi for lo, hi in spans)):
                doomed.append((int(r["id"]), r["scope"]))
                continue
            deltas = [json.loads(r["outcome_json"] or "{}") or {}]
            pred = json.loads(r["predicted_json"]) if r["predicted_json"] else {}
            deltas.append((pred or {}).get("delta") or {})
            # A None delta (the slot was removed) records no value.
            if any(key in d and d[key] is not None and d[key] != live for d in deltas):
                doomed.append((int(r["id"]), r["scope"]))
        return doomed

    # ------------------------------------------------------------ reads
    def recall(self, scope: Scope, *, query: Optional[str] = None,
               limit: int = 20, kind: Optional[str] = None,
               as_of: Optional[float] = None) -> List[Memory]:
        """Read this scope and its ancestors, nearest-weighted. Nothing else.

        `as_of` (unix seconds) answers with the values that were current at that
        instant — from history or the live row — under the same scope rules.
        """
        names = _visible_names(scope)
        if as_of is not None:
            self._require("recall(as_of=...)")
        rows = (self._current_rows(names, kind) if as_of is None
                else self._rows_as_of(names, kind, float(as_of)))

        out: List[Memory] = []
        for m in rows:
            w = Scope.parse(m.scope).weight_for(scope)
            if w <= 0.0:
                # Belt and braces. The IN clause should make this unreachable;
                # if it ever is reachable, dropping the row is the safe answer
                # and a leak is not.
                continue
            if query and query.lower() not in (m.value or "").lower() \
                    and query.lower() not in (m.key or "").lower():
                continue
            m.weight = w
            out.append(m)
        # Nearest scope first, then most recently updated. A platform fact must
        # not outrank a project fact just because it was written first.
        out.sort(key=lambda m: (-m.weight, -m.updated))
        return out[:limit]

    def _current_rows(self, names: List[str], kind: Optional[str]) -> List[Memory]:
        sql = f"SELECT * FROM memories WHERE scope IN ({_in(names)})"
        args: List[Any] = list(names)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [_memory(r) for r in self._db.execute(sql, args).fetchall()]

    def _rows_as_of(self, names: List[str], kind: Optional[str], ts: float) -> List[Memory]:
        """One Memory per (scope, key) whose interval [valid_from, valid_to) holds ts."""
        found: Dict[Tuple[str, str], Memory] = {}
        with self._snapshot():
            hist = self._history_rows(names, None)
        for h in hist:
            if not (h.valid_from <= ts and (h.valid_to is None or ts < h.valid_to)):
                continue
            if kind and h.kind != kind:
                continue
            found[(h.scope, h.key)] = Memory(
                scope=h.scope, key=h.key, value=h.value, kind=h.kind,
                created=h.valid_from, updated=h.valid_from, hits=0, meta=h.meta)
        return list(found.values())

    def _history_rows(self, names: List[str], key: Optional[str]) -> List[HistoryEntry]:
        """Past values plus live ones for `names`, oldest first."""
        where = f"scope IN ({_in(names)})"
        args: List[Any] = list(names)
        if key is not None:
            where += " AND key = ?"
            args.append(key)
        out = [HistoryEntry(r["scope"], r["key"], r["value"], r["kind"],
                            json.loads(r["meta"] or "{}"), r["valid_from"], r["valid_to"],
                            r["reason"])
               for r in self._db.execute(
                   f"SELECT * FROM memory_history WHERE {where} ORDER BY id", args)]
        for r in self._db.execute(f"SELECT * FROM memories WHERE {where}", args):
            out.append(HistoryEntry(r["scope"], r["key"], r["value"], r["kind"],
                                    json.loads(r["meta"] or "{}"), self._start(r),
                                    None, CURRENT))
        # Stable sort: equal starts keep insertion order (history by id, live last).
        out.sort(key=lambda h: h.valid_from)
        return out

    def history(self, scope: Scope, key: str) -> List[HistoryEntry]:
        """Every value `key` has held, oldest first, the live one last (`valid_to` None).

        Same visibility as `recall`: this scope and its ancestors, never a
        sibling — a sibling's PAST values are as private as its current ones.
        """
        names = _visible_names(scope)
        if not key or not isinstance(key, str):
            raise ScopeError("key must be a non-empty string")
        self._require("history")
        with self._snapshot():
            rows = self._history_rows(names, key)
        return [h for h in rows
                if Scope.parse(h.scope).weight_for(scope) > 0.0]

    def count(self, scope: Optional[Scope] = None) -> int:
        if scope is None:
            return self._db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        return self._db.execute("SELECT COUNT(*) c FROM memories WHERE scope=?",
                                (str(scope),)).fetchone()["c"]

    # ------------------------------------------------------------ reconcile
    def reconcile_and_remember(self, scope: Scope, fact: str, *, subject: str,
                               reconciler: Any = None, kind: str = "fact",
                               meta: Optional[Dict[str, Any]] = None,
                               overwrite: bool = False) -> Any:
        """Ask a reconciler whether `fact` adds to, updates or repeats `subject`.

        Candidates are the live facts at EXACTLY `scope` whose key is `subject`
        or starts with `subject.` — the write scope, because a decision to
        "update" an ancestor's fact would be a write at a scope the caller did
        not name. The decision is validated before it is applied; an update
        goes through `remember`, so history keeps the value it replaces.
        Returns the applied `Decision`.

        An update may only supersede a value this path owns: one written by a
        reconcile (its meta names a subject) with the same `kind`. The subject is
        derived from the fact's own text by some callers, so without this a line
        like "deploy.host: evil" would silently replace an owner's `rule` written
        by key, and downgrade its kind. Any other target raises `ReconcileError`
        unless `overwrite=True` -- the same consent `awm_remember` asks for.
        Owned means owned BY THIS SUBJECT: the target's stored `subject` must be
        `subject`. A slot reconciled under its own subject (`deploy.approver`) is
        a candidate of the wider subject `deploy`, but a fact reconciled as
        `deploy` (possibly a line quoting a tool output) must not let a model
        rewrite it with a value planted in that text. `meta` is stored with the
        fact (subject and reason are always added).

        An `add` is refused the same way when it would SHADOW an ancestor's row
        this path does not own: the nearest ancestor holding the key holds it at
        another `kind` (an owner `rule` under a `fact` reconcile), or as a `rule`
        not reconciled under this subject. The new row would win `encode_state`
        (nearest scope) and rank first in recall, so an add of `subject.x` a model
        was talked into by the fact text would override the owner's rule at this
        scope without ever naming it. `overwrite=True` is the consent.
        """
        from .reconcile import SlotReconciler, bind_subject, validate

        _check_scope(scope)
        self._require("reconcile_and_remember")
        if not isinstance(subject, str) or not subject.strip():
            raise ScopeError("subject must be a non-empty string")
        if not isinstance(fact, str) or not fact.strip():
            raise ScopeError("fact must be a non-empty string")
        subject = subject.strip()
        candidates = self._subject_candidates(scope, subject)
        rec = bind_subject(reconciler if reconciler is not None else SlotReconciler(),
                           subject)
        decision = validate(rec.decide(fact, candidates), candidates, subject)
        if decision.action == "update" and not overwrite:
            target = next(c for c in candidates if c.key == decision.key)
            owner = (target.meta or {}).get("subject")
            if owner and target.kind == kind and owner != subject:
                raise _ReconcileError(
                    f"update would supersede {decision.key!r} at {scope}, which was "
                    f"reconciled under subject {owner!r}, not {subject!r}; reconcile "
                    f"it under its own subject, or pass overwrite=True to replace it")
            if target.kind != kind or not owner:
                raise _ReconcileError(
                    f"update would supersede {decision.key!r} at {scope} "
                    f"(kind {target.kind!r}, "
                    f"{'reconciled' if (target.meta or {}).get('subject') else 'written by key'})"
                    f", which a {kind!r} reconcile does not own; pass overwrite=True "
                    f"to replace it")
        if decision.action == "add" and not overwrite:
            held = self._nearest_ancestor_row(scope, decision.key)
            if held is not None:
                owner = (held.meta or {}).get("subject")
                if held.kind != kind or (held.kind == "rule" and owner != subject):
                    raise _ReconcileError(
                        f"add of {decision.key!r} at {scope} would shadow the "
                        f"{held.kind!r} held at {held.scope} "
                        f"({'reconciled under ' + repr(owner) if owner else 'written by key'})"
                        f", which a {kind!r} reconcile under {subject!r} does not own; "
                        f"pass overwrite=True to shadow it")
        if decision.action in ("add", "update"):
            payload = dict(meta or {})
            payload.update({"subject": subject, "reason": decision.reason})
            self.remember(scope, decision.key, decision.value, kind=kind, meta=payload)
        return decision

    def _nearest_ancestor_row(self, scope: Scope, key: str) -> Optional[Memory]:
        """The row for `key` at the nearest STRICT ancestor of `scope`, or None."""
        names = [n for n in _visible_names(scope) if n != str(scope)]
        if not names:
            return None
        rank = {n: i for i, n in enumerate(names)}
        rows = [_memory(r) for r in self._db.execute(
            f"SELECT * FROM memories WHERE key = ? AND scope IN ({_in(names)})",
            [key, *names])]
        return min(rows, key=lambda m: rank[m.scope]) if rows else None

    def _subject_candidates(self, scope: Scope, subject: str) -> List[Memory]:
        # substr, not LIKE: a subject containing `%` or `_` must not widen.
        prefix = subject + "."
        rows = self._db.execute(
            "SELECT * FROM memories WHERE scope = ? AND (key = ? OR substr(key, 1, ?) = ?) "
            "ORDER BY key", (str(scope), subject, len(prefix), prefix)).fetchall()
        return [_memory(r) for r in rows]

    # ------------------------------------------------------------ entities
    def resolve_entity(self, scope: Scope, mention: str) -> _ent.Resolution:
        """Who is `mention`? Confirmed, possible (nothing merged), or new. See entities.py."""
        _check_scope(scope)
        self._require("entities")
        with self._tx():
            return _ent.resolve(self._db, scope, mention, self._clock())

    def confirm_alias(self, scope: Scope, alias: str, entity_id: int, *,
                      reconciler: Any = None) -> _ent.Resolution:
        """Promote a possible alias to confirmed at exactly `scope`; drop its rivals.

        Facts held pending against this (alias, entity) by `remember_about` are then
        applied through `reconcile_and_remember` (an update keeps the old value in
        history); the same facts pending against the rival candidates are dropped.
        `Resolution.applied` lists each applied fact and its decision.
        """
        _check_scope(scope)
        self._require("entities")
        norm = _ent.compact(_ent.normalize(alias))
        with self._tx():
            res = _ent.confirm(self._db, scope, alias, entity_id, self._clock())
            held = _pend.rows(self._db, [str(scope)], alias_norm=norm,
                              scope_exact=str(scope))
        applied = []
        for p in held:
            if p.entity_id != res.entity_id:
                continue
            try:
                dec = self.reconcile_and_remember(scope, p.fact, subject=p.subject,
                                                  reconciler=reconciler)
            except _ReconcileError as exc:
                # Confirming WHO a mention names is not consent to replace a value
                # the reconcile path does not own: the fact stays pending, reported.
                applied.append({"pending_id": p.id, "fact": p.fact, "subject": p.subject,
                                "decision": None, "refused": str(exc)})
                continue
            with self._tx():
                now = self._clock()
                _pend.settle(self._db, p.rid, _pend.APPLIED, now, f"confirmed: {dec.action}")
                for rival in held:
                    if rival.group_id == p.group_id and rival.id != p.id:
                        _pend.settle(self._db, rival.rid, _pend.DROPPED, now,
                                     f"alias confirmed as entity {res.entity_id}")
            applied.append({"pending_id": p.id, "fact": p.fact, "subject": p.subject,
                            "decision": dec.to_dict()})
        res.applied = applied
        return res

    def reject_alias(self, scope: Scope, alias: str, entity_id: int) -> bool:
        """Refuse one alias -> entity link at exactly `scope`. True if it was live.

        Facts pending against this (alias, entity) are dropped; a fact whose every
        candidate is now rejected is marked ``orphaned`` -- written nowhere.
        """
        _check_scope(scope)
        self._require("entities")
        norm = _ent.compact(_ent.normalize(alias))
        with self._tx():
            now = self._clock()
            live = _ent.reject(self._db, scope, alias, entity_id, now)
            for p in _pend.rows(self._db, [str(scope)], alias_norm=norm,
                                entity_id=int(entity_id), scope_exact=str(scope)):
                _pend.settle(self._db, p.rid, _pend.DROPPED, now, "alias rejected")
                _pend.orphan_if_last(self._db, p.rgroup, now)
            return live

    # ------------------------------------------------------------ ambiguous mentions
    def remember_about(self, scope: Scope, mention: str, fact: str, *,
                       prefix: str = "people", reconciler: Any = None) -> _pend.AboutResult:
        """Record `fact` about the person/thing `mention` names. See pending.py.

        CONFIRMED (or a brand-new entity): `reconcile_and_remember` under subject
        ``<prefix>.<slug(canonical)>``. POSSIBLE: written to NO entity -- held as a
        pending update against every candidate, flagged in `recall_entity` and in
        `surprise_log` (kind ``ambiguous_entity``) until `confirm_alias` applies it
        or `reject_alias` drops it.
        """
        _check_scope(scope)
        self._require("entities")
        if not isinstance(fact, str) or not fact.strip():
            raise ScopeError("fact must be a non-empty string")
        if not isinstance(prefix, str) or not prefix.strip():
            raise ScopeError("prefix must be a non-empty string")
        res = self.resolve_entity(scope, mention)
        if res.status == _ent.CONFIRMED and res.entity_id is not None:
            subject = f"{prefix.strip()}.{_pend.slug(res.canonical or mention)}"
            dec = self.reconcile_and_remember(scope, fact, subject=subject,
                                              reconciler=reconciler)
            return _pend.AboutResult(_ent.CONFIRMED, res.entity_id, res.canonical,
                                     subject, decision=dec)
        norm = _ent.compact(_ent.normalize(mention))
        names = _visible_names(scope)
        cands = []
        with self._tx():
            for eid in res.possible:
                ent = self._db.execute("SELECT canonical FROM entities WHERE id = ?",
                                       (int(eid),)).fetchone()
                if ent is None:
                    continue
                ev = self._db.execute(
                    f"SELECT evidence FROM entity_aliases WHERE alias_norm = ? AND "
                    f"entity_id = ? AND status = ? AND scope IN "
                    f"({','.join('?' * len(names))}) LIMIT 1",
                    (norm, int(eid), _ent.POSSIBLE, *names)).fetchone()
                cands.append({"entity_id": int(eid),
                              "subject": f"{prefix.strip()}.{_pend.slug(ent['canonical'])}",
                              "evidence": ev["evidence"] if ev else "possible link"})
            held = _pend.insert(self._db, str(scope), mention.strip(), norm, cands,
                                fact.strip(), self._clock())
        return _pend.AboutResult(_ent.POSSIBLE, None, None, None, pending=held,
                                 candidates=[c["entity_id"] for c in cands])

    def pending_updates(self, scope: Scope, *, entity_id: Optional[int] = None,
                        status: Optional[str] = _pend.PENDING) -> List[_pend.PendingUpdate]:
        """Pending facts visible from `scope` (status None = every status)."""
        _check_scope(scope)
        self._require("entities")
        return _pend.rows(self._db, _visible_names(scope), entity_id=entity_id,
                          status=status)

    def recall_entity(self, scope: Scope, entity_id: int, *,
                      prefix: str = "people") -> _pend.EntityRecall:
        """The entity's current facts (keys under its subject) PLUS its pending updates."""
        _check_scope(scope)
        self._require("entities")
        ent = {e.id: e for e in self.entities(scope)}.get(int(entity_id))
        if ent is None:
            raise ScopeError(f"no entity {entity_id} visible from {scope}")
        subject = f"{prefix.strip()}.{_pend.slug(ent.canonical)}"
        cur = [m for m in self.recall(scope, limit=10_000)
               if m.key == subject or m.key.startswith(subject + ".")]
        return _pend.EntityRecall(ent.id, ent.canonical, subject, cur,
                                  self.pending_updates(scope, entity_id=ent.id))

    def entities(self, scope: Scope) -> List[_ent.Entity]:
        """Entities visible from `scope` (it and its ancestors), nearest first."""
        _check_scope(scope)
        self._require("entities")
        return _ent.list_entities(self._db, scope)

    def merge_entities(self, scope: Scope, keep: int, drop: int) -> Dict[str, Any]:
        """`drop` is the same entity as `keep`: move its aliases, reversibly.

        A write at exactly `scope` (see `entities.merge` for the refusals). The
        returned `merged_id` (the dropped entity's id) is what `split_entity`
        takes to undo it.
        """
        _check_scope(scope)
        self._require("merge_entities / split_entity")
        with self._tx():
            # A v3 file written by 0.5.0 predates the table: adding a table is
            # invisible to a 0.5.0 reader, so no version bump is needed.
            self._db.execute(_ent.MERGES_DDL)
            self._db.execute(_ent.MERGES_INDEX)
            return _ent.merge(self._db, scope, int(keep), int(drop), self._clock())

    def split_entity(self, scope: Scope, merged_id: int) -> Dict[str, Any]:
        """Undo the merge of entity `merged_id` at exactly `scope`: prior alias sets back."""
        _check_scope(scope)
        self._require("merge_entities / split_entity")
        with self._tx():
            if not self._has_table("entity_merges"):
                raise ScopeError(f"no merge of entity {merged_id} at {scope}")
            return _ent.split(self._db, scope, int(merged_id), self._clock())

    # ------------------------------------------------------------ world model
    # Thin delegates: the logic and its reasoning live in world.py. All need v3.
    _WORLD = "world model (encode/observe/predict/transitions/surprise)"

    def encode_state(self, scope: Scope, *, prefix: Optional[str] = None,
                     as_of: Optional[float] = None) -> Any:
        """s_t: the slots visible from `scope` (under `prefix`), now or `as_of`."""
        from . import world
        self._require(self._WORLD)
        return world.encode_state(self, scope, prefix=prefix, as_of=as_of)

    def observe_transition(self, scope: Scope, before: Any, action: Any, after: Any,
                           predicted: Any = None, *, predictor: Any = None,
                           source: str = "observed") -> Any:
        """Record (before, action) -> after at EXACTLY `scope`, with its surprise."""
        from . import world
        self._require(self._WORLD)
        return world.observe_transition(self, scope, before, action, after, predicted,
                                        predictor=predictor, source=source)

    def predict_outcome(self, scope: Scope, state: Any, action: Any,
                        predictor: Any = None) -> Any:
        """RECALLED, else PREDICTED (on a miss), else GENERALIZED, else NONE."""
        from . import world
        self._require(self._WORLD)
        return world.predict_outcome(self, scope, state, action, predictor)

    def transitions(self, scope: Scope, **filters: Any) -> List[Any]:
        """Recorded transitions visible from `scope`, oldest first."""
        from . import world
        self._require(self._WORLD)
        return world.transitions(self, scope, **filters)

    def clear_transitions(self, scope: Scope) -> int:
        """Delete every transition recorded at EXACTLY `scope`. Returns rows removed.

        The reset of one agent's dynamics. Never ancestors, never descendants: the
        same narrowness as `forget` and `purge_history`.
        """
        _check_scope(scope)
        self._require(self._WORLD)
        with self._tx():
            cur = self._db.execute("DELETE FROM transitions WHERE scope = ?", (str(scope),))
        return cur.rowcount

    def unexplained_changes(self, scope: Scope, since: float,
                            until: Optional[float] = None, *,
                            prefix: Optional[str] = None) -> List[Any]:
        """Slot changes in [since, until] no recorded transition accounts for."""
        from . import world
        self._require(self._WORLD)
        return world.unexplained_changes(self, scope, since, until, prefix=prefix)

    def surprise_log(self, scope: Scope, since: float, until: Optional[float] = None, *,
                     prefix: Optional[str] = None, include_zero: bool = False) -> List[Any]:
        """Scored transitions and unexplained changes, oldest first."""
        from . import world
        self._require(self._WORLD)
        return world.surprise_log(self, scope, since, until, prefix=prefix,
                                  include_zero=include_zero)

    def surprise_stats(self, scope: Scope, since: float = 0.0,
                       until: Optional[float] = None, *, prefix: Optional[str] = None,
                       buckets: int = 5) -> Any:
        """Surprise distribution + confidence calibration over [since, until]. See world.py."""
        from . import world
        self._require("surprise_stats")
        return world.surprise_stats(self, scope, since, until, prefix=prefix,
                                    buckets=buckets)
