"""Core nodes: the 21 elementary operations and the standard library (docs/core.md, sections 2 and 3).

Every node is a field. A node knows its type without data (`type`), its
content-hash id (`id`), which of its inputs a filter may be pushed through
(`part`), and how to compute its value from its children's values."""

import hashlib
import importlib
from functools import cached_property

import numpy as np
import pandas as pd

from .. import numerics as nm
from ..errors import DropCurve, GlueError
from ..settings import METHODS, MethodSpec
from ..util import Inline, fmt_key, normalize
from . import LIB
from . import formula as F
from .formula import Pred
from .store import dumps
from .types import FType, Out, Var

DIGITS = 10
REGISTRY = {}


def register(cls):
    REGISTRY[cls.op] = cls
    return cls


def rnd(values):
    return np.round(np.asarray(values, dtype=float), DIGITS) + 0.0


def parse_method(text, allow=()):
    if text in METHODS or text in allow:
        return MethodSpec(text)
    if text.startswith("smooth:"):
        return MethodSpec("smooth", float(text.split(":", 1)[1]))
    if text == "smooth":
        return MethodSpec("smooth", None)
    raise GlueError(f"unknown method {text!r}. Use {', '.join(sorted(METHODS))}, smooth:S" +
                    (", " + ", ".join(allow) if allow else ""))


def method_text(spec):
    if spec.name == "smooth":
        return "smooth" if spec.s is None else f"smooth:{spec.s!r}"
    return spec.name


class _Params:
    """Adapts node parameters to the settings interface numerics expects."""

    def __init__(self, **kw):
        self.kw = kw

    def get(self, key):
        return self.kw.get(key, {"nan_rows": "drop", "duplicates": "error"}.get(key))


# ------------------------------------------------------------------ base


class Node:
    op = "?"
    lib = False           # standard-library op
    leaf = False
    eval_children = True  # stdlib nodes that expand evaluate their expansion instead

    def __init__(self, kids, params, name=None):
        self.kids = kids
        self.params = params
        self.name = name
        self.type = self.infer()
        self.id = self.make_id()

    # --- structure

    def children(self):
        for slot, v in self.kids.items():
            if isinstance(v, Node):
                yield (slot,), v
            elif isinstance(v, dict):
                for label, n in v.items():
                    yield (slot, label), n
            else:
                for i, n in enumerate(v):
                    yield (slot, i), n

    def kid(self, slot="of"):
        return self.kids[slot]

    def in_ids(self):
        out = {}
        for slot, v in self.kids.items():
            if isinstance(v, Node):
                out[slot] = v.id
            elif isinstance(v, dict):
                out[slot] = {k: n.id for k, n in v.items()}
            else:
                out[slot] = [n.id for n in v]
        return out

    def make_id(self):
        canon = {"op": self.op, "params": self.params, "in": self.in_ids()}
        if self.lib:
            canon["lib"] = LIB
        return hashlib.sha256(dumps(canon).encode()).hexdigest()[:12]

    def label(self):
        return self.name or self.op

    def leaves(self):
        seen = {}

        def go(n):
            if n.leaf:
                seen.setdefault(n.id, n)
            for _, c in n.all_children():
                go(c)

        go(self)
        return list(seen.values())

    def all_children(self):
        yield from self.children()

    def walk(self):
        seen = set()
        stack = [self]
        while stack:
            n = stack.pop()
            if n.id in seen:
                continue
            seen.add(n.id)
            yield n
            stack.extend(c for _, c in n.all_children())

    # --- pushdown

    def blocked(self):
        return set()

    def map_in(self, path, u):
        child = dict(self.children())[path] if not isinstance(path, Node) else path
        return u if child.type.has_input(u) else None

    @cached_property
    def part(self):
        if self.leaf:
            return frozenset(self.type.input_names)
        out = set()
        kids = list(self.children())
        for u in self.type.input_names:
            if u in self.blocked():
                continue
            found = False
            ok = True
            for path, c in kids:
                m = self.map_in(path, u)
                if m is None:
                    continue
                found = True
                if m not in c.part:
                    ok = False
            if found and ok:
                out.add(u)
        return frozenset(out)

    def child_sels(self, sels):
        out = {}
        for path, c in self.children():
            lst = []
            for p in sels:
                m = self.map_in(path, p.name)
                if m is not None and m in c.part:
                    lst.append(p if m == p.name else Pred(m, p.op, p.value, p.lo, p.hi))
            out[path] = lst
        return out

    def pid(self):
        """Point-set provenance: nodes with the same pid have exactly the same points."""
        return self.id

    # --- evaluation

    def compute(self, ctx, ins, sels):
        raise NotImplementedError

    def describe(self):
        skip = {"def", "digits"}
        parts = [f"{k}={_short(v)}" for k, v in self.params.items() if k not in skip]
        return f"{self.op}({', '.join(parts)})"

    # --- serialization

    def to_toml(self):
        d = {"op": self.op}
        for slot, v in self.in_ids().items():
            d[slot] = Inline(v) if isinstance(v, dict) else v
        for k, v in self.params.items():
            d[k] = Inline(v) if isinstance(v, dict) else (list(v) if isinstance(v, tuple) else v)
        d.update(self.toml_labels())
        if self.name:
            d["name"] = self.name
        return d

    def toml_labels(self):
        return {}

    PARAMS = ()
    SLOTS = {"of": "one"}

    @classmethod
    def from_toml(cls, d, get, resolver):
        kids = {}
        for slot, kind in cls.SLOTS.items():
            ref = d.get(slot)
            if ref is None:
                raise GlueError(f"node {cls.op}: missing {slot!r}")
            if kind == "one":
                kids[slot] = get(ref)
            elif kind == "map":
                kids[slot] = {k: get(v) for k, v in ref.items()}
            else:
                kids[slot] = [get(v) for v in ref]
        params = {}
        for p in cls.PARAMS:
            if p not in d:
                raise GlueError(f"node {cls.op}: missing parameter {p!r}")
            v = d[p]
            params[p] = dict(v) if isinstance(v, dict) else v
        return cls(kids, params, d.get("name"))


def _short(v):
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_short(x)}" for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_short(x) for x in v) + "]"
    if isinstance(v, str) and (" " in v or "," in v):
        return repr(v)
    return str(v)


def _curves(frame, key):
    if not key:
        yield (), frame
        return
    for k, g in frame.groupby(key, sort=False, dropna=False):
        yield (k if isinstance(k, tuple) else (k,)), g


def _order(frame, ftype):
    cols = ftype.input_names + ftype.output_names
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise GlueError(f"internal: column {missing[0]} missing from result")
    return frame[cols].reset_index(drop=True)


def _along_check(child, along, what):
    v = child.type.need_input(along, what)
    if v.dtype == "str":
        raise GlueError(f"{what}: {along} is a string input and can't be used as an axis")
    return v


# ------------------------------------------------------------------ leaves


@register
class SourceNode(Node):
    op = "source"
    leaf = True
    SLOTS = {}
    PARAMS = ("def", "duplicates", "digits")

    def __init__(self, kids, params, name=None, table=None):
        self.table = table
        super().__init__(kids, params, name)

    @classmethod
    def build(cls, table, duplicates, name=None):
        digits = {v: table.digits(v) for v in table.inputs if table.ctype(v) == "float"}
        return cls({}, {"def": table.def_id, "duplicates": duplicates, "digits": digits}, name or table.name,
                   table=table)

    def infer(self):
        return self.table.ftype()

    def compute(self, ctx, ins, sels):
        return self.table.read_points(sels, self.params["duplicates"], ctx)

    def toml_labels(self):
        return {"table": self.table.name}

    def describe(self):
        return f"source({self.table.name}, duplicates={self.params['duplicates']})"

    @classmethod
    def from_toml(cls, d, get, resolver):
        table = resolver("table", d.get("table"))
        node = cls.build(table, d.get("duplicates", "error"), d.get("name"))
        return node


@register
class DatasetNode(Node):
    op = "dataset"
    leaf = True
    SLOTS = {}
    PARAMS = ("root",)

    def __init__(self, kids, params, name=None, table=None):
        self.table = table
        super().__init__(kids, params, name)

    @classmethod
    def build(cls, table, name=None):
        return cls({}, {"root": table.info.root}, name or table.name, table=table)

    def make_id(self):
        return self.params["root"]

    def infer(self):
        return self.table.info.type

    def compute(self, ctx, ins, sels):
        return self.table.frame()

    def toml_labels(self):
        return {"dataset": self.table.name}

    def describe(self):
        return f"dataset({self.table.name})"

    @classmethod
    def from_toml(cls, d, get, resolver):
        return cls.build(resolver("dataset", d.get("dataset")), d.get("name"))


@register
class ConstNode(Node):
    op = "const"
    leaf = True
    SLOTS = {}
    PARAMS = ("value", "unit")

    def infer(self):
        return FType((), (Out("value", self.params["unit"]),))

    def compute(self, ctx, ins, sels):
        return pd.DataFrame({"value": [float(self.params["value"])]})

    def describe(self):
        return f"const({self.params['value']:g})"


def parse_axis_values(spec):
    kind, _, rest = spec.partition(":")
    try:
        if kind == "list":
            vals = [float(v) for v in rest.strip("[]").split(",") if v.strip()]
            return np.asarray(vals, dtype=float)
        a, b, n = rest.split(":")
        a, b, n = float(a), float(b), int(n)
        if kind == "lin":
            return np.linspace(a, b, n)
        if kind == "log":
            if a <= 0 or b <= 0:
                raise GlueError("a log axis needs positive ends")
            return np.geomspace(a, b, n)
    except ValueError:
        pass
    raise GlueError(f"bad axis values {spec!r}. Use list:[...], lin:a:b:n, or log:a:b:n")


@register
class AxisNode(Node):
    op = "axis"
    leaf = True
    SLOTS = {}
    PARAMS = ("input", "values")

    def infer(self):
        parse_axis_values(self.params["values"])
        return FType((Var(self.params["input"], "float", True),), (), self.params["input"])

    def compute(self, ctx, ins, sels):
        vals = np.unique(rnd(parse_axis_values(self.params["values"])))
        return pd.DataFrame({self.params["input"]: vals})

    def describe(self):
        return f"axis({self.params['input']}, {self.params['values']})"


# ------------------------------------------------------------------ structure


@register
class SelectNode(Node):
    op = "select"
    PARAMS = ("outputs",)

    def infer(self):
        t = self.kid().type
        outs = [t.need_output(o, "select") for o in self.params["outputs"]]
        return t.with_outputs(outs)

    def pid(self):
        return self.kid().pid()

    def compute(self, ctx, ins, sels):
        return _order(ins[("of",)], self.type)


@register
class RenameNode(Node):
    op = "rename"
    PARAMS = ("mapping",)

    def infer(self):
        t = self.kid().type
        m = self.params["mapping"]
        for old in m:
            if not (t.has_input(old) or t.out(old)):
                raise GlueError(f"rename: no input or output {old!r} (has {', '.join(t.input_names + t.output_names)})")
        ins = tuple(Var(m.get(v.name, v.name), v.dtype, v.exact, v.unit) for v in t.inputs)
        outs = tuple(Out(m.get(o.name, o.name), o.unit, o.label) for o in t.outputs)
        return FType(ins, outs, m.get(t.axis, t.axis) if t.axis else None)

    def map_in(self, path, u):
        inv = {v: k for k, v in self.params["mapping"].items()}
        old = inv.get(u, u)
        return old if self.kid().type.has_input(old) else None

    def pid(self):
        renamed_inputs = any(self.kid().type.has_input(k) for k in self.params["mapping"])
        return self.id if renamed_inputs else self.kid().pid()

    def compute(self, ctx, ins, sels):
        return _order(ins[("of",)].rename(columns=self.params["mapping"]), self.type)


@register
class TransformNode(Node):
    op = "transform"
    PARAMS = ("input", "to", "formula")

    def infer(self):
        t = self.kid().type
        src = t.need_input(self.params["input"], "transform")
        ast = F.parse(self.params["formula"])
        self.params["formula"] = ast.src()
        deps = F.names(ast)
        cols = set(t.input_names) | set(t.output_names)
        bad = sorted(deps - cols)
        if bad:
            raise GlueError(f"transform: formula {self.params['formula']!r} uses {bad[0]!r}, which isn't an input "
                            f"or output (has {', '.join(sorted(cols))})")
        exact = all(t.var(d) is not None and t.var(d).exact for d in deps)
        to = self.params["to"]
        if to != src.name and (t.has_input(to) or t.out(to)):
            raise GlueError(f"transform: {to!r} is already used in this field")
        return t.replace_var(src.name, Var(to, "float", exact, ""))

    def blocked(self):
        return {self.params["to"]}

    def compute(self, ctx, ins, sels):
        f = ins[("of",)].copy()
        new = rnd(F.evaluate(F.parse(self.params["formula"]), f))
        src, to = self.params["input"], self.params["to"]
        f = f.drop(columns=[src])
        f[to] = new
        f = _order(f, self.type)
        keys = self.type.input_names
        dup = f.duplicated(keys, keep=False)
        if dup.any():
            row = f[dup].iloc[0]
            where = ", ".join(f"{k}={row[k]}" for k in keys)
            raise GlueError(f"transform {to} = {self.params['formula']}: {int(dup.sum())} points collide, "
                            f"for example at {where}. The inputs would no longer determine the outputs.")
        return f


@register
class FilterNode(Node):
    op = "filter"
    PARAMS = ("predicate",)

    def infer(self):
        t = self.kid().type
        preds = F.parse_predicate(self.params["predicate"])
        self.params["predicate"] = F.predicate_text(preds)
        cols = set(t.input_names) | set(t.output_names)
        for p in preds:
            for n in (p.name, p.ref):
                if n is not None and n not in cols:
                    raise GlueError(f"filter {p.src()}: no input or output {n!r} "
                                    f"(has {', '.join(t.input_names + t.output_names)})")
        return t

    @cached_property
    def preds(self):
        return F.parse_predicate(self.params["predicate"])

    def child_sels(self, sels):
        c = self.kid()
        extra = [p for p in self.preds if p.literal and p.name in c.part]
        return {("of",): list(sels) + extra}

    def compute(self, ctx, ins, sels):
        return F.apply_preds(self.preds, ins[("of",)])

    def describe(self):
        return f"filter({self.params['predicate']})"


@register
class SliceNode(Node):
    op = "slice"
    PARAMS = ("input", "value")

    def infer(self):
        t = self.kid().type
        t.need_input(self.params["input"], "slice")
        return t.without_input(self.params["input"])

    def child_sels(self, sels):
        c = self.kid()
        extra = []
        if self.params["input"] in c.part:
            extra = [Pred(self.params["input"], "=", self.params["value"])]
        return {("of",): [p for p in sels if c.type.has_input(p.name)] + extra}

    def compute(self, ctx, ins, sels):
        f = F.apply_preds([Pred(self.params["input"], "=", self.params["value"])], ins[("of",)])
        return _order(f, self.type)


def _key_text(frame, key, row):
    return fmt_key(key, [row[k] for k in key])


@register
class JoinNode(Node):
    op = "join"
    SLOTS = {"of": "map"}
    PARAMS = ("unmatched",)

    def infer(self):
        ops = self.kids["of"]
        if not ops:
            raise GlueError("join needs at least one operand")
        ins, outs, axis = [], [], None
        for label, n in ops.items():
            for v in n.type.inputs:
                prev = next((i for i, w in enumerate(ins) if w.name == v.name), None)
                if prev is None:
                    ins.append(v)
                else:
                    w = ins[prev]
                    if (w.dtype == "str") != (v.dtype == "str"):
                        raise GlueError(f"join: input {v.name} is a string in one operand and a number in another")
                    ins[prev] = Var(w.name, w.dtype, w.exact and v.exact, w.unit or v.unit)
            for o in n.type.outputs:
                outs.append(Out(f"{label}.{o.name}", o.unit, o.label))
            axis = axis or n.type.axis
        return FType(tuple(ins), tuple(outs), axis)

    def compute(self, ctx, ins, sels):
        ops = self.kids["of"]
        frames = {}
        for label, n in ops.items():
            f = ins[("of", label)]
            frames[label] = f.rename(columns={o: f"{label}.{o}" for o in n.type.output_names})
        labels = list(ops)
        result = frames[labels[0]]
        cols = set(ops[labels[0]].type.input_names)
        for label in labels[1:]:
            n = ops[label]
            on = [c for c in n.type.input_names if c in cols]
            result = result.merge(frames[label], on=on, how="inner") if on else result.merge(frames[label], how="cross")
            cols |= set(n.type.input_names)
        if len(labels) > 1:
            self._unmatched(ctx, frames, result)
        return _order(result, self.type)

    def _unmatched(self, ctx, frames, result):
        ops = self.kids["of"]
        for label, n in ops.items():
            inputs = n.type.input_names
            f = frames[label]
            if not inputs or len(f) == 0:
                continue
            matched = result[inputs].drop_duplicates()
            m = f[inputs].merge(matched, on=inputs, how="left", indicator=True)
            lost = m[m["_merge"] == "left_only"]
            if len(lost) == 0:
                continue
            key = [k for k in inputs if k != n.type.axis]
            keys = lost[key].drop_duplicates() if key else lost.iloc[:1]
            text = n.label()
            if self.params["unmatched"] == "error":
                row = keys.iloc[0]
                raise GlueError(f"{len(keys)} curve keys have no match, for example "
                                f"{_key_text(keys, key, row)} in {text}. Set unmatched drop to leave them out.")
            msg = f"dropped {{n}} unmatched keys (only in {text})"
            for _, row in keys.iterrows():
                ctx.report.count(msg, 1, _key_text(keys, key, row))


@register
class StackNode(Node):
    op = "stack"
    SLOTS = {"of": "map"}
    PARAMS = ("tag",)

    def infer(self):
        ops = self.kids["of"]
        first = next(iter(ops.values()))
        tag = self.params["tag"]
        for label, n in ops.items():
            if set(n.type.input_names) != set(first.type.input_names) or n.type.output_names != first.type.output_names:
                raise GlueError(f"stack needs members with the same inputs and outputs. {label} has "
                                f"{n.type.text()}, but the first has {first.type.text()}")
            if n.type.has_input(tag) or n.type.out(tag):
                raise GlueError(f"stack tag {tag!r} is already used in {label}")
        ins = []
        for v in first.type.inputs:
            ex = all(n.type.var(v.name).exact for n in ops.values())
            ins.append(Var(v.name, v.dtype, ex, v.unit))
        return FType(tuple(ins) + (Var(tag, "str", True),), first.type.outputs, first.type.axis)

    @cached_property
    def part(self):
        base = set(Node.part.func(self))
        return frozenset(base | {self.params["tag"]})

    def child_sels(self, sels):
        tag = self.params["tag"]
        tag_sels = [p for p in sels if p.name == tag]
        out = {}
        for path, c in self.children():
            label = path[1]
            if tag_sels:
                probe = pd.DataFrame({tag: [label]})
                if not all(F.pred_mask(p, probe)[0] for p in tag_sels):
                    out[path] = None
                    continue
            out[path] = [p for p in sels if p.name != tag and c.type.has_input(p.name) and p.name in c.part]
        return out

    def compute(self, ctx, ins, sels):
        frames = []
        for label, n in self.kids["of"].items():
            f = ins.get(("of", label))
            if f is None:
                continue
            f = f.copy()
            f[self.params["tag"]] = label
            frames.append(f[self.type.input_names + self.type.output_names])
        if not frames:
            return pd.DataFrame(columns=self.type.input_names + self.type.output_names)
        return pd.concat(frames, ignore_index=True)


@register
class PointsNode(Node):
    op = "points"

    def infer(self):
        return self.kid().type.with_outputs(())

    def pid(self):
        return self.kid().pid()

    def compute(self, ctx, ins, sels):
        return ins[("of",)][self.type.input_names].drop_duplicates().reset_index(drop=True)


@register
class UnionNode(Node):
    op = "union"
    SLOTS = {"of": "list"}
    PARAMS = ()

    def infer(self):
        ops = self.kids["of"]
        names = set(ops[0].type.input_names)
        for n in ops[1:]:
            if set(n.type.input_names) != names:
                raise GlueError("union needs point sets with the same inputs")
        ins = tuple(Var(v.name, v.dtype, all(n.type.var(v.name).exact for n in ops), v.unit)
                    for v in ops[0].type.inputs)
        return FType(ins, (), ops[0].type.axis)

    def compute(self, ctx, ins, sels):
        cols = self.type.input_names
        frames = [ins[("of", i)][cols] for i in range(len(self.kids["of"]))]
        return pd.concat(frames, ignore_index=True).drop_duplicates().reset_index(drop=True)


@register
class SpanNode(Node):
    op = "span"
    PARAMS = ("input", "lo", "hi", "count", "step", "scale")

    def infer(self):
        t = self.kid().type
        for k in ("lo", "hi"):
            t.need_output(self.params[k], "span")
        x = self.params["input"]
        if t.has_input(x):
            raise GlueError(f"span: {x} is already an input")
        if not self.params["count"] and not self.params["step"]:
            raise GlueError("span needs count or step")
        return FType(t.inputs + (Var(x, "float", False),), (), x)

    def blocked(self):
        return {self.params["input"]}

    def compute(self, ctx, ins, sels):
        p = self.params
        return span_points(ins[("of",)], self.kid().type.inputs, p["input"], p["lo"], p["hi"], p["count"],
                           p["step"], p["scale"], ctx)


def span_points(L, key_vars, x, lo, hi, count, step, scale, ctx):
    rows = []
    bad = 0
    for _, r in L.iterrows():
        a, b = r[lo], r[hi]
        if not (np.isfinite(a) and np.isfinite(b)) or a >= b:
            bad += 1
            continue
        if count:
            n = int(count)
            pts = np.geomspace(a, b, n) if scale == "log" else np.linspace(a, b, n)
        else:
            s = float(step)
            pts = np.arange(a, b + s * 1e-9, s)
            pts = pts[pts <= b + 1e-12]
        d = {v.name: np.full(len(pts), r[v.name], dtype=object if v.dtype == "str" else None) for v in key_vars}
        d[x] = rnd(pts)
        rows.append(pd.DataFrame(d))
    if bad:
        ctx.report.count("dropped {n} keys with no overlapping range", bad)
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype=float) for c in [v.name for v in key_vars] + [x]})
    return pd.concat(rows, ignore_index=True)


# ------------------------------------------------------------------ pointwise


@register
class MapNode(Node):
    op = "map"
    PARAMS = ("outputs", "keep", "units")

    def infer(self):
        t = self.kid().type
        canon = {}
        cols = set(t.input_names) | set(t.output_names)
        for name, text in self.params["outputs"].items():
            ast = F.parse(text)
            bad = sorted(F.names(ast) - cols)
            if bad:
                raise GlueError(f"map {name} = {text}: {bad[0]!r} isn't an input or output "
                                f"(has {', '.join(sorted(cols))})")
            if t.has_input(name):
                raise GlueError(f"map: {name!r} is an input and can't also be an output")
            canon[name] = ast.src()
        self.params["outputs"] = canon
        units = self.params["units"]
        outs = [o for o in t.outputs if o.name not in canon] if self.params["keep"] else []
        outs += [Out(n, units.get(n, "")) for n in canon]
        return t.with_outputs(outs)

    def pid(self):
        return self.kid().pid()

    def compute(self, ctx, ins, sels):
        f = ins[("of",)]
        out = f[self.kid().type.input_names].copy()
        if self.params["keep"]:
            for o in self.kid().type.output_names:
                out[o] = f[o]
        for name, text in self.params["outputs"].items():
            out[name] = F.evaluate(F.parse(text), f)
        return _order(out, self.type)

    def describe(self):
        return "map(" + ", ".join(f"{k} = {v}" for k, v in self.params["outputs"].items()) + ")"


# ------------------------------------------------------------------ along an input


class AlongNode(Node):
    """Base for operations that act on each curve along one input."""

    def blocked(self):
        return {self.params["along"]}

    def key(self):
        return [n for n in self.kid().type.input_names if n != self.params["along"]]

    def curves(self, frame):
        return _curves(frame, self.key())


def _xy(g, along, out):
    x = g[along].to_numpy(dtype=float)
    y = g[out].to_numpy(dtype=float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    order = np.argsort(x, kind="stable")
    return x[order], y[order]


@register
class ResampleNode(AlongNode):
    op = "resample"
    SLOTS = {"of": "one", "grid": "one"}
    PARAMS = ("along", "method", "extrapolate", "fewpoints")

    def infer(self):
        t = self.kid().type
        g = self.kids["grid"].type
        x = self.params["along"]
        _along_check(self.kid(), x, "resample")
        gx = g.need_input(x, "resample grid")
        extra = [n for n in g.input_names if n != x and not t.has_input(n)]
        if extra:
            raise GlueError(f"resample: the grid has input {extra[0]}, which the field doesn't have")
        parse_method(self.params["method"])
        v = t.var(x)
        return t.replace_var(x, Var(x, v.dtype, gx.exact, v.unit))

    def map_in(self, path, u):
        n = self.kids[path[0]]
        return u if n.type.has_input(u) else None

    def compute(self, ctx, ins, sels):
        Fr, G = ins[("of",)], ins[("grid",)]
        x = self.params["along"]
        key = self.key()
        gkeys = [n for n in self.kids["grid"].type.input_names if n != x]
        method = parse_method(self.params["method"])
        ext = self.params["extrapolate"]
        opts = _Params(fewpoints=self.params["fewpoints"])
        outs = self.kid().type.output_names
        G = G[gkeys + [x]].drop_duplicates()
        groups = dict(_curves(Fr, key))
        frames, nogrid, dropped = [], 0, {}
        for k, g in groups.items():
            kd = dict(zip(key, k))
            if gkeys:
                m = np.ones(len(G), dtype=bool)
                for c in gkeys:
                    m &= (G[c] == kd[c]).to_numpy()
                pts = G[x].to_numpy(dtype=float)[m]
            else:
                pts = G[x].to_numpy(dtype=float)
            pts = np.unique(pts)
            if len(pts) == 0:
                nogrid += 1
                continue
            desc = fmt_key(key, k)
            row = {c: np.full(len(pts), kd[c], dtype=object if isinstance(kd[c], str) else None) for c in key}
            row[x] = pts
            any_ok = False
            for o in outs:
                xs, ys = _xy(g, x, o)
                try:
                    f = nm.fit(xs, ys, method, opts, ctx.report, desc)
                    row[o] = f(pts, ext, desc)
                    any_ok = True
                except DropCurve as e:
                    row[o] = np.full(len(pts), np.nan)
                    dropped.setdefault(e.reason, []).append(desc)
            if any_ok:
                frames.append(pd.DataFrame(row))
        for reason, keys in dropped.items():
            for d in keys:
                ctx.report.count(f"dropped {{n}} curves ({reason})", 1, d)
        quiet = getattr(self, "in_align", False)
        if nogrid and not quiet:
            ctx.report.count(f"dropped {{n}} curves with no grid points (resample of {self.kid().label()})", nogrid)
        if frames and not quiet:
            ctx.report.count(f"resampled {self.kid().label()}: {{n}} curves, grid {self.kids['grid'].label()}, "
                             f"method {self.params['method']}", len(frames))
        if not frames:
            return pd.DataFrame({c: pd.Series(dtype=float) for c in self.type.input_names + outs})
        return _order(pd.concat(frames, ignore_index=True), self.type)

    def describe(self):
        return (f"resample(along {self.params['along']}, grid {self.kids['grid'].label()}, "
                f"method {self.params['method']})")


@register
class DerivNode(AlongNode):
    op = "deriv"
    PARAMS = ("along", "order", "method", "fewpoints")

    def infer(self):
        t = self.kid().type
        _along_check(self.kid(), self.params["along"], "deriv")
        if int(self.params["order"]) not in (1, 2, 3):
            raise GlueError("deriv: order must be 1, 2, or 3")
        parse_method(self.params["method"], allow=("fd",))
        return t.with_outputs([Out(o.name) for o in t.outputs])

    def pid(self):
        return self.kid().pid()

    def compute(self, ctx, ins, sels):
        Fr = ins[("of",)]
        x = self.params["along"]
        order = int(self.params["order"])
        method = parse_method(self.params["method"], allow=("fd",))
        opts = _Params(fewpoints=self.params["fewpoints"])
        out = Fr.copy()
        for k, g in self.curves(Fr):
            desc = fmt_key(self.key(), k)
            for o in self.kid().type.output_names:
                xs, ys = _xy(g, x, o)
                res = np.full(len(g), np.nan)
                gx = g[x].to_numpy(dtype=float)
                if len(xs) >= 2:
                    if method.name == "fd":
                        dy = ys
                        for _ in range(order):
                            dy = np.gradient(dy, xs)
                        vals = dy
                    else:
                        try:
                            vals = nm.fit(xs, ys, method, opts, ctx.report, desc).derivative(order)(xs, "extend")
                        except DropCurve as e:
                            ctx.report.count(f"dropped {{n}} curves ({e.reason})", 1, desc)
                            vals = np.full(len(xs), np.nan)
                    lookup = dict(zip(xs, vals))
                    res = np.array([lookup.get(v, np.nan) for v in gx])
                out.loc[g.index, o] = res
        return _order(out, self.type)


@register
class CumintNode(AlongNode):
    op = "cumint"
    PARAMS = ("along", "method")

    def infer(self):
        t = self.kid().type
        _along_check(self.kid(), self.params["along"], "cumint")
        if self.params["method"] != "trapz":
            parse_method(self.params["method"])
        return t.with_outputs([Out(o.name) for o in t.outputs])

    def pid(self):
        return self.kid().pid()

    def compute(self, ctx, ins, sels):
        Fr = ins[("of",)]
        x = self.params["along"]
        out = Fr.copy()
        for k, g in self.curves(Fr):
            desc = fmt_key(self.key(), k)
            gx = g[x].to_numpy(dtype=float)
            for o in self.kid().type.output_names:
                xs, ys = _xy(g, x, o)
                if len(xs) < 2:
                    vals = np.zeros(len(xs))
                elif self.params["method"] == "trapz":
                    vals = nm.cumtrapz(ys, xs)
                else:
                    Fi = nm.fit(xs, ys, parse_method(self.params["method"]), _Params(fewpoints="linear"),
                                ctx.report, desc).antiderivative()
                    vals = Fi(xs, "extend") - Fi(np.array([xs[0]]), "extend")[0]
                lookup = dict(zip(xs, vals))
                out.loc[g.index, o] = np.array([lookup.get(v, np.nan) for v in gx])
        return _order(out, self.type)


REDUCE_OPS = ("max", "min", "mean", "sum", "count", "first", "last", "argmax", "argmin", "integral")


@register
class ReduceNode(AlongNode):
    op = "reduce"
    PARAMS = ("along", "reduce", "method")

    def infer(self):
        t = self.kid().type
        _along_check(self.kid(), self.params["along"], "reduce")
        op = self.params["reduce"]
        if op not in REDUCE_OPS:
            raise GlueError(f"reduce: unknown op {op!r}. Use {', '.join(REDUCE_OPS)}")
        keep_unit = op in ("max", "min", "mean", "sum", "first", "last")
        outs = [Out(o.name, o.unit if keep_unit else "", o.label) for o in t.outputs]
        return t.without_input(self.params["along"]).with_outputs(outs)

    def compute(self, ctx, ins, sels):
        Fr = ins[("of",)]
        x = self.params["along"]
        op = self.params["reduce"]
        key = self.key()
        rows = []
        for k, g in self.curves(Fr):
            desc = fmt_key(key, k)
            row = dict(zip(key, k))
            for o in self.kid().type.output_names:
                xs, ys = _xy(g, x, o)
                row[o] = self._one(op, xs, ys, ctx, desc)
            rows.append(row)
        if not rows:
            return pd.DataFrame({c: pd.Series(dtype=float) for c in self.type.input_names + self.type.output_names})
        return _order(pd.DataFrame(rows), self.type)

    def _one(self, op, xs, ys, ctx, desc):
        if op == "count":
            return float(len(ys))
        if len(ys) == 0:
            return np.nan
        if op == "max":
            return ys.max()
        if op == "min":
            return ys.min()
        if op == "mean":
            return ys.mean()
        if op == "sum":
            return ys.sum()
        if op == "first":
            return ys[0]
        if op == "last":
            return ys[-1]
        if op == "argmax":
            return xs[np.argmax(ys)]
        if op == "argmin":
            return xs[np.argmin(ys)]
        if self.params["method"] in ("trapz", "-"):
            return nm.trapezoid(ys, xs)
        Fi = nm.fit(xs, ys, parse_method(self.params["method"]), _Params(fewpoints="linear"),
                    ctx.report, desc).antiderivative()
        return Fi(np.array([xs[-1]]), "extend")[0] - Fi(np.array([xs[0]]), "extend")[0]


@register
class SwapNode(AlongNode):
    op = "swap"
    PARAMS = ("along", "output", "branches", "flat_tol")

    def infer(self):
        t = self.kid().type
        xv = _along_check(self.kid(), self.params["along"], "swap")
        y = t.need_output(self.params["output"], "swap")
        if self.params["branches"] not in ("error", "split"):
            raise GlueError("swap: branches must be error or split")
        ins = [v for v in t.inputs if v.name != xv.name] + [Var(y.name, "float", False, y.unit)]
        if self.params["branches"] == "split":
            ins.append(Var("branch", "int", True))
        outs = [Out(xv.name, xv.unit)] + [o for o in t.outputs if o.name != y.name]
        return FType(tuple(ins), tuple(outs), y.name)

    def blocked(self):
        return {self.params["along"], self.params["output"], "branch"}

    def compute(self, ctx, ins, sels):
        Fr = ins[("of",)]
        x, y = self.params["along"], self.params["output"]
        tol = float(self.params["flat_tol"])
        split = self.params["branches"] == "split"
        others = [o for o in self.kid().type.output_names if o != y]
        key = self.key()
        frames, flat_total = [], 0
        for k, g in self.curves(Fr):
            g = g[~g[y].isna()].sort_values(x, kind="stable")
            if len(g) == 0:
                continue
            ys = g[y].to_numpy(dtype=float)
            keep = np.ones(len(g), dtype=bool)
            if len(ys) > 1:
                rng = ys.max() - ys.min()
                flat = np.abs(np.diff(ys)) <= tol * rng
                for i in range(len(ys)):
                    adj = ([flat[i - 1]] if i > 0 else []) + ([flat[i]] if i < len(flat) else [])
                    if adj and all(adj):
                        keep[i] = False
            flat_total += int((~keep).sum())
            g = g[keep]
            ys = g[y].to_numpy(dtype=float)
            branch = np.zeros(len(g), dtype=int)
            if len(ys) > 1:
                s = np.sign(np.diff(ys))
                if not (np.all(s > 0) or np.all(s < 0)):
                    if not split:
                        desc = fmt_key(key, k)
                        raise GlueError(f"swap: {y} isn't monotonic along {x} on curve {desc}, so {y} doesn't "
                                        f"determine {x} there. Use branches=split, or restrict the {x} range.")
                    b = 0
                    for i in range(1, len(s)):
                        if s[i] != s[i - 1]:
                            b += 1
                        branch[i + 1] = b
            out = pd.DataFrame({c: g[c].to_numpy() for c in key})
            out[y] = rnd(ys)
            if split:
                out["branch"] = branch
            out[x] = g[x].to_numpy(dtype=float)
            for o in others:
                out[o] = g[o].to_numpy(dtype=float)
            frames.append(out)
        if flat_total:
            ctx.report.count(f"swap {x}↔{y}: dropped {{n}} points in flat regions", flat_total)
        if not frames:
            return pd.DataFrame({c: pd.Series(dtype=float) for c in self.type.input_names + self.type.output_names})
        return _order(pd.concat(frames, ignore_index=True), self.type)


# ------------------------------------------------------------------ extension


def find_plugin(ref):
    from ..plugins import REGISTRY as PLUGINS

    target, _, version = ref.partition("@")
    if ":" in target:
        module, _, qual = target.partition(":")
    else:
        module, _, qual = target.rpartition(".")

    def lookup():
        for opdef in PLUGINS.values():
            if opdef.module == module and opdef.qualname == qual:
                return opdef
        return None

    found = lookup()
    if found is None:
        try:
            importlib.import_module(module)
        except ImportError as e:
            raise GlueError(f"plugin module {module!r} could not be imported: {e}") from None
        found = lookup()
    if found is None:
        raise GlueError(f"plugin {ref} is not registered")
    return found


@register
class ApplyNode(Node):
    op = "apply"
    PARAMS = ("fn", "kind", "along", "params")

    def infer(self):
        t = self.kid().type
        kind = self.params["kind"]
        if kind not in ("pointwise", "curve", "reduce"):
            raise GlueError("apply: kind must be pointwise, curve, or reduce")
        if kind == "pointwise":
            return t
        _along_check(self.kid(), self.params["along"], "apply")
        if kind == "curve":
            if len(t.outputs) != 1:
                raise GlueError("apply: a curve plugin needs a field with one output")
            v = t.var(self.params["along"])
            return t.replace_var(v.name, Var(v.name, v.dtype, False, v.unit))
        return t.without_input(self.params["along"]).with_outputs([Out(o.name) for o in t.outputs])

    def blocked(self):
        return set() if self.params["kind"] == "pointwise" else {self.params["along"]}

    def pid(self):
        return self.kid().pid() if self.params["kind"] == "pointwise" else self.id

    def compute(self, ctx, ins, sels):
        opdef = find_plugin(self.params["fn"])
        Fr = ins[("of",)]
        kw = dict(self.params["params"])
        kind = self.params["kind"]
        if kind == "pointwise":
            out = Fr.copy()
            for o in self.type.output_names:
                out[o] = np.asarray(opdef.fn(Fr[o].to_numpy(dtype=float), **kw), dtype=float)
            return out
        x = self.params["along"]
        key = [n for n in self.kid().type.input_names if n != x]
        rows = []
        for k, g in _curves(Fr, key):
            kd = dict(zip(key, k))
            for o in self.kid().type.output_names:
                xs, ys = _xy(g, x, o)
                if kind == "curve":
                    x2, y2 = opdef.fn(xs, ys, **kw)
                    d = {c: np.full(len(x2), kd[c], dtype=object if isinstance(kd[c], str) else None) for c in key}
                    d[x] = rnd(x2)
                    d[o] = np.asarray(y2, dtype=float)
                    rows.append(pd.DataFrame(d))
                else:
                    kd[o] = float(opdef.fn(xs, ys, **kw))
            if kind == "reduce":
                rows.append(pd.DataFrame([kd]))
        if not rows:
            return pd.DataFrame({c: pd.Series(dtype=float) for c in self.type.input_names + self.type.output_names})
        return _order(pd.concat(rows, ignore_index=True), self.type)

    def describe(self):
        return f"apply({self.params['fn']}, {self.params['kind']})"


# ------------------------------------------------------------------ standard library


class StdNode(Node):
    """Defined by an expansion into elementary nodes, evaluated through it."""

    lib = True
    eval_children = False

    def __init__(self, kids, params, name=None):
        self.kids = kids
        self.params = params
        self.name = name
        self.expanded = self.expand()
        super().__init__(kids, params, name)

    def infer(self):
        return self.expanded.type

    def all_children(self):
        yield ("expansion",), self.expanded

    @cached_property
    def part(self):
        return self.expanded.part

    def child_sels(self, sels):
        return {}

    def compute(self, ctx, ins, sels):
        return ctx.value(self.expanded, sels)


@register
class EvalNode(StdNode):
    op = "eval"
    PARAMS = ("along", "value", "method", "extrapolate", "fewpoints")

    def expand(self):
        p = self.params
        ax = AxisNode({}, {"input": p["along"], "values": f"list:[{float(p['value'])!r}]"})
        r = ResampleNode({"of": self.kids["of"], "grid": ax},
                         {"along": p["along"], "method": p["method"], "extrapolate": p["extrapolate"],
                          "fewpoints": p["fewpoints"]})
        r.in_align = True
        return SliceNode({"of": r}, {"input": p["along"], "value": float(rnd([p["value"]])[0])})


@register
class RangeNode(Node):
    op = "range"
    lib = True
    PARAMS = ("along",)

    def infer(self):
        t = self.kid().type
        _along_check(self.kid(), self.params["along"], "range")
        return t.without_input(self.params["along"]).with_outputs([Out("lo"), Out("hi")])

    def blocked(self):
        return {self.params["along"]}

    def compute(self, ctx, ins, sels):
        return _order(_ranges(ins[("of",)], self.kid().type, self.params["along"], "lo", "hi"), self.type)


def _ranges(frame, ftype, along, lo, hi):
    key = [n for n in ftype.input_names if n != along]
    if key:
        g = frame.groupby(key, sort=False)[along].agg(["min", "max"]).reset_index()
    else:
        g = pd.DataFrame({"min": [frame[along].min()], "max": [frame[along].max()]}) if len(frame) else \
            pd.DataFrame({"min": [], "max": []})
    g = g.rename(columns={"min": lo, "max": hi})
    return g


class _OverlapBase(Node):
    lib = True
    SLOTS = {"of": "map"}

    def _check_ops(self, what):
        x = self.params["along"]
        for label, n in self.kids["of"].items():
            _along_check(n, x, f"{what} ({label})")

    def _key_inputs(self):
        x = self.params["along"]
        ins = []
        for n in self.kids["of"].values():
            for v in n.type.inputs:
                if v.name != x and all(w.name != v.name for w in ins):
                    ins.append(v)
        return ins

    def blocked(self):
        return {self.params["along"]}

    def _shared_range(self, ctx, ins):
        x = self.params["along"]
        R = None
        cols = set()
        for i, (label, n) in enumerate(self.kids["of"].items()):
            r = _ranges(ins[("of", label)], n.type, x, f"lo{i}", f"hi{i}")
            key = [c for c in n.type.input_names if c != x]
            if R is None:
                R, cols = r, set(key)
                continue
            on = [c for c in key if c in cols]
            merged = R.merge(r, on=on, how="inner") if on else R.merge(r, how="cross")
            self._report_lost(ctx, R, merged, sorted(cols), list(self.kids["of"].items())[:i], "left")
            self._report_lost(ctx, r, merged, key, [(label, n)], "right")
            R = merged
            cols |= set(key)
        n_ops = len(self.kids["of"])
        R["lo"] = R[[f"lo{i}" for i in range(n_ops)]].max(axis=1)
        R["hi"] = R[[f"hi{i}" for i in range(n_ops)]].min(axis=1)
        return R

    def _report_lost(self, ctx, before, after, key, operands, side):
        if not key or len(before) == 0:
            return
        kept = after[key].drop_duplicates()
        m = before[key].drop_duplicates().merge(kept, on=key, how="left", indicator=True)
        lost = m[m["_merge"] == "left_only"]
        if len(lost) == 0:
            return
        text = ", ".join(n.label() for _, n in operands)
        if self.params["unmatched"] == "error":
            row = lost.iloc[0]
            raise GlueError(f"{len(lost)} curve keys have no match, for example {_key_text(lost, key, row)} "
                            f"in {text}. Set unmatched drop to leave them out.")
        msg = f"dropped {{n}} unmatched keys (only in {text})"
        for _, row in lost.iterrows():
            ctx.report.count(msg, 1, _key_text(lost, key, row))


@register
class OverlapNode(_OverlapBase):
    op = "overlap"
    PARAMS = ("along", "count", "step", "unmatched")

    def infer(self):
        self._check_ops("overlap")
        x = self.params["along"]
        return FType(tuple(self._key_inputs()) + (Var(x, "float", False),), (), x)

    def compute(self, ctx, ins, sels):
        R = self._shared_range(ctx, ins)
        kv = self._key_inputs()
        L = R[[v.name for v in kv] + ["lo", "hi"]]
        return span_points(L, kv, self.params["along"], "lo", "hi", self.params["count"], self.params["step"],
                           "lin", ctx)


@register
class MergePointsNode(_OverlapBase):
    op = "merge_points"
    PARAMS = ("along", "unmatched")

    def infer(self):
        self._check_ops("merge_points")
        names = None
        for n in self.kids["of"].values():
            s = set(n.type.input_names)
            if names is not None and s != names:
                raise GlueError("merge_points needs operands with the same inputs")
            names = s
        x = self.params["along"]
        return FType(tuple(self._key_inputs()) + (Var(x, "float", False),), (), x)

    def compute(self, ctx, ins, sels):
        x = self.params["along"]
        R = self._shared_range(ctx, ins)
        key = [v.name for v in self._key_inputs()]
        pts = pd.concat([ins[("of", label)][key + [x]] for label in self.kids["of"]], ignore_index=True)
        pts = pts.drop_duplicates()
        m = pts.merge(R[key + ["lo", "hi"]], on=key, how="inner") if key else pts.merge(R[["lo", "hi"]], how="cross")
        m = m[(m[x] >= m["lo"]) & (m[x] <= m["hi"])]
        return m[key + [x]].reset_index(drop=True)


@register
class AlignNode(StdNode):
    op = "align"
    SLOTS = {"of": "map", "grid": "one"}
    PARAMS = ("along", "method", "extrapolate", "fewpoints", "unmatched")

    def expand(self):
        x = self.params["along"]
        ops = {}
        for label, n in self.kids["of"].items():
            if n.type.has_input(x):
                ops[label] = ResampleNode({"of": n, "grid": self.kids["grid"]},
                                          {"along": x, "method": self.params["method"],
                                           "extrapolate": self.params["extrapolate"],
                                           "fewpoints": self.params["fewpoints"]}, n.name)
                ops[label].in_align = True
            else:
                ops[label] = n
        return JoinNode({"of": ops}, {"unmatched": self.params["unmatched"]}, self.name)

    def compute(self, ctx, ins, sels):
        f = ctx.value(self.expanded, sels)
        x = self.params["along"]
        key = [c for c in self.type.input_names if c != x]
        n = len(f[key].drop_duplicates()) if key else (1 if len(f) else 0)
        labels = ", ".join(n_.label() for n_ in self.kids["of"].values() if n_.type.has_input(x))
        ctx.report.count(f"aligned {labels}: {{n}} curves matched on ({', '.join(key)}), "
                         f"grid {self.kids['grid'].label()}, method {self.params['method']}", n)
        return f

    def describe(self):
        return f"align(along {self.params['along']}, grid {self.kids['grid'].label()}, method {self.params['method']})"


@register
class LegendreNode(StdNode):
    op = "legendre"
    PARAMS = ("along", "output", "slope", "result", "method", "branches", "flat_tol")

    def expand(self):
        p = self.params
        Fn = self.kids["of"]
        x, y, s, g = p["along"], p["output"], p["slope"], p["result"]
        S = RenameNode({"of": DerivNode({"of": SelectNode({"of": Fn}, {"outputs": [y]})},
                                        {"along": x, "order": 1, "method": p["method"], "fewpoints": "drop"})},
                       {"mapping": {y: s}})
        H = MapNode({"of": JoinNode({"of": {"f": Fn, "s": S}}, {"unmatched": "error"})},
                    {"outputs": {s: f"s.{s}", g: f"s.{s} * {x} - f.{y}"}, "keep": False, "units": {}})
        return SwapNode({"of": H}, {"along": x, "output": s, "branches": p["branches"],
                                    "flat_tol": float(p["flat_tol"])})


# ------------------------------------------------------------------ helpers for building trees


def select(of, outputs, name=None):
    return SelectNode({"of": of}, {"outputs": list(outputs)}, name)


def rename(of, mapping, name=None):
    return RenameNode({"of": of}, {"mapping": dict(mapping)}, name)


def mapn(of, outputs, keep=False, units=None, name=None):
    return MapNode({"of": of}, {"outputs": dict(outputs), "keep": bool(keep), "units": dict(units or {})}, name)


def filt(of, predicate, name=None):
    return FilterNode({"of": of}, {"predicate": predicate}, name)


def join(operands, unmatched="drop", name=None):
    return JoinNode({"of": dict(operands)}, {"unmatched": unmatched}, name)


def points(of, name=None):
    return PointsNode({"of": of}, {}, name)


def const(value, unit="", name=None):
    return ConstNode({}, {"value": float(value), "unit": unit}, name)


def axis(inp, values, name=None):
    return AxisNode({}, {"input": inp, "values": values}, name)


def resample(of, grid, along, method, extrapolate, fewpoints, name=None):
    return ResampleNode({"of": of, "grid": grid}, {"along": along, "method": method, "extrapolate": extrapolate,
                                                   "fewpoints": fewpoints}, name)
