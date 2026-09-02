# awm

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awm/)**  ·  [Source](https://github.com/Aitherium/awm)  ·  `pip install awm`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awm** is one of its 41 bricks — each installs on its own, runs offline, and needs no account.
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

## Everything it does

| | |
|---|---|
| `awm remember --scope S --key K --value V` | write at exactly one scope |
| `awm recall --scope S [--query Q] [--kind K] [--limit N]` | this scope and its ancestors |
| `awm forget --scope S --key K` | remove one row |
| `awm doctor` | what is installed, and whether the store answers |
| `MemoryStore` · `Memory` · `Scope` · `visible_scopes` | the Python API |
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
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awdelphi](https://github.com/Aitherium/awdelphi) | one agent's confident take on a decision | the round trace, the anonymity, and who dissents |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| [awrtifact](https://github.com/Aitherium/awrtifact) | a hand-rolled split script and a hand-edited worker manifest | byte-verified parts in a release, served with Range + CORS, sizes asserted by a live gate |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awfocus](https://github.com/Aitherium/awfocus) | twelve terminal tabs and a bad memory | one command that names every session, finds any transcript, and opens or steers the one you want |
| [awgym](https://github.com/Aitherium/awgym) | that a world model learned anything from the games it saw | transitions captured from real play, fed back, and the retrodiction score falling on grids it never saw |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | [docs](https://aitherium.github.io/awpack/) |
| **awm** _(you are here)_ | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awdelphi](https://github.com/Aitherium/awdelphi) | Anonymous multi-round expert panels — a converged answer with a trace | [docs](https://aitherium.github.io/awdelphi/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | [docs](https://aitherium.github.io/gobbonet-agentic/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrtifact](https://github.com/Aitherium/awrtifact) | Deliberately chunk artifacts into GitHub release assets — the productized aitherkvcache mirror lane | [docs](https://aitherium.github.io/awrtifact/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awfocus](https://github.com/Aitherium/awfocus) | See, search and steer every Claude session from one command | [docs](https://aitherium.github.io/awfocus/) |
| [awgym](https://github.com/Aitherium/awgym) | An ARC training gym — a game a world model can watch, and six roles that play through it | [docs](https://aitherium.github.io/awgym/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | [docs](https://aitherium.github.io/awsh/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awm"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
