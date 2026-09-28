"""Translate surface expressions into core trees (docs/core.md, section 5).

Settings are only read here. The trees this produces have every parameter
written in, so evaluating or saving them needs no settings."""

import difflib
from collections import OrderedDict
from dataclasses import dataclass, field

from . import lang
from .core import nodes as N
from .core.formula import pred_from_selector, predicate_text
from .errors import GlueError
from .plugins import REGISTRY as PLUGINS
from .settings import RECIPE_KEYS, convert, to_text

ELEMENTWISE = {"abs", "sqrt", "exp", "log", "log10", "sin", "cos", "tan", "sinh", "cosh", "tanh"}
REDUCERS = {"max", "min", "mean", "sum", "first", "last", "count", "argmax", "argmin"}
BUILTINS = ELEMENTWISE | REDUCERS | {"resample", "d", "int", "integral", "at", "stack", "using", "rename",
                                     "transform", "swap", "legendre"}
LABELS = "abcdefghijklmnopqrstuvwxyz"


@dataclass
class Entity:
    kind: str  # var, view, table, dataset, calc
    name: str
    ast: object = None
    overrides: dict = field(default_factory=dict)
    unit: str = None
    label: str = None
    table: object = None
    description: str = ""
    root: object = None


def _method_text(spec):
    return N.method_text(spec)


class Elaborator:
    def __init__(self, ns, settings):
        self.ns = ns
        self.settings = settings
        self.nan_error = False
        self._cache = {}
        self._stack = []
        self.inputs = OrderedDict()   # label -> Entity for every table and dataset used

    # ------------------------------------------------------------ entry points

    def build(self, ast, name=None, overrides=None, unit=None):
        self.settings.push(overrides or {})
        try:
            node = self.el(ast)
        finally:
            self.settings.pop()
        if name:
            node = self.named(node, name, unit)
        return node

    def named(self, node, name, unit=None):
        outs = node.type.output_names
        if len(outs) == 1:
            if outs[0] != name or unit is not None:
                units = {name: unit if unit is not None else node.type.outputs[0].unit}
                if outs[0] == name:
                    node = N.mapn(node, {name: name}, keep=False, units=units, name=name)
                else:
                    node = N.mapn(node, {name: outs[0]}, keep=False, units=units, name=name) \
                        if unit is not None else N.rename(node, {outs[0]: name}, name=name)
            else:
                node.name = name
        return node

    # ------------------------------------------------------------ settings helpers

    def s(self, key):
        return self.settings.get(key)

    def _settings_key(self):
        return tuple(to_text(k, self.s(k)) for k in RECIPE_KEYS + ["align", "strict", "branches", "flat_tol"])

    # ------------------------------------------------------------ names

    def entity(self, name):
        return self.ns.lookup(name)

    def entity_node(self, ent):
        key = (ent.name, self._settings_key())
        if key in self._cache:
            return self._cache[key]
        if ent.name in self._stack:
            raise GlueError(f"{ent.name} refers to itself: {' -> '.join(self._stack + [ent.name])}")
        if ent.kind == "table":
            self.inputs[ent.name] = ent
            node = N.SourceNode.build(ent.table, self.s("duplicates"), ent.name)
        elif ent.kind == "dataset":
            self.inputs[ent.name] = ent
            node = N.DatasetNode.build(ent.table, ent.name)
        elif ent.kind == "calc":
            node = ent.root
            for label, e in getattr(ent, "leaf_entities", {}).items():
                self.inputs.setdefault(label, e)
            node.name = ent.name
        else:
            self._stack.append(ent.name)
            self.settings.push(ent.overrides or {})
            try:
                node = self.el(ent.ast)
            finally:
                self.settings.pop()
                self._stack.pop()
            node = self.named(node, ent.name, ent.unit)
        self._cache[key] = node
        return node

    def _unknown(self, name):
        names = list(self.ns.names()) + sorted(BUILTINS | set(PLUGINS))
        close = difflib.get_close_matches(name, names, n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        return GlueError(f"unknown name {name!r}.{hint}")

    # ------------------------------------------------------------ dispatch

    def el(self, a):
        # nan_rows=error is a check while evaluating, not part of the tree: it can
        # stop a calculation, but never changes its values
        if self.s("nan_rows") == "error":
            self.nan_error = True
        if isinstance(a, lang.Num):
            return N.const(a.value, name=a.src())
        if isinstance(a, lang.Str):
            raise GlueError(f"the string {a.src()} can only be used in selectors and options")
        if isinstance(a, lang.Name):
            ent = self.entity(a.id)
            if ent is None:
                raise self._unknown(a.id)
            return self.entity_node(ent)
        if isinstance(a, lang.Attr):
            return self._attr(a)
        if isinstance(a, lang.Sel):
            return self._sel(a)
        if isinstance(a, (lang.Bin, lang.Neg)):
            return self._arith(a)
        if isinstance(a, lang.AtE):
            return self._at(self.el(a.obj), a.name, a.value, a.src())
        if isinstance(a, lang.Call):
            if a.func in ELEMENTWISE:
                return self._arith(a)
            if a.func in ("min", "max") and len(a.args) == 2 and not a.kwargs and not self._is_reduction(a):
                return self._arith(a)            # the pointwise maximum of two fields
            return self._call(a)
        if isinstance(a, (lang.ListLit, lang.TupleLit)):
            raise GlueError(f"a list like {a.src()} can only be used as an argument")
        raise GlueError(f"can't evaluate {a.src()}")

    def _attr(self, a):
        base = self.el(a.obj)
        c = a.name
        t = base.type
        text = f"{base.name or a.obj.src()}.{c}"
        if t.out(c) is not None:
            if len(t.outputs) == 1 and not base.leaf:
                return base
            return N.select(base, [c], name=text)
        if t.has_input(c):
            return N.mapn(N.points(base), {"value": c}, units={"value": t.var(c).unit}, name=text)
        cols = t.input_names + t.output_names
        raise GlueError(f"{base.name or a.obj.src()} has no column {c!r}. Columns: {', '.join(cols)}")

    def _sel(self, a):
        base = self.el(a.obj)
        preds = [pred_from_selector(s) for s in a.selectors]
        cols = set(base.type.input_names) | set(base.type.output_names)
        for p in preds:
            if p.name not in cols:
                names = base.type.input_names + base.type.output_names
                raise GlueError(f"{base.name}[{p.src()}]: {base.name} has no input or output {p.name} "
                                f"(it has {', '.join(names)})")
        return N.filt(base, predicate_text(preds), name=a.src())

    # ------------------------------------------------------------ arithmetic

    def _collect(self, a, operands):
        """Turn an arithmetic surface expression into a formula over labelled operand fields."""
        bare, entity_names = set(), set()

        def field_term(node, src):
            if len(node.type.outputs) != 1:
                raise GlueError(f"{src} has several outputs ({', '.join(node.type.output_names)}). "
                                f"Pick one, as in {src}.{node.type.output_names[0] if node.type.outputs else 'E'}")
            if node.id not in operands:
                operands[node.id] = (LABELS[len(operands)] if len(operands) < 26 else f"x{len(operands)}", node)
            label = operands[node.id][0]
            return lang.Attr(lang.Name(label), node.type.output_names[0])

        def term(x):
            if isinstance(x, lang.Num):
                return x
            if isinstance(x, lang.Bin):
                return lang.Bin(x.op, term(x.left), term(x.right))
            if isinstance(x, lang.Neg):
                return lang.Neg(x.op, term(x.operand))
            if isinstance(x, lang.Call) and x.func in ELEMENTWISE:
                if len(x.args) != 1 or x.kwargs:
                    raise GlueError(f"{x.func} takes one argument")
                return lang.Call(x.func, [term(x.args[0])])
            if isinstance(x, lang.Call) and x.func in ("min", "max") and len(x.args) == 2 and not x.kwargs \
                    and not self._is_reduction(x):
                return lang.Call(x.func, [term(x.args[0]), term(x.args[1])])
            if isinstance(x, lang.Name):
                ent = self.entity(x.id)
                if ent is None:
                    bare.add(x.id)
                    return lang.Name(x.id)
                entity_names.add(x.id)
            return field_term(self.el(x), x.src())

        return term(a), bare, entity_names

    def _is_reduction(self, call):
        """max(y, n) with n an input of y reduces along n. Otherwise max(a, b) is the pointwise maximum."""
        arg = call.args[1]
        if not isinstance(arg, lang.Name) or self.entity(arg.id) is not None:
            return False
        try:
            return self.el(call.args[0]).type.has_input(arg.id)
        except GlueError:
            return False

    def _check_names(self, nodes, bare, entity_names):
        all_inputs = set()
        for n in nodes:
            all_inputs |= set(n.type.input_names)
        clash = sorted(entity_names & all_inputs)
        if clash:
            raise GlueError(f"{clash[0]!r} is both a name and an input here. Use table.{clash[0]} for the input, "
                            "or rename the other one.")
        unknown = sorted(bare - all_inputs)
        if unknown and nodes:
            raise self._unknown(unknown[0])

    def _arith(self, a):
        operands = OrderedDict()   # node id -> (label, node)
        formula, bare, entity_names = self._collect(a, operands)
        nodes = [n for _, n in operands.values()]
        if not nodes:
            if bare:
                raise GlueError(f"{sorted(bare)[0]!r} is an input name, but the expression has no field to take it "
                                "from. Combine it with a field, as in dmft.E / T.")
            return N.const(float(_const_value(formula)), name=a.src())
        self._check_names(nodes, bare, entity_names)
        out_name, unit = _result_name(formula, operands)
        text = a.src()
        if len(nodes) == 1:
            label, node = next(iter(operands.values()))
            f = _replace_labels(formula, {label: None})
            if isinstance(f, lang.Name) and f.id == node.type.output_names[0]:
                return node
            return N.mapn(node, {out_name: f.src()}, units={out_name: unit}, name=text)
        J = self.combine(OrderedDict((lab, n) for lab, n in operands.values()), text)
        return N.mapn(J, {out_name: formula.src()}, units={out_name: unit}, name=text)

    def _transform(self, y, old, to, expr, text):
        """transform(y, old, new = formula). The formula may use y's inputs and outputs, and other fields."""
        y.type.need_input(old, "transform")
        operands = OrderedDict()
        operands[y.id] = ("a", y)
        # names of y's outputs refer to y itself, not to fields of the same name
        own = {o: lang.Attr(lang.Name("a"), o) for o in y.type.output_names}

        def pre(x):
            if isinstance(x, lang.Name) and x.id in own and self.entity(x.id) is None:
                return own[x.id]
            if isinstance(x, lang.Bin):
                return lang.Bin(x.op, pre(x.left), pre(x.right))
            if isinstance(x, lang.Neg):
                return lang.Neg(x.op, pre(x.operand))
            if isinstance(x, lang.Call) and not x.kwargs:
                return lang.Call(x.func, [pre(v) for v in x.args])
            return x

        expr = pre(expr)
        own_refs = {id(v) for v in own.values()}
        formula, bare, entity_names = self._collect_with_own(expr, operands, own_refs)
        nodes = [n for _, n in operands.values()]
        self._check_names(nodes, bare, entity_names)
        if len(nodes) == 1:
            f = _replace_labels(formula, {"a": None})
            return N.TransformNode({"of": y}, {"input": old, "to": to, "formula": f.src()}, text)
        J = self.combine(OrderedDict((lab, n) for lab, n in operands.values()), text)
        T = N.TransformNode({"of": J}, {"input": old, "to": to, "formula": formula.src()})
        keep = [f"a.{o}" for o in y.type.output_names]
        return N.rename(N.select(T, keep), {k: k[2:] for k in keep}, name=text)

    def _collect_with_own(self, expr, operands, own_refs):
        def walk(x):
            if id(x) in own_refs:
                return x
            if isinstance(x, lang.Bin):
                return lang.Bin(x.op, walk(x.left), walk(x.right))
            if isinstance(x, lang.Neg):
                return lang.Neg(x.op, walk(x.operand))
            if isinstance(x, lang.Num):
                return x
            if isinstance(x, lang.Call) and (x.func in ELEMENTWISE or x.func in ("min", "max")):
                return lang.Call(x.func, [walk(v) for v in x.args])
            f, b, e = self._collect(x, operands)
            bare.update(b)
            ents.update(e)
            return f

        bare, ents = set(), set()
        return walk(expr), bare, ents

    def combine(self, ops, text):
        """join, or align along ragged inputs whose points differ (core.md 5.3)."""
        unmatched = self.s("unmatched")
        align = self.s("align")
        if align == "auto":
            counts = {}
            for n in ops.values():
                for v in n.type.inputs:
                    counts.setdefault(v.name, []).append(n)
            cands = []
            for u, ns in counts.items():
                if len(ns) < 2:
                    continue
                ragged = any(not n.type.var(u).exact for n in ns)
                if ragged and len({n.pid() for n in ns}) > 1:
                    cands.append(u)
        else:
            cands = list(align)
            for u in cands:
                if not any(n.type.has_input(u) for n in ops.values()):
                    raise GlueError(f"{text}: align=[{u}] but the operands have no input {u}")
        if not cands:
            return N.join(ops, unmatched, name=text)
        if len(cands) > 1:
            raise GlueError(f"{text}: the operands share more than one input that needs aligning "
                            f"({', '.join(cands)}). Choose one with `with align=[{cands[0]}]`, or resample first.")
        u = cands[0]
        for lab, n in ops.items():
            v = n.type.var(u)
            if v is not None and v.dtype == "str":
                raise GlueError(f"{text}: can't align along the string input {u}")
        if self.s("strict") and self.settings.source("grid") != "statement":
            raise GlueError(f"strict mode: {text} needs aligning along {u}. Add `with grid=...` or use resample().")
        with_u = OrderedDict((lab, n) for lab, n in ops.items() if n.type.has_input(u))
        grid = self.grid_node(self.s("grid"), with_u, u)
        return N.AlignNode({"of": dict(ops), "grid": grid},
                           {"along": u, "method": _method_text(self.s("method")),
                            "extrapolate": self.s("extrapolate"), "fewpoints": self.s("fewpoints"),
                            "unmatched": unmatched}, text)

    def grid_node(self, spec, ops, u):
        k = spec.kind
        text = spec.text()
        if k == "overlap":
            return N.OverlapNode({"of": dict(ops)}, {"along": u, "count": int(spec.n or 0),
                                                    "step": float(spec.step or 0.0),
                                                    "unmatched": self.s("unmatched")}, text)
        if k == "union":
            if len(ops) == 1:
                return N.points(next(iter(ops.values())), name=text)
            return N.MergePointsNode({"of": dict(ops)}, {"along": u, "unmatched": self.s("unmatched")}, text)
        if k == "like":
            g = self.el(spec.like)
            if not g.type.has_input(u):
                raise GlueError(f"like({spec.like.src()}): it has no input {u}")
            return N.points(g, name=text)
        if k == "linspace":
            return N.axis(u, f"lin:{spec.a!r}:{spec.b!r}:{spec.n}", name=text)
        if k == "logspace":
            return N.axis(u, f"log:{spec.a!r}:{spec.b!r}:{spec.n}", name=text)
        if k == "points":
            return N.axis(u, "list:[" + ", ".join(repr(float(p)) for p in spec.points) + "]", name=text)
        raise AssertionError(k)

    # ------------------------------------------------------------ calls

    def _axis_of(self, node, arg, fname):
        if arg is None:
            if not node.type.axis:
                raise GlueError(f"{fname}: {node.name or 'this field'} has no default axis. "
                                f"Name the input, as in {fname}(y, T)")
            return node.type.axis
        if not isinstance(arg, lang.Name):
            raise GlueError(f"{fname}: the input to act along must be a name, as in {fname}(y, T)")
        _ = node.type.need_input(arg.id, fname)
        return arg.id

    def _value_named(self, node, text):
        if len(node.type.outputs) == 1 and node.type.output_names[0] != "value":
            return N.rename(node, {node.type.output_names[0]: "value"}, name=text)
        node.name = text
        return node

    def _call(self, a):
        f, args, kw = a.func, a.args, dict(a.kwargs)
        text = a.src()

        def need(n_min, n_max, allowed=()):
            if not (n_min <= len(args) <= n_max):
                want = n_min if n_min == n_max else f"{n_min} to {n_max}"
                raise GlueError(f"{f} takes {want} argument(s), got {len(args)}")
            for k in kw:
                if k not in allowed:
                    raise GlueError(f"{f} has no option {k!r}" +
                                    (f". Options: {', '.join(sorted(allowed))}" if allowed else ""))

        if f in REDUCERS:
            need(1, 2)
            y = self.el(args[0])
            x = self._axis_of(y, args[1] if len(args) > 1 else None, f)
            node = N.ReduceNode({"of": y}, {"along": x, "reduce": f, "method": "-"})
            return self._value_named(node, text)
        if f == "integral":
            need(1, 2, {"method"})
            y = self.el(args[0])
            x = self._axis_of(y, args[1] if len(args) > 1 else None, f)
            m = self._method_arg(kw.get("method"), trapz=True)
            return self._value_named(N.ReduceNode({"of": y}, {"along": x, "reduce": "integral", "method": m}), text)
        if f == "resample":
            need(1, 1, {"grid", "method", "along"})
            y = self.el(args[0])
            x = self._axis_of(y, kw.get("along"), f)
            from .settings import parse_grid

            spec = parse_grid(kw["grid"]) if "grid" in kw else self.s("grid")
            grid = self.grid_node(spec, {"a": y}, x)
            m = self._method_arg(kw.get("method"))
            node = N.resample(y, grid, x, m, self.s("extrapolate"), self.s("fewpoints"), name=text)
            return node
        if f == "d":
            need(1, 2, {"order", "method", "grid"})
            y = self.el(args[0])
            x = self._axis_of(y, args[1] if len(args) > 1 else None, f)
            order = int(kw["order"].value) if "order" in kw else 1
            m = self._method_arg(kw.get("method"), fd=True)
            node = N.DerivNode({"of": y}, {"along": x, "order": order, "method": m,
                                           "fewpoints": self.s("fewpoints")})
            if "grid" in kw:
                from .settings import parse_grid

                grid = self.grid_node(parse_grid(kw["grid"]), {"a": node}, x)
                node = N.resample(node, grid, x, _method_text(self.s("method")), self.s("extrapolate"),
                                  self.s("fewpoints"))
            return self._value_named(node, text)
        if f == "int":
            need(1, 2, {"method"})
            y = self.el(args[0])
            x = self._axis_of(y, args[1] if len(args) > 1 else None, f)
            m = self._method_arg(kw.get("method"), trapz=True)
            return self._value_named(N.CumintNode({"of": y}, {"along": x, "method": m}), text)
        if f == "at":
            if len(args) != 1 or len(kw) != 1:
                raise GlueError("at takes a field and one point, as in at(dmft.E, T=0.1)")
            (x, v), = kw.items()
            return self._at(self.el(args[0]), x, v, text)
        if f == "stack":
            if args:
                raise GlueError("stack takes named members, as in stack(dmft=dmft.E, ed=ed.E)")
            tag = "source"
            members = OrderedDict()
            for k, v in a.kwargs:
                if k == "tag":
                    if not isinstance(v, (lang.Str, lang.Name)):
                        raise GlueError('stack: tag must be a name, as in tag="method"')
                    tag = v.value if isinstance(v, lang.Str) else v.id
                else:
                    members[k] = self.el(v)
            if len(members) < 2:
                raise GlueError("stack needs at least two members")
            first = next(iter(members.values()))
            if len(first.type.outputs) == 1:
                name0 = first.type.output_names[0]
                for k, n in list(members.items()):
                    if len(n.type.outputs) == 1 and n.type.output_names[0] != name0:
                        members[k] = N.rename(n, {n.type.output_names[0]: name0}, name=n.name)
            return N.StackNode({"of": dict(members)}, {"tag": tag}, text)
        if f == "using":
            if len(args) != 1:
                raise GlueError("using takes one expression and settings, as in using(dmft.E - ed.E, method=cubic)")
            overrides = {k: convert(k, v) for k, v in a.kwargs}
            self.settings.push(overrides)
            try:
                return self.el(args[0])
            finally:
                self.settings.pop()
        if f == "rename":
            if len(args) != 1:
                raise GlueError("rename takes one field and old=new names, as in rename(ed.E, u=U)")
            y = self.el(args[0])
            mapping = {}
            for k, v in a.kwargs:
                if not isinstance(v, lang.Name):
                    raise GlueError("rename takes old=new names, as in rename(ed.E, u=U)")
                mapping[k] = v.id
            return N.rename(y, mapping, name=text)
        if f == "transform":
            if len(args) != 2 or len(a.kwargs) != 1 or not isinstance(args[1], lang.Name):
                raise GlueError("transform takes a field, the input to replace, and new=formula, "
                                "as in transform(ed.E, u, U = u * 2.0)")
            (to, expr), = a.kwargs
            return self._transform(self.el(args[0]), args[1].id, to, expr, text)
        if f == "swap":
            need(2, 2, {"branches", "flat_tol"})
            y = self.el(args[0])
            x = self._axis_of(y, args[1], f)
            if len(y.type.outputs) != 1:
                raise GlueError("swap needs a field with one output, as in swap(dmft.E, T)")
            return N.SwapNode({"of": y}, {"along": x, "output": y.type.output_names[0],
                                          "branches": self._opt(kw, "branches", "branches"),
                                          "flat_tol": float(self._opt(kw, "flat_tol", "flat_tol"))}, text)
        if f == "legendre":
            need(1, 2, {"slope", "result", "method", "branches", "flat_tol"})
            y = self.el(args[0])
            x = self._axis_of(y, args[1] if len(args) > 1 else None, f)
            if len(y.type.outputs) != 1:
                raise GlueError("legendre needs a field with one output")
            return N.LegendreNode({"of": y}, {
                "along": x, "output": y.type.output_names[0],
                "slope": _name_opt(kw.get("slope"), "p"), "result": _name_opt(kw.get("result"), "G"),
                "method": self._method_arg(kw.get("method")),
                "branches": self._opt(kw, "branches", "branches"),
                "flat_tol": float(self._opt(kw, "flat_tol", "flat_tol"))}, text)
        if f in PLUGINS:
            opdef = PLUGINS[f]
            kind = {"elementwise": "pointwise"}.get(opdef.kind, opdef.kind)
            if len(args) == 2 and kind != "pointwise" and isinstance(args[1], lang.Name) and "along" not in kw:
                kw["along"] = args[1]            # f(y, x), like max(y, n)
                args = args[:1]
            if len(args) != 1:
                raise GlueError(f"{f} takes one field plus options" +
                                ("" if kind == "pointwise" else ", and optionally the input to work along"))
            y = self.el(args[0])
            along = "-"
            if kind != "pointwise":
                along = self._axis_of(y, kw.pop("along", None), f)
            params = {}
            for k, v in kw.items():
                if isinstance(v, lang.Num):
                    params[k] = v.value
                elif isinstance(v, lang.Str):
                    params[k] = v.value
                elif isinstance(v, lang.Name):
                    params[k] = v.id
                else:
                    raise GlueError(f"{f}: option {k} must be a number or a string")
            node = N.ApplyNode({"of": y}, {"fn": opdef.ref(), "kind": kind, "along": along, "params": params})
            return self._value_named(node, text) if kind != "pointwise" else node
        raise self._unknown(f)

    def _opt(self, kw, key, setting):
        if key not in kw:
            return self.s(setting)
        v = kw[key]
        return convert(setting, v)

    def _method_arg(self, node, trapz=False, fd=False):
        if node is None:
            return "trapz" if trapz else _method_text(self.s("method"))
        if isinstance(node, lang.Name) and ((trapz and node.id == "trapz") or (fd and node.id == "fd")):
            return node.id
        from .settings import parse_method

        return _method_text(parse_method(node, allow_fd=fd))

    def _at(self, y, xname, value, text):
        y.type.need_input(xname, "@")
        if isinstance(value, lang.Num):
            return N.EvalNode({"of": y}, {"along": xname, "value": float(value.value),
                                          "method": _method_text(self.s("method")),
                                          "extrapolate": self.s("extrapolate"), "fewpoints": self.s("fewpoints")},
                              text)
        if isinstance(value, lang.ListLit) and all(isinstance(i, lang.Num) for i in value.items):
            grid = N.axis(xname, "list:[" + ", ".join(repr(float(i.value)) for i in value.items) + "]",
                          name=f"points({value.src()})")
            return N.resample(y, grid, xname, _method_text(self.s("method")), self.s("extrapolate"),
                              self.s("fewpoints"), name=text)
        raise GlueError(f"@ {xname}= needs a number or a list of numbers")


def _name_opt(node, default):
    if node is None:
        return default
    if isinstance(node, (lang.Name, lang.Str)):
        return node.id if isinstance(node, lang.Name) else node.value
    raise GlueError("expected a name")


def _const_value(f):
    import pandas as pd

    from .core import formula as F

    return F.evaluate(F.parse(f.src()), pd.DataFrame(index=[0]))[0]


def _replace_labels(f, labels):
    """Drop operand labels: a.E → E, for formulas over a single field."""
    if isinstance(f, lang.Attr) and isinstance(f.obj, lang.Name) and f.obj.id in labels:
        return lang.Name(f.name)
    if isinstance(f, lang.Bin):
        return lang.Bin(f.op, _replace_labels(f.left, labels), _replace_labels(f.right, labels))
    if isinstance(f, lang.Neg):
        return lang.Neg(f.op, _replace_labels(f.operand, labels))
    if isinstance(f, lang.Call):
        return lang.Call(f.func, [_replace_labels(x, labels) for x in f.args])
    return f


def _result_name(f, operands):
    """0.1 naming: negation and abs keep the column name, everything else is `value`. Units follow +/-."""
    by_label = {lab: n for lab, n in operands.values()}

    def unit(x):
        if isinstance(x, lang.Num):
            return None
        if isinstance(x, lang.Attr) and isinstance(x.obj, lang.Name) and x.obj.id in by_label:
            o = by_label[x.obj.id].type.out(x.name)
            return o.unit if o else ""
        if isinstance(x, lang.Name):
            for n in by_label.values():
                v = n.type.var(x.id)
                if v is not None:
                    return v.unit
            return ""
        if isinstance(x, lang.Neg):
            return unit(x.operand)
        if isinstance(x, lang.Call):
            return unit(x.args[0]) if x.func == "abs" else ""
        if isinstance(x, lang.Bin):
            a, b = unit(x.left), unit(x.right)
            if x.op in "+-":
                if a is None:
                    return b
                if b is None:
                    return a
                return a if a == b else ""
            if a is None and b is None:
                return None
            return ""
        return ""

    u = unit(f) or ""
    inner = f.operand if isinstance(f, lang.Neg) else (f.args[0] if isinstance(f, lang.Call) and f.func == "abs"
                                                       else None)
    if inner is not None and isinstance(inner, lang.Attr):
        return inner.name, u
    return "value", u
