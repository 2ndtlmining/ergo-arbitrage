"""Find the trade size that maximises absolute profit (issue #10).

Profit curves for AMM/bank paths are concave apart from small rounding steps
(bank integer math, flat miner fees), so a coarse log-spaced scan locates the
peak's neighbourhood and a golden-section search refines it.
"""
import math
from typing import Callable

GOLDEN = (math.sqrt(5) - 1) / 2


def maximize(f: Callable[[float], float], lo: float, hi: float, tol: float = 0.01,
             coarse_points: int = 24) -> tuple[float, float]:
    """(x, f(x)) maximising f on [lo, hi]."""
    if hi <= lo:
        return lo, f(lo)
    # Coarse log-spaced scan (trade sizes span orders of magnitude)
    ratio = (hi / lo) ** (1 / (coarse_points - 1))
    xs = [lo * ratio ** i for i in range(coarse_points)]
    xs[-1] = hi
    values = [f(x) for x in xs]
    i = max(range(len(xs)), key=values.__getitem__)
    a = xs[max(i - 1, 0)]
    b = xs[min(i + 1, len(xs) - 1)]
    best_x, best_v = xs[i], values[i]

    # Golden-section refinement inside the bracket
    c, d = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    fc, fd = f(c), f(d)
    while b - a > tol:
        if fc >= fd:
            b, d, fd = d, c, fc
            c = b - GOLDEN * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + GOLDEN * (b - a)
            fd = f(d)
    for x, v in ((c, fc), (d, fd)):
        if v > best_v:
            best_x, best_v = x, v
    return best_x, best_v
