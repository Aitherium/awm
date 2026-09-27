"""awm land --install-wake / --uninstall-wake drive awrise through its CLI.

A fake `awrise` on PATH records every argv it is given and keeps a tiny job
table, so the tests assert the EXACT command lines awm sends -- including that a
second install updates the same job instead of adding a duplicate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from awm.cli import main

FAKE = r'''
import json, os, sys
log = os.environ["FAKE_AWRISE_LOG"]
jobs_path = os.environ["FAKE_AWRISE_JOBS"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
jobs = json.load(open(jobs_path, encoding="utf-8")) if os.path.exists(jobs_path) else {}
args = sys.argv[1:]
cmd = args[0] if args else ""
def opt(flag):
    return args[args.index(flag) + 1]
if cmd == "list":
    print(json.dumps(jobs))
elif cmd == "add":
    if opt("--name") in jobs:
        print("Job exists", file=sys.stderr); sys.exit(1)
    jobs[opt("--name")] = {"every": opt("--every"), "run": opt("--run")}
    print("Added " + opt("--name"))
elif cmd == "set":
    for kv in args[3:]:
        k, v = kv.split("=", 1)
        jobs[opt("--name")][k] = v
    print("Updated " + opt("--name"))
elif cmd == "remove":
    jobs.pop(opt("--name"))
    print("Removed " + opt("--name"))
else:
    sys.exit(2)
json.dump(jobs, open(jobs_path, "w", encoding="utf-8"))
'''


@pytest.fixture()
def fake_awrise(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "fake_awrise.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        (bindir / "awrise.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / "awrise"
        exe.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
        exe.chmod(0o755)
    log = tmp_path / "argv.jsonl"
    jobs = tmp_path / "jobs.json"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_AWRISE_LOG", str(log))
    monkeypatch.setenv("FAKE_AWRISE_JOBS", str(jobs))

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def table():
        return json.loads(jobs.read_text(encoding="utf-8")) if jobs.exists() else {}

    return calls, table


def _mem(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


def _run_line(scope: str, d: Path) -> str:
    argv = ["awm", "land", "--scope", scope, str(d.resolve())]
    return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(argv)


def test_install_adds_the_job_with_the_exact_argv(tmp_path, fake_awrise):
    calls, table = fake_awrise
    d = _mem(tmp_path)
    rc = main(["land", "--install-wake", "--every", "30m", "--scope", "acme:alice:proj", str(d)])
    assert rc == 0
    run = _run_line("acme:alice:proj", d)
    assert calls() == [
        ["list", "--json"],
        ["add", "--name", "awm-land", "--every", "30m", "--run", run],
    ]
    assert table() == {"awm-land": {"every": "30m", "run": run}}


def test_install_again_updates_the_same_named_job(tmp_path, fake_awrise):
    calls, table = fake_awrise
    d = _mem(tmp_path)
    assert main(["land", "--install-wake", "--scope", "acme:alice:proj", str(d)]) == 0
    assert main(["land", "--install-wake", "--every", "2h",
                 "--scope", "acme:alice:proj", str(d)]) == 0
    run = _run_line("acme:alice:proj", d)
    assert calls() == [
        ["list", "--json"],
        ["add", "--name", "awm-land", "--every", "1h", "--run", run],
        ["list", "--json"],
        ["set", "--name", "awm-land", "every=2h", f"run={run}"],
    ]
    assert list(table()) == ["awm-land"]
    assert table()["awm-land"]["every"] == "2h"


def test_uninstall_removes_and_is_idempotent(tmp_path, fake_awrise):
    calls, table = fake_awrise
    d = _mem(tmp_path)
    assert main(["land", "--install-wake", "--scope", "acme:alice:proj", str(d)]) == 0
    assert main(["land", "--uninstall-wake"]) == 0
    assert table() == {}
    assert main(["land", "--uninstall-wake"]) == 0
    assert calls()[2:] == [
        ["list", "--json"],
        ["remove", "--name", "awm-land"],
        ["list", "--json"],
    ]


def test_awrise_absent_prints_install_and_exits_2(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    d = _mem(tmp_path)
    rc = main(["land", "--install-wake", "--scope", "acme:alice:proj", str(d)])
    assert rc == 2
    assert "pip install awrise" in capsys.readouterr().err
    assert main(["land", "--uninstall-wake"]) == 2


def test_install_refuses_a_bad_scope_before_calling_awrise(tmp_path, fake_awrise):
    calls, _table = fake_awrise
    d = _mem(tmp_path)
    assert main(["land", "--install-wake", "--scope", "acme", str(d)]) == 2
    assert calls() == []


def test_install_needs_scope_and_dirs(fake_awrise):
    calls, _table = fake_awrise
    assert main(["land", "--install-wake"]) == 2
    assert calls() == []


def test_plain_land_still_needs_scope_and_dirs():
    assert main(["land"]) == 2
