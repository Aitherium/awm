"""awm's own lines for `awm doctor`: the memory file's schema against this code's.

Read by the GENERATED `_doctor.py` through its `_doctor_local()` /
`_doctor_local_verdict()` hook, so it survives regeneration. Read-only: the file
is probed with `mode=ro` (`store.probe_schema`) and never opened as a
`MemoryStore`, which would create a missing file and is the one thing a
diagnostic must not do.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

#: Set by `awm doctor --db P` (cli._doctor_db). None = the default path.
DB_PATH: Optional[Path] = None


def _db_path() -> Path:
    if DB_PATH is not None:
        return Path(DB_PATH)
    return Path.home() / ".aither" / "awm" / "memory.db"  # cli.DEFAULT_DB


def _partial_backups(db: Path) -> List[Path]:
    """Interrupted migration backups next to ``db`` (``<name>.v<N>-backup-*.partial``)."""
    try:
        return sorted(p for p in db.parent.glob(db.name + ".v*-backup-*.partial"))
    except OSError:
        return []


def _code_lines() -> List[str]:
    """The awm code THIS process runs, beside the installed distribution's metadata.

    The generated `self` line reads `importlib.metadata.version("awm")`: the
    INSTALLED distribution, which is not the running code when another copy is on
    sys.path first (PYTHONPATH, a source tree). It printed "awm 0.1.1" next to a
    schema line computed by 0.6.0 code. Both are shown; a mismatch is spelled out,
    because the `awm` command on PATH then runs the other copy.
    """
    from . import __file__ as pkg_file
    from . import __version__
    lines = [f"code       awm {__version__} at {Path(pkg_file).resolve().parent}"]
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            dist: Optional[str] = version("awm")
        except PackageNotFoundError:
            dist = None
    except Exception:  # noqa: BLE001 -- a diagnostic never raises
        dist = None
    if dist is None:
        lines.append("dist       no installed awm distribution (running from a source tree)")
    elif dist != __version__:
        lines.append(f"dist       MISMATCH: installed distribution is awm {dist}; this "
                     f"process runs {__version__} (another copy is first on sys.path). "
                     f"The `awm` command on PATH runs the installed {dist}")
    return lines


def _doctor_local() -> List[str]:
    from .store import AUTO_MIGRATE_ENV, probe_schema

    info = probe_schema(_db_path())
    code = info["code_version"]
    lines = _code_lines()
    if not info["exists"]:
        lines.append(f"schema     no memory file at {info['path']} "
                     f"(the first write creates it at v{code})")
    elif info["error"]:
        lines.append(f"schema     could not read {info['path']}: {info['error']}")
    else:
        v = info["file_version"]
        lines.append(f"schema     file v{v}, code v{code} at {info['path']}")
        if info["compat"]:
            lines.append(f"compat     ACTIVE: features newer than v{v} raise NeedsMigration; "
                         f"run `awm migrate --db {info['path']}` (byte backup first)")
        elif info["newer"]:
            lines.append("compat     file is NEWER than this code: refused on open; "
                         "upgrade awm")
        else:
            lines.append("compat     off (file is current)")
    for p in _partial_backups(_db_path()):
        lines.append(f"backup     INCOMPLETE {p.name}: an interrupted migrate; not a backup, "
                     f"delete it")
    lines.append(f"migrate    {AUTO_MIGRATE_ENV}="
                 f"{'1 (migrates on open)' if info['auto_migrate'] else 'unset (opt-in only)'}")
    return lines


def _doctor_local_verdict() -> Tuple[List[str], List[str]]:
    """(problems, unjudged). A file in compat mode works: it is not a problem."""
    from .store import probe_schema

    info = probe_schema(_db_path())
    if info["exists"] and info["error"]:
        return [], [f"memory file {info['path']}: {info['error']}"]
    partial = [f"interrupted migration backup {p} (not a usable backup; delete it)"
               for p in _partial_backups(_db_path())]
    if partial:
        return partial, []
    if info["newer"]:
        return [f"memory file {info['path']} is schema v{info['file_version']}, newer "
                f"than this awm (v{info['code_version']}); it refuses to open it"], []
    return [], []
