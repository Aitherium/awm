# awm — a small, dependency-light world model

`world_model` is a learned-dynamics library: encode an observation to a
latent, predict the next latent given an action, score how surprised the
model was, optionally plan in latent space. Two engines, a small adapter
protocol, no framework lock-in.

- **`world_model.core.lewm.LeWorldModel`** — a LeWM-style JEPA (joint-embedding
  predictive architecture): a two-term loss (next-latent MSE + SIGReg
  isotropy regularization), a CEM planner, and an optional value head
  (`_ValueHead`/`_FsAdapter`) for value-guided planning — trained on returns
  over a frozen latent, off unless you construct with a value config. This
  is the full engine behind an ARC-AGI-3 solving agent, not a cut-down demo
  of it.
- **`world_model.core.mlp.MLPWorldModel`** — a lighter embedding-MLP
  transition model with a tabular cold-start fallback (tabular → hybrid →
  neural), for when a full JEPA is more machinery than the problem needs.
- **`world_model.contracts`** — the two Protocols (`WorldModel`,
  `EnvironmentAdapter`) everything else is written against. Write an adapter
  for your environment; nothing in an engine has to change.
- **`world_model.adapters.code_world`** — an example adapter mapping a
  codebase (landmarks + code chunks) into the engine's observation/action
  space. Reference implementation, not a complete environment.
- **`world_model.training.online`** — an optional, flag-gated
  surprise-modulated online learning-rate controller. Off by default. See
  [`world_model/training/PROVENANCE.md`](world_model/training/PROVENANCE.md)
  for concept provenance and license boundary — the *concepts* (not code)
  are credited to an external, restricted-license research project; this
  implementation is clean-room.

Every engine follows one rule: **degrade loudly, never silently.** A missing
torch install or an unreadable checkpoint sets `ok = False` and returns
`None`/`[]` — it never raises into a caller's turn loop and never fabricates
a prediction. That property mattered more than any architecture choice; see
[`RESULTS.md`](RESULTS.md).

## Install

```bash
pip install -e ".[torch]"   # torch + numpy are optional; engines degrade loudly without them
```

## Quick use

```python
from world_model.core.mlp import MLPWorldModel

model = MLPWorldModel()
model.observe(state, action, next_state, reward=0.0, done=False)
model.train_step()
z = model.encode(state)
pred = model.predict(z, action)
print(model.surprise(state, action, next_state))
```

## Why this exists

The goal is to help someone bootstrap their own world model — a working
engine, a planner, an optional value head, a contract to write your own
adapter against, and no framework lock-in — not a stripped demo pointing at
a hosted service. This is the actual core out of a larger internal agent
platform (Aitherium), extracted whole rather than trimmed, plus the
*findings* from running it against ARC-AGI-3: see [`RESULTS.md`](RESULTS.md)
for the honest version, including two negative results that mattered more
than any positive one.

## Status

Research code. The contracts and degrade-loudly discipline are load-bearing
and tested (`tests/`); the engines themselves are still moving. `lewm.py` is
the same file the ARC-AGI-3 solving agent runs, value head included — see
`world_model/__init__.py` for what's on by default vs. opt-in.

## License

MIT (see `LICENSE`). See `world_model/training/PROVENANCE.md` for the one
file with an external concept-attribution.

<!-- aitherium-ecosystem:start -->
## Aitherium open-source ecosystem

This repo is one piece of a connected set. All public, MIT/BSL-licensed:

| repo | what it is | pages |
|---|---|---|
| [aither-adk](https://github.com/Aitherium/aither-adk) | Build AI agent fleets — 3 lines, any backend | [docs](https://aitherium.github.io/aither-adk/) |
| [aither-skills](https://github.com/Aitherium/aither-skills) | Free agent skills, scripts & automations | [docs](https://aitherium.github.io/aither-skills/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework | [docs](https://aitherium.github.io/AitherZero/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git | [docs](https://aitherium.github.io/awgit/) |
| [awgraph](https://github.com/Aitherium/awgraph) | Code knowledge graph for AI agents | [docs](https://aitherium.github.io/awgraph/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Agent-to-agent messaging over any chat server | — |
| [awm](https://github.com/Aitherium/awm) | A small world model (LeWM JEPA + MLP) to bootstrap your own | [docs](https://aitherium.github.io/awm/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension: federated AI search & desktop bridge | — |
| [homebrew-tap](https://github.com/Aitherium/homebrew-tap) | `brew tap aitherium/tap` | — |

Built by [Aitherium](https://aitherium.com).
<!-- aitherium-ecosystem:end -->
