"""Land a directory of agent memory files into a scope, and ship it as a sealed bundle.

Agent harnesses keep file-based memory: one Markdown file per fact, with a small
frontmatter block (``name``, ``description``, ``type`` -- the layout Claude Code's
auto-memory uses, and a common convention elsewhere). ``land`` reads such a
directory and writes each file into a :class:`~awm.Scope` as one memory, keyed by
its file stem. It is idempotent: a file is re-written only when its content
digest changed, so running it on a timer costs nothing when nothing moved.

    awm land --scope acme:alice:proj ~/.claude/projects/proj/memory
    awm sync --out ./bundles ~/.claude/projects/proj/memory   # needs awm[share]

``sync`` seals the directory (awseal) and bundles it (awshare) so it can travel
to another machine or object store and be verified on arrival. It copies the
``*.md`` files to a staging directory first: sealing writes a manifest into the
tree it signs, and the live memory directory is not ours to add files to.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .scope import Scope
from .store import MemoryStore

VALUE_CAP = 4000
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_INDEX_NAMES = {"memory.md", "memory-archive.md", "readme.md"}


@dataclass
class MemoryFile:
    slug: str
    name: str
    description: str
    kind: str
    body: str
    digest: str


def parse_memory_file(path: Path) -> Optional[MemoryFile]:
    """One memory file, or None for an index/readme or a file with no content."""
    if path.name.lower() in _INDEX_NAMES:
        return None
    raw = path.read_text(encoding="utf-8", errors="replace")
    fields: Dict[str, str] = {}
    body = raw
    m = _FRONT.match(raw)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and not line.startswith((" ", "\t")):
                fields[k.strip().lower()] = v.strip().strip("\"'")
            elif sep and k.strip().lower() == "type":  # nested `metadata:\n  type: x`
                fields.setdefault("type", v.strip().strip("\"'"))
    if not body.strip() and not fields.get("description"):
        return None
    return MemoryFile(
        slug=path.stem,
        name=fields.get("name") or path.stem,
        description=fields.get("description", ""),
        kind=fields.get("type") or "fact",
        body=body.strip(),
        digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    )


def land_dir(store: MemoryStore, scope: Scope, mem_dir: Path,
             state: Dict[str, str], *, dry_run: bool = False) -> Dict[str, int]:
    """Write changed memory files into ``scope``. ``state`` maps slug -> digest.

    Returns counts: landed, unchanged, skipped (index/empty files).
    """
    counts = {"landed": 0, "unchanged": 0, "skipped": 0}
    for path in sorted(mem_dir.glob("*.md")):
        mf = parse_memory_file(path)
        if mf is None:
            counts["skipped"] += 1
            continue
        key = f"{scope}/{mf.slug}"
        if state.get(key) == mf.digest:
            counts["unchanged"] += 1
            continue
        if not dry_run:
            value = f"{mf.description}\n\n{mf.body}".strip()[:VALUE_CAP]
            store.remember(scope, mf.slug, value, kind=mf.kind,
                           meta={"source": "memory-file", "name": mf.name,
                                 "digest": mf.digest})
            state[key] = mf.digest
        counts["landed"] += 1
    return counts


def load_state(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class ShareUnavailableError(RuntimeError):
    """``sync`` needs the optional ``awshare`` + ``awseal`` packages (awm[share])."""


def sync_dir(mem_dir: Path, out_dir: Path, *, name: Optional[str] = None,
             key_path: Optional[Path] = None) -> Dict[str, object]:
    """Seal and bundle the directory's ``*.md`` files into ``out_dir``.

    Returns the manifest's digest, file count and bundle name.
    """
    try:
        import awshare
    except ImportError as exc:
        raise ShareUnavailableError("pip install 'awm[share]' to seal and bundle memory") from exc
    files: List[Path] = sorted(p for p in mem_dir.glob("*.md") if p.is_file())
    if not files:
        raise ValueError(f"{mem_dir} has no memory files -- refusing an empty bundle")
    label = name or f"memory-{mem_dir.parent.name or mem_dir.name}"
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / label
        stage.mkdir()
        for p in files:
            shutil.copy2(p, stage / p.name)
        m = awshare.publish(stage, out_dir, name=label, seal=True, key_path=key_path,
                            meta={"source": "awm sync", "files": len(files)})
    return {"name": label, "digest": m.digest, "files": len(files)}
