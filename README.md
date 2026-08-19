# awm — a small, dependency-light world model

`world_model` is a learned-dynamics library: encode an observation to a
latent, predict the next latent given an action, score how surprised the
model was, optionally plan in latent space. Two engines, a small adapter
protocol, no framework lock-in.

- **`world_model.core.lewm.LeWorldModel`** — a LeWM-style JEPA (joint-embedding
  predictive architecture): a two-term loss (next-latent MSE + SIGReg
  isotropy regularization) plus a CEM planner. This is the engine behind an
  ARC-AGI-3 solving agent.
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

This is the world-model core out of a larger internal agent platform
(Aitherium), extracted because the *findings* from using it against
ARC-AGI-3 are more useful shared than kept — see
[`RESULTS.md`](RESULTS.md) for the honest version, including the two
negative results that mattered more than any positive one.

## Status

Research code. The contracts and degrade-loudly discipline are load-bearing
and tested (`tests/`); the engines themselves are still moving. `lewm.py`
has a further fork (adding a value head) used inside the solving agent that
has not been merged back here yet — noted in `world_model/__init__.py`.

## License

MIT (see `LICENSE`) — proposed, not yet finalized. See `PROVENANCE.md` for
the one file with an external concept-attribution.
