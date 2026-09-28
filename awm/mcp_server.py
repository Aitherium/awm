"""`awm mcp` -- scoped agent memory for any MCP coding agent, over stdio.

    {"mcpServers": {"awm": {"command": "awm", "args": ["mcp"]}}}

WHY STDLIB JSON-RPC AND NOT THE `mcp` SDK
-----------------------------------------
awm has no dependencies on purpose: an agent's memory is the last thing that
should need a network, a service, or a package resolve to work. The `mcp` SDK
also split into incompatible 1.x and 2.x surfaces, and a server written against
one fails on a machine that resolved the other. The stdio transport is
newline-delimited JSON-RPC 2.0 with four methods that matter (`initialize`,
`tools/list`, `tools/call`, `ping`), so it is implemented here directly: no
extra, no import cost, start-up is the interpreter's.

THE SCOPE
---------
Every tool takes an optional `scope`. Omitted, it is derived from the server's
working directory (see `awm.defaults`): tenant from the git remote owner, user
from $AWM_USER / $AITHER_USER / the login name, project from the repository
directory. The same refusals as the CLI apply -- a wildcard user under a named
project is refused, never normalised.

WHAT IS GATED
-------------
- `awm_recall`, `awm_list`, `awm_scope`, `awm_history` read.
- `awm_remember` writes at EXACTLY one scope. It refuses to REPLACE an existing
  key's different value unless `overwrite` is true -- an upsert that silently
  replaced another session's fact is a delete with extra steps. With `subject`
  it goes through `reconcile_and_remember` (SlotReconciler) instead: the fact
  updates the subject's slot and the replaced value is kept in history, so the
  update is visible afterwards rather than silent. That update is only free for
  a slot the subject path itself wrote with the same kind; superseding a value
  written by key (or of another kind) needs `overwrite` exactly as above.
- `awm_resolve_entity` may create an entity or a POSSIBLE alias; it never merges
  two entities on a guess. `awm_confirm_alias` / `awm_reject_alias` settle a
  POSSIBLE link at exactly the given scope. `awm_merge_entities` folds one
  entity into another only when asked, at exactly one scope, and records the
  prior alias sets; `awm_split_entity` puts them back exactly.
- `awm_surprise_stats` reads: surprise p50/p90 and a confidence calibration
  table computed from the recorded transitions.
- On a memory file older than this awm (compat mode, see `awm migrate`), a
  feature the file cannot hold is refused with a NEEDS MIGRATION error; the file
  is never migrated by a tool call.
- `awm_world_state` and `awm_predict` read. `awm_observe` records ONE transition
  at exactly one scope: the state as of `before_as_of` (the `ts` awm_world_state
  returned before acting), the action, and the state now. It never changes a
  fact; only the dynamics table grows.
- `awm_forget` is destructive and is refused unless the server was started with
  `awm mcp --allow-forget`. That flag is set by whoever writes the client
  config, not by the model making the call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

_SCOPE_ARG = {
    "type": "string",
    "description": (
        "tenant:user:project, e.g. 'acme:alice:widgets'. OMIT IT to use this "
        "repository's own scope (call awm_scope to see what that is). '*' widens a "
        "level; a project under a '*' user is refused."),
}


def _tools(allow_forget: bool) -> List[Dict[str, Any]]:
    forget_note = ("" if allow_forget else
                   " DISABLED on this server (start it with --allow-forget); "
                   "calls are refused.")
    return [
        {
            "name": "awm_scope",
            "description": (
                "Show the memory scope this server uses when you omit `scope`, and "
                "where each segment came from (git remote owner, user, repo "
                "directory). Call this first if you are unsure where a fact will "
                "land."),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_recall",
            "description": (
                "Recall facts a previous agent stored for this project. Returns the "
                "scope's own facts and its ancestors' (user-wide, org-wide, "
                "platform-wide), nearest scope first then newest; never a sibling "
                "user's or tenant's. `query` is ONE case-insensitive substring "
                "matched against key and value -- use a single distinctive word "
                "('postgres', 'deploy'), not a sentence. Use before debugging "
                "something that may have a documented prior cause."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "one word or short substring; omit for newest"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                              "default": 20},
                    "kind": {"type": "string",
                             "description": "only memories of this kind (e.g. 'fact')"},
                    "as_of": {"type": ["number", "string"], "description": (
                        "unix seconds or an ISO date: answer with the values that "
                        "were current THEN (see awm_history). Omit for now.")},
                    "scope": _SCOPE_ARG,
                },
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_list",
            "description": (
                "List memory KEYS visible from a scope (key, scope, kind, weight) "
                "without their values -- a cheap index before awm_recall. Newest "
                "first."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                              "default": 100},
                    "kind": {"type": "string"},
                    "scope": _SCOPE_ARG,
                },
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_remember",
            "description": (
                "Store one fact the NEXT agent on this project needs (a measured "
                "cause, a trap, a decision and its reason). Writes at exactly one "
                "scope, keyed: the same key at the same scope is the same fact. If "
                "the key already holds a different value the call is refused unless "
                "`overwrite` is true -- read it with awm_recall first. Keep values "
                "short and self-contained; never store secrets."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "short stable slug, e.g. 'pg-replication-state'"},
                    "value": {"type": "string", "description": "the fact itself"},
                    "kind": {"type": "string", "default": "fact"},
                    "overwrite": {"type": "boolean", "default": False},
                    "subject": {"type": "string", "description": (
                        "INSTEAD of key: the slot this fact is about, e.g. "
                        "'user.ui_theme'. A new value updates the slot (the old one "
                        "goes to history, see awm_history); the same value is "
                        "ignored. A slot written by key, or of another kind, is "
                        "refused unless overwrite is true.")},
                    "scope": _SCOPE_ARG,
                },
                "required": ["value"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False,
                            "idempotentHint": True},
        },
        {
            "name": "awm_history",
            "description": (
                "Every value a key has held, oldest first, with the interval each "
                "was true for (valid_from, valid_to; the live value has valid_to "
                "null) and why it ended (superseded, forgotten). Same visibility as "
                "awm_recall. Use when a fact may have changed over time."),
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "scope": _SCOPE_ARG},
                "required": ["key"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_resolve_entity",
            "description": (
                "Resolve a mention of a person or thing ('vansh', 'vansh from india', "
                "'VS', 'me') to one entity. Returns status 'confirmed' with an "
                "entity_id, or 'possible' with candidate ids when the mention is "
                "only initials/an abbreviation -- nothing is merged on a guess. A "
                "mention nothing matches creates a new entity."),
            "inputSchema": {
                "type": "object",
                "properties": {"mention": {"type": "string"}, "scope": _SCOPE_ARG},
                "required": ["mention"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "awm_confirm_alias",
            "description": (
                "Settle a 'possible' result from awm_resolve_entity: `alias` names "
                "entity `entity_id`. The link becomes confirmed at exactly this scope "
                "and the alias's other possible links there are dropped."),
            "inputSchema": {
                "type": "object",
                "properties": {"alias": {"type": "string"},
                               "entity_id": {"type": "integer"}, "scope": _SCOPE_ARG},
                "required": ["alias", "entity_id"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False,
                            "idempotentHint": True},
        },
        {
            "name": "awm_reject_alias",
            "description": (
                "Refuse one possible link: `alias` does NOT name `entity_id`. Kept as "
                "a tombstone at exactly this scope so it is never proposed again."),
            "inputSchema": {
                "type": "object",
                "properties": {"alias": {"type": "string"},
                               "entity_id": {"type": "integer"}, "scope": _SCOPE_ARG},
                "required": ["alias", "entity_id"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False,
                            "idempotentHint": True},
        },
        {
            "name": "awm_merge_entities",
            "description": (
                "Declare that entity `drop` IS entity `keep` (e.g. after a human "
                "confirms 'VS' and 'Vansh Sharma' are one person). Moves drop's "
                "aliases onto keep at exactly this scope and records the prior alias "
                "sets; returns `merged_id` for awm_split_entity. Refused if drop lives "
                "at another scope or is named at other scopes. Never call this on a "
                "guess -- a 'possible' resolution is not a reason to merge."),
            "inputSchema": {
                "type": "object",
                "properties": {"keep": {"type": "integer"}, "drop": {"type": "integer"},
                               "scope": _SCOPE_ARG},
                "required": ["keep", "drop"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "awm_split_entity",
            "description": (
                "Undo awm_merge_entities: restores the dropped entity (`merged_id`) "
                "and both entities' alias sets at exactly this scope as they were "
                "before the merge. Aliases the survivor gained later, under names "
                "neither had, stay with the survivor and are listed."),
            "inputSchema": {
                "type": "object",
                "properties": {"merged_id": {"type": "integer"}, "scope": _SCOPE_ARG},
                "required": ["merged_id"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "awm_surprise_stats",
            "description": (
                "How surprising the world has been since `since`: count, mean, p50, "
                "p90 of transition surprise (0 = as predicted, 1 = every slot wrong), "
                "unexplained changes, and a calibration table -- per stated-confidence "
                "bucket, how often the prediction was exactly right. Use it to decide "
                "how far to trust awm_predict's confidence."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {"type": ["number", "string"],
                              "description": "unix seconds or ISO date; omit for all"},
                    "prefix": {"type": "string",
                               "description": "narrows the unexplained-change count"},
                    "buckets": {"type": "integer", "minimum": 1, "maximum": 100,
                                "default": 5},
                    "scope": _SCOPE_ARG,
                },
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_world_state",
            "description": (
                "The world state s_t: every current fact visible from the scope "
                "(nearest scope wins a key), optionally only keys under `prefix`, "
                "with a stable digest. Returns `ts`: pass it as `before_as_of` to "
                "awm_observe after you act."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": (
                        "only keys equal to it or starting with '<prefix>.'")},
                    "as_of": {"type": ["number", "string"],
                              "description": "unix seconds or ISO date; omit for now"},
                    "scope": _SCOPE_ARG,
                },
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_predict",
            "description": (
                "What an ACTION is expected to do from the current state. `source` "
                "says where the answer came from: RECALLED (this exact state and "
                "action were observed; confidence = agreement), GENERALIZED (the "
                "action seen only from other states), NONE (no model -- do not "
                "treat it as 'nothing changes')."),
            "inputSchema": {
                "type": "object",
                "properties": {"action": {"type": "string"},
                               "prefix": {"type": "string"}, "scope": _SCOPE_ARG},
                "required": ["action"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "awm_observe",
            "description": (
                "Record what an action did: the state as of `before_as_of` (the ts "
                "from awm_world_state taken BEFORE acting), the action, and the "
                "state now. Scored for surprise against what awm would have "
                "predicted. Writes one transition at exactly one scope; facts are "
                "not changed."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "before_as_of": {"type": ["number", "string"]},
                    "prefix": {"type": "string"},
                    "source": {"type": "string", "default": "observed"},
                    "scope": _SCOPE_ARG,
                },
                "required": ["action", "before_as_of"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "awm_forget",
            "description": (
                "Delete ONE memory by key at EXACTLY the given scope (never "
                "descendants). Irreversible." + forget_note),
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "scope": _SCOPE_ARG},
                "required": ["key"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


class ToolError(Exception):
    """A refusal or bad argument, returned to the agent as an isError result."""


class AwmMcp:
    """The tool implementations, independent of the transport (testable)."""

    def __init__(self, db: Path, cwd: Optional[Path] = None,
                 allow_forget: bool = False, user: Optional[str] = None):
        self.db = Path(db)
        self.cwd = cwd
        self.allow_forget = allow_forget
        self.user = user

    # -- helpers
    def _scope(self, args: Dict[str, Any]):
        from .defaults import resolve
        from .scope import ScopeError
        try:
            return resolve(args.get("scope"), self.cwd, self.user)
        except ScopeError as exc:
            raise ToolError(f"REFUSED: {exc}") from exc

    def _store(self, read: bool = False):
        from .store import MemoryStore
        # Never migrate from the server, whatever AWM_AUTO_MIGRATE says: its first call
        # is usually a read, and a migrated shared file locks older readers out.
        # Newer features on an old file raise NeedsMigration -> a tool error.
        # A READ tool never creates a missing file either (an empty store instead).
        return MemoryStore(self.db, auto_migrate=False, create=not read)

    @staticmethod
    def _int(args: Dict[str, Any], name: str, default: int, hi: int) -> int:
        v = args.get(name, default)
        try:
            v = int(v)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{name} must be an integer") from exc
        return max(1, min(v, hi))

    # -- tools
    def awm_scope(self, args: Dict[str, Any]) -> Any:
        from .defaults import derive_scope
        from .scope import ScopeError
        try:
            sc, how = derive_scope(self.cwd, self.user)
        except ScopeError as exc:
            return {"scope": None, "refused": str(exc),
                    "hint": "pass `scope` explicitly on every call, or set AWM_SCOPE"}
        return {"scope": str(sc), "derived_from": how, "db": str(self.db)}

    @staticmethod
    def _as_of(args: Dict[str, Any]) -> Optional[float]:
        raw = args.get("as_of")
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ToolError("as_of must be unix seconds or an ISO date string")
        from .cli import check_ts, parse_as_of
        from .scope import ScopeError
        try:
            # A JSON number is checked too: Python's json accepts NaN/Infinity.
            return check_ts(raw) if isinstance(raw, (int, float)) else parse_as_of(raw)
        except ScopeError as exc:
            raise ToolError(str(exc)) from exc

    def awm_recall(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        limit = self._int(args, "limit", 20, 200)
        as_of = self._as_of(args)
        with self._store(read=True) as st:
            rows = st.recall(sc, query=args.get("query") or None, limit=limit,
                             kind=args.get("kind") or None, as_of=as_of)
        return {"scope": str(sc), "count": len(rows), "as_of": as_of,
                "memories": [{"key": r.key, "value": r.value, "scope": r.scope,
                              "kind": r.kind, "weight": r.weight,
                              "updated": r.updated} for r in rows]}

    def awm_list(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        limit = self._int(args, "limit", 100, 500)
        with self._store(read=True) as st:
            rows = st.recall(sc, limit=limit, kind=args.get("kind") or None)
        return {"scope": str(sc), "count": len(rows),
                "keys": [{"key": r.key, "scope": r.scope, "kind": r.kind,
                          "weight": r.weight} for r in rows]}

    def awm_remember(self, args: Dict[str, Any]) -> Any:
        key, value = args.get("key"), args.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ToolError("value must be a non-empty string")
        subject = args.get("subject")
        if subject is not None:
            return self._remember_subject(args, subject, value)
        if not isinstance(key, str) or not key.strip():
            raise ToolError("key (or subject) must be a non-empty string")
        sc = self._scope(args)
        with self._store() as st:
            existing = st._db.execute(
                "SELECT value FROM memories WHERE scope=? AND key=?",
                (str(sc), key)).fetchone()
            if existing is not None and existing["value"] != value \
                    and not args.get("overwrite"):
                raise ToolError(
                    f"REFUSED: {key!r} at {sc} already holds a different value "
                    f"({existing['value'][:160]!r}). Pass overwrite=true to replace "
                    f"it, or choose a new key.")
            m = st.remember(sc, key, value, kind=str(args.get("kind") or "fact"))
        return {"remembered": m.key, "scope": m.scope,
                "replaced": existing is not None and existing["value"] != value}

    def _remember_subject(self, args: Dict[str, Any], subject: Any, value: str) -> Any:
        from .reconcile import ReconcileError, SlotReconciler
        if not isinstance(subject, str) or not subject.strip():
            raise ToolError("subject must be a non-empty string")
        if args.get("key"):
            raise ToolError("pass key OR subject, not both -- the subject picks the key")
        sc = self._scope(args)
        with self._store() as st:
            try:
                # The key path's consent rule, not a way around it: superseding a
                # value this path did not write (another kind, or written by key)
                # needs overwrite=true here too.
                d = st.reconcile_and_remember(sc, value, subject=subject.strip(),
                                              reconciler=SlotReconciler(),
                                              kind=str(args.get("kind") or "fact"),
                                              overwrite=bool(args.get("overwrite")))
            except ReconcileError as exc:
                raise ToolError(f"REFUSED: {exc}") from exc
        return {"action": d.action, "key": d.key, "scope": str(sc), "reason": d.reason}

    def awm_history(self, args: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ToolError("key must be a non-empty string")
        sc = self._scope(args)
        with self._store(read=True) as st:
            rows = st.history(sc, key)
        return {"scope": str(sc), "key": key, "count": len(rows),
                "history": [h.to_dict() for h in rows]}

    def awm_resolve_entity(self, args: Dict[str, Any]) -> Any:
        mention = args.get("mention")
        if not isinstance(mention, str) or not mention.strip():
            raise ToolError("mention must be a non-empty string")
        sc = self._scope(args)
        with self._store() as st:
            res = st.resolve_entity(sc, mention)
        return {"scope": str(sc), "mention": mention, **res.to_dict()}

    def _alias_args(self, args: Dict[str, Any]) -> Any:
        alias, eid = args.get("alias"), args.get("entity_id")
        if not isinstance(alias, str) or not alias.strip():
            raise ToolError("alias must be a non-empty string")
        if isinstance(eid, bool) or not isinstance(eid, int):
            raise ToolError("entity_id must be an integer (from awm_resolve_entity)")
        return self._scope(args), alias, eid

    def awm_confirm_alias(self, args: Dict[str, Any]) -> Any:
        sc, alias, eid = self._alias_args(args)
        with self._store() as st:
            res = st.confirm_alias(sc, alias, eid)
        return {"scope": str(sc), "alias": alias, **res.to_dict()}

    def awm_reject_alias(self, args: Dict[str, Any]) -> Any:
        sc, alias, eid = self._alias_args(args)
        with self._store() as st:
            was_live = st.reject_alias(sc, alias, eid)
        return {"scope": str(sc), "alias": alias, "entity_id": eid, "rejected": was_live}

    @staticmethod
    def _id(args: Dict[str, Any], name: str) -> int:
        v = args.get(name)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ToolError(f"{name} must be an integer entity id")
        return v

    def awm_merge_entities(self, args: Dict[str, Any]) -> Any:
        keep, drop = self._id(args, "keep"), self._id(args, "drop")
        sc = self._scope(args)
        with self._store() as st:
            return st.merge_entities(sc, keep, drop)

    def awm_split_entity(self, args: Dict[str, Any]) -> Any:
        mid = self._id(args, "merged_id")
        sc = self._scope(args)
        with self._store() as st:
            return st.split_entity(sc, mid)

    def awm_surprise_stats(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        since = self._as_of({"as_of": args.get("since")}) or 0.0
        buckets = self._int(args, "buckets", 5, 100)
        with self._store(read=True) as st:
            return st.surprise_stats(sc, since, prefix=self._prefix(args),
                                     buckets=buckets).to_dict()

    @staticmethod
    def _prefix(args: Dict[str, Any]) -> Optional[str]:
        raw = args.get("prefix")
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("prefix must be a non-empty string")
        return raw.strip()

    @staticmethod
    def _action(args: Dict[str, Any]) -> str:
        act = args.get("action")
        if not isinstance(act, str) or not act.strip():
            raise ToolError("action must be a non-empty string")
        return act

    def awm_world_state(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        with self._store(read=True) as st:
            ws = st.encode_state(sc, prefix=self._prefix(args), as_of=self._as_of(args))
        return ws.to_dict()

    def awm_predict(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        act = self._action(args)
        with self._store(read=True) as st:
            ws = st.encode_state(sc, prefix=self._prefix(args))
            pred = st.predict_outcome(sc, ws, act)
        return {"scope": str(sc), "state": ws.digest, "action": act, **pred.to_dict()}

    def awm_observe(self, args: Dict[str, Any]) -> Any:
        sc = self._scope(args)
        act = self._action(args)
        before_ts = self._as_of({"as_of": args.get("before_as_of")})
        if before_ts is None:
            raise ToolError("before_as_of is required: the ts awm_world_state returned "
                            "before you acted")
        source = args.get("source") or "observed"
        if not isinstance(source, str):
            raise ToolError("source must be a string")
        prefix = self._prefix(args)
        with self._store() as st:
            before = st.encode_state(sc, prefix=prefix, as_of=before_ts)
            after = st.encode_state(sc, prefix=prefix)
            t = st.observe_transition(sc, before, act, after, source=source)
        return t.to_dict()

    def awm_forget(self, args: Dict[str, Any]) -> Any:
        if not self.allow_forget:
            raise ToolError(
                "REFUSED: forget is destructive and this server was started without "
                "--allow-forget. Ask the owner, or use `awm forget` from a shell.")
        key = args.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ToolError("key must be a non-empty string")
        sc = self._scope(args)
        with self._store() as st:
            gone = st.forget(sc, key)
        if not gone:
            raise ToolError(f"no memory {key!r} at exactly {sc} (forget never widens)")
        return {"forgotten": key, "scope": str(sc)}

    def call(self, name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """An MCP CallToolResult dict. Refusals come back as isError, never raise."""
        from .store import NeedsMigration

        fn: Optional[Callable[[Dict[str, Any]], Any]] = {
            "awm_scope": self.awm_scope, "awm_recall": self.awm_recall,
            "awm_list": self.awm_list, "awm_remember": self.awm_remember,
            "awm_forget": self.awm_forget, "awm_history": self.awm_history,
            "awm_resolve_entity": self.awm_resolve_entity,
            "awm_confirm_alias": self.awm_confirm_alias,
            "awm_reject_alias": self.awm_reject_alias,
            "awm_merge_entities": self.awm_merge_entities,
            "awm_split_entity": self.awm_split_entity,
            "awm_surprise_stats": self.awm_surprise_stats,
            "awm_world_state": self.awm_world_state, "awm_predict": self.awm_predict,
            "awm_observe": self.awm_observe}.get(name)
        if fn is None:
            return _text_result(f"unknown tool {name!r}", error=True)
        try:
            out = fn(dict(args or {}))
        except ToolError as exc:
            return _text_result(str(exc), error=True)
        except NeedsMigration as exc:
            return _text_result(f"NEEDS MIGRATION: {exc}", error=True)
        except Exception as exc:  # noqa: BLE001 -- report, never kill the server
            return _text_result(f"{type(exc).__name__}: {exc}", error=True)
        return _text_result(json.dumps(out, ensure_ascii=False, indent=1))


def _text_result(text: str, error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def handle(srv: AwmMcp, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, one response out (None for notifications)."""
    from . import __version__

    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:  # a notification (initialized, cancelled, ...): no reply
        return None
    params = msg.get("params") or {}

    def ok(result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        ver = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[-1]
        return ok({
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "awm", "version": __version__},
            "instructions": (
                "Scoped agent memory for this repository. awm_recall before deep "
                "debugging (a prior session may have recorded the cause); "
                "awm_remember facts the NEXT agent needs. Scope defaults to this "
                "repo -- see awm_scope."),
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": _tools(srv.allow_forget)})
    if method == "tools/call":
        return ok(srv.call(str(params.get("name")), params.get("arguments")))
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(srv: AwmMcp, stdin=None, stdout=None) -> int:
    """Newline-delimited JSON-RPC over stdio until EOF."""
    inp = stdin if stdin is not None else sys.stdin.buffer
    out = stdout if stdout is not None else sys.stdout.buffer
    for raw in inp:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(msg, list):  # a batch (2025-03-26)
                replies = [r for r in (handle(srv, m) for m in msg
                                       if isinstance(m, dict)) if r]
                if replies:
                    out.write(json.dumps(replies).encode("utf-8") + b"\n")
                    out.flush()
                continue
            resp = handle(srv, msg) if isinstance(msg, dict) else None
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()
    return 0


def main(db: Path, allow_forget: bool = False, user: Optional[str] = None) -> int:
    try:
        return serve(AwmMcp(db, allow_forget=allow_forget, user=user))
    except KeyboardInterrupt:
        return 130
