"""Optional couplings to the rest of the aw family. Every one is GUARDED.

awm is SQLite, no service, no network, and a brick's ``adopt:`` may not name a
sibling (EC003) -- so nothing here imports a sibling at module load. Each coupling
answers "not available" honestly when its package or its data is absent, and the
core ``remember`` / ``recall`` never change shape because of one.

- **awgit / git** -- :func:`git_provenance` stamps a landed memory with the commit it
  was written against, so a later reader can tell a fact about an old tree.
- **awgraph** -- :func:`stale_symbols` checks the code identifiers a memory names
  against an awgraph symbol index; a name the index no longer has is a memory about
  code that moved or died.
- **awsettings** -- :func:`record_bundle` writes a sealed bundle's digest and signing
  key into the memory config awsettings already syncs (domain ``memory``), so another
  machine knows which bundle to trust.
- **awseal / awshare** -- used by :func:`awm.land.sync_dir`.
- **awpredict** -- the :mod:`awm.predict` Protocol.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# `name`, `mod.func`, `Class.method` inside backticks; paths and prose excluded.
_CODE_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?`")


def git_provenance(path: Path) -> Dict[str, str]:
    """``{"repo": <toplevel>, "head": <sha>}`` for the repo containing ``path``, or {}."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return {}
    return {"repo": out[0], "head": out[1]} if len(out) == 2 else {}


def code_names(text: str) -> List[str]:
    """Code identifiers a memory names in backticks (deduped, order kept)."""
    seen: Dict[str, None] = {}
    for m in _CODE_NAME.finditer(text or ""):
        name = m.group(1)
        if len(name) >= 3 and not name.isupper():  # `ok`, `GET`, `PATH` are not symbols
            seen.setdefault(name, None)
    return list(seen)


def stale_symbols(text: str, graph_root: Path) -> Optional[List[str]]:
    """Names in ``text`` that the awgraph index of ``graph_root`` does not know.

    ``None`` = could not judge (awgraph missing, or no index built for that root) --
    never an empty list, which would read as "every reference still resolves".
    """
    try:
        from awgraph import symbols  # type: ignore[import-not-found]
    except ImportError:
        return None
    store = symbols.store_path(str(graph_root))
    if not os.path.exists(store):
        return None
    stale = []
    for name in code_names(text):
        # A dotted name resolves if the full name or its last part is known.
        if (symbols.lookup(store, name, "calls") is None
                and symbols.lookup(store, name.rsplit(".", 1)[-1], "calls") is None):
            stale.append(name)
    return stale


def memory_config_path() -> Path:
    """The file awsettings' ``memory`` domain syncs (same resolution as awsettings)."""
    try:
        from awsettings.domains import memory_config_path as _p  # type: ignore
        return _p()
    except ImportError:
        override = (os.environ.get("AWSETTINGS_MEMORY_FILE") or "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return Path.home() / ".aither" / "memory-lander" / "config.json"


def record_bundle(project: str, digest: str, public_key: str = "",
                  path: Optional[Path] = None) -> Path:
    """Record ``project``'s latest bundle in the awsettings-synced memory config.

    Merges per project (the domain is ``deep``): another project's entry, and any
    key this function does not own, are kept.
    """
    cfg_path = path or memory_config_path()
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("version", 1)
    projects = cfg.setdefault("projects", {})
    entry = projects.setdefault(project, {})
    entry["bundle_digest"] = digest
    if public_key:
        entry["public_key"] = public_key
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(cfg_path)
    return cfg_path


# ── awrecover ─────────────────────────────────────────────────────────────────

class RecoverUnavailableError(RuntimeError):
    """``backup`` / ``restore`` need the optional ``awrecover`` package (awm[share])."""


def _awrecover():
    try:
        import awrecover  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RecoverUnavailableError("pip install awrecover to back up the memory db") from exc
    return awrecover


def backup_db(db: Path, store: Path, label: str) -> Dict[str, object]:
    """Snapshot the memory db under ``label`` and PROVE it restores (awrecover.verify).

    The copy is taken with SQLite's online backup API, never a file copy: a live db
    in WAL mode is several files, and copying ``memory.db`` alone can drop committed
    writes still sitting in the WAL.
    """
    import sqlite3
    import tempfile

    awrecover = _awrecover()
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "awm"
        stage.mkdir()
        src = sqlite3.connect(str(db))
        dst = sqlite3.connect(str(stage / "memory.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        snap = awrecover.snapshot(stage, store, label, meta={"source": "awm backup"})
    proof = awrecover.verify(store, label)
    return {"label": label, "digest": getattr(snap, "digest", ""), "verified": proof}


_SIDECARS = ("-journal", "-wal", "-shm")


def _keep_name(db: Path) -> Path:
    """A fresh ``<db>.before-restore-<UTC stamp>[-n]`` path: never an existing file.

    A fixed name was overwritten by the next restore, destroying the only copy of
    rows written before the first one.
    """
    import datetime

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = db.with_name(f"{db.name}.before-restore-{stamp}")
    cand, n = base, 1
    while cand.exists():
        cand = base.with_name(f"{base.name}-{n}")
        n += 1
    return cand


def _keep_previous(db: Path) -> str:
    """Copy the current db aside CONSISTENTLY and return where; '' when there is none.

    Opening the db first lets SQLite roll back a hot rollback ``-journal`` (or fold a
    WAL) left by a crashed writer, and the online backup API then copies committed
    pages only. A plain file copy of ``memory.db`` without its journal is a torn,
    malformed file. If SQLite cannot open it at all, the main file AND its sidecars
    are copied byte-for-byte so nothing is lost.
    """
    import shutil
    import sqlite3

    if not db.exists():
        return ""
    kept = _keep_name(db)
    try:
        src = sqlite3.connect(str(db))
        try:
            src.execute("SELECT count(*) FROM sqlite_master").fetchone()  # rolls a hot journal back
            dst = sqlite3.connect(str(kept))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.DatabaseError:
        kept.unlink(missing_ok=True)
        shutil.copy2(db, kept)
        for suf in _SIDECARS:
            side = db.with_name(db.name + suf)
            if side.exists():
                shutil.copy2(side, kept.with_name(kept.name + suf))
    return str(kept)


def restore_db(store: Path, label: str, db: Path) -> Dict[str, object]:
    """Put the snapshot ``label`` back as ``db``. The previous db is kept beside it.

    Every restore keeps the previous db under its own timestamped name, and every
    sidecar (rollback ``-journal``, ``-wal``, ``-shm``) is removed before the swap: a
    stale hot journal would otherwise be rolled back OVER the restored pages on the
    next open.
    """
    import shutil
    import tempfile

    awrecover = _awrecover()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "restored"
        awrecover.restore(store, label, out)
        restored = out / "memory.db"
        if not restored.exists():
            raise RecoverUnavailableError(f"snapshot {label!r} holds no memory.db")
        db.parent.mkdir(parents=True, exist_ok=True)
        kept = _keep_previous(db)
        tmp = db.with_name(db.name + ".restoring")
        shutil.copy2(restored, tmp)
        for suf in _SIDECARS:
            db.with_name(db.name + suf).unlink(missing_ok=True)
        tmp.replace(db)
    return {"label": label, "db": str(db), "previous": kept}
