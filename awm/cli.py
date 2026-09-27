"""`awm` — a portable, scoped agent memory.

    awm remember --scope acme:alice:proj --key style --value "prefers tables"
    awm recall   --scope acme:alice:proj [--query tables]
    awm forget   --scope acme:alice:proj --key style
    awm land     --scope acme:alice:proj DIR [DIR ...]   # memory files -> scope
    awm land     --install-wake [--every 1h] --scope S DIR  # ...on an awrise schedule
    awm land     --uninstall-wake
    awm sync     --out BUNDLES DIR                      # sealed bundle (awm[share])
    awm --self-test

Scopes are `tenant:user:project`, `*` meaning "not narrowed here". A write lands
at exactly one scope; a read sees that scope and its ancestors, weighted by
distance, and never a sibling.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .scope import Scope, ScopeError
from .store import MemoryStore

# A Windows console defaults to cp1252; one stored memory containing an emoji made
# every `recall` that matched it crash with UnicodeEncodeError. Replace, never crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        continue

DEFAULT_DB = Path.home() / ".aither" / "awm" / "memory.db"


def _store(a) -> MemoryStore:
    return MemoryStore(Path(a.db) if a.db else DEFAULT_DB)


def _cmd_remember(a) -> int:
    with _store(a) as st:
        m = st.remember(Scope.parse(a.scope), a.key, a.value, kind=a.kind)
    print(f"remembered {m.key} at {m.scope}")
    return 0


def _cmd_recall(a) -> int:
    with _store(a) as st:
        rows = st.recall(Scope.parse(a.scope), query=a.query, limit=a.limit,
                         kind=a.kind)
    if a.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2))
        return 0
    if not rows:
        print("no memories visible from this scope")
        return 0
    stale_note = ""
    if a.graph:
        from .integrations import stale_symbols
        judged = [(r, stale_symbols(r.value, Path(a.graph).expanduser())) for r in rows]
        if any(s is None for _r, s in judged):
            stale_note = f"(awgraph: no index for {a.graph} or awgraph not installed)"
    for r in rows:
        line = f"[{r.weight:.2f}] {r.scope:28} {r.key:20} {r.value[:60]}"
        if a.graph and not stale_note:
            gone = next(s for rr, s in judged if rr is r)
            if gone:
                line += f"  STALE: {', '.join(gone[:4])} not in the code graph"
        print(line)
    if stale_note:
        print(stale_note, file=sys.stderr)
    return 0


def _cmd_forget(a) -> int:
    with _store(a) as st:
        gone = st.forget(Scope.parse(a.scope), a.key)
    print("forgotten" if gone else "no such memory at that exact scope")
    return 0 if gone else 1


def _cmd_land_wake(a) -> int:
    from . import wake

    try:
        if a.uninstall_wake:
            return wake.uninstall(name=a.wake_name)
        if not a.scope or not a.dirs:
            print("REFUSED: --install-wake needs --scope and at least one DIR",
                  file=sys.stderr)
            return 2
        Scope.parse(a.scope)
        return wake.install(a.scope, a.dirs, every=a.every, name=a.wake_name,
                            db=a.db, state=a.state, repo=a.repo)
    except wake.AwriseMissingError as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        return 2


def _cmd_land(a) -> int:
    from .land import land_dir, load_state, save_state

    if a.install_wake or a.uninstall_wake:
        return _cmd_land_wake(a)
    if not a.scope or not a.dirs:
        print("REFUSED: land needs --scope and at least one DIR", file=sys.stderr)
        return 2
    scope = Scope.parse(a.scope)
    state_path = Path(a.state) if a.state else DEFAULT_DB.parent / "land-state.json"
    state = load_state(state_path)
    rc = 0
    with _store(a) as st:
        for d in a.dirs:
            path = Path(d).expanduser()
            if not path.is_dir():
                print(f"NOT RUN: {path} is not a directory", file=sys.stderr)
                rc = 2
                continue
            extra = {}
            if a.repo:
                from .integrations import git_provenance
                extra = {"git": git_provenance(Path(a.repo).expanduser())}
                if not extra["git"]:
                    print(f"NOT RUN: {a.repo} is not a git repository", file=sys.stderr)
                    return 2
            c = land_dir(st, scope, path, state, dry_run=a.dry_run, extra_meta=extra)
            print(f"{path}: landed={c['landed']} unchanged={c['unchanged']} "
                  f"skipped={c['skipped']} -> {scope}")
    if not a.dry_run:
        save_state(state_path, state)
    return rc


def _cmd_sync(a) -> int:
    from .land import ShareUnavailableError, sync_dir

    try:
        r = sync_dir(Path(a.dir).expanduser(), Path(a.out).expanduser(), name=a.name)
    except ShareUnavailableError as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"sealed {r['files']} file(s) -> {a.out}/{r['name']} digest={r['digest']}")
    if a.record:
        from .integrations import record_bundle
        pub = ""
        try:
            import awseal  # type: ignore[import-not-found]
            pub = awseal.public_key_hex()
        except Exception:  # noqa: BLE001 -- the digest alone still pins the bundle
            pub = ""
        cfg = record_bundle(a.record, str(r["digest"]), pub)
        print(f"recorded {a.record} in {cfg} (synced by `awsettings --domain memory push`)")
    return 0


def _cmd_backup(a) -> int:
    from .integrations import RecoverUnavailableError, backup_db

    try:
        r = backup_db(Path(a.db) if a.db else DEFAULT_DB, Path(a.store).expanduser(), a.label)
    except RecoverUnavailableError as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        return 2
    print(f"snapshot {r['label']} verified (restored and compared) -> {a.store}")
    return 0


def _cmd_restore(a) -> int:
    from .integrations import RecoverUnavailableError, restore_db

    try:
        r = restore_db(Path(a.store).expanduser(), a.label, Path(a.db) if a.db else DEFAULT_DB)
    except RecoverUnavailableError as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        return 2
    print(f"restored {r['label']} -> {r['db']}"
          + (f" (previous kept at {r['previous']})" if r["previous"] else ""))
    return 0


def self_test() -> int:
    import tempfile
    ok = True

    def chk(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label} -> {got!r} (want {want!r})")

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        st = MemoryStore(db)

        plat = Scope("platform")
        acme = Scope("acme")
        alice = Scope("acme", "alice")
        proj = Scope("acme", "alice", "orchestrator")
        bob = Scope("acme", "bob")
        globex = Scope("globex")
        # The prefix trap, as a real tenant.
        acmecorp = Scope("acmecorp")

        st.remember(plat, "convention", "anchor the keep-set")
        st.remember(acme, "policy", "no cloud inference")
        st.remember(alice, "style", "prefers tables")
        st.remember(proj, "recipe", "rank 16, lr 2e-5")
        st.remember(bob, "style", "prefers prose")
        st.remember(globex, "policy", "cloud is fine")
        st.remember(acmecorp, "policy", "different company entirely")

        seen = {m.key for m in st.recall(proj)}
        chk("a project read sees its own memory", "recipe" in seen, True)
        chk("  and its user's", "style" in seen, True)
        chk("  and its tenant's", "policy" in seen, True)
        chk("  and the platform's", "convention" in seen, True)

        # THE SECURITY PROPERTY. Siblings share an ancestor and nothing else.
        vals = {m.value for m in st.recall(proj)}
        chk("a sibling USER's memory is invisible",
            "prefers prose" in vals, False)
        chk("another TENANT's memory is invisible",
            "cloud is fine" in vals, False)
        # The prefix trap: `"acmecorp:...".startswith("acme")` is True, and a
        # LIKE-based query leaks here silently.
        chk("a tenant whose name PREFIXES ours is invisible",
            "different company entirely" in vals, False)

        # Nearer scopes must outrank further ones, or a hierarchy is just a bag.
        rows = st.recall(proj)
        chk("the nearest scope ranks first", rows[0].key, "recipe")
        chk("  and the platform fact ranks last", rows[-1].key, "convention")
        chk("  weights decay with distance", rows[0].weight > rows[-1].weight, True)

        # A read from a WIDER scope must not see narrower memories: alice's
        # style is not the tenant's.
        tenant_vals = {m.value for m in st.recall(acme)}
        chk("a tenant read does NOT see a user's memory",
            "prefers tables" in tenant_vals, False)
        chk("  but does see its own", "no cloud inference" in tenant_vals, True)

        # forget is exact, never cascading.
        chk("forget at the wrong scope does nothing", st.forget(acme, "style"), False)
        chk("  the memory survives", any(m.key == "style" for m in st.recall(alice)), True)
        chk("forget at the exact scope works", st.forget(alice, "style"), True)

        # Malformed scopes refuse rather than normalise.
        for bad in ("acme", "a:b:c:d", "", "acme::proj"):
            try:
                Scope.parse(bad)
                chk(f"refuses malformed scope {bad!r}", "no raise", "raise")
            except ScopeError:
                chk(f"refuses malformed scope {bad!r}", "raise", "raise")
        try:
            Scope("acme", "*", "secret")
            chk("refuses a project under a wildcard user", "no raise", "raise")
        except ScopeError:
            chk("refuses a project under a wildcard user", "raise", "raise")
        try:
            Scope("ac:me")
            chk("refuses a separator inside a segment", "no raise", "raise")
        except ScopeError:
            chk("refuses a separator inside a segment", "raise", "raise")

        st.close()

    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    ap = argparse.ArgumentParser(prog="awm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("remember")
    r.add_argument("--scope", required=True)
    r.add_argument("--key", required=True)
    r.add_argument("--value", required=True)
    r.add_argument("--kind", default="fact")
    r.set_defaults(fn=_cmd_remember)

    c = sub.add_parser("recall")
    c.add_argument("--scope", required=True)
    c.add_argument("--query")
    c.add_argument("--kind")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--json", action="store_true")
    c.add_argument("--graph", help="repo root with an awgraph index: flag stale code names")
    c.set_defaults(fn=_cmd_recall)

    f = sub.add_parser("forget")
    f.add_argument("--scope", required=True)
    f.add_argument("--key", required=True)
    f.set_defaults(fn=_cmd_forget)

    ld = sub.add_parser("land", help="land a directory of memory files into a scope")
    ld.add_argument("--scope")
    ld.add_argument("--state", help="digest state file (default next to the db)")
    ld.add_argument("--dry-run", action="store_true")
    ld.add_argument("--repo", help="stamp each memory with this repo's HEAD (git provenance)")
    wk = ld.add_mutually_exclusive_group()
    wk.add_argument("--install-wake", action="store_true",
                    help="register an awrise job that runs this land on --every")
    wk.add_argument("--uninstall-wake", action="store_true",
                    help="remove the awrise job --install-wake registered")
    ld.add_argument("--every", default="1h", help="wake interval for --install-wake (1h)")
    ld.add_argument("--wake-name", default="awm-land", help="awrise job name (awm-land)")
    ld.add_argument("dirs", nargs="*")
    ld.set_defaults(fn=_cmd_land)

    sy = sub.add_parser("sync", help="seal + bundle a memory directory (awm[share])")
    sy.add_argument("--out", required=True)
    sy.add_argument("--name")
    sy.add_argument("--record", metavar="PROJECT",
                    help="record the bundle digest + key in the awsettings memory config")
    sy.add_argument("dir")
    sy.set_defaults(fn=_cmd_sync)

    bk = sub.add_parser("backup", help="snapshot the memory db and prove it restores")
    bk.add_argument("--store", required=True)
    bk.add_argument("label")
    bk.set_defaults(fn=_cmd_backup)

    rs = sub.add_parser("restore", help="put a verified snapshot back as the memory db")
    rs.add_argument("--store", required=True)
    rs.add_argument("label")
    rs.set_defaults(fn=_cmd_restore)

    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except ScopeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
