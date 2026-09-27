"""The operation tree and its evaluation.

Every node can list its curve keys without reading data (`keys`) and compute
one curve for one key (`compute`). The runner loops over keys, so memory
holds one curve's inputs at a time plus the results."""

import re
from collections import OrderedDict

import numpy as np
import pandas as pd

from . import numerics as nm
from .errors import DropCurve, GlueError
from .util import fmt_key, relpath


# ------------------------------------------------------------------ values


class Curve:
    __slots__ = ("x", "y", "files")

    def __init__(self, x, y, files=()):
        self.x = x
        self.y = y
        self.files = files


class Point:
    __slots__ = ("y", "files")

    def __init__(self, y, files=()):
        self.y = y
        self.files = files


class FreeX:
    """A bare x name like T. It takes its points from the curve it meets."""

    __slots__ = ("fn",)

    def __init__(self, fn):
        self.fn = fn


class TableValue:
    __slots__ = ("frame", "files")

    def __init__(self, frame, files=()):
        self.frame = frame
        self.files = files


class _Any:
    def __repr__(self):
        return "ANY"


ANY = _Any()
KIND_ORDER = {"scalar": 0, "keyed": 1, "curves": 2}


# ------------------------------------------------------------------ report


class Report:
    def __init__(self):
        self.items = OrderedDict()  # message -> count
        self.details = OrderedDict()  # message -> list of detail strings

    def count(self, message, n=1, detail=None):
        self.items[message] = self.items.get(message, 0) + n
        if detail is not None:
            self.details.setdefault(message, []).append(detail)

    def note(self, message):
        self.items.setdefault(message, None)

    def lines(self, level="short"):
        if level == "off":
            return []
        out = []
        for msg, n in self.items.items():
            if n is None:
                out.append(msg)
            elif "{n}" in msg:
                text = msg.format(n=n)
                if n == 1:
                    text = re.sub(r"\b1 (curve|key|row|file)s\b", r"1 \1", text)
                    text = re.sub(r"\b1 unmatched keys\b", "1 unmatched key", text)
                out.append(text)
            else:
                out.append(f"{msg}: {n}")
            if level == "full":
                for d in self.details.get(msg, []):
                    out.append(f"    {d}")
        return out


# ------------------------------------------------------------------ context


class Context:
    def __init__(self, settings, like_nodes=None, memo_size=20000):
        self.settings = settings
        self.report = Report()
        self.like_nodes = like_nodes or {}
        self._memo = OrderedDict()
        self._keys = {}
        self._deps = []
        self.memo_size = memo_size
        self.explain = None
        self.columns_read = {}

    def keys(self, node, sels):
        sig = (id(node), tuple(s.src() for s in sels))
        if sig not in self._keys:
            self._keys[sig] = node.keys(self, sels)
        return self._keys[sig]

    def eval(self, node, keyd):
        mkey = (id(node), tuple(keyd[c] for c in node.coords))
        hit = self._memo.get(mkey)
        if hit is not None:
            value, deps = hit
            if self._deps:
                self._deps[-1].update(deps)
            return value
        self._deps.append(set())
        try:
            value = node.compute(self, keyd)
        finally:
            deps = self._deps.pop()
        if self._deps:
            self._deps[-1].update(deps)
        self._memo[mkey] = (value, frozenset(deps))
        if len(self._memo) > self.memo_size:
            self._memo.popitem(last=False)
        return value

    def add_deps(self, table, rels):
        if self._deps:
            self._deps[-1].update((table, r) for r in rels)

    def like_x(self, spec, keyd):
        node = self.like_nodes.get(id(spec.like))
        if node is None:
            raise GlueError(f"like({spec.like.src()}) could not be resolved")
        v = self.eval(node, _proj(keyd, node.coords))
        if not isinstance(v, Curve):
            raise GlueError(f"like() needs curves, but {spec.like.src()} is not a curve family")
        return v.x


def _proj(keyd, coords):
    return {c: keyd[c] for c in coords}


def _py_key(v):
    return v.item() if hasattr(v, "item") else v


def restrict(sels, coords):
    return [s for s in sels if s.name in coords]


# ------------------------------------------------------------------ nodes


class ENode:
    kind = "curves"
    coords = ()
    xname = None
    yname = "value"
    unit = ""
    label = ""
    text = "?"
    origin = None

    def children(self):
        return []

    def keys(self, ctx, sels):
        raise NotImplementedError

    def compute(self, ctx, keyd):
        raise NotImplementedError

    def ctype(self, coord):
        for ch in self.children():
            if coord in ch.coords:
                return ch.ctype(coord)
        return "float"

    def key_desc(self, keyd):
        return fmt_key(self.coords, [keyd[c] for c in self.coords])

    def explain(self, out, depth=0):
        for ch in self.children():
            ch.explain(out, depth)


class Const(ENode):
    kind = "scalar"

    def __init__(self, value):
        self.value = float(value)
        self.coords = []
        self.text = repr(self.value)
        self.origin = ("const",)

    def keys(self, ctx, sels):
        return [()]

    def compute(self, ctx, keyd):
        return Point(self.value)


class TableNode(ENode):
    kind = "table"

    def __init__(self, table, coord_sels=(), x_sels=()):
        self.table = table
        self.coords = list(table.coords)
        self.xname = table.x
        self.coord_sels = list(coord_sels)
        self.x_sels = list(x_sels)
        self.text = table.name
        self.yname = table.name
        self.origin = ("src", id(table), tuple(s.src() for s in self.x_sels))

    def ctype(self, coord):
        return self.table.ctype(coord)

    def keys(self, ctx, sels):
        return self.table.keys(list(sels) + self.coord_sels, explain=ctx.explain)

    def compute(self, ctx, keyd):
        t = self.table
        cols = ([t.x] if t.x else []) + [c for c in t.values if c != t.x]
        key = tuple(keyd[c] for c in t.coords)
        df, chunks = t.read_key(key, cols, self.x_sels)
        ctx.add_deps(t.name, [c.rel for c in chunks])
        ctx.columns_read.setdefault(t.name, set()).update(cols)
        return TableValue(df.reset_index(drop=True), tuple(relpath(c.path, ".") for c in chunks))

    def explain(self, out, depth=0):
        out.setdefault("columns", {}).setdefault(self.table.name, set()).update(
            ([self.table.x] if self.table.x else []) + list(self.table.values))


class SourceCol(ENode):
    def __init__(self, table, col, coord_sels=(), x_sels=()):
        self.table = table
        self.col = col
        self.coords = list(table.coords)
        self.xname = table.x
        self.kind = "curves" if table.x else "keyed"
        self.coord_sels = list(coord_sels)
        self.x_sels = list(x_sels)
        self.yname = col
        self.unit = table.unit(col)
        self.label = table.label(col)
        sel = "".join(f"[{s.src()}]" for s in self.coord_sels + self.x_sels)
        self.text = f"{table.name}{sel}.{col}"
        self.origin = ("src", id(table), tuple(s.src() for s in self.x_sels))

    def ctype(self, coord):
        return self.table.ctype(coord)

    def keys(self, ctx, sels):
        return self.table.keys(list(sels) + self.coord_sels, explain=ctx.explain)

    def columns(self):
        t = self.table
        if self.col == t.x or not t.x:
            return [self.col]
        return [t.x, self.col]

    def compute(self, ctx, keyd):
        t = self.table
        key = tuple(keyd[c] for c in t.coords)
        cols = self.columns()
        df, chunks = t.read_key(key, cols, self.x_sels)
        ctx.add_deps(t.name, [c.rel for c in chunks])
        ctx.columns_read.setdefault(t.name, set()).update(cols)
        files = tuple(relpath(c.path, ".") for c in chunks)
        if not t.x:
            if len(df) == 0:
                raise DropCurve("no rows")
            if len(df) > 1:
                raise GlueError(f"keyed table {t.name} has {len(df)} rows for {self.key_desc(keyd)} "
                                f"({files[0] if files else ''}). A table without x needs one row per key.")
            return Point(float(df[self.col].iloc[0]), files)
        x = df[t.x].to_numpy(dtype=float)
        y = x.copy() if self.col == t.x else df[self.col].to_numpy(dtype=float)
        return Curve(x, y, files)

    def explain(self, out, depth=0):
        out.setdefault("columns", {}).setdefault(self.table.name, set()).update(self.columns())


class CoordRef(ENode):
    kind = "keyed"

    def __init__(self, name, source=None, ctype="float"):
        self.name = name
        self.source = source
        self.coords = [name]
        self._ctype = ctype
        self.yname = name
        self.text = name if source is None else f"{source.text}.{name}"
        self.origin = ("coord", name)

    def ctype(self, coord):
        return self._ctype

    def children(self):
        return [self.source] if self.source is not None else []

    def keys(self, ctx, sels):
        if self.source is None:
            return ANY
        ks = ctx.keys(self.source, restrict(sels, self.source.coords))
        i = self.source.coords.index(self.name)
        return sorted({(k[i],) for k in ks}, key=lambda t: (isinstance(t[0], str), t[0]))

    def compute(self, ctx, keyd):
        v = keyd[self.name]
        if isinstance(v, str):
            raise GlueError(f"coordinate {self.name} is a string and can't be used in arithmetic")
        return Point(float(v))


class XRef(ENode):
    """A bare x name, like T in C / T."""

    def __init__(self, name):
        self.xname = name
        self.coords = []
        self.yname = name
        self.text = name
        self.origin = ("freex",)

    def keys(self, ctx, sels):
        return [()]

    def compute(self, ctx, keyd):
        return FreeX(lambda x: x)


class XOf(ENode):
    def __init__(self, node):
        self.node = node
        self.coords = list(node.coords)
        self.xname = node.xname
        self.yname = node.xname
        self.text = f"{node.text}.{node.xname}"
        self.origin = node.origin

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        return ctx.keys(self.node, sels)

    def compute(self, ctx, keyd):
        v = ctx.eval(self.node, keyd)
        return Curve(v.x, v.x.copy(), v.files)


class Named(ENode):
    def __init__(self, node, name, unit=None, label=None):
        self.node = node
        self.kind = node.kind
        self.coords = node.coords
        self.xname = node.xname
        self.yname = name
        self.unit = node.unit if unit is None else unit
        self.label = label or name
        self.text = name
        self.origin = node.origin

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        return ctx.keys(self.node, sels)

    def compute(self, ctx, keyd):
        return ctx.eval(self.node, keyd)


class Using(ENode):
    def __init__(self, node, overrides):
        self.node = node
        self.overrides = overrides
        self.kind = node.kind
        self.coords = node.coords
        self.xname = node.xname
        self.yname = node.yname
        self.unit = node.unit
        self.label = node.label
        self.text = node.text
        self.origin = node.origin

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        ctx.settings.push(self.overrides)
        try:
            return ctx.keys(self.node, sels)
        finally:
            ctx.settings.pop()

    def compute(self, ctx, keyd):
        ctx.settings.push(self.overrides)
        try:
            return ctx.eval(self.node, keyd)
        finally:
            ctx.settings.pop()

    def explain(self, out, depth=0):
        s = out.get("settings")
        if s is not None:
            s.push(self.overrides)
        try:
            self.node.explain(out, depth)
        finally:
            if s is not None:
                s.pop()


class SelectNode(ENode):
    def __init__(self, node, coord_sels, x_sels):
        self.node = node
        self.kind = node.kind
        self.coords = node.coords
        self.xname = node.xname
        self.yname = node.yname
        self.unit = node.unit
        self.label = node.label
        self.coord_sels = coord_sels
        self.x_sels = x_sels
        self.text = f"{node.text}[{', '.join(s.src() for s in coord_sels + x_sels)}]"
        self.origin = (node.origin, "sel", tuple(s.src() for s in x_sels)) if x_sels else node.origin

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        return ctx.keys(self.node, list(sels) + self.coord_sels)

    def compute(self, ctx, keyd):
        v = ctx.eval(self.node, keyd)
        if self.x_sels and isinstance(v, Curve):
            from .sources import x_mask

            m = x_mask(v.x, self.x_sels)
            return Curve(v.x[m], v.y[m], v.files)
        return v


def _apply_fn(fn, v):
    if isinstance(v, Curve):
        with np.errstate(all="ignore"):
            return Curve(v.x, fn(v.y), v.files)
    if isinstance(v, Point):
        with np.errstate(all="ignore"):
            return Point(float(fn(np.float64(v.y))), v.files)
    if isinstance(v, FreeX):
        f = v.fn
        return FreeX(lambda x: fn(f(x)))
    raise GlueError("can't apply a function to a table. Pick a column, as in dmft.E")


class Elementwise(ENode):
    def __init__(self, fname, fn, node):
        self.fname = fname
        self.fn = fn
        self.node = node
        self.kind = node.kind
        self.coords = node.coords
        self.xname = node.xname
        self.unit = node.unit if fname == "neg" or fname == "abs" else ""
        self.yname = node.yname if fname in ("neg", "abs") else "value"
        self.text = f"-{node.text}" if fname == "neg" else f"{fname}({node.text})"
        self.origin = node.origin

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        return ctx.keys(self.node, sels)

    def compute(self, ctx, keyd):
        return _apply_fn(self.fn, ctx.eval(self.node, keyd))


OPS = {
    "+": np.add,
    "-": np.subtract,
    "*": np.multiply,
    "/": np.divide,
    "^": np.power,
}


class Binary(ENode):
    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right
        lk, rk = left.kind, right.kind
        self.kind = lk if KIND_ORDER[lk] >= KIND_ORDER[rk] else rk
        if isinstance(left, CoordRef) and left.source is None:
            self.coords = list(right.coords) + ([left.name] if left.name not in right.coords else [])
        else:
            self.coords = list(left.coords) + [c for c in right.coords if c not in left.coords]
        self.xname = left.xname or right.xname
        if lk == "curves" and rk == "curves" and left.xname != right.xname:
            raise GlueError(f"{left.text} has x {left.xname} but {right.text} has x {right.xname}. "
                            "Curves can only be combined when they share the same x.")
        if op in "+-" and left.unit == right.unit:
            self.unit = left.unit
        elif op in "+-" and (lk == "scalar" or rk == "scalar"):
            self.unit = left.unit or right.unit
        else:
            self.unit = ""
        self.text = f"{left.text} {op} {right.text}"
        both_curves = lk == "curves" and rk == "curves"
        free = isinstance(left, XRef) or isinstance(right, XRef) or left.origin == ("freex",) or right.origin == ("freex",)
        if both_curves and not free:
            self.aligned = left.origin != right.origin
            self.origin = ("align", id(self)) if self.aligned else left.origin
        else:
            self.aligned = False
            if right.origin == ("freex",) and left.origin == ("freex",):
                self.origin = ("freex",)
            elif lk == "curves" and left.origin != ("freex",):
                self.origin = left.origin
            elif rk == "curves":
                self.origin = right.origin
            else:
                self.origin = left.origin

    def ctype(self, coord):
        for ch in (self.left, self.right):
            if coord in ch.coords:
                return ch.ctype(coord)
        return "float"

    def children(self):
        return [self.left, self.right]

    def keys(self, ctx, sels):
        lk = ctx.keys(self.left, restrict(sels, self.left.coords))
        rk = ctx.keys(self.right, restrict(sels, self.right.coords))
        if lk is ANY and rk is ANY:
            return ANY
        if lk is ANY or rk is ANY:
            known, node = (rk, self.right) if lk is ANY else (lk, self.left)
            free = self.left if lk is ANY else self.right
            if free.coords[0] not in node.coords:
                raise GlueError(f"{free.text} is a coordinate, but {node.text} has no coordinate {free.text}")
            return [tuple(dict(zip(node.coords, k))[c] for c in self.coords) for k in known]
        lc, rc = self.left.coords, self.right.coords
        shared = [c for c in lc if c in rc]
        groups = OrderedDict()
        ri = [rc.index(c) for c in shared]
        for k in rk:
            groups.setdefault(tuple(k[i] for i in ri), []).append(k)
        li = [lc.index(c) for c in shared]
        out = []
        used = set()
        left_only = []
        for k in lk:
            g = tuple(k[i] for i in li)
            matches = groups.get(g)
            if not matches:
                left_only.append(k)
                continue
            used.add(g)
            kd = dict(zip(lc, k))
            for m in matches:
                d = dict(kd)
                d.update(zip(rc, m))
                out.append(tuple(d[c] for c in self.coords))
        right_only = [k for g, ks in groups.items() if g not in used for k in ks]
        if left_only or right_only:
            if ctx.settings.get("unmatched") == "error":
                side = left_only[:1] and (self.left, left_only[0]) or (self.right, right_only[0])
                raise GlueError(f"{len(left_only) + len(right_only)} curve keys have no match, for example "
                                f"{fmt_key(side[0].coords, side[1])} in {side[0].text}. "
                                "Set unmatched drop to leave them out.")
            for node, ks in ((self.left, left_only), (self.right, right_only)):
                if ks:
                    msg = f"dropped {{n}} unmatched keys (only in {node.text})"
                    for k in ks:
                        ctx.report.count(msg, 1, fmt_key(node.coords, k))
        return out

    def compute(self, ctx, keyd):
        a = ctx.eval(self.left, _proj(keyd, self.left.coords))
        b = ctx.eval(self.right, _proj(keyd, self.right.coords))
        return self.combine(ctx, keyd, a, b)

    def combine(self, ctx, keyd, a, b):
        fn = OPS[self.op]
        if isinstance(a, TableValue) or isinstance(b, TableValue):
            raise GlueError("can't do arithmetic on a table. Pick a column, as in dmft.E")
        with np.errstate(all="ignore"):
            if isinstance(a, FreeX) or isinstance(b, FreeX):
                if isinstance(a, FreeX) and isinstance(b, FreeX):
                    fa, fb = a.fn, b.fn
                    return FreeX(lambda x: fn(fa(x), fb(x)))
                if isinstance(a, FreeX):
                    if isinstance(b, Point):
                        f, p = a.fn, b.y
                        return FreeX(lambda x: fn(f(x), p))
                    return Curve(b.x, fn(a.fn(b.x), b.y), b.files)
                if isinstance(a, Point):
                    f, p = b.fn, a.y
                    return FreeX(lambda x: fn(p, f(x)))
                return Curve(a.x, fn(a.y, b.fn(a.x)), a.files)
            if isinstance(a, Point) and isinstance(b, Point):
                return Point(float(fn(a.y, b.y)), a.files + b.files)
            if isinstance(a, Curve) and isinstance(b, Point):
                return Curve(a.x, fn(a.y, b.y), a.files)
            if isinstance(a, Point) and isinstance(b, Curve):
                return Curve(b.x, fn(a.y, b.y), b.files)
            if not self.aligned:
                if len(a.x) != len(b.x):
                    raise GlueError(f"internal: row-aligned curves differ in length for {self.key_desc(keyd)}")
                return Curve(a.x, fn(a.y, b.y), a.files + b.files)
            return self.align(ctx, keyd, a, b, fn)

    def align(self, ctx, keyd, a, b, fn):
        s = ctx.settings
        grid = s.get("grid")
        if s.get("strict") and s.source("grid") != "statement":
            raise GlueError(f"strict mode: {self.left.text} and {self.right.text} are on different grids. "
                            "Add `with grid=...` or use resample().")
        method = s.get("method")
        desc = self.key_desc(keyd)
        xa, ya = nm.prepare(a.x, a.y, s, ctx.report, desc, a.files, self.xname)
        xb, yb = nm.prepare(b.x, b.y, s, ctx.report, desc, b.files, self.xname)
        fa = nm.fit(xa, ya, method, s, ctx.report, desc, a.files)
        fb = nm.fit(xb, yb, method, s, ctx.report, desc, b.files)
        like = ctx.like_x(grid, keyd) if grid.kind == "like" else None
        pts = nm.grid_points(grid, [xa, xb], like)
        ext = s.get("extrapolate")
        ctx.report.count(f"aligned {self.left.text}, {self.right.text}: {{n}} curves matched on "
                         f"({', '.join(self.coords)}), grid {grid.text()}, method {method.text()}")
        return Curve(pts, fn(fa(pts, ext, desc), fb(pts, ext, desc)), a.files + b.files)

    def explain(self, out, depth=0):
        self.left.explain(out, depth)
        self.right.explain(out, depth)
        if self.aligned:
            s = out.get("settings")
            how = f": grid {s.get('grid').text()}, method {s.get('method').text()}" if s else ""
            out.setdefault("steps", []).append(f"align {self.left.text} and {self.right.text}{how}")


def _curve_input(ctx, node, keyd, what):
    v = ctx.eval(node, keyd)
    if isinstance(v, FreeX):
        raise GlueError(f"{what} needs curves, but {node.text} is only an x name")
    if not isinstance(v, Curve):
        raise GlueError(f"{what} needs curves, but {node.text} has one value per key")
    return v


class CurveOp(ENode):
    """Base for single-input operations on curves."""

    fname = "?"

    def __init__(self, node, **opts):
        self.node = node
        self.opts = opts
        self.coords = node.coords
        self.xname = node.xname
        self.yname = "value"
        self.text = f"{self.fname}({node.text})"
        self.origin = ("op", id(self))

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        extra = [self.opts["like"]] if self.opts.get("like") is not None else []
        return [self.node] + extra

    def keys(self, ctx, sels):
        return ctx.keys(self.node, sels)

    def method(self, ctx):
        return self.opts.get("method") or ctx.settings.get("method")

    def prepared(self, ctx, keyd):
        v = _curve_input(ctx, self.node, keyd, self.fname)
        desc = self.key_desc(keyd)
        x, y = nm.prepare(v.x, v.y, ctx.settings, ctx.report, desc, v.files, self.node.xname)
        if len(x) == 0:
            raise DropCurve("no points")
        return v, x, y, desc

    def explain(self, out, depth=0):
        self.node.explain(out, depth)
        out.setdefault("steps", []).append(self.step_text(out.get("settings")))

    def step_text(self, s):
        m = self.opts.get("method") or (s.get("method") if s else None)
        g = self.opts.get("grid") or (s.get("grid") if s and self.fname == "resample" else None)
        parts = []
        if g is not None:
            parts.append(f"grid {g.text()}")
        if m is not None and self.fname not in ("max", "min", "mean", "first", "last", "count", "argmax", "argmin"):
            if not (self.fname in ("int", "integral") and self.opts.get("method") is None):
                parts.append(f"method {m.text()}")
        return self.text + (f": {', '.join(parts)}" if parts else "")


class Resample(CurveOp):
    fname = "resample"

    def __init__(self, node, grid=None, method=None):
        super().__init__(node, grid=grid, method=method)
        self.unit = node.unit
        self.yname = node.yname

    def compute(self, ctx, keyd):
        v, x, y, desc = self.prepared(ctx, keyd)
        grid = self.opts["grid"] or ctx.settings.get("grid")
        method = self.method(ctx)
        f = nm.fit(x, y, method, ctx.settings, ctx.report, desc, v.files)
        like = ctx.like_x(grid, keyd) if grid.kind == "like" else None
        pts = nm.grid_points(grid, [x], like)
        ctx.report.count(f"resampled {self.node.text}: {{n}} curves, grid {grid.text()}, method {method.text()}")
        return Curve(pts, f(pts, ctx.settings.get("extrapolate"), desc), v.files)


class Deriv(CurveOp):
    fname = "d"

    def __init__(self, node, order=1, method=None, grid=None):
        super().__init__(node, order=order, method=method, grid=grid)
        self.text = f"d({node.text}, {node.xname})" if order == 1 else f"d({node.text}, {node.xname}, order={order})"

    def compute(self, ctx, keyd):
        v, x, y, desc = self.prepared(ctx, keyd)
        method = self.method(ctx)
        order = self.opts["order"]
        grid = self.opts["grid"]
        if method.name == "fd":
            if len(x) < 2:
                raise DropCurve("fewer than 2 points")
            dy = y
            for _ in range(order):
                dy = np.gradient(dy, x)
            if grid is not None:
                pts = nm.grid_points(grid, [x], ctx.like_x(grid, keyd) if grid.kind == "like" else None)
                return Curve(pts, np.interp(pts, x, dy, left=np.nan, right=np.nan), v.files)
            return Curve(x, dy, v.files)
        f = nm.fit(x, y, method, ctx.settings, ctx.report, desc, v.files).derivative(order)
        pts = x if grid is None else nm.grid_points(grid, [x], ctx.like_x(grid, keyd) if grid.kind == "like" else None)
        return Curve(pts, f(pts, ctx.settings.get("extrapolate"), desc), v.files)


class CumInt(CurveOp):
    fname = "int"

    def __init__(self, node, method=None):
        super().__init__(node, method=method)
        self.text = f"int({node.text}, {node.xname})"

    def compute(self, ctx, keyd):
        v, x, y, desc = self.prepared(ctx, keyd)
        method = self.opts["method"]
        if method is None or method.name == "trapz":
            return Curve(x, nm.cumtrapz(y, x), v.files)
        F = nm.fit(x, y, method, ctx.settings, ctx.report, desc, v.files).antiderivative()
        return Curve(x, F(x, "extend") - F(np.array([x[0]]), "extend")[0], v.files)


class Reduce(CurveOp):
    def __init__(self, node, fname, method=None):
        self.fname = fname
        super().__init__(node, method=method)
        self.kind = "keyed"
        self.origin = ("keyed",)
        if fname in ("max", "min", "mean", "first", "last"):
            self.unit = node.unit
        elif fname in ("argmax", "argmin"):
            self.unit = ""
        if fname == "integral":
            self.text = f"integral({node.text}, {node.xname})"
        self.xname = None

    def compute(self, ctx, keyd):
        v = _curve_input(ctx, self.node, keyd, self.fname)
        x = np.asarray(v.x, dtype=float)
        y = np.asarray(v.y, dtype=float)
        ok = ~(np.isnan(x) | np.isnan(y))
        x, y = x[ok], y[ok]
        f = self.fname
        if f == "count":
            return Point(float(len(y)), v.files)
        if len(y) == 0:
            raise DropCurve("no points")
        order = np.argsort(x, kind="stable")
        if f == "max":
            r = y.max()
        elif f == "min":
            r = y.min()
        elif f == "mean":
            r = y.mean()
        elif f == "first":
            r = y[order[0]]
        elif f == "last":
            r = y[order[-1]]
        elif f == "argmax":
            r = x[np.argmax(y)]
        elif f == "argmin":
            r = x[np.argmin(y)]
        elif f == "integral":
            desc = self.key_desc(keyd)
            xs, ys = nm.prepare(x, y, ctx.settings, ctx.report, desc, v.files, self.node.xname)
            method = self.opts["method"]
            if method is None or method.name == "trapz":
                r = nm.trapezoid(ys, xs)
            else:
                F = nm.fit(xs, ys, method, ctx.settings, ctx.report, desc, v.files).antiderivative()
                r = F(np.array([xs[-1]]), "extend")[0] - F(np.array([xs[0]]), "extend")[0]
        else:
            raise AssertionError(f)
        return Point(float(r), v.files)


class At(CurveOp):
    fname = "at"

    def __init__(self, node, points):
        super().__init__(node, points=points)
        self.single = not isinstance(points, (list, tuple))
        self.unit = node.unit
        self.yname = node.yname
        if self.single:
            self.kind = "keyed"
            self.origin = ("keyed",)
            self.xname = None
        pts = points if self.single else "[" + ", ".join(f"{p:g}" for p in points) + "]"
        self.text = f"{node.text} @ {node.xname}={pts}"

    def compute(self, ctx, keyd):
        v, x, y, desc = self.prepared(ctx, keyd)
        f = nm.fit(x, y, ctx.settings.get("method"), ctx.settings, ctx.report, desc, v.files)
        ext = ctx.settings.get("extrapolate")
        if self.single:
            return Point(float(f(np.array([self.opts["points"]]), ext, desc)[0]), v.files)
        pts = np.asarray(self.opts["points"], dtype=float)
        return Curve(pts, f(pts, ext, desc), v.files)


class Stack(ENode):
    def __init__(self, members, tag):
        self.members = members  # list of (label, node)
        self.tag = tag
        first = members[0][1]
        for label, node in members:
            if node.kind != first.kind or set(node.coords) != set(first.coords) or node.xname != first.xname:
                raise GlueError(f"stack needs members with the same coordinates and x. {label}={node.text} "
                                f"differs from {members[0][0]}={first.text}")
            if tag in node.coords:
                raise GlueError(f"stack tag {tag!r} is already a coordinate of {node.text}")
        self.kind = first.kind
        self.coords = list(first.coords) + [tag]
        self.xname = first.xname
        self.yname = first.yname
        self.unit = first.unit if all(n.unit == first.unit for _, n in members) else ""
        self.text = "stack(" + ", ".join(f"{l}={n.text}" for l, n in members) + f", tag={tag!r})"
        self.origin = ("stack", id(self))

    def ctype(self, coord):
        return "str" if coord == self.tag else self.members[0][1].ctype(coord)

    def children(self):
        return [n for _, n in self.members]

    def keys(self, ctx, sels):
        tag_sels = [s for s in sels if s.name == self.tag]
        from .sources import match_selector

        out = []
        for label, node in self.members:
            if not all(match_selector(s, label, "str") for s in tag_sels):
                continue
            ks = ctx.keys(node, restrict(sels, node.coords))
            for k in ks:
                d = dict(zip(node.coords, k))
                d[self.tag] = label
                out.append(tuple(d[c] for c in self.coords))
        return out

    def compute(self, ctx, keyd):
        label = keyd[self.tag]
        node = dict(self.members)[label]
        return ctx.eval(node, _proj(keyd, node.coords))


class PluginOp(ENode):
    def __init__(self, opdef, node, kwargs):
        self.opdef = opdef
        self.node = node
        self.kwargs = kwargs
        self.coords = node.coords
        self.xname = node.xname if opdef.kind != "reduce" else None
        self.kind = {"elementwise": node.kind, "curve": "curves", "reduce": "keyed"}[opdef.kind]
        self.text = f"{opdef.name}({node.text})"
        self.origin = node.origin if opdef.kind == "elementwise" else ("op", id(self))

    def ctype(self, coord):
        return self.node.ctype(coord)

    def children(self):
        return [self.node]

    def keys(self, ctx, sels):
        return ctx.keys(self.node, sels)

    def compute(self, ctx, keyd):
        k = self.opdef.kind
        if k == "elementwise":
            return _apply_fn(lambda y: self.opdef.fn(y, **self.kwargs), ctx.eval(self.node, keyd))
        v = _curve_input(ctx, self.node, keyd, self.opdef.name)
        if k == "curve":
            x, y = self.opdef.fn(np.asarray(v.x), np.asarray(v.y), **self.kwargs)
            return Curve(np.asarray(x, dtype=float), np.asarray(y, dtype=float), v.files)
        return Point(float(self.opdef.fn(np.asarray(v.x), np.asarray(v.y), **self.kwargs)), v.files)


# ------------------------------------------------------------------ running


def check_where(node, sels):
    coord_sels, x_sels = [], []
    for s in sels:
        if s.name in node.coords:
            coord_sels.append(s)
        elif node.xname and s.name == node.xname:
            x_sels.append(s)
        else:
            names = list(node.coords) + ([node.xname] if node.xname else [])
            raise GlueError(f"where {s.src()}: {node.text} has no coordinate {s.name} "
                            f"(it has {', '.join(names) or 'none'})")
    return coord_sels, x_sels


def result_names(node):
    y = node.yname or "value"
    taken = set(node.coords) | ({node.xname} if node.xname else set())
    while y in taken:
        y = y + "_"
    return y


def run(node, ctx, where=(), keys=None, limit=None):
    """Evaluate `node` curve by curve. Returns (frame, keys_done, dropped)."""
    coord_sels, x_sels = check_where(node, where)
    if keys is None:
        keys = ctx.keys(node, coord_sels)
    if keys is ANY:
        raise GlueError(f"{node.text} needs a table to take its values from")
    yname = result_names(node)
    cols = {c: [] for c in node.coords}
    xs, ys = [], []
    tables = []
    done, dropped, deps = [], [], {}
    nrows = 0
    for key in keys:
        if limit is not None and nrows >= limit:
            break
        keyd = dict(zip(node.coords, key))
        ctx._deps.append(set())
        try:
            v = ctx.eval(node, keyd)
        except DropCurve as e:
            ctx.report.count(f"dropped {{n}} curves ({e.reason})", 1, node.key_desc(keyd))
            dropped.append(key)
            deps[key] = ctx._deps.pop()
            continue
        except BaseException:
            ctx._deps.pop()
            raise
        deps[key] = ctx._deps.pop()
        if isinstance(v, FreeX):
            raise GlueError(f"{node.text} is only an x name. Combine it with a curve, as in dmft.E / T")
        if isinstance(v, TableValue):
            df = v.frame.copy()
            for i, c in enumerate(node.coords):
                df.insert(i, c, keyd[c])
            tables.append(df)
            nrows += len(df)
            done.append(key)
            continue
        if isinstance(v, Curve):
            if x_sels:
                from .sources import x_mask

                m = x_mask(v.x, x_sels)
                x, y = v.x[m], v.y[m]
            else:
                x, y = v.x, v.y
            n = len(x)
            xs.append(np.asarray(x, dtype=float))
            ys.append(np.asarray(y, dtype=float))
        else:
            n = 1
            ys.append(np.array([v.y], dtype=float))
        for c in node.coords:
            cols[c].append(np.full(n, keyd[c], dtype=object if isinstance(keyd[c], str) else None))
        nrows += n
        done.append(key)
    if node.kind == "table":
        if tables:
            frame = pd.concat(tables, ignore_index=True)
        else:
            frame = pd.DataFrame(columns=list(node.coords))
        return frame, done, dropped, deps
    data = {}
    for c in node.coords:
        data[c] = np.concatenate(cols[c]) if cols[c] else np.array([])
    if node.kind == "curves" or xs:
        data[node.xname] = np.concatenate(xs) if xs else np.array([], dtype=float)
    data[yname] = np.concatenate(ys) if ys else np.array([], dtype=float)
    frame = pd.DataFrame(data)
    for c in node.coords:
        if len(frame) and node.ctype(c) != "str":
            frame[c] = pd.to_numeric(frame[c])
    return frame, done, dropped, deps
