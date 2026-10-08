# awm

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awm/)**  ·  [Source](https://github.com/Aitherium/awm)  ·  `pip install awm`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awm** is one of its 67 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Give one agent a memory scoped to one project and watch it stop re-asking.

<!-- aither-header:end -->

Agents forget everything between sessions, so they re-derive the same facts
forever. The usual fix — one global memory — is worse: now every project sees
every other project's notes, and one customer's context can end up in another
customer's answer while the answer still looks like an answer.

`awm` is a memory with a **scope on every row**. SQLite file, no service, no
network, no account.

```bash
pip install awm

awm remember --scope acme:alice:orchestrator --key recipe --value "rank 16, lr 2e-5"
awm recall   --scope acme:alice:orchestrator
```

Python 3.10+. The only dependency is the standard library.

## The scope

```
platform:*:*               everyone
{tenant}:*:*               one org
{tenant}:{user}:*          one person
{tenant}:{user}:{project}  one piece of work
```

Three segments, always. `*` means "not narrowed at this level". `platform` is a
**reserved sentinel** meaning everyone — a real organisation cannot be called
`platform`, because resolving that ambiguity at read time would let a tenant
name itself into the root of the hierarchy and see every other tenant's
memories.

Two shapes are refused outright rather than interpreted:

| you write | what happens |
|---|---|
| `acme:alice` | `ScopeError` — a scope has exactly three segments |
| `acme:*:secret` | `ScopeError` — a project under a wildcard user is ambiguous |
| `ac:me:alice:x` | `ScopeError` — a segment may not contain the separator |

That last one matters more than it looks: a `:` inside a segment silently
re-partitions the scope, and a re-partitioned scope is a different scope with
different visibility.

## Two rules, deliberately asymmetric

**A write lands at exactly one scope.** There is no "write somewhere in this
subtree" — that is how a memory becomes visible to a scope its author never
considered. `remember()` upserts on `(scope, key)`, so writing twice updates
rather than duplicating.

**A read includes ancestors, weighted by distance.** A project query surfaces
the user's preferences and the org's conventions — that is the entire point of a
hierarchy — but weight decays by `0.5` per level, so a platform fact never
outranks a project fact merely by being older.

```python
from awm import MemoryStore, Scope

store = MemoryStore("~/.awm/memories.db")
store.remember(Scope.parse("acme:*:*"),                "style", "British spelling")
store.remember(Scope.parse("acme:alice:orchestrator"), "lr",    "2e-5")

for m in store.recall(Scope.parse("acme:alice:orchestrator")):
    print(f"{m.weight:>4}  {m.scope:<26} {m.key} = {m.value}")

#  1.0  acme:alice:orchestrator    lr = 2e-5
#  0.25 acme:*:*                   style = British spelling
```

`recall()` takes `query=` (a substring over key and value), `kind=` and
`limit=` (default 20). Results are sorted nearest-scope first, then most
recently updated.

## One property that is security, not tidiness

**Siblings never see each other.** `acme:alice:*` and `acme:bob:*` share an
ancestor and nothing else.

The scope check is **segment-wise, never a string prefix** — because
`"acmecorp:secrets".startswith("acme")` is `True`. That is one customer's memory
entering another's context, silently. So `recall()` computes the exact visible
set and issues `WHERE scope IN (...)`:

```sql
-- what awm does                 -- what a prefix match would have done
WHERE scope IN ('acme:alice:x',  WHERE scope LIKE 'acme:%'
                'acme:alice:*',  --                    ^ also matches acmecorp:
                'acme:*:*',
                'platform:*:*')
```

There is a second check behind the first: every row that survives the `IN` is
re-weighted, and a weight of zero drops it. The `IN` should make that
unreachable — and if it ever is reachable, dropping the row is the safe answer
and a leak is not.

## Prediction is the tail, not the substrate

`awm` answers *what happened, and who may see it*. A predictor answers *what
happens next*. They compose, and the whole coupling is one structural `Protocol`
that awm defines and never imports an implementation of:

```bash
pip install awm            # memory; a retrieval miss is simply a miss
pip install awm awpredict  # misses come back marked PREDICTED, not RECALLED
```

Nothing in awm changes shape between those two worlds. That is on purpose:
`awpredict` wants torch, awm is sold as *SQLite, no service, no network*, and a
stranger who wants scoped memory should not have to acquire a deep-learning
stack to get it.

**And recall runs first, always.** Measured on real transitions:

| | next-state class |
|---|---|
| online last-outcome lookup | **0.9720** |
| the trained latent model | 0.9357 |

The learned model *loses* to a self-updating lookup on 98.9% of rows and wins
only on the ~1.1% carrying a genuinely novel action. A design that consults a
model before consulting memory is choosing the worse answer for almost every
query.

## Landing an agent's memory files

Agent harnesses keep file-based memory: one Markdown file per fact, with a small
frontmatter block (`name`, `description`, `type`). `awm land` writes each file into a
scope as one memory, keyed by its file stem. It is idempotent -- a file is re-written
only when its content changed -- so it is safe to run on a timer.

```bash
awm land --scope acme:alice:proj ~/.claude/projects/proj/memory
awm recall --scope acme:alice:proj --query "pressure"
```

### On a schedule

awm has no clock of its own; [awrise](https://github.com/Aitherium/awrise) is the
wake brick, and `--install-wake` registers one awrise job that runs the same `land`
on an interval:

```bash
pip install awrise
awm land --install-wake --every 1h --scope acme:alice:proj ~/.claude/projects/proj/memory
awrise install-clock      # once per machine: the host scheduler that ticks awrise
awrise status             # every wake of the job, and why it did or did not fire
awm land --uninstall-wake # remove the job
```

The job is named `awm-land` (`--wake-name` to choose another). Installing again
**updates** that job -- a new interval, scope or directory list -- rather than adding
a second one. Directories are stored as absolute paths, and `--db`, `--state` and
`--repo` are carried into the scheduled command. `awm` itself must be on the PATH the
scheduler sees. If awrise is not installed the command prints how to install it and
exits 2; nothing is registered.

`awm sync` seals the directory and bundles it so it can travel to another machine or
store and be verified on arrival (needs the optional extra: `pip install 'awm[share]'`):

```bash
awm sync --out ./bundles ~/.claude/projects/proj/memory
```

## Working with the rest of the family

Every coupling is optional: awm imports none of these at load time, and each one says
"not available" plainly when its package or its data is missing.

| with | command | what it adds |
|---|---|---|
| git | `awm land --repo DIR ...` | stamps each memory with the commit it was written against |
| awgraph | `awm recall --graph ROOT ...` | flags memories that name code the index no longer has (`STALE`) |
| awseal + awshare | `awm sync --out B DIR` | a signed bundle another machine verifies on arrival |
| awsettings | `awm sync ... --record PROJECT` | records the bundle digest and signing key in the config `awsettings --domain memory` syncs |
| awrecover | `awm backup --store S LABEL` / `awm restore --store S LABEL` | a snapshot of the memory db that is proven to restore before it counts |
| awpredict | `awm.predict` | a retrieval miss can come back PREDICTED instead of empty |

`pip install 'awm[share]'` pulls awshare, awseal and awrecover.

## For coding agents: MCP server and a SessionStart hook

`awm mcp` serves the store over MCP stdio -- stdlib only, no `mcp` package needed:

```json
{"mcpServers": {"awm": {"command": "awm", "args": ["mcp"], "env": {"AWM_USER": "alice"}}}}
```

Tools: `awm_scope`, `awm_recall`, `awm_list`, `awm_remember`, `awm_forget`,
`awm_history`, `awm_resolve_entity`, `awm_confirm_alias`, `awm_reject_alias`,
`awm_world_state`, `awm_predict`, `awm_observe` (`awm_recall` takes an optional `as_of`). Every `scope`
argument is optional: omitted, it is derived from the working directory -- tenant = the
`origin` remote's owner, user = `$AWM_USER` / `$AITHER_USER` / the login name, project = the
repository directory (`$AWM_SCOPE` overrides all three). A wildcard user under a named
project is refused exactly as on the command line. `awm_remember` refuses to replace a
different value unless `overwrite` is true; `awm_forget` is refused unless the server was
started with `--allow-forget`.

`awm recall --claude-hook` prints the ten nearest, newest facts for this repository as a
Claude Code `SessionStart` hook payload (`additionalContext`, capped at 1500 characters).
It exits 0 and prints nothing when there is no store, no derivable scope, or no memory,
and it never creates the database.

## When a fact changes, and who "VS" is (v0.4, schema v2)

Session 1: "I prefer dark mode". Session 5: "I switched to light mode". Session 8:
"which mode do I use?" -- a memory that appends answers with both; one that upserts
destroys the first. awm does neither:

```python
st.reconcile_and_remember(scope, "prefers dark mode", subject="user.ui_theme")
st.reconcile_and_remember(scope, "switched to light mode", subject="user.ui_theme")
st.recall(scope)                  # light mode only
st.history(scope, "user.ui_theme")  # dark (valid_to set, superseded), then light
st.recall(scope, as_of=ts)        # what was true at ts
```

A changed value moves the old one to `memory_history` with the interval it was true
for; `forget` records the deletion there too, and only `purge_history(scope, key)`, at
exactly one scope, erases it. The decision to add, update or ignore is a
`Reconciler`'s: `SlotReconciler` is deterministic, `LLMReconciler(complete)` takes a
model callable the host injects (awm itself never calls a model), and every decision is
validated before it is applied -- a malformed model reply raises, it is never guessed.

`resolve_entity(scope, mention)` links "vansh from india" to "vansh" (a known name plus
a qualifier is CONFIRMED, the qualifier kept as evidence), but "VS" only becomes a
POSSIBLE alias of every plausible entity -- nothing is merged until
`confirm_alias`/`reject_alias`. The qualifier must follow the name directly: "Vansh
Kumar, the designer" and "Vansh A. Sharma" are only POSSIBLE matches for "vansh", and
the order does not matter -- "vansh from india" first creates "vansh". "me", "I" and
"myself" are the scope's own user (an entity already named after the user is reused).
`reconcile_and_remember` reads candidates at the write scope only: a project value for
a subject the user scope also holds is an override, and `recall` ranks it first.
History, `as_of` and entities obey the same rule as `recall`: ancestors visible,
siblings never. (Up to 0.5.0 a v1 file migrated on first open; since 0.6.0 that is
opt-in -- see "Upgrading a memory file" below.)

## What an action does, and when it surprised us (v0.5, schema v3)

The facts visible from a scope ARE a world state. `encode_state(scope, prefix=...)`
returns them as a `WorldState` with a stable digest (sorted key/value pairs, nearest
scope winning a key); `as_of=` gives the state at any past instant. Record what an
action did and awm keeps a transition table beside the facts:

```python
before = st.encode_state(scope, prefix="grid")
# ... act: the agent (or the user) changes some facts ...
after = st.encode_state(scope, prefix="grid")
t = st.observe_transition(scope, before, "move right", after)   # t.surprise in [0,1]
p = st.predict_outcome(scope, st.encode_state(scope, prefix="grid"), "move right")
p.source      # RECALLED | PREDICTED | GENERALIZED | NONE -- never merged
st.unexplained_changes(scope, since)   # slots that changed with no action covering them
st.surprise_log(scope, since)          # scored transitions + unexplained changes
```

Prediction is tabular first: the most recent outcome of the exact (state, action),
confidence = the share of observations that agree (RECALLED). Only on a miss is an
injected `predictor` asked (PREDICTED); failing that, the action's outcome over every
state (GENERALIZED, labelled state-blind); failing that, NONE -- "no model", never
"nothing changes". Measured on real transitions, a self-updating last-outcome table
predicts the next state better (0.972) than a trained MLP (0.936); the learned model is
the tail.

Surprise is violation of expectation: the fraction of touched slots whose predicted
next value differs from the observed one, NULL when nothing was predicted (novelty is
not surprise). A slot that changes with no recorded transition taking it to that value
is an UNEXPLAINED change -- "I switched to light mode" recorded as an action explains
dark -> light; the same write with nothing announcing it is a teleport. Transitions
obey the scope rules: written at exactly one scope, read from it and its ancestors,
never a sibling. A newer file is refused.

`surprise_stats(scope, since)` (0.6) summarises it: `count`, `mean`, `p50`/`p90` of the
scored transitions, `novel` (nothing predicted), `unexplained` changes, and a
calibration table -- for each stated-confidence bucket, the mean confidence against the
share of predictions that were exactly right, plus `ece`, the n-weighted gap. A bucket
with no transitions reports None, not 0. It reads the predictions as they were made and
scored at the time; nothing is re-predicted.

## Entities: typos, initials, and undoing a merge (v0.6)

- A near-miss spelling is a POSSIBLE link, never a confirmed one: "Vanhs" proposes
  "vansh" (edit distance 1, with a neighbour swap counted as one edit, when both names
  have at least 5 characters; distance 2 at 8+). Names that differ in their digits
  ("server01"/"server02") are never typos. The canonical name is matched as well as
  the confirmed aliases.
- A mention WRITTEN as initials ("VS", "V.S.") ranks the entities whose multi-word name
  it is exactly the initials of first ("vansh sharma" before "vosk"). Lowercase "vs"
  keeps the older order: it may be an abbreviation.
- `merge_entities(scope, keep, drop)` declares two entities one: drop's aliases move to
  keep at exactly `scope`, and the prior alias sets are recorded. `split_entity(scope,
  merged_id)` restores them exactly (an alias keep gained afterwards, under a name
  neither had, stays with keep and is listed). Refused: an id not visible from the
  scope (a sibling's), a `drop` living at another scope or named at another scope, a
  name confirmed for one and rejected for the other, and splitting out of order. CLI:
  `awm entity merge KEEP DROP --scope S`, `awm entity split MERGED_ID --scope S`; MCP:
  `awm_merge_entities`, `awm_split_entity`.

## Upgrading a memory file (v0.6)

awm 0.4.0 and 0.5.0 migrated an older file the moment they opened it. The version bump
locked every installed older reader out: awm 0.3.x refuses any file that is not schema
v1, so one `awm recall` from a newer install broke every other tool sharing
`~/.aither/awm/memory.db`. **0.6.0 never migrates on open.**

- An older file opens in COMPAT mode and is not written on open. `remember`, `recall`,
  `forget` and `count` behave exactly as the file's version does (v1: an in-place
  upsert, no history), so 0.3.x keeps reading it. Every newer feature (history,
  `as_of`, reconcile, entities, the world model, surprise stats, merge) raises
  `NeedsMigration`, which names the command. A v2 file keeps its v2 features.
- `awm migrate [--db P] [--no-backup] [--dry-run]` (or `MemoryStore.migrate()`) takes
  the write lock, writes a byte backup next to the file and checks its sha256 against
  the original, migrates in one transaction, and compares every table's row count and a
  digest of every memory before and after; any difference rolls back. It prints both
  counts. After it, awm older than 0.4.0 cannot open the file; the backup can.
- `awm doctor [--db P]` shows the file's schema against the code's and whether compat
  mode is active. It opens the file read-only and never creates one.
- `AWM_AUTO_MIGRATE=1` restores migrate-on-open (still with the backup).
- New files are created at the current schema (v3).

## Everything it does

| | |
|---|---|
| `awm remember --scope S --key K --value V` | write at exactly one scope |
| `awm recall --scope S [--query Q] [--kind K] [--limit N]` | this scope and its ancestors |
| `awm forget --scope S --key K` | remove one row |
| `awm recall --as-of TS` | what was true then (unix seconds or ISO date) |
| `awm history KEY` | every value KEY has held, oldest first |
| `awm entity resolve\|confirm\|reject\|list` | who a mention names; nothing merged on a guess |
| `awm world state\|predict ACTION\|surprises\|stats` | the world state, what an action does, what surprised us, how calibrated it was |
| `awm entity merge KEEP DROP` · `awm entity split MERGED_ID` | declare two entities one; undo it exactly |
| `awm migrate [--dry-run] [--no-backup]` | older file -> current schema, verified backup first |
| `awm doctor [--db P]` | what is installed, and the file's schema vs the code's |
| `MemoryStore` · `Memory` · `HistoryEntry` · `Scope` · `visible_scopes` | the Python API |
| `Reconciler` · `SlotReconciler` · `LLMReconciler` · `Decision` · `Resolution` · `normalize` | reconcile and entities |
| `WorldState` · `WorldPrediction` · `Transition` · `SurpriseEvent` · `SurpriseStats` · `encode_state` · `state_digest` | the dynamics layer (`awm.world`) |
| `NeedsMigration` · `MigrationError` · `probe_schema` | compat mode and the opt-in migration |
| `ANCESTOR_DECAY` · `PLATFORM` · `WILDCARD` · `SCHEMA_VERSION` | the constants that define the rules |

## Licence

Apache-2.0.

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awpack](https://github.com/Aitherium/awpack) | that the pack you want shipped inside somebody's SDK, under whatever licence that SDK happens to carry | the pack as its own versioned artifact, with its own licence, that any agent runtime can install |
| **awm** _(you are here)_ | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awdesk](https://github.com/Aitherium/awdesk) | that the agent is somewhere behind a browser tab | a tray icon, a face on your desktop, and the decision card that pops when it needs you |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awdelphi](https://github.com/Aitherium/awdelphi) | one agent's confident take on a decision | the round trace, the anonymity, and who dissents |
| [awclassify](https://github.com/Aitherium/awclassify) | a filename, a folder, or whoever last touched it | doc_type, visibility, audience and topics, with the evidence lines that decided each |
| [awdecide](https://github.com/Aitherium/awdecide) | a hosted classifier's probability that never learns whether it was right | the decision, its probability, and the calibration curve from your own resolved outcomes |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awsuite](https://github.com/Aitherium/awsuite) | that an agent holding your mailbox will not send on its own | every send, draft, upload and create returns a dry-run until confirm is true |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awrena](https://github.com/Aitherium/awrena) | a leaderboard someone can edit, and votes nobody counted | a scored duel with both answers kept, and a result bound to them |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awstorage](https://github.com/Aitherium/awstorage) | a du you ran last month, and a peers file that says 3 TB free | an inventory snapshot per node with a diff since the last one, and each tree classified re-fetchable or not |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awask](https://github.com/Aitherium/awask) | that anyone read the paragraph where you asked | the ask itself, with a button that steers the session that raised it |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awswarm](https://github.com/Aitherium/awswarm) | that a model either fits your GPU or it doesn't run at all | a placement plan and an acquisition-probability estimate before you spend on a run |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [awvoice](https://github.com/Aitherium/awvoice) | that a cloud vendor may hold your audio | a transcript and a wav from a service you host |
| [awvision](https://github.com/Aitherium/awvision) | a filename and a caption somebody wrote | what a model actually reports about the pixels |
| [awscreen](https://github.com/Aitherium/awscreen) | a selector that was true when the page was written | the elements actually rendered, by what they look like |
| [awbeads](https://github.com/Aitherium/awbeads) | that a layout your users built survives the next deploy | the arrangement as data you can read back, diff, and hand to another surface |
| [awbonsai](https://github.com/Aitherium/awbonsai) | that inference always means a request left the machine | a WebGPU model answering on the tab's own GPU, with a consent record logged before it ever loaded |
| [gawbbonet](https://github.com/Aitherium/gawbbonet) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [awrtifact](https://github.com/Aitherium/awrtifact) | a hand-rolled split script and a hand-edited worker manifest | byte-verified parts in a release, served with Range + CORS, sizes asserted by a live gate |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awreport](https://github.com/Aitherium/awreport) | that the report you pasted carried no token in it | a redacted report, and the duplicate it merged into instead of filing twice |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awfocus](https://github.com/Aitherium/awfocus) | twelve terminal tabs and a bad memory | one command that names every session, finds any transcript, and opens or steers the one you want |
| [awgym](https://github.com/Aitherium/awgym) | that a world model learned anything from the games it saw | transitions captured from real play, fed back, and the retrodiction score falling on grids it never saw |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awevolve](https://github.com/Aitherium/awevolve) | that your optimisation loop is finding anything | every version it kept, the score that version earned, and the edit that produced it |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awmine](https://github.com/Aitherium/awmine) | that a session's lesson survived the session | a row per outcome, a candidate per lesson, and the transcript line each one came from |
| [awrise](https://github.com/Aitherium/awrise) | that a scheduled agent ran at all, and ran exactly once | a durable record of every wake -- fired, skipped, overlapped or timed out -- each with its reason |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |
| [awwall](https://github.com/Aitherium/awwall) | that a service only talks to the hosts you think it talks to | an explicit egress allowlist, where a denial names the rule that denied it |
| [awembed](https://github.com/Aitherium/awembed) | a general-purpose embedder that has never seen your code | a held-out split of whole directories, scored teacher vs student vs int8 |
| [awtax](https://github.com/Aitherium/awtax) | a closed tax app's sealed file you can never read again | a plain, provider-neutral schema of every figure, with the page it came from |
| [awsettings](https://github.com/Aitherium/awsettings) | that you will remember to re-approve the same thing on every box you work from | one profile, unioned rather than overwritten, with the credentials left behind |
| [awavatar](https://github.com/Aitherium/awavatar) | a cloud 3D vendor's opaque task id | a manifest with a sha256, a licence and a rig-audit verdict per file |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | [docs](https://aitherium.github.io/awpack/) |
| **awm** _(you are here)_ | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awdesk](https://github.com/Aitherium/awdesk) | Aither World Desk -- the desktop body of AitherOS Online: tray, avatars, decision cards, the Living Desktop as an overlay | [docs](https://aitherium.github.io/awdesk/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awdelphi](https://github.com/Aitherium/awdelphi) | Anonymous multi-round expert panels — a converged answer with a trace | [docs](https://aitherium.github.io/awdelphi/) |
| [awclassify](https://github.com/Aitherium/awclassify) | Classify any document -- what it is, who may read it, who it is for, what it is about | — |
| [awdecide](https://github.com/Aitherium/awdecide) | One typed-decision contract -- choice / score / bool with a probability -- over a ladder of backends you already run (rules, tiny local models, an LLM's logprobs), fail-closed, with a Brier ledger that resolves every decision against its outcome | — |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awsuite](https://github.com/Aitherium/awsuite) | Your Google Workspace as agent tools, and no write happens without a yes | — |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awrena](https://github.com/Aitherium/awrena) | Put two agents head to head and get a verdict you can check | [docs](https://aitherium.github.io/awrena/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awstorage](https://github.com/Aitherium/awstorage) | Every drive on every node, indexed, classified and diffed -- so you can see what you own before you delete it | [docs](https://aitherium.github.io/awstorage/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awask](https://github.com/Aitherium/awask) | Your agent asks you a question — and acts on your answer | [docs](https://aitherium.github.io/awask/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awswarm](https://github.com/Aitherium/awswarm) | Run one model too big for any single GPU across a pool of small ones | — |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awvoice](https://github.com/Aitherium/awvoice) | Hear and speak — transcribe audio, synthesize a voice | [docs](https://aitherium.github.io/awvoice/) |
| [awvision](https://github.com/Aitherium/awvision) | See an image — describe it, ask it a question, compare two | [docs](https://aitherium.github.io/awvision/) |
| [awscreen](https://github.com/Aitherium/awscreen) | See this machine — what is on screen, and where to click it | [docs](https://aitherium.github.io/awscreen/) |
| [awkit](https://github.com/Aitherium/awkit) | Render an agent panel from a tool result — one component, any React app | — |
| [awbeads](https://github.com/Aitherium/awbeads) | A spatial canvas for a page — arrange things, connect them, and keep the arrangement | — |
| [awbonsai](https://github.com/Aitherium/awbonsai) | Run a real model in the visitor's own browser — no server round trip, no upload | — |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [awbrain](https://github.com/Aitherium/awbrain) | Your history as a wiki of linked markdown — claims pinned to the evidence | — |
| [gawbbonet](https://github.com/Aitherium/gawbbonet) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | [docs](https://aitherium.github.io/gawbbonet/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrtifact](https://github.com/Aitherium/awrtifact) | Deliberately chunk artifacts into GitHub release assets — the productized aitherkvcache mirror lane | [docs](https://aitherium.github.io/awrtifact/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awreport](https://github.com/Aitherium/awreport) | File a bug report that has already scrubbed your secrets and collapsed the duplicate | — |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awfocus](https://github.com/Aitherium/awfocus) | See, search and steer every Claude session from one command | [docs](https://aitherium.github.io/awfocus/) |
| [awgym](https://github.com/Aitherium/awgym) | An ARC training gym — a game a world model can watch, and six roles that play through it | [docs](https://aitherium.github.io/awgym/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awevolve](https://github.com/Aitherium/awevolve) | Point an agent at a file and a command that scores it, and let it improve | — |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | [docs](https://aitherium.github.io/awsh/) |
| [awmine](https://github.com/Aitherium/awmine) | Mine what your agents did -- outcomes, lessons and procedures out of the transcripts they left behind | — |
| [awrise](https://github.com/Aitherium/awrise) | Wake an agent on a schedule, let it do one thing, and put it back to sleep | [docs](https://aitherium.github.io/awrise/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |
| [awwall](https://github.com/Aitherium/awwall) | Say what a workload may reach, and watch everything else fail closed | [docs](https://aitherium.github.io/awwall/) |
| [awrouter](https://github.com/Aitherium/awrouter) | OpenRouter for your own fleet: pick a model backend by cost/latency/ capability, fail over, fit the context window, stream. Standalone, OpenAI-compatible, no Aither-specifics required to be valuable | — |
| [awembed](https://github.com/Aitherium/awembed) | Train an embedding model that knows your corpus, and prove it beats the big one | [docs](https://aitherium.github.io/awembed/) |
| [awtax](https://github.com/Aitherium/awtax) | Turn any tax PDF -- returns, W-2, 1099, statements, even scans -- into structured data you can check | [docs](https://aitherium.github.io/awtax/) |
| [awflow](https://github.com/Aitherium/awflow) | A deterministic workflow runtime — chain agent calls with journal replay and budget control | [docs](https://aitherium.github.io/awflow/) |
| [awsettings](https://github.com/Aitherium/awsettings) | Your agent's permissions and config, following you to the next machine | [docs](https://aitherium.github.io/awsettings/) |
| [awavatar](https://github.com/Aitherium/awavatar) | One character spec in, a rigged, animated, multi-style avatar pack out | [docs](https://aitherium.github.io/awavatar/) |

**Built on** [llama.cpp](https://github.com/ggml-org/llama.cpp) · [vLLM](https://github.com/vllm-project/vllm) · [ComfyUI](https://github.com/comfyanonymous/ComfyUI) · [CentOS Stream](https://www.centos.org/centos-stream/) · [Podman](https://github.com/containers/podman) · [Docker](https://github.com/moby/moby) · [LanceDB](https://github.com/lancedb/lancedb) · [WireGuard](https://www.wireguard.com/) · [FFmpeg](https://ffmpeg.org/) · [Blender + Rigify](https://www.blender.org/) · [headroom](https://github.com/headroomlabs-ai/headroom) · [SANA](https://github.com/NVlabs/Sana) · [Hunyuan3D](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) · [repowise](https://github.com/repowise-dev/repowise) · [Playwright](https://github.com/microsoft/playwright) · [Chromium](https://www.chromium.org/) · [Next.js](https://github.com/vercel/next.js) · [React](https://github.com/facebook/react).

<div id="aither-constellation" data-self="awm"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
