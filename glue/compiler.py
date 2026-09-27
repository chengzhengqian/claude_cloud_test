"""Turn expression ASTs into engine nodes, and expand views for recipes."""

import difflib
from dataclasses import dataclass, field

import numpy as np

from . import engine as E
from . import lang
from .errors import GlueError
from .plugins import REGISTRY
from .settings import convert, parse_grid, parse_method, to_text

ELEMENTWISE = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log, "log10": np.log10,
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
}
REDUCERS = {"max", "min", "mean", "first", "last", "count", "argmax", "argmin"}
BUILTINS = set(ELEMENTWISE) | REDUCERS | {"resample", "d", "int", "integral", "at", "stack", "using"}


@dataclass
class Entity:
    kind: str  # var, view, table, dataset
    name: str
    ast: object = None
    overrides: dict = field(default_factory=dict)
    unit: str = None
    label: str = None
    table: object = None
    description: str = ""


@dataclass
class Env:
    coords: dict  # name -> ctype
    xnames: set


class Compiler:
    def __init__(self, ns, settings):
        self.ns = ns
        self.settings = settings
        self.entities = {}
        self.inputs = {}
        self.plugins = {}
        self.like_nodes = {}
        self._stack = []
        self._compile_like(settings.get("grid"))

    # --- public

    def compile(self, ast, name=None, overrides=None, unit=None):
        node = self._compile_ast(ast)
        if overrides:
            for v in overrides.values():
                self._compile_like(v)
            node = E.Using(node, overrides)
        if name:
            node = E.Named(node, name, unit=unit)
        return node

    # --- internals

    def _compile_like(self, spec):
        if getattr(spec, "kind", None) == "like" and id(spec.like) not in self.like_nodes:
            self.like_nodes[id(spec.like)] = self._compile_ast(spec.like)

    def _entity_node(self, ent):
        if ent.name in self.entities:
            return self.entities[ent.name]
        if ent.name in self._stack:
            raise GlueError(f"{ent.name} refers to itself: {' -> '.join(self._stack + [ent.name])}")
        if ent.kind in ("table", "dataset"):
            self.inputs[ent.name] = ent.table
            node = E.TableNode(ent.table)
        else:
            self._stack.append(ent.name)
            try:
                node = self._compile_ast(ent.ast)
                if ent.overrides:
                    for v in ent.overrides.values():
                        self._compile_like(v)
                    node = E.Using(node, ent.overrides)
                node = E.Named(node, ent.name, unit=ent.unit, label=ent.label)
            finally:
                self._stack.pop()
        self.entities[ent.name] = node
        return node

    def _env(self, ast):
        coords, xnames = {}, set()
        for n in lang.walk(ast):
            if isinstance(n, lang.Name):
                ent = self.ns.lookup(n.id)
                if ent is None:
                    continue
                node = self._entity_node(ent)
                for c in node.coords:
                    coords.setdefault(c, node.ctype(c))
                if node.xname:
                    xnames.add(node.xname)
        return Env(coords, xnames)

    def _compile_ast(self, ast):
        return self._c(ast, self._env(ast))

    def _unknown(self, name):
        names = list(self.ns.names()) + sorted(BUILTINS | set(REGISTRY))
        close = difflib.get_close_matches(name, names, n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        return GlueError(f"unknown name {name!r}.{hint}")

    def _c(self, a, env):
        if isinstance(a, lang.Num):
            return E.Const(a.value)
        if isinstance(a, lang.Str):
            raise GlueError(f"the string {a.src()} can only be used in selectors and options")
        if isinstance(a, lang.Name):
            return self._name(a, env)
        if isinstance(a, lang.Attr):
            return self._attr(a, env)
        if isinstance(a, lang.Sel):
            return self._sel(a, env)
        if isinstance(a, lang.Bin):
            left, right = self._c(a.left, env), self._c(a.right, env)
            for side in (left, right):
                if side.kind == "table":
                    raise GlueError(f"can't do arithmetic on the table {side.text}. "
                                    f"Pick a column, as in {side.text}.{_first_value(side)}")
            return E.Binary(a.op, left, right)
        if isinstance(a, lang.Neg):
            node = self._c(a.operand, env)
            if a.op == "+":
                return node
            self._need_value(node, "-")
            return E.Elementwise("neg", np.negative, node)
        if isinstance(a, lang.AtE):
            node = self._c(a.obj, env)
            return self._at(node, a.name, a.value)
        if isinstance(a, lang.Call):
            return self._call(a, env)
        if isinstance(a, (lang.ListLit, lang.TupleLit)):
            raise GlueError(f"a list like {a.src()} can only be used as an argument")
        raise GlueError(f"can't evaluate {a.src()}")

    def _name(self, a, env):
        ent = self.ns.lookup(a.id)
        if ent is not None:
            if a.id in env.coords:
                raise GlueError(f"{a.id!r} is both a {ent.kind} and a coordinate here. "
                                f"Use table.{a.id} for the coordinate, or rename the {ent.kind}.")
            node = self._entity_node(ent)
            if ent.kind == "dataset":
                vals = ent.table.values
                if len(vals) == 1:
                    return E.SourceCol(ent.table, vals[0])
            return node
        if a.id in env.coords:
            return E.CoordRef(a.id, ctype=env.coords[a.id])
        if a.id in env.xnames:
            return E.XRef(a.id)
        raise self._unknown(a.id)

    def _attr(self, a, env):
        base = self._c(a.obj, env)
        name = a.name
        if isinstance(base, E.TableNode):
            t = base.table
            if name in t.coords:
                return E.CoordRef(name, source=base, ctype=t.ctype(name))
            if name == t.x or name in t.columns():
                return E.SourceCol(t, name, base.coord_sels, base.x_sels)
            cols = list(t.coords) + ([t.x] if t.x else []) + [c for c in t.columns() if c not in t.coords]
            raise GlueError(f"{t.name} has no column {name!r}. Columns: {', '.join(dict.fromkeys(cols))}")
        if name == base.yname:
            return base
        if name in base.coords:
            return E.CoordRef(name, source=base, ctype=base.ctype(name))
        if base.xname and name == base.xname:
            return E.XOf(base)
        raise GlueError(f"{base.text} has no column {name!r}. It has {base.yname}"
                        + (f", x {base.xname}" if base.xname else "")
                        + (f", coordinates {', '.join(base.coords)}" if base.coords else ""))

    def _split_sels(self, node, sels):
        coord, xs = [], []
        for s in sels:
            if s.name in node.coords:
                coord.append(s)
            elif node.xname and s.name == node.xname:
                xs.append(s)
            else:
                names = list(node.coords) + ([node.xname] if node.xname else [])
                raise GlueError(f"{node.text}[{s.src()}]: {node.text} has no coordinate {s.name} "
                                f"(it has {', '.join(names)})")
        return coord, xs

    def _sel(self, a, env):
        base = self._c(a.obj, env)
        coord, xs = self._split_sels(base, a.selectors)
        if isinstance(base, E.TableNode):
            return E.TableNode(base.table, base.coord_sels + coord, base.x_sels + xs)
        if isinstance(base, E.SourceCol):
            return E.SourceCol(base.table, base.col, base.coord_sels + coord, base.x_sels + xs)
        return E.SelectNode(base, coord, xs)

    def _need_value(self, node, what):
        if node.kind == "table":
            raise GlueError(f"{what} needs a column, not the table {node.text}. "
                            f"Use {node.text}.{_first_value(node)}")

    def _need_curves(self, node, what):
        self._need_value(node, what)
        if node.kind != "curves" or isinstance(node, E.XRef):
            raise GlueError(f"{what} needs curves, but {node.text} has one value per key")

    def _axis(self, node, arg, fname):
        if not isinstance(arg, lang.Name):
            raise GlueError(f"the second argument of {fname} is the x name, as in {fname}({node.text}, {node.xname})")
        if arg.id != node.xname:
            raise GlueError(f"{fname}: {node.text} has x {node.xname}, not {arg.id}")

    def _at(self, node, xname, value):
        self._need_curves(node, "@")
        if xname != node.xname:
            raise GlueError(f"{node.text} has x {node.xname}, not {xname}")
        if isinstance(value, lang.Num):
            return E.At(node, value.value)
        if isinstance(value, lang.ListLit) and all(isinstance(i, lang.Num) for i in value.items):
            return E.At(node, [i.value for i in value.items])
        raise GlueError(f"at: {xname}= needs a number or a list of numbers")

    def _call(self, a, env):
        f = a.func
        kw = dict(a.kwargs)
        if f in ELEMENTWISE:
            self._nargs(a, 1)
            node = self._c(a.args[0], env)
            self._need_value(node, f)
            return E.Elementwise(f, ELEMENTWISE[f], node)
        if f in REDUCERS:
            self._nargs(a, 1)
            node = self._c(a.args[0], env)
            self._need_curves(node, f)
            return E.Reduce(node, f)
        if f == "resample":
            self._nargs(a, 1, {"grid", "method"})
            node = self._c(a.args[0], env)
            self._need_curves(node, f)
            grid = parse_grid(kw["grid"]) if "grid" in kw else None
            self._compile_like(grid)
            method = parse_method(kw["method"]) if "method" in kw else None
            return E.Resample(node, grid=grid, method=method)
        if f == "d":
            self._nargs(a, 2, {"order", "method", "grid"})
            node = self._c(a.args[0], env)
            self._need_curves(node, f)
            self._axis(node, a.args[1], f)
            order = int(kw["order"].value) if "order" in kw else 1
            if order not in (1, 2, 3):
                raise GlueError("d: order must be 1, 2, or 3")
            method = parse_method(kw["method"], allow_fd=True) if "method" in kw else None
            grid = parse_grid(kw["grid"]) if "grid" in kw else None
            self._compile_like(grid)
            return E.Deriv(node, order=order, method=method, grid=grid)
        if f in ("int", "integral"):
            self._nargs(a, 2, {"method"})
            node = self._c(a.args[0], env)
            self._need_curves(node, f)
            self._axis(node, a.args[1], f)
            method = None
            if "method" in kw:
                m = kw["method"]
                if not (isinstance(m, lang.Name) and m.id == "trapz"):
                    method = parse_method(m)
            if f == "int":
                return E.CumInt(node, method=method)
            return E.Reduce(node, "integral", method=method)
        if f == "at":
            if len(a.args) != 1 or len(a.kwargs) != 1:
                raise GlueError("at takes a curve and one point, as in at(dmft.E, T=0.1)")
            node = self._c(a.args[0], env)
            (xname, value), = a.kwargs
            return self._at(node, xname, value)
        if f == "stack":
            if a.args:
                raise GlueError("stack takes named members, as in stack(dmft=dmft.E, ed=ed.E)")
            tag = "source"
            members = []
            for k, v in a.kwargs:
                if k == "tag":
                    if not isinstance(v, (lang.Str, lang.Name)):
                        raise GlueError("stack: tag must be a name, as in tag=\"method\"")
                    tag = v.value if isinstance(v, lang.Str) else v.id
                else:
                    node = self._c(v, env)
                    self._need_value(node, "stack")
                    members.append((k, node))
            if len(members) < 2:
                raise GlueError("stack needs at least two members")
            return E.Stack(members, tag)
        if f == "using":
            if len(a.args) != 1:
                raise GlueError("using takes one expression and settings, as in using(dmft.E - ed.E, method=cubic)")
            node = self._c(a.args[0], env)
            overrides = {k: convert(k, v) for k, v in a.kwargs}
            for v in overrides.values():
                self._compile_like(v)
            return E.Using(node, overrides)
        if f in REGISTRY:
            opdef = REGISTRY[f]
            if len(a.args) != 1:
                raise GlueError(f"{f} takes one expression plus options")
            node = self._c(a.args[0], env)
            if opdef.kind != "elementwise":
                self._need_curves(node, f)
            kwargs = {}
            for k, v in a.kwargs:
                if isinstance(v, lang.Num):
                    kwargs[k] = v.value
                elif isinstance(v, lang.Str):
                    kwargs[k] = v.value
                elif isinstance(v, lang.Name):
                    kwargs[k] = v.id
                else:
                    raise GlueError(f"{f}: option {k} must be a number or a string")
            self.plugins[f] = opdef
            return E.PluginOp(opdef, node, kwargs)
        raise self._unknown(f)

    def _nargs(self, a, n, allowed_kw=()):
        if len(a.args) != n:
            raise GlueError(f"{a.func} takes {n} argument{'s' if n > 1 else ''}, got {len(a.args)}")
        for k, _ in a.kwargs:
            if k not in allowed_kw:
                raise GlueError(f"{a.func} has no option {k!r}"
                                + (f". Options: {', '.join(sorted(allowed_kw))}" if allowed_kw else ""))


def _first_value(node):
    t = getattr(node, "table", None)
    if t is not None and t.values:
        return t.values[0]
    return "COLUMN"


# ------------------------------------------------------------------ expansion for recipes


class Expander:
    """Inline session variables and views, so a recipe only names tables and datasets."""

    def __init__(self, ns):
        self.ns = ns
        self.inputs = {}
        self.plugins = {}
        self._stack = []

    def expand(self, a):
        if isinstance(a, lang.Name):
            ent = self.ns.lookup(a.id)
            if ent is None:
                return a
            if ent.kind in ("table", "dataset"):
                self.inputs[a.id] = ent
                return a
            return self._entity(ent)
        if isinstance(a, lang.Attr):
            if isinstance(a.obj, lang.Name):
                ent = self.ns.lookup(a.obj.id)
                if ent is not None and ent.kind in ("var", "view") and a.name == ent.name:
                    return self._entity(ent)
            return lang.Attr(self.expand(a.obj), a.name)
        if isinstance(a, lang.Sel):
            return lang.Sel(self.expand(a.obj), a.selectors)
        if isinstance(a, lang.Bin):
            return lang.Bin(a.op, self.expand(a.left), self.expand(a.right))
        if isinstance(a, lang.Neg):
            return lang.Neg(a.op, self.expand(a.operand))
        if isinstance(a, lang.AtE):
            return lang.AtE(self.expand(a.obj), a.name, a.value)
        if isinstance(a, lang.Call):
            if a.func in REGISTRY:
                self.plugins[a.func] = REGISTRY[a.func]
            kwargs = [(k, self.expand(v)) for k, v in a.kwargs]
            return lang.Call(a.func, [self.expand(x) for x in a.args], kwargs, a.pos)
        if isinstance(a, lang.ListLit):
            return lang.ListLit([self.expand(x) for x in a.items])
        return a

    def _entity(self, ent):
        if ent.name in self._stack:
            raise GlueError(f"{ent.name} refers to itself")
        self._stack.append(ent.name)
        try:
            inner = self.expand(ent.ast)
        finally:
            self._stack.pop()
        if ent.overrides:
            inner = lang.Call("using", [inner], [(k, overrides_ast(k, v)) for k, v in ent.overrides.items()])
        return inner


def overrides_ast(key, value):
    return lang.parse_optvalue_text(_quote_if_needed(to_text(key, value)))


def _quote_if_needed(text):
    try:
        lang.parse_optvalue_text(text)
        return text
    except Exception:
        return '"' + text.replace('"', '\\"') + '"'
