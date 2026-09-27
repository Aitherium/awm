"""Put `awm land` on a schedule by registering one awrise job.

awm has no clock of its own and should not grow one. awrise is the wake brick:
it owns the schedule, the record of every wake, and the host scheduler entry that
ticks it. This module only speaks awrise's CLI, through a subprocess, so awm keeps
zero runtime dependencies and an absent awrise is a plain message, not an
ImportError.

One named job (``awm-land`` unless ``--wake-name`` says otherwise). Installing
again UPDATES that job -- `awrise set` -- instead of failing on a duplicate name
or registering a second copy that lands the same directory twice per window.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_JOB = "awm-land"
DEFAULT_EVERY = "1h"

INSTALL_HINT = (
    "awrise is not installed (or not on PATH); it is the clock awm uses.\n"
    "  install it:  pip install awrise\n"
    "  then:        awrise install-clock   # registers this machine's scheduler once"
)


class AwriseMissingError(RuntimeError):
    """awrise is not on PATH."""


def _join(argv: list[str]) -> str:
    """One shell line for awrise's shell executor, quoted for THIS host."""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def land_argv(scope: str, dirs: list[str], *, db: str | None = None,
              state: str | None = None, repo: str | None = None) -> list[str]:
    """The `awm land` invocation the wake runs. Directories are made absolute:
    the scheduler's working directory is not the one you typed them in."""
    argv = ["awm"]
    if db:
        argv += ["--db", str(Path(db).expanduser().resolve())]
    argv += ["land", "--scope", scope]
    if state:
        argv += ["--state", str(Path(state).expanduser().resolve())]
    if repo:
        argv += ["--repo", str(Path(repo).expanduser().resolve())]
    argv += [str(Path(d).expanduser().resolve()) for d in dirs]
    return argv


def _awrise() -> str:
    exe = shutil.which("awrise")
    if not exe:
        raise AwriseMissingError(INSTALL_HINT)
    return exe


def _call(exe: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _jobs(exe: str) -> dict | None:
    """awrise's registered jobs, or None when awrise could not say."""
    cp = _call(exe, ["list", "--json"])
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _relay(cp: subprocess.CompletedProcess) -> None:
    if cp.stdout:
        print(cp.stdout.rstrip())
    if cp.stderr:
        print(cp.stderr.rstrip(), file=sys.stderr)


def install(scope: str, dirs: list[str], *, every: str = DEFAULT_EVERY,
            name: str = DEFAULT_JOB, db: str | None = None, state: str | None = None,
            repo: str | None = None) -> int:
    """Register (or update) the awrise job. 0 done, 1 awrise refused, 2 unjudged."""
    exe = _awrise()
    run = _join(land_argv(scope, dirs, db=db, state=state, repo=repo))
    jobs = _jobs(exe)
    if jobs is None:
        print("NOT RUN: `awrise list --json` did not answer; cannot tell add from update",
              file=sys.stderr)
        return 2
    if name in jobs:
        cp = _call(exe, ["set", "--name", name, f"every={every}", f"run={run}"])
        verb = "updated"
    else:
        cp = _call(exe, ["add", "--name", name, "--every", every, "--run", run])
        verb = "registered"
    _relay(cp)
    if cp.returncode != 0:
        print(f"REFUSED: awrise did not accept the job (exit {cp.returncode})",
              file=sys.stderr)
        return 1
    print(f"{verb} awrise job {name!r}: every {every} -> {run}")
    print("awrise runs due jobs only while its host clock ticks: "
          "`awrise install-clock` (once per machine), `awrise status` to check.")
    return 0


def uninstall(*, name: str = DEFAULT_JOB) -> int:
    """Remove the awrise job. Absent already is success: the end state holds."""
    exe = _awrise()
    jobs = _jobs(exe)
    if jobs is None:
        print("NOT RUN: `awrise list --json` did not answer", file=sys.stderr)
        return 2
    if name not in jobs:
        print(f"no awrise job {name!r}; nothing to remove")
        return 0
    cp = _call(exe, ["remove", "--name", name])
    _relay(cp)
    if cp.returncode != 0:
        print(f"REFUSED: awrise did not remove {name!r} (exit {cp.returncode})",
              file=sys.stderr)
        return 1
    return 0
