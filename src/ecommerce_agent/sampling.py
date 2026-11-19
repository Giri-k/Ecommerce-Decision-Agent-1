"""Distribution primitives shared by independent planning and evaluator streams."""

import hashlib
import json
import math


def uniform(seed: int, *key) -> float:
    """Stable exogenous draw in (0,1), independent of call order and policy."""
    payload = json.dumps([seed, *key], separators=(",", ":")).encode()
    bits = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") >> 12
    return (bits + 0.5) / 2**52


def negative_binomial_quantile(u: float, mean: float, dispersion: float) -> int:
    """Inverse CDF via NB probability recurrence, with Var=mean+mean**2/k.

    Bounded numerical support fails explicitly rather than silently clipping
    demand. Core and declared stress configurations are well inside this range.
    """
    if not all(math.isfinite(x) for x in (u, mean, dispersion)):
        raise ValueError("distribution parameters must be finite")
    if not 0 < u < 1 or not 0 <= mean <= 100_000 or not 0 < dispersion <= 1000:
        raise ValueError("unsupported demand distribution parameters")
    if mean == 0:
        return 0
    probability = (dispersion / (dispersion + mean)) ** dispersion
    if probability == 0:
        raise ValueError("demand probability underflow")
    cumulative, value = probability, 0
    while cumulative < u:
        value += 1
        probability *= (value - 1 + dispersion) / value * mean / (dispersion + mean)
        cumulative += probability
        if value > 1_000_000 or probability == 0:
            raise ValueError("demand inverse CDF did not converge")
    return value
