"""`awm recall --claude-hook` -- scoped facts for this repo, as SessionStart context.

A fresh coding-agent session starts knowing nothing the last one learned about
this repository. The facts exist (`awm remember` put them at this repo's scope);
nothing surfaces them. This prints the nearest, most recent ones in the shape a
Claude Code SessionStart hook injects into the model's context:

    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                            "additionalContext": "..."}}

Contract, because a hook that fails loudly breaks every session start:

- **exit 0, always.** Silent when the scope cannot be derived or the store is
  missing or empty (no memory is a normal state). When anything raises, still
  exit 0 but say so in ONE stderr line: an unreadable store is lost memory.
- **never creates the database.** A read-only hook that leaves a new file behind
  on a machine that never used awm is a side effect nobody asked for.
- **capped** at `MAX_CHARS` of context, each value clipped, so one long memory
  cannot crowd the rest out.
- the scope is derived from the hook payload's `cwd` (stdin JSON) when present,
  else the process cwd.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_CHARS = 1500
MAX_FACTS = 10
VALUE_CLIP = 220


def _stdin_payload(timeout: float = 0.3) -> Dict[str, Any]:
    """The hook's JSON payload, or {} -- never blocks on an open terminal/pipe."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except (AttributeError, ValueError):
        return {}
    box: List[str] = []

    def _read() -> None:
        try:
            box.append(sys.stdin.read())
        except Exception:  # noqa: BLE001
            box.append("")

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if not box or not box[0].strip():
        return {}
    try:
        data = json.loads(box[0])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def render(scope: str, rows: List[Any], total: int,
           max_chars: int = MAX_CHARS) -> str:
    """The additionalContext text: a header, then one line per fact, capped."""
    head = (f"awm memory for {scope} (nearest + newest first; "
            f"`awm recall --query <word>` for more of {total}):")
    lines = [head]
    used = len(head)
    for r in rows:
        value = " ".join(str(r.value).split())
        if len(value) > VALUE_CLIP:
            value = value[:VALUE_CLIP - 1] + "…"
        line = f"- {r.key}: {value}"
        if used + 1 + len(line) > max_chars:
            break
        lines.append(line)
        used += 1 + len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def build(db: Path, cwd: Optional[Path] = None, user: Optional[str] = None,
          scope: Optional[str] = None, limit: int = MAX_FACTS) -> Optional[Dict[str, Any]]:
    """The hook JSON, or None when there is nothing to say."""
    from .defaults import resolve
    from .store import MemoryStore

    if not Path(db).is_file():
        return None
    from .scope import ScopeError
    try:
        sc = resolve(scope, cwd, user)
    except ScopeError:
        return None  # no derivable scope: no memory to inject, a normal state
    # auto_migrate=False overrides AWM_AUTO_MIGRATE: a read path never migrates the
    # shared file (migrating locks every older installed reader out of it).
    # create=False: a hook never creates a schema -- a file with no awm tables
    # (0 bytes, or not awm's) reads as an empty store. NOT a `mode=ro` probe: after
    # a killed writer left a hot journal, a read-only connection cannot roll it
    # back and refuses every read ("attempt to write a readonly database"), so the
    # hook lost every memory. SQLite's own rollback on a normal open restores the
    # file to its last committed bytes -- what any other reader would do next.
    with MemoryStore(db, auto_migrate=False, create=False) as st:
        rows = st.recall(sc, limit=10_000)
    if not rows:
        return None
    text = render(str(sc), rows[:limit], len(rows))
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": text}}


def run(db: Path, user: Optional[str] = None, scope: Optional[str] = None,
        limit: int = MAX_FACTS) -> int:
    """Entry point. Always 0; prints JSON only when there is something to add."""
    try:
        payload = _stdin_payload()
        cwd = payload.get("cwd")
        out = build(db, Path(cwd) if isinstance(cwd, str) and cwd else None,
                    user=user, scope=scope, limit=limit)
        if out:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001 -- a hook must never break session start
        # Exit 0 (never break a session start), but never SILENT either: a store
        # that exists and could not be read is a lost memory, not "no memory".
        try:
            sys.stderr.write(f"awm claude-hook: memories NOT injected, {db} could not "
                             f"be read: {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        return 0
    return 0
