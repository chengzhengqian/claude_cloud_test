"""Custom operations for the spin chain tutorial. The project loads this module through [plugins]."""

import numpy as np

import glue


@glue.op(kind="reduce", version="1")
def intercept(x, y, deg=1):
    """Fit a polynomial of degree `deg` to y(x) and return its value at x = 0.

    With x = 1/L^2 this extrapolates a finite-size result to the infinite chain.
    """
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() <= deg:
        return np.nan
    return float(np.polyval(np.polyfit(x[ok], y[ok], int(deg)), 0.0))
