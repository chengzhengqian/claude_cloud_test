"""Custom operations for this project, registered with @glue.op."""

import numpy as np

import glue


@glue.op(kind="reduce", version="1")
def fwhm(x, y):
    """Full width at half maximum of the highest peak in a curve."""
    order = np.argsort(x)
    x, y = np.asarray(x, float)[order], np.asarray(y, float)[order]
    i = int(np.nanargmax(y))
    half = y[i] / 2
    left = np.where(y[:i] < half)[0]
    right = np.where(y[i:] < half)[0]
    if len(left) == 0 or len(right) == 0:
        return np.nan
    a, b = left[-1], i + right[0]
    xl = np.interp(half, [y[a], y[a + 1]], [x[a], x[a + 1]])
    xr = np.interp(half, [y[b], y[b - 1]], [x[b], x[b - 1]])
    return xr - xl
