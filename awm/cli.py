"""`awm` — a portable, scoped agent memory.

    awm remember --scope acme:alice:proj --key style --value "prefers tables"
    awm recall   --scope acme:alice:proj [--query tables]
    awm forget   --scope acme:alice:proj --key style
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
    for r in rows:
        print(f"[{r.weight:.2f}] {r.scope:28} {r.key:20} {r.value[:60]}")
    return 0


def _cmd_forget(a) -> int:
    with _store(a) as st:
        gone = st.forget(Scope.parse(a.scope), a.key)
    print("forgotten" if gone else "no such memory at that exact scope")
    return 0 if gone else 1


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
    c.set_defaults(fn=_cmd_recall)

    f = sub.add_parser("forget")
    f.add_argument("--scope", required=True)
    f.add_argument("--key", required=True)
    f.set_defaults(fn=_cmd_forget)

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
