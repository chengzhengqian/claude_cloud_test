"""Curve preparation, interpolation, and grids. All numerical work is NumPy/SciPy."""

import numpy as np
from scipy import interpolate as si

from .errors import DropCurve, GlueError

try:
    _trapezoid = np.trapezoid
except AttributeError:  # numpy < 2
    _trapezoid = np.trapz


def describe(key_desc, files):
    where = f"curve {key_desc}" if key_desc else "the curve"
    if files:
        where += f" in {files[0]}" + (f" (+{len(files) - 1} more files)" if len(files) > 1 else "")
    return where


def prepare(x, y, settings, report, key_desc="", files=(), xname="x"):
    """Sort by x, remove missing rows, and handle repeated x values."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    bad = np.isnan(x) | np.isnan(y)
    if bad.any():
        if settings.get("nan_rows") == "error":
            raise GlueError(f"{describe(key_desc, files)} has {int(bad.sum())} rows with a missing value. "
                            "Set nan_rows drop to skip them.")
        report.count("rows with missing values removed", int(bad.sum()))
        x, y = x[~bad], y[~bad]
    order = np.argsort(x, kind="stable")
    x, y = x[order], y[order]
    if len(x) > 1:
        dup = np.diff(x) == 0
        if dup.any():
            policy = settings.get("duplicates")
            if policy == "error":
                first = x[1:][dup][0]
                raise GlueError(f"{describe(key_desc, files)} has repeated {xname} = {first:g}. "
                                "Set duplicates to mean, first, last, or drop to continue.")
            report.count(f"curves with repeated {xname} ({policy})", 1, key_desc)
            ux, idx, counts = np.unique(x, return_index=True, return_counts=True)
            if policy == "mean":
                sums = np.add.reduceat(y, idx)
                x, y = ux, sums / counts
            elif policy == "first":
                x, y = ux, y[idx]
            elif policy == "last":
                x, y = ux, y[idx + counts - 1]
            elif policy == "drop":
                keep = counts == 1
                x, y = ux[keep], y[idx][keep]
    return x, y


class Interp:
    def __init__(self, obj, kind, xmin, xmax):
        self.obj = obj
        self.kind = kind
        self.xmin = xmin
        self.xmax = xmax

    def _raw(self, xq):
        if self.kind == "smooth":
            return self.obj(xq, ext=0)
        return self.obj(xq, extrapolate=True)

    def __call__(self, xq, extrapolate="nan", key_desc=""):
        xq = np.asarray(xq, dtype=float)
        outside = (xq < self.xmin) | (xq > self.xmax)
        if extrapolate == "clamp":
            return self._raw(np.clip(xq, self.xmin, self.xmax))
        if extrapolate == "error" and outside.any():
            raise GlueError(f"{describe(key_desc, ())}: point {xq[outside][0]:g} is outside the data range "
                            f"[{self.xmin:g}, {self.xmax:g}]. Set extrapolate to nan, extend, or clamp.")
        y = np.asarray(self._raw(xq), dtype=float)
        if extrapolate == "nan":
            y = np.where(outside, np.nan, y)
        return y

    def derivative(self, n=1):
        return Interp(self.obj.derivative(n), self.kind, self.xmin, self.xmax)

    def antiderivative(self):
        return Interp(self.obj.antiderivative(), self.kind, self.xmin, self.xmax)


def fit(x, y, method, settings, report, key_desc="", files=()):
    """Fit an interpolant. Applies the fewpoints setting."""
    name = method.name
    n = len(x)
    if n < method.min_points:
        policy = settings.get("fewpoints")
        if policy == "error":
            raise GlueError(f"{describe(key_desc, files)} has {n} points, but {name} needs {method.min_points}. "
                            "Set fewpoints to linear or drop.")
        if policy == "linear" and n >= 2:
            report.count(f"curves with too few points for {name} (used linear)", 1)
            name = "linear"
        else:
            raise DropCurve(f"{n} points, {name} needs {method.min_points}")
    if n < 2:
        raise DropCurve(f"{n} point(s)")
    if name == "linear":
        obj = si.make_interp_spline(x, y, k=1)
    elif name == "cubic":
        obj = si.CubicSpline(x, y)
    elif name == "pchip":
        obj = si.PchipInterpolator(x, y)
    elif name == "akima":
        obj = si.Akima1DInterpolator(x, y)
    elif name == "smooth":
        s = method.s if method.s is not None else None
        obj = si.UnivariateSpline(x, y, k=3, s=s)
    else:
        raise GlueError(f"method {name} can't be used for interpolation")
    return Interp(obj, name, x[0], x[-1])


def grid_points(spec, xs, like_x=None):
    """Grid for one matched set of curves. xs is a list of sorted x arrays."""
    k = spec.kind
    if k in ("overlap", "union"):
        lo = max(x[0] for x in xs)
        hi = min(x[-1] for x in xs)
        if not lo < hi:
            raise DropCurve("x ranges don't overlap")
        if k == "union":
            allx = np.unique(np.concatenate(xs))
            return allx[(allx >= lo) & (allx <= hi)]
        if spec.step is not None:
            pts = np.arange(lo, hi + spec.step * 1e-9, spec.step)
            return pts[pts <= hi]
        return np.linspace(lo, hi, spec.n)
    if k == "like":
        return np.unique(np.asarray(like_x, dtype=float))
    if k == "linspace":
        return np.linspace(spec.a, spec.b, spec.n)
    if k == "logspace":
        return np.geomspace(spec.a, spec.b, spec.n)
    if k == "points":
        return np.asarray(spec.points, dtype=float)
    raise AssertionError(k)


def trapezoid(y, x):
    return float(_trapezoid(y, x)) if len(x) > 1 else float("nan")


def cumtrapz(y, x):
    from scipy.integrate import cumulative_trapezoid as ct

    if len(x) < 2:
        return np.zeros(len(x))
    return ct(y, x, initial=0.0)
