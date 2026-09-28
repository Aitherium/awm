"""awm's guarded couplings: git provenance, awgraph staleness, awsettings record."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from awm import MemoryStore, Scope
from awm.cli import main
from awm.integrations import code_names, git_provenance, record_bundle, stale_symbols

MEM = "---\nname: rbac\ndescription: rbac cache\ntype: feedback\n---\n\n" \
      "`get_rbac_manager()` caches; `deleted_helper` was removed. Use `GET`.\n"


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a",
                                 "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    return repo


def test_code_names_skips_short_and_constant_tokens():
    assert code_names(MEM) == ["get_rbac_manager", "deleted_helper"]


def test_git_provenance_names_repo_and_head(tmp_path):
    prov = git_provenance(_git_repo(tmp_path))
    assert prov["repo"] and len(prov["head"]) == 40
    assert git_provenance(tmp_path / "not-a-repo-dir") == {}


def test_land_with_repo_stamps_provenance(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "rbac.md").write_text(MEM, encoding="utf-8")
    db = tmp_path / "m.db"
    repo = _git_repo(tmp_path)
    assert main(["--db", str(db), "land", "--scope", "a:b:c", "--state",
                 str(tmp_path / "s.json"), "--repo", str(repo), str(mem)]) == 0
    with MemoryStore(db) as st:
        meta = st.recall(Scope.parse("a:b:c"))[0].meta
    assert len(meta["git"]["head"]) == 40
    assert main(["--db", str(db), "land", "--scope", "a:b:c", "--repo",
                 str(tmp_path / "nope"), str(mem)]) == 2


def test_stale_symbols_against_a_real_awgraph_store(tmp_path, monkeypatch):
    symbols = pytest.importorskip("awgraph.symbols")
    store = tmp_path / "symbols.db"
    chunk = SimpleNamespace(id="1", name="get_rbac_manager", calls=[], called_by=[],
                            chunk_type=SimpleNamespace(value="function"),
                            source_path=str(tmp_path / "rbac.py"), start_line=1,
                            signature="def get_rbac_manager()", body_preview="")
    symbols.build([chunk], str(store))
    monkeypatch.setattr(symbols, "store_path", lambda root: str(store))
    assert stale_symbols(MEM, tmp_path) == ["deleted_helper"]


def test_stale_symbols_without_an_index_cannot_judge(tmp_path, monkeypatch):
    symbols = pytest.importorskip("awgraph.symbols")
    monkeypatch.setattr(symbols, "store_path", lambda root: str(tmp_path / "absent.db"))
    assert stale_symbols(MEM, tmp_path) is None  # never [] -- that would read "all fine"


def test_record_bundle_merges_per_project(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"version": 1, "targets": ["x"],
                               "projects": {"other": {"bundle_digest": "old"}}}))
    record_bundle("proj", "d1", "pk", path=cfg)
    data = json.loads(cfg.read_text())
    assert data["targets"] == ["x"]
    assert data["projects"]["other"] == {"bundle_digest": "old"}
    assert data["projects"]["proj"] == {"bundle_digest": "d1", "public_key": "pk"}


def test_backup_is_verified_and_restore_brings_the_memory_back(tmp_path):
    pytest.importorskip("awrecover")
    db, snaps = tmp_path / "m.db", tmp_path / "snaps"
    with MemoryStore(db) as st:
        st.remember(Scope.parse("a:b:c"), "k", "before")
    assert main(["--db", str(db), "backup", "--store", str(snaps), "pre"]) == 0
    with MemoryStore(db) as st:
        st.forget(Scope.parse("a:b:c"), "k")
        assert st.recall(Scope.parse("a:b:c")) == []
    assert main(["--db", str(db), "restore", "--store", str(snaps), "pre"]) == 0
    with MemoryStore(db) as st:
        assert st.recall(Scope.parse("a:b:c"))[0].value == "before"
    assert len(list(tmp_path.glob("m.db.before-restore-*"))) == 1
