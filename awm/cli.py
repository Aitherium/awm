"""`awm` — a portable, scoped agent memory.

    awm remember --scope acme:alice:proj --key style --value "prefers tables"
    awm recall   --scope acme:alice:proj [--query tables]
    awm recall   --as-of 2026-09-01     # what was true then (unix ts or ISO date)
    awm history  KEY [--scope S]        # every value KEY has held, oldest first
    awm entity   resolve|confirm|reject|list ...   # who is "VS"?
    awm entity   merge KEEP DROP --scope S          # DROP is KEEP (reversible)
    awm entity   split MERGED_ID --scope S          # undo that merge exactly
    awm world    state [--prefix P] [--as-of T]      # s_t: digest + slots
    awm world    predict ACTION [--prefix P]         # RECALLED|GENERALIZED|NONE
    awm world    surprises [--since T] [--prefix P]  # scored + unexplained changes
    awm world    stats [--since T]                   # surprise p50/p90 + calibration
    awm migrate  [--db P] [--no-backup] [--dry-run]  # older file -> current schema
    awm doctor   [--db P]                            # stack + file vs code schema
    awm forget   --scope acme:alice:proj --key style
    awm land     --scope acme:alice:proj DIR [DIR ...]   # memory files -> scope
    awm recall   [--query w]            # scope derived from this repo + $AWM_USER
    awm recall   --claude-hook          # SessionStart additionalContext JSON
    awm mcp      [--allow-forget]       # stdio MCP server for coding agents
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
import math
import re
import sys
from pathlib import Path

from .scope import Scope, ScopeError
from .store import MemoryStore, MigrationError, NeedsMigration
from .world import WorldError

# A Windows console defaults to cp1252; one stored memory containing an emoji made
# every `recall` that matched it crash with UnicodeEncodeError. Replace, never crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        continue

DEFAULT_DB = Path.home() / ".aither" / "awm" / "memory.db"


def _store(a, *, read: bool = False) -> MemoryStore:
    """Open the store. ``read=True`` never migrates, whatever AWM_AUTO_MIGRATE says:
    a read must not lock older installed readers out of the shared file, and for
    the same reason a read never CREATES a missing file (it sees an empty store)."""
    return MemoryStore(Path(a.db) if a.db else DEFAULT_DB,
                       auto_migrate=False if read else None, create=not read)


def _cmd_remember(a) -> int:
    with _store(a) as st:
        m = st.remember(Scope.parse(a.scope), a.key, a.value, kind=a.kind)
    print(f"remembered {m.key} at {m.scope}")
    return 0


#: The largest instant every platform's time functions accept (9999-12-31T23:59:59Z).
_TS_MAX = 253402300799.0
_UNIX = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6}))?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?)?\Z")


def check_ts(value: float, text: object = None) -> float:
    """A finite instant in [0, 9999-12-31]; anything else raises ScopeError.

    NaN compares false with everything, so a NaN `as_of` silently matched nothing
    (and reached JSON as a bare `NaN`, which strict parsers reject); an inf or
    1e400 crashed `localtime`. Refused here, once, for the CLI and MCP alike.
    """
    shown = value if text is None else text
    if not isinstance(value, (int, float)) or isinstance(value, bool)             or not math.isfinite(value) or not 0.0 <= float(value) <= _TS_MAX:
        raise ScopeError(f"as-of {shown!r} is not a finite time between 1970 and 9999")
    return float(value)


def parse_as_of(text: str) -> float:
    """Unix seconds, or an ISO-8601 calendar date/datetime. A naive time is LOCAL time.

    Parsed by ONE grammar, not `datetime.fromisoformat`, whose accepted forms differ
    by version (3.11 added `Z`, any fraction length, `+0000`, week dates): the same
    `--as-of` answered on 3.12 and was refused on 3.10, where CI gates. Accepted:
    `YYYY-MM-DD`, then optionally `[T ]HH:MM[:SS[.f{1,6}]]` and `Z` / `+HH:MM` /
    `+HHMM`. Unix seconds are plain decimals (no exponent, no nan/inf). An 8-digit
    integer that is a valid calendar date (`20991231`) is refused as ambiguous:
    read as seconds it is a day in 1970 and silently matches nothing.
    """
    from datetime import datetime, timedelta, timezone
    t = (text or "").strip()
    if _UNIX.match(t):
        if re.fullmatch(r"\d{8}", t):
            try:
                datetime(int(t[:4]), int(t[4:6]), int(t[6:]))
            except ValueError:
                pass
            else:
                raise ScopeError(f"--as-of {text!r} is ambiguous: a compact date or unix "
                                 f"seconds? Write {t[:4]}-{t[4:6]}-{t[6:]} (or seconds with "
                                 f"a decimal point)")
        return check_ts(float(t), text)
    m = _ISO.match(t)
    if m is None:
        raise ScopeError(f"--as-of {text!r} is neither unix seconds nor an ISO date "
                         f"(YYYY-MM-DD[THH:MM[:SS[.ffffff]]][Z|+HH:MM])")
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                      int((frac or "0").ljust(6, "0")))
    except ValueError as exc:
        raise ScopeError(f"--as-of {text!r} is not a real date/time ({exc})") from exc
    if tz:
        if tz in ("Z", "z"):
            off = timedelta(0)
        else:
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            off = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            if abs(off) >= timedelta(hours=24):
                raise ScopeError(f"--as-of {text!r} has an offset of 24h or more")
        dt = dt.replace(tzinfo=timezone(off))
    try:
        ts = dt.timestamp()
    except (OverflowError, OSError, ValueError) as exc:
        raise ScopeError(f"--as-of {text!r} is outside the platform's time range") from exc
    return check_ts(ts, text)


def _cmd_recall(a) -> int:
    from .defaults import resolve
    as_of = parse_as_of(a.as_of) if a.as_of else None
    with _store(a, read=True) as st:
        rows = st.recall(resolve(a.scope, user=a.user), query=a.query, limit=a.limit,
                         kind=a.kind, as_of=as_of)
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


def _fmt_ts(ts) -> str:
    import time as _t
    return "now" if ts is None else _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(ts))


def _cmd_history(a) -> int:
    from .defaults import resolve
    with _store(a, read=True) as st:
        rows = st.history(resolve(a.scope, user=a.user), a.key)
    if a.json:
        print(json.dumps([h.to_dict() for h in rows], indent=2))
        return 0
    if not rows:
        print(f"no history for {a.key!r} visible from this scope")
        return 1
    for h in rows:
        print(f"{_fmt_ts(h.valid_from)} -> {_fmt_ts(h.valid_to):19} {h.reason:10} "
              f"{h.scope:28} {h.value[:60]}")
    return 0


def _cmd_entity(a) -> int:
    from .defaults import resolve
    if a.action == "list":
        with _store(a, read=True) as st:
            ents = st.entities(resolve(a.scope, user=a.user))
        if a.json:
            print(json.dumps([e.to_dict() for e in ents], indent=2))
            return 0
        for e in ents:
            names = ", ".join(f"{x['alias']}({x['status'][0]})" for x in e.aliases)
            print(f"{e.id:>5} {e.scope:28} {e.canonical:24} {names}")
        if not ents:
            print("no entities visible from this scope")
        return 0
    # Resolve, confirm, reject, merge and split WRITE, so like `remember` they
    # name the scope.
    if not a.scope:
        raise ScopeError(f"entity {a.action} writes: pass --scope explicitly")
    sc = Scope.parse(a.scope)
    if a.action in ("merge", "split"):
        first = _int_arg(a.name, "KEEP" if a.action == "merge" else "MERGED_ID")
        with _store(a) as st:
            if a.action == "split":
                out = st.split_entity(sc, first)
            elif a.entity_id is None:
                raise ScopeError("entity merge needs KEEP and DROP ids")
            else:
                out = st.merge_entities(sc, first, a.entity_id)
        print(json.dumps(out, indent=None if not a.json else 2))
        return 0
    with _store(a) as st:
        if a.action == "resolve":
            out = st.resolve_entity(sc, a.name).to_dict()
        elif a.entity_id is None:
            raise ScopeError(f"entity {a.action} needs ENTITY_ID")
        elif a.action == "confirm":
            out = st.confirm_alias(sc, a.name, a.entity_id).to_dict()
        else:
            out = {"rejected": st.reject_alias(sc, a.name, a.entity_id)}
    print(json.dumps(out, indent=None if not a.json else 2))
    return 0


def _int_arg(text, name: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError) as exc:
        raise ScopeError(f"{name} must be an entity id (an integer), got {text!r}") from exc


def _cmd_world(a) -> int:
    from .defaults import resolve
    sc = resolve(a.scope, user=a.user)
    # Every `world` action here only reads: never migrate or create the file.
    with _store(a, read=True) as st:
        if a.action == "state":
            as_of = parse_as_of(a.as_of) if a.as_of else None
            out = st.encode_state(sc, prefix=a.prefix, as_of=as_of).to_dict()
            if not a.json:
                print(f"{out['digest'][:16]}  {len(out['slots'])} slot(s) at {sc}"
                      f"{'' if as_of is None else ' as of ' + _fmt_ts(as_of)}")
                for k in sorted(out["slots"]):
                    print(f"  {k:28} {out['slots'][k][:60]}")
                return 0
        elif a.action == "predict":
            if not a.target:
                raise ScopeError("world predict needs an ACTION")
            state = st.encode_state(sc, prefix=a.prefix)
            out = st.predict_outcome(sc, state, a.target).to_dict()
            if not a.json:
                print(f"{out['source']} confidence={out['confidence']:.3f} "
                      f"support={out['support']}"
                      + (f"  ({out['note']})" if out["note"] else ""))
                for k, v in sorted(out["delta"].items()):
                    print(f"  {k:28} -> {'(removed)' if v is None else v[:60]}")
                return 0 if out["source"] != "NONE" else 1
        elif a.action == "stats":
            since = parse_as_of(a.since) if a.since else 0.0
            stats = st.surprise_stats(sc, since, prefix=a.prefix)
            out = stats.to_dict()
            if not a.json:
                def f(x):
                    return "-" if x is None else f"{x:.3f}"
                print(f"{stats.count} scored transition(s), {stats.novel} novel, "
                      f"{stats.unexplained} unexplained change(s) at {sc}")
                print(f"  surprise mean={f(stats.mean)} p50={f(stats.p50)} "
                      f"p90={f(stats.p90)} max={f(stats.max)}")
                print(f"  calibration (ece={f(stats.ece)}, "
                      f"{stats.uncalibrated} without a confidence):")
                for row in stats.calibration:
                    print(f"    [{row['lo']:.2f}, {row['hi']:.2f}{']' if row['hi'] >= 1 else ')'}"
                          f" n={row['n']:<5} confidence={f(row['mean_confidence'])} "
                          f"matched={f(row['match_rate'])}")
                return 0
        else:
            since = parse_as_of(a.since) if a.since else 0.0
            events = st.surprise_log(sc, since, prefix=a.prefix)
            out = [e.to_dict() for e in events]
            if not a.json:
                for e in events:
                    what = e.action if e.kind == "transition" else e.key
                    print(f"{_fmt_ts(e.ts)} {e.kind:11} {e.score:.3f} {e.scope:28} {what}")
                if not events:
                    print("no surprises visible from this scope")
                return 0
    print(json.dumps(out, indent=2, ensure_ascii=False))
    # Same exit contract as text mode: a script reading --json must still be able
    # to tell "no transition recorded" from the exit code.
    if a.action == "predict" and out["source"] == "NONE":
        return 1
    return 0


def _counts(counts) -> str:
    return " ".join(f"{t}={'-' if n is None else n}" for t, n in (counts or {}).items())


def _cmd_migrate(a) -> int:
    """Older file -> current schema: content-verified backup, one transaction, counts compared."""
    path = Path(a.db) if a.db else DEFAULT_DB
    if not path.is_file():
        print(f"NOT RUN: no memory file at {path}", file=sys.stderr)
        return 2
    try:
        # create=False: a never-initialised (e.g. 0-byte) file is NOT stamped here.
        # There is nothing to migrate in it, and stamping it at this version would
        # lock installed 0.3.x out of a file it would initialise itself.
        with MemoryStore(path, auto_migrate=False, create=False) as st:
            if st.ephemeral:
                if a.json:
                    print(json.dumps({"path": str(path), "from": None, "to": None,
                                      "migrated": False, "dry_run": bool(a.dry_run),
                                      "note": "never initialised; nothing to migrate"}))
                    return 0
                print(f"awm migrate: {path}")
                print("  note         never initialised (no awm tables); nothing "
                      "to migrate, left exactly as it is")
                return 0
            rep = st.migrate(backup=not a.no_backup, dry_run=a.dry_run)
    except MigrationError as exc:
        print(f"FAILED (rolled back): {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"awm migrate: {rep['path']}")
    print(f"  schema       v{rep['from']} -> v{rep['to']}")
    if rep.get("note"):
        print(f"  note         {rep['note']}")
    print(f"  rows before  {_counts(rep['before'])}")
    if rep["migrated"]:
        print(f"  rows after   {_counts(rep['after'])}")
        same = rep["digest_before"] == rep["digest_after"]
        print(f"  memories     digest {'unchanged' if same else 'CHANGED'} "
              f"({rep['digest_after'][:16]})")
        how = ("byte-identical to the original" if rep.get("backup_method") == "byte-copy"
               else "WAL file: copied with the SQLite backup API")
        print(f"  backup       {rep['backup'] or 'none (--no-backup)'}"
              + (f" sha256={rep['backup_sha256'][:16]} ({how}; row counts and "
                 f"memories digest verified in the copy)" if rep["backup"] else ""))
        for name in rep.get("reaped_backups") or []:
            print(f"  reaped       {name} (left by a killed earlier migration)")
        print("migrated. awm older than 0.4.0 can no longer open this file; the "
              "backup can.")
    elif a.dry_run and rep["from"] < rep["to"]:
        print("dry run: nothing written.")
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


def _fast_path(argv) -> "int | None":
    """`awm mcp` and `awm recall --claude-hook` skip the repo-state banner below.

    Both speak a machine protocol on stdout (JSON-RPC, hook JSON): a banner line
    printed ahead of it corrupts the stream, and the banner's import costs most of
    a second on a path that must answer in under one.
    """
    if argv[:1] == ["mcp"]:
        ap = argparse.ArgumentParser(prog="awm mcp")
        ap.add_argument("--db")
        ap.add_argument("--user", help="user segment for derived scopes")
        ap.add_argument("--allow-forget", action="store_true",
                        help="expose awm_forget (destructive); refused otherwise")
        a = ap.parse_args(argv[1:])
        from .mcp_server import main as mcp_main
        return mcp_main(Path(a.db) if a.db else DEFAULT_DB,
                        allow_forget=a.allow_forget, user=a.user)
    if "recall" in argv and "--claude-hook" in argv:
        ap = argparse.ArgumentParser(prog="awm recall --claude-hook")
        ap.add_argument("--claude-hook", action="store_true")
        ap.add_argument("--db")
        ap.add_argument("--user")
        ap.add_argument("--scope")
        ap.add_argument("--limit", type=int, default=10)
        try:
            a, _ = ap.parse_known_args([x for x in argv if x != "recall"])
        except SystemExit:
            return 0  # a hook never fails session start
        from .claude_hook import run
        return run(Path(a.db) if a.db else DEFAULT_DB, user=a.user,
                   scope=a.scope, limit=a.limit)
    return None


def _doctor_db(argv) -> None:
    """`awm doctor --db P`: point the schema check at P. The intercept below is generated."""
    if argv[:1] != ["doctor"]:
        return
    from . import doctor_local
    doctor_local.DB_PATH = DEFAULT_DB
    for i, tok in enumerate(argv):
        if tok == "--db" and i + 1 < len(argv):
            doctor_local.DB_PATH = Path(argv[i + 1])
        elif tok.startswith("--db="):
            doctor_local.DB_PATH = Path(tok.split("=", 1)[1])


def _hoist_doctor(argv: list) -> list:
    """`awm --db P doctor` -> `awm doctor --db P`.

    The usage line advertises a global `awm [--db DB] {cmd}`, but `doctor` is served
    by intercepts that look at argv[0] only; argparse has no `doctor` subcommand, so
    the global-first order died with exit 2. Leading global `--db` options are moved
    behind `doctor`; any other argv is returned unchanged.
    """
    i, moved = 0, []
    while i < len(argv):
        if argv[i] == "--db" and i + 1 < len(argv):
            moved += argv[i:i + 2]
            i += 2
        elif argv[i].startswith("--db="):
            moved.append(argv[i])
            i += 1
        else:
            break
    if moved and argv[i:i + 1] == ["doctor"]:
        return ["doctor", *moved, *argv[i + 1:]]
    return argv


def main(argv=None) -> int:
    # Rebinding `argv` is deliberate: the generated intercepts below read it.
    argv = _hoist_doctor(list(argv) if argv is not None else sys.argv[1:])
    _fp = _fast_path(list(argv) if argv is not None else sys.argv[1:])
    if _fp is not None:
        return _fp
    _doctor_db(list(argv) if argv is not None else sys.argv[1:])
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
    c.add_argument("--scope", help="default: derived from this repo (awm.defaults)")
    c.add_argument("--user", help="user segment for a derived scope (default $AWM_USER)")
    c.add_argument("--claude-hook", action="store_true",
                   help="print top facts as Claude Code SessionStart hook JSON")
    c.add_argument("--query")
    c.add_argument("--kind")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--json", action="store_true")
    c.add_argument("--graph", help="repo root with an awgraph index: flag stale code names")
    c.add_argument("--as-of", help="values current at this instant (unix ts or ISO date)")
    c.set_defaults(fn=_cmd_recall)

    h = sub.add_parser("history", help="every value a key has held, oldest first")
    h.add_argument("key")
    h.add_argument("--scope", help="default: derived from this repo (awm.defaults)")
    h.add_argument("--user")
    h.add_argument("--json", action="store_true")
    h.set_defaults(fn=_cmd_history)

    en = sub.add_parser("entity",
                        help="resolve | confirm | reject | list | merge | split entities")
    en.add_argument("action",
                    choices=("resolve", "confirm", "reject", "list", "merge", "split"))
    en.add_argument("name", nargs="?", help=(
        "the mention (resolve), alias (confirm/reject), KEEP id (merge) or "
        "MERGED_ID (split)"))
    en.add_argument("entity_id", nargs="?", type=int, help="entity id; DROP id for merge")
    en.add_argument("--scope", help="required for resolve/confirm/reject (they write)")
    en.add_argument("--user")
    en.add_argument("--json", action="store_true")
    en.set_defaults(fn=_cmd_entity)

    w = sub.add_parser("world", help="world model: state | predict | surprises | stats")
    w.add_argument("action", choices=("state", "predict", "surprises", "stats"))
    w.add_argument("target", nargs="?", help="the action to predict (predict)")
    w.add_argument("--scope", help="default: derived from this repo (awm.defaults)")
    w.add_argument("--user")
    w.add_argument("--prefix", help="only slots whose key is P or starts with 'P.'")
    w.add_argument("--as-of", help="state as of this instant (state)")
    w.add_argument("--since", help="surprises from this instant (unix ts or ISO date)")
    w.add_argument("--json", action="store_true")
    w.set_defaults(fn=_cmd_world)

    mg = sub.add_parser("migrate", help="bring an older memory file to the current schema")
    # SUPPRESS: a subcommand default of None would overwrite a top-level --db.
    mg.add_argument("--db", default=argparse.SUPPRESS)
    mg.add_argument("--no-backup", action="store_true",
                    help="skip the byte backup (it is written next to the file by default)")
    mg.add_argument("--dry-run", action="store_true", help="report; write nothing")
    mg.add_argument("--json", action="store_true")
    mg.set_defaults(fn=_cmd_migrate)

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

    sub.add_parser("mcp", help="serve scoped memory over MCP stdio (awm mcp --help)")

    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    if getattr(a, "cmd", None) == "entity" and a.action != "list" and not a.name:
        ap.error(f"entity {a.action} needs a NAME")
    try:
        return a.fn(a)
    except NeedsMigration as exc:
        print(f"NEEDS MIGRATION: {exc}", file=sys.stderr)
        return 2
    except (ScopeError, WorldError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
