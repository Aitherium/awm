"""Derive the default scope for "this repository, this person" -- no flag needed.

An agent that must be told `--scope tenant:user:project` on every call either
guesses (and writes to a scope nobody reads) or skips memory entirely. The
directory it is working in already says which project it is, and the git remote
already says which organisation owns it, so:

    tenant  = the owner segment of the `origin` remote URL, lower-cased
              (github.com/Acme/widgets.git -> "acme")
    user    = $AWM_USER, else $AITHER_USER, else the login name
    project = the basename of the repository's top-level directory

`$AWM_SCOPE` overrides all three at once. Every derived value goes through
`Scope(...)`, so a wildcard user with a named project is REFUSED here exactly as
it is on the command line -- a default is not a way around the rule.

Reads `.git/config` directly rather than spawning `git`: this runs on every
SessionStart hook and every MCP call that omits a scope, and a subprocess is
most of a second on some hosts. A worktree (`.git` is a FILE) is followed to
its common dir, where the remotes live.
"""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from .scope import PLATFORM, Scope, ScopeError

#: Characters a derived segment may keep. Anything else becomes `-`, so a repo
#: directory called `my repo` or a login `DOMAIN\\me` cannot smuggle a `:` (the
#: scope separator) or `*` (the wildcard) into the scope.
_SAFE = re.compile(r"[^A-Za-z0-9._@+-]+")


def _clean(text: str) -> str:
    return _SAFE.sub("-", text.strip()).strip("-")


def find_repo_root(start: Path) -> Optional[Path]:
    """The nearest ancestor of `start` holding a `.git` (dir or worktree file)."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return None


def _git_dir(root: Path) -> Optional[Path]:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    try:
        text = dot.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^gitdir:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    gd = Path(m.group(1))
    if not gd.is_absolute():
        gd = (root / gd).resolve()
    # A linked worktree keeps its remotes in the common dir, not its own.
    common = gd / "commondir"
    if common.is_file():
        try:
            c = Path(common.read_text(encoding="utf-8").strip())
        except OSError:
            return gd
        return c if c.is_absolute() else (gd / c).resolve()
    return gd


def remote_url(root: Path, remote: str = "origin") -> Optional[str]:
    """The URL of `remote` from the repo's config file, or None."""
    gd = _git_dir(root)
    if gd is None:
        return None
    try:
        text = (gd / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        if section == f'[remote "{remote}"]':
            k, _, v = line.partition("=")
            if k.strip() == "url":
                return v.strip()
    return None


def owner_from_url(url: str) -> Optional[str]:
    """`https://host/Owner/repo(.git)` or `git@host:Owner/repo` -> "Owner"."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        path = url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else ""
    elif re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith(("/", ".")):
        return None  # a local-path remote names no owner
    elif ":" in url:  # scp-like: git@host:Owner/repo
        path = url.split(":", 1)[1]
    else:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2]


def default_user() -> Optional[str]:
    for var in ("AWM_USER", "AITHER_USER"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- no login name is a refusal, below
        return None


def derive_scope(cwd: Optional[Path] = None,
                 user: Optional[str] = None) -> Tuple[Scope, Dict[str, str]]:
    """(scope, how) for `cwd`. Raises ScopeError when it cannot be derived.

    `how` names the source of each segment, so a caller can show an agent WHY
    it landed where it did instead of a bare string it has to trust.
    """
    env = os.environ.get("AWM_SCOPE", "").strip()
    if env:
        return Scope.parse(env), {"source": "AWM_SCOPE"}

    root = find_repo_root(Path(cwd) if cwd else Path.cwd())
    if root is None:
        raise ScopeError(
            f"{cwd or Path.cwd()} is not inside a git repository, so there is no "
            f"project to scope to -- pass a scope explicitly or set AWM_SCOPE")
    url = remote_url(root)
    owner = owner_from_url(url) if url else None
    if not owner:
        raise ScopeError(
            f"{root} has no parsable 'origin' remote, so there is no tenant -- "
            f"pass a scope explicitly or set AWM_SCOPE")
    who = user if user is not None else default_user()
    tenant, person, project = _clean(owner).lower(), _clean(who or ""), _clean(root.name)
    if not person:
        raise ScopeError("no user could be determined -- set AWM_USER or pass a scope")
    if tenant == PLATFORM:
        # `platform` is the reserved "everyone" sentinel (depth 0, covers every
        # scope): a derived platform:<user>:<repo> would weigh the user-wide row
        # the same as the project's own, and a newer user-wide value would outrank
        # the project fact. A remote owner cannot choose the sentinel.
        raise ScopeError(
            f"the origin remote owner {owner!r} ({url}) is the reserved tenant "
            f"{PLATFORM!r} -- pass a scope explicitly or set AWM_SCOPE")
    # Scope() refuses '*' under a named project and any empty segment.
    scope = Scope(tenant or "", person, project or "")
    how = {"tenant": f"origin remote owner ({url})",
           "user": "argument" if user is not None else (
               "AWM_USER" if os.environ.get("AWM_USER", "").strip() else
               "AITHER_USER" if os.environ.get("AITHER_USER", "").strip() else
               "login name"),
           "project": f"repository directory ({root})"}
    return scope, how


def resolve(scope: Optional[str], cwd: Optional[Path] = None,
            user: Optional[str] = None) -> Scope:
    """An explicit scope string wins; otherwise derive one."""
    if scope and scope.strip():
        return Scope.parse(scope)
    return derive_scope(cwd, user)[0]
