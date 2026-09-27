"""awm land / sync: memory files become scoped memories, idempotently."""

from pathlib import Path

import pytest
from awm import MemoryStore, Scope
from awm.cli import main
from awm.land import land_dir, parse_memory_file

MEM = """---
name: fleet-overload-is-io
description: load 200 was IO, not CPU
metadata:
  type: feedback
---

Read /proc/pressure/io first.
"""


def _dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    (d / "fleet-overload-is-io.md").write_text(MEM, encoding="utf-8")
    (d / "MEMORY.md").write_text("# index\n- [x](fleet-overload-is-io.md)\n", encoding="utf-8")
    return d


def test_parse_reads_frontmatter_and_nested_type(tmp_path):
    mf = parse_memory_file(_dir(tmp_path) / "fleet-overload-is-io.md")
    assert mf is not None
    assert (mf.slug, mf.name, mf.kind) == ("fleet-overload-is-io", "fleet-overload-is-io",
                                           "feedback")
    assert mf.description == "load 200 was IO, not CPU"
    assert "pressure/io" in mf.body


def test_index_file_is_skipped(tmp_path):
    assert parse_memory_file(_dir(tmp_path) / "MEMORY.md") is None


def test_land_writes_once_then_is_idempotent(tmp_path):
    d = _dir(tmp_path)
    scope = Scope.parse("acme:alice:proj")
    state: dict = {}
    with MemoryStore(tmp_path / "m.db") as st:
        first = land_dir(st, scope, d, state)
        again = land_dir(st, scope, d, state)
        rows = st.recall(scope, query="pressure")
    assert first == {"landed": 1, "unchanged": 0, "skipped": 1}
    assert again["landed"] == 0 and again["unchanged"] == 1
    assert rows and rows[0].key == "fleet-overload-is-io" and rows[0].kind == "feedback"


def test_changed_file_relands(tmp_path):
    d = _dir(tmp_path)
    scope = Scope.parse("acme:alice:proj")
    state: dict = {}
    with MemoryStore(tmp_path / "m.db") as st:
        land_dir(st, scope, d, state)
        (d / "fleet-overload-is-io.md").write_text(MEM + "\nAlso check wchan.\n",
                                                  encoding="utf-8")
        assert land_dir(st, scope, d, state)["landed"] == 1
        assert "wchan" in st.recall(scope, query="wchan")[0].value


def test_sibling_scope_never_sees_landed_memory(tmp_path):
    d = _dir(tmp_path)
    with MemoryStore(tmp_path / "m.db") as st:
        land_dir(st, Scope.parse("acme:alice:proj"), d, {})
        assert st.recall(Scope.parse("acme:bob:proj")) == []


def test_cli_land_and_missing_dir(tmp_path, capsys):
    d = _dir(tmp_path)
    db, state = str(tmp_path / "m.db"), str(tmp_path / "s.json")
    assert main(["--db", db, "land", "--scope", "acme:alice:proj", "--state", state,
                 str(d)]) == 0
    assert "landed=1" in capsys.readouterr().out
    assert main(["--db", db, "land", "--scope", "acme:alice:proj", "--state", state,
                 str(tmp_path / "nope")]) == 2


try:
    import awseal
    import awshare
    _HAS_SHARE = True
except ImportError:  # the optional awm[share] extra is not installed
    _HAS_SHARE = False


@pytest.mark.skipif(_HAS_SHARE, reason="awm[share] installed; the refusal arm needs it absent")
def test_sync_without_share_extra_says_why(tmp_path):
    assert main(["sync", "--out", str(tmp_path / "b"), str(_dir(tmp_path))]) == 2


@pytest.mark.skipif(not _HAS_SHARE, reason="needs the awm[share] extra")
def test_sync_seals_a_verifiable_bundle(tmp_path):
    from awm.land import sync_dir

    key = awseal.keygen(tmp_path / "signing.key")
    r = sync_dir(_dir(tmp_path), tmp_path / "b", key_path=key)
    assert r["files"] == 2
    got = awshare.fetch(tmp_path / "b" / f"{r['name']}.awshare.json", tmp_path / "out")
    assert got["verified"] is True
    assert (tmp_path / "out" / "fleet-overload-is-io.md").exists()


def test_sync_refuses_an_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main(["sync", "--out", str(tmp_path / "b"), str(empty)])
    assert code == (1 if _HAS_SHARE else 2)
