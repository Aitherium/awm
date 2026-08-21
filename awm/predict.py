"""The seam awm offers a predictor — and deliberately nothing more.

awm answers "what happened, and who may see it". A predictor answers "what
happens next". They compose, and this module is the whole of the coupling: a
structural Protocol awm defines and never imports an implementation of.

WHY A PROTOCOL AND NOT A DEPENDENCY. `awpredict` ships a learned latent model
(LeWM JEPA + MLP) and wants torch. awm is sold as SQLite, no service, no network,
and a stranger who wants scoped memory should not acquire a deep-learning stack
to get it. EC003 says a brick's `adopt:` may not name a sibling; importing one
would make that sentence a lie.

So: `pip install awm` gives you memory, and a retrieval miss is simply a miss.
`pip install awm awpredict` and pass an engine, and misses come back marked
PREDICTED rather than RECALLED. Nothing in awm changes shape between those two
worlds.

WHY THE PREDICTOR IS THE TAIL AND NOT THE SUBSTRATE, which is the design
decision this file quietly enforces. Measured on real transitions by
`check_world_model_floor.py`:

    ONLINE last-outcome(state, action)   next-state class  0.9720
    the trained MLP                      next-state class  0.9357

The learned model LOSES to a self-updating lookup table on 98.9% of rows; it
wins only on the ~1.1% carrying a genuinely novel action. A design that consults
a model before consulting memory is choosing the worse answer for almost every
query. Hence: recall first, predict only on a miss, and never merge the two.

The members here are a strict subset of the `WorldModel` protocol `awpredict`
already publishes, so its engines satisfy this structurally -- no adapter, no
shim, no registration.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

__all__ = ["Predictor", "Prediction", "is_predictor"]


@runtime_checkable
class Predictor(Protocol):
    """What awm needs from a world model. A strict subset of `WorldModel`.

    Deliberately does NOT include `train_step`, `plan`, `save` or `load`: awm
    never trains, plans or checkpoints anything. Asking for members it does not
    use would exclude a perfectly good predictor for no reason, and would make
    this file quietly become a second definition of the world-model contract --
    which is how two copies of one rule start drifting.
    """

    def predict(self, z: Any, action: Any, **kwargs: Any) -> Any:
        """Latent + action -> predicted next latent, or None when degraded."""

    def surprise(self, obs: Any, action: Any, next_obs: Any,
                 *args: Any, **kwargs: Any) -> Optional[float]:
        """Prediction error for the observed transition; None when degraded."""


class Prediction:
    """A predicted answer, kept distinguishable from a recalled one.

    THE POINT OF THIS CLASS is that a caller can always tell which it got. A
    prediction rendered identically to a memory is the silent-no-op failure this
    codebase keeps re-learning: the feature appears to work, and what it returns
    is a guess. `recalled` is False here, always, and there is no constructor
    that sets it True.
    """

    __slots__ = ("value", "surprise", "engine")

    recalled = False

    def __init__(self, value: Any, surprise: Optional[float] = None,
                 engine: Optional[str] = None) -> None:
        self.value = value
        self.surprise = surprise
        self.engine = engine

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Prediction(value={self.value!r}, surprise={self.surprise!r}, "
                f"engine={self.engine!r})")


def is_predictor(obj: Any) -> bool:
    """True if `obj` can stand in as a predictor.

    `isinstance` against a runtime_checkable Protocol only checks that the
    NAMES exist, never their signatures. That is enough here and is stated
    rather than assumed: awm calls both members through keyword-tolerant
    wrappers, so an engine with extra parameters still works, and one with the
    wrong shape fails loudly at the call rather than silently returning None.
    """
    return isinstance(obj, Predictor)
