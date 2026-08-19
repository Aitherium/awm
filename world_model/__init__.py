"""world_model — a small, dependency-light world-model package (JEPA + MLP engines).

One package, two engines, N environment adapters:

  * ``world_model.core.lewm.LeWorldModel`` — the LeWM-style JEPA
    (two-term loss: next-latent MSE + SIGReg; CEM planner), used as the
    world model behind an ARC-AGI-3 solving agent. There is a fork of this
    file living inside that agent with a value head
    (_ValueHead/_FsAdapter/value()/train_value_step) not yet merged back —
    a known, deliberate divergence, not drift.
  * ``world_model.core.mlp.MLPWorldModel`` — an embedding-MLP transition
    model (tabular → hybrid → neural).

Contracts live in ``world_model.contracts`` (WorldModel, EnvironmentAdapter).
Torch and numpy are OPTIONAL at import time — engines degrade loudly
(ok == False), never raise into a caller.
"""

from world_model.contracts import EnvironmentAdapter, WorldModel, conforms

__version__ = "0.1.0"

__all__ = ["EnvironmentAdapter", "WorldModel", "conforms", "__version__"]
