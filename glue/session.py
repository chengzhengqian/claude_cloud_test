"""A session: loaded tables, views, calcs, datasets, variables, settings, and the value store."""

import hashlib
import json
import dataclasses
import os
import sys

import numpy as np
import pandas as pd

from . import dataset as D
from . import lang
from .core import nodes as N
from .core.context import Context, EXPENSIVE
from .core.formula import pred_from_selector
from .core.store import Store, bump
from .core.tree import render
from .elaborate import Elaborator, Entity
from .errors import GlueError
from .plugins import load_modules
from .settings import PLOT_SETTINGS, SETTINGS, Settings, convert, to_text
from .sources import (READ_CACHE, CacheTable, ColumnMeta, SqliteTable, apply_sel_frame, check_keys,
                      check_version, describe_source, load_table)
from .util import (check_name, expand_path, normalize, read_toml, relpath, remove_toml_entry, set_toml_entry)

PROJECT_KEYS = {"glue", "kind", "name", "description", "tables", "include", "views", "calcs", "datasets",
                "settings", "plot", "plugins", "meta"}


class MemoryTable(CacheTable):
    """Data registered from Python. It has no file, so it can't be part of a recipe."""

    def __init__(self, name, frame, by, x=None, y=None):
        super().__init__(name, None, "", "memory")
        self.is_dataset = False
        self.inputs = list(by) + ([x] if x else [])
        self.x = x
        self.coords = list(by)
        self.locator_coords = list(by)
        self.outputs_decl = list(y) if y else [c for c in frame.columns if c not in self.inputs]
        self._values = list(self.outputs_decl)
        for c in self.inputs:
            is_str = frame[c].dtype == object or str(frame[c].dtype).startswith("str")
            self.m(c).type = "str" if is_str else "float"
        frame = frame[self.inputs + self.outputs_decl].copy()
        for c in self.inputs:
            if self.ctype(c) == "float":
                frame[c] = np.round(frame[c].astype(float), 10) + 0.0
        self._frame = frame
        h = hashlib.sha256(name.encode() + pd.util.hash_pandas_object(frame, index=False).values.tobytes())
        self.def_id = "py" + h.hexdigest()[:10]

    def frame(self):
        return self._frame

    def _load_index(self):
        from .sources import Chunk

        return [Chunk(f"<python:{self.name}>", "", {})]

    def keys(self, selectors, explain=None):
        return super().keys(selectors, None)

    def chunk_entries(self, mode="stat", salt=None):
        return {}

    def digest(self, mode="stat", salt=None):
        return self.def_id

    def plan(self, sels):
        return 1, 1, "python"

    def read_points(self, sels, duplicates="error", ctx=None):
        return apply_sel_frame(self._frame, sels, self).reset_index(drop=True)


class Handle:
    """Operator overloading for the Python API: s.dmft.E - s.ed.E builds an expression."""

    def __init__(self, session, ast):
        object.__setattr__(self, "_s", session)
        object.__setattr__(self, "_ast", ast)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return Handle(self._s, lang.Attr(self._ast, name))

    def _wrap(self, other):
        if isinstance(other, Handle):
            return other._ast
        if isinstance(other, (int, float, np.floating, np.integer)):
            v = float(other)
            return lang.Num(v, repr(v))
        raise TypeError(f"can't combine an expression with {type(other).__name__}")

    def _bin(self, op, other, rev=False):
        a, b = self._ast, self._wrap(other)
        return Handle(self._s, lang.Bin(op, b, a) if rev else lang.Bin(op, a, b))

    def __add__(self, o): return self._bin("+", o)
    def __radd__(self, o): return self._bin("+", o, True)
    def __sub__(self, o): return self._bin("-", o)
    def __rsub__(self, o): return self._bin("-", o, True)
    def __mul__(self, o): return self._bin("*", o)
    def __rmul__(self, o): return self._bin("*", o, True)
    def __truediv__(self, o): return self._bin("/", o)
    def __rtruediv__(self, o): return self._bin("/", o, True)
    def __pow__(self, o): return self._bin("^", o)
    def __neg__(self): return Handle(self._s, lang.Neg("-", self._ast))

    def to_pandas(self, where=None, **settings):
        return self._s.eval(self._ast, where=where, **settings).to_pandas()

    def __repr__(self):
        return f"<glue expression {self._ast.src()}>"


class Result:
    def __init__(self, session, node, comp, where, overrides):
        self.session = session
        self.node = node
        self.comp = comp
        self.where = where
        self.overrides = overrides
        self._frame = None
        self.report = []

    @property
    def type(self):
        return self.node.type

    def to_pandas(self):
        if self._frame is None:
            frame, ctx, _, _ = self.session.evaluate(self.node, self.comp, self.where)
            self.report = ctx.report.lines(self.session.settings.get("report"))
            self._frame = frame
        return self._frame

    def to_numpy(self):
        return self.to_pandas().to_numpy()

    def tree(self):
        return "\n".join(render(self.node))

    def __repr__(self):
        return f"<glue result {self.node.name or self.node.op}: {self.node.type.text()}>"


class Session:
    def __init__(self, trust=False, log=print, warn_stale=True):
        self.warn_stale = warn_stale
        self.tables = {}
        self.datasets = {}
        self.views = {}
        self.calcs = {}
        self.variables = {}
        self.settings = Settings()
        self.plot_defaults = {k: v[1] for k, v in PLOT_SETTINGS.items()}
        self.project_path = None
        self.project_dir = None
        self.index_dir = None
        self.glue_dir = None
        self.loaded = []
        self.trust = trust
        self.log = log
        self.figure = None
        self.store = Store()

    # ---------------------------------------------------------------- names

    def lookup(self, name):
        if name in self.variables:
            return self.variables[name]
        if name in self.views:
            return self.views[name]
        if name in self.tables:
            return Entity("table", name, table=self.tables[name])
        if name in self.datasets:
            return Entity("dataset", name, table=self.datasets[name])
        if name in self.calcs:
            return self.calcs[name]
        return None

    def names(self):
        return list(self.variables) + list(self.views) + list(self.tables) + list(self.datasets) + list(self.calcs)

    def _claim(self, name, what):
        check_name(name, what)
        for kind, d in (("table", self.tables), ("view", self.views), ("dataset", self.datasets),
                        ("calc", self.calcs)):
            if name in d:
                raise GlueError(f"{what} {name!r} clashes with the {kind} of the same name")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self.lookup(name) is None:
            raise AttributeError(f"no table, view, dataset, or variable named {name!r}")
        return Handle(self, lang.Name(name))

    # ---------------------------------------------------------------- loading

    def load(self, path, prefix="", name=None):
        """Load a file. `name` renames a single table, dataset, or calc. For a project it is a prefix (old -> old_dmft)."""
        path = os.path.abspath(path)
        data = read_toml(path)
        kind = data.get("kind")
        if name and kind == "project":
            prefix, name = prefix + (name if name.endswith("_") else name + "_"), None
        if kind == "project":
            self._load_project(path, data, prefix=prefix, included=bool(prefix) and self.project_path is not None)
        elif kind == "table":
            if self.index_dir is None:
                self._set_dirs(os.path.dirname(path))
            t = load_table(path, name, index_dir=self.index_dir, base_dir=self.project_dir)
            if prefix:
                t.name = prefix + t.name
            self._claim(t.name, "table")
            self.tables[t.name] = t
            self.log(f"loaded table {t.name}")
        elif kind == "dataset":
            t = D.load_dataset(path, name)
            if prefix:
                t.name = prefix + t.name
            self._claim(t.name, "dataset")
            self.datasets[t.name] = t
            self.log(f"loaded dataset {t.name}")
            self._warn_status(t)
        elif kind == "calc":
            ent = self._calc_entity(path, name, prefix)
            self._claim(ent.name, "calc")
            self.calcs[ent.name] = ent
            self.log(f"loaded calc {ent.name}")
        else:
            raise GlueError(f"{path}: unknown kind {kind!r}. Use table, project, calc, or dataset")
        self.loaded.append((path, prefix, name))

    def _set_dirs(self, base):
        self.project_dir = self.project_dir or base
        self.glue_dir = os.path.join(self.project_dir, ".glue")
        self.index_dir = os.path.join(self.glue_dir, "index")

    def _calc_entity(self, path, name, prefix=""):
        info = D.load_info(path, name)
        root, ns, _ = D.build_tree(info)
        ent = Entity("calc", prefix + (name or info.name), root=root, description=info.description)
        ent.leaf_entities = ns.entities
        ent.info = info
        return ent

    def _load_project(self, path, data, prefix="", included=False):
        check_version(data, path)
        check_keys(data, PROJECT_KEYS, path)
        base = os.path.dirname(path)
        if not included:
            if self.project_path is not None and self.project_path != path:
                self.log(f"note: merging project {relpath(path, os.getcwd())} into "
                         f"{relpath(self.project_path, os.getcwd())}")
            if self.project_path is None:
                self.project_path = path
                self.project_dir = base
                self._set_dirs(base)
                self.settings = Settings(data.get("settings", {}))
                plot = data.get("plot", {})
                check_keys(plot, set(PLOT_SETTINGS), f"{path} [plot]")
                for k, v in plot.items():
                    if k.startswith("x_"):
                        continue
                    self.plot_defaults[k] = tuple(v) if k == "size" else PLOT_SETTINGS[k][0](v)
        renames = {}
        tables = data.get("tables", {})
        datasets = data.get("datasets", {})
        views = data.get("views", {})
        calcs = data.get("calcs", {})
        for n in list(tables) + list(datasets) + list(views) + list(calcs):
            renames[n] = prefix + n
        index_dir = os.path.join(base, ".glue", "index")
        for n, spec in tables.items():
            file = spec if isinstance(spec, str) else spec.get("file")
            if not file:
                raise GlueError(f"{path} [tables]: {n} needs a file")
            t = load_table(expand_path(file, base), prefix + n, index_dir=index_dir, base_dir=base)
            self._claim(t.name, "table")
            self.tables[t.name] = t
        for n, file in datasets.items():
            dpath = expand_path(file, base)
            if not os.path.exists(dpath):
                # the script that saves it may be about to run, so don't stop the load
                self.log(f"warning: dataset {prefix + n}: {file} not found, skipped. "
                         f"Save it again, or remove it from [datasets]")
                continue
            t = D.load_dataset(dpath, prefix + n)
            self._claim(t.name, "dataset")
            self.datasets[t.name] = t
        for n, file in calcs.items():
            ent = self._calc_entity(expand_path(file, base), n, prefix)
            self._claim(ent.name, "calc")
            self.calcs[ent.name] = ent
        for n, spec in views.items():
            ent = self._view_entity(prefix + n, spec, f"{path} [views.{n}]", renames if prefix else None)
            self._claim(ent.name, "view")
            self.views[ent.name] = ent
        for inc in data.get("include", []):
            check_keys(inc, {"file", "prefix"}, f"{path} [[include]]")
            ipath = expand_path(inc["file"], base)
            idata = read_toml(ipath)
            if idata.get("kind") != "project":
                raise GlueError(f"{ipath}: include needs a project file")
            self._load_project(ipath, idata, prefix + inc.get("prefix", ""), included=True)
        plugins = data.get("plugins")
        if plugins:
            check_keys(plugins, {"modules", "path"}, f"{path} [plugins]")
            self._trust_plugins(path, plugins)
            load_modules(plugins.get("modules", []), plugins.get("path", []), base)
        if not included:
            name = data.get("name") or os.path.basename(base)
            extra = f", {len(self.calcs)} calcs" if self.calcs else ""
            self.log(f"loaded project {name}: {len(self.tables)} tables, {len(self.views)} views{extra}, "
                     f"{len(self.datasets)} datasets")
            for t in self.datasets.values():
                self._warn_status(t)

    def _view_entity(self, name, spec, where, renames=None):
        if isinstance(spec, str):
            spec = {"expr": spec}
        allowed = {"expr", "description", "unit", "label"} | set(SETTINGS)
        check_keys(spec, allowed, where)
        if "expr" not in spec:
            raise GlueError(f"{where}: a view needs expr")
        try:
            ast = lang.parse_expression(spec["expr"])
        except GlueError as e:
            raise GlueError(f"{where}: {e}") from None
        if renames:
            for node in lang.walk(ast):
                if isinstance(node, lang.Name) and node.id in renames:
                    node.id = renames[node.id]
        overrides = {k: convert(k, v) for k, v in spec.items() if k in SETTINGS}
        return Entity("view", name, ast=ast, overrides=overrides, unit=spec.get("unit"),
                      label=spec.get("label"), description=spec.get("description", ""))

    def _trust_plugins(self, path, plugins):
        if self.trust:
            return
        trust_file = os.path.join(os.path.dirname(path), ".glue", "trust")
        trusted = []
        if os.path.exists(trust_file):
            with open(trust_file) as f:
                trusted = json.load(f)
        if path in trusted:
            return
        mods = ", ".join(plugins.get("modules", []))
        if not sys.stdin.isatty():
            raise GlueError(f"{path} loads Python plugins ({mods}), which runs code. "
                            "Run with --trust, or open it in the shell once to approve.")
        ans = input(f"{relpath(path, os.getcwd())} loads Python plugins ({mods}), which runs code. Trust it? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            raise GlueError("plugins not trusted, project not loaded")
        os.makedirs(os.path.dirname(trust_file), exist_ok=True)
        with open(trust_file, "w") as f:
            json.dump(trusted + [path], f)

    def _warn_status(self, t):
        if not self.warn_stale:
            return
        try:
            st = D.status(t.info, self.index_dir)
        except GlueError as e:
            self.log(f"warning: dataset {t.name}: {e}")
            return
        if st.state not in ("fresh", "no recipe"):
            self.log(f"warning: dataset {t.name} is {st.state}" + (f": {st.details[0]}" if st.details else ""))

    def reload(self):
        files = list(self.loaded)
        variables, session_settings = self.variables, self.settings.session
        self.__init__(trust=self.trust, log=self.log, warn_stale=self.warn_stale)
        READ_CACHE.clear()
        for f, prefix, name in files:
            self.load(f, prefix, name)
        self.variables = variables
        self.settings.session = session_settings

    def rescan(self, name=None):
        tables = [self.tables[name]] if name else list(self.tables.values())
        if name and name not in self.tables:
            raise GlueError(f"{name} is not a table")
        out = []
        for t in tables:
            chunks = t.rescan()
            coords = {c: len({ch.coords[c] for ch in chunks if c in ch.coords}) for c in t.locator_coords}
            desc = ", ".join(f"{c} ({n} values)" for c, n in coords.items())
            out.append(f"{t.name}: {len(chunks)} chunks" + (f", {t.skipped} files skipped" if t.skipped else "")
                       + (f", inputs {desc}" if desc else ""))
        return out

    # ---------------------------------------------------------------- compile and evaluate

    def compile(self, expr, name=None, overrides=None, unit=None):
        ast = lang.parse_expression(expr) if isinstance(expr, str) else expr
        el = Elaborator(self, self.settings)
        return el.build(ast, name=name, overrides=overrides, unit=unit), el

    def context(self):
        self.store.limit = self.settings.get("read_cache_mb") << 20
        self.store.disk_dir = os.path.join(self.glue_dir, "cache") if (
            self.glue_dir and self.settings.get("disk_cache")) else None
        self.store.disk_limit = self.settings.get("disk_cache_mb") << 20
        READ_CACHE.limit = self.settings.get("read_cache_mb") << 20
        return Context(self.store, self.glue_dir, self.settings.get("fingerprint"), self.settings.get("report"))

    def where_preds(self, node, where):
        preds = [pred_from_selector(s) if not hasattr(s, "ref") else s for s in where]
        cols = set(node.type.input_names) | set(node.type.output_names)
        for p in preds:
            if p.name not in cols:
                names = node.type.input_names + node.type.output_names
                raise GlueError(f"where {p.src()}: {node.name or 'the result'} has no input {p.name} "
                                f"(it has {', '.join(names) or 'none'})")
        return [_int_values(p, node.type) for p in preds]

    def evaluate(self, node, comp=None, where=(), limit=None, keys=None):
        preds = self.where_preds(node, where)
        ctx = self.context()
        if (comp is not None and getattr(comp, "nan_error", False)) or self.settings.get("nan_rows") == "error":
            ctx.nan_rows = "error"
        frame = D.sort_frame(ctx.run(node, preds), node.type)
        if limit is not None:
            frame = frame.head(limit)
        return frame, ctx, [], []

    def plot(self, text):
        """Python API: s.plot("dE vs T by J where n=0.5 > fig.png")."""
        from .plotting import parse_plot, plot

        p = lang.Parser(text)
        p.accept_word("plot")
        return plot(self, parse_plot(p, text), log=self.log)

    def eval(self, expr, where=None, **settings):
        """Python API: evaluate an expression. Returns a lazy Result."""
        ast = expr._ast if isinstance(expr, Handle) else expr
        overrides = {k: convert(k, v) for k, v in settings.items()}
        node, comp = self.compile(ast, overrides=overrides or None)
        sels = lang.parse_where_text(where) if isinstance(where, str) else list(where or [])
        return Result(self, node, comp, sels, overrides)

    # ---------------------------------------------------------------- variables and settings

    def define(self, name, expr, overrides=None, **settings):
        """Define a session variable. expr is text or an AST; settings apply like `with`."""
        ast = lang.parse_expression(expr) if isinstance(expr, str) else expr
        overrides = dict(overrides or {})
        overrides.update({k: convert(k, v) for k, v in settings.items()})
        check_name(name, "variable")
        if name in self.tables:
            raise GlueError(f"variable {name!r} clashes with the table of the same name")
        if name not in self.variables:
            for kind, d in (("view", self.views), ("dataset", self.datasets), ("calc", self.calcs)):
                if name in d:
                    self.log(f"  note: variable {name} hides the {kind} {name} in this session")
        ent = Entity("var", name, ast=ast, overrides=overrides or {})
        old = self.variables.get(name)
        self.variables[name] = ent
        try:
            self.compile(lang.Name(name))
        except GlueError:
            if old is None:
                del self.variables[name]
            else:
                self.variables[name] = old
            raise
        return ent

    def delete(self, name):
        if name not in self.variables:
            raise GlueError(f"{name} is not a session variable")
        del self.variables[name]

    def set_setting(self, key, node):
        if isinstance(node, str):
            try:
                node = lang.parse_optvalue_text(node)
            except GlueError:
                node = lang.Str(node)
        if key.startswith("plot."):
            k = key[5:]
            if k not in PLOT_SETTINGS:
                raise GlueError(f"unknown plot setting {k!r}. Plot settings: {', '.join(PLOT_SETTINGS)}")
            if k == "size":
                if not isinstance(node, lang.TupleLit):
                    raise GlueError("plot.size needs (width, height)")
                self.plot_defaults[k] = tuple(i.value for i in node.items)
            else:
                self.plot_defaults[k] = PLOT_SETTINGS[k][0](node)
            return
        self.settings.session[key] = convert(key, node)

    def unset_setting(self, key):
        if key.startswith("plot."):
            self.plot_defaults[key[5:]] = PLOT_SETTINGS[key[5:]][1]
        else:
            self.settings.session.pop(key, None)

    def settings_lines(self):
        out = []
        for k in SETTINGS:
            out.append(f"{k:14} {to_text(k, self.settings.get(k)):24} ({self.settings.source(k)})")
        for k, v in self.plot_defaults.items():
            out.append(f"plot.{k:9} {str(v):24}")
        return out

    # ---------------------------------------------------------------- python data

    def register(self, name, frame, by, x=None, y=None):
        self._claim(name, "table")
        frame = pd.DataFrame(frame)
        missing = [c for c in list(by) + ([x] if x else []) if c not in frame.columns]
        if missing:
            raise GlueError(f"register: columns {', '.join(missing)} not in the data")
        self.tables[name] = MemoryTable(name, frame, by, x, y)

    # ---------------------------------------------------------------- datasets and calcs

    def save(self, name, path=None, where=(), grid=None, fmt=None, unit=None, recipe_only=False, view=False):
        if isinstance(where, str):
            where = lang.parse_where_text(where)
        if isinstance(grid, str):
            from .settings import parse_grid

            grid = parse_grid(grid)
        if view:
            return self.save_view(name)
        info, report = D.save(self, name, path, where, grid, fmt, unit, recipe_only)
        replaced = None
        if info.name in self.variables:
            del self.variables[info.name]
            replaced = "variable"
        if info.name in self.views:
            del self.views[info.name]
            replaced = "view"
            if self.project_path:
                remove_toml_entry(self.project_path, "views", info.name)
        if info.name in self.tables:
            raise GlueError(f"{info.name} clashes with a table")
        section = "calcs" if recipe_only else "datasets"
        if recipe_only:
            ent = self._calc_entity(info.path, info.name)
            if replaced is None and info.name in self.calcs:
                replaced = "calc"
            self.calcs[info.name] = ent
        else:
            if replaced is None and info.name in self.datasets:
                replaced = "dataset"
            self.datasets[info.name] = D.table_from_info(info)
        if self.project_path:
            set_toml_entry(self.project_path, section, info.name, f'"{relpath(info.path, self.project_dir)}"')
        lines = report.lines(self.settings.get("report"))
        if recipe_only:
            lines.append(f"wrote calc {relpath(info.path, os.getcwd())}: {info.type.text()}")
        else:
            lines.append(f"wrote {relpath(info.path, os.getcwd())} and {relpath(info.cache_file, os.getcwd())} "
                         f"({info.curves} curves, {info.points} rows)")
        if replaced in ("dataset", "calc"):
            lines.append(f"{section[:-1]} {info.name} was overwritten")
        elif replaced:
            lines.append(f"{section[:-1]} {info.name} replaces the {replaced} of the same name")
        return lines

    def save_view(self, name):
        ent = self.variables.get(name)
        if ent is None:
            raise GlueError(f"{name} is not a session variable")
        if not self.project_path:
            raise GlueError("no project loaded, so there is nowhere to write the view")
        expr = ent.ast.src()
        if ent.overrides:
            parts = [f'expr = "{_esc(expr)}"'] + [f'{k} = "{_esc(to_text(k, v))}"' for k, v in ent.overrides.items()]
            value = "{ " + ", ".join(parts) + " }"
        else:
            value = f'"{_esc(expr)}"'
        set_toml_entry(self.project_path, "views", name, value)
        del self.variables[name]
        self.views[name] = ent
        ent.kind = "view"
        return [f"wrote view {name} to {relpath(self.project_path, os.getcwd())}"]

    def dataset_infos(self, name=None):
        if name:
            if name not in self.datasets:
                raise GlueError(f"{name} is not a dataset")
            return [self.datasets[name].info]
        return [t.info for t in self.datasets.values()]

    def status_lines(self, name=None, all=False):  # noqa: A002
        if all:
            return self.status_lines(name) + self.cache_status_lines()
        out = []
        seen = {}
        for info in self.dataset_infos(name):
            st = D.status(info, self.index_dir, seen)
            state = st.state + (" (pinned)" if st.pinned else "")
            out.append(f"{info.name:14} {state:10} " + (st.details[0] if st.details else ""))
            for d in st.details[1:]:
                out.append(f"{'':25} {d}")
        return out or ["no datasets"]

    def refresh(self, name=None, full=False, force=False):
        lines = []
        done = set()
        for info in self.dataset_infos(name):
            D.refresh(info, full=full, force=force, index_dir=self.index_dir, log=lines.append, _done=done)
        for n, t in list(self.datasets.items()):
            self.datasets[n] = D.table_from_info(D.load_info(t.info.path, n), n)
        return lines

    def pin(self, name, value=True):
        info = self.dataset_infos(name)[0]
        info.pinned = value
        info.write()
        return [f"{name} {'pinned' if value else 'unpinned'}"]

    # ---------------------------------------------------------------- invalidate, why, gc

    def invalidate(self, name, where=()):
        ent = self.lookup(name)
        if ent is None:
            raise GlueError(f"unknown name {name!r}")
        if ent.kind == "table":
            t = ent.table
            if not self.glue_dir:
                raise GlueError("invalidate needs a project folder to record it in")
            if where:
                preds = [pred_from_selector(s) for s in where]
                chunks = t.filter_chunks(preds)
                if not chunks:
                    raise GlueError(f"invalidate {name}: no files match {', '.join(p.src() for p in preds)}")
                bump(self.glue_dir, t.def_id, [c.rel for c in chunks])
                msg = f"marked {len(chunks)} of {len(t.index())} files of {name} as changed"
            else:
                bump(self.glue_dir, t.def_id)
                msg = f"marked every file of {name} as changed"
            # no need to clear the store: the marks change the fingerprints, so cached
            # values are updated from the marked files on next use
            return [msg, "values that depend on them are recomputed when next used, and saved datasets are stale"]
        if ent.kind == "dataset":
            D.invalidate(ent.table.info)
            return [f"{name} is marked invalid. refresh {name} recomputes it"]
        node, _ = self.compile(lang.Name(name))
        dropped = self.store.drop(n.id for n in node.walk())
        return [f"dropped the cached values of {name}" + (f" ({dropped} files on disk)" if dropped else "")]

    def why(self, name):
        ent = self.lookup(name)
        if ent is None:
            raise GlueError(f"unknown name {name!r}")
        lines = []
        if ent.kind == "dataset":
            info = ent.table.info
            st = D.status(info, self.index_dir)
            lines.append(f"dataset {name}: {st.state}" + (" (pinned)" if info.pinned else ""))
            for d in st.details:
                lines.append(f"  {d}")
            if not info.reproducible:
                return lines
            root, ns, _ = D.build_tree(info)
        else:
            root, _ = self.compile(lang.Name(name))
            lines.append(f"{ent.kind} {name}: {root.type.text()}")
        lines += ["  " + ln for ln in render(root)]
        ctx = self.context()
        for leaf in root.leaves():
            if isinstance(leaf, N.SourceNode):
                t = leaf.table
                from .core.store import salts

                epoch, chunks = salts(self.glue_dir, t.def_id)
                extra = []
                if epoch:
                    extra.append(f"invalidated {epoch} time(s)")
                if chunks:
                    extra.append(f"{len(chunks)} files marked changed")
                lines.append(f"  leaf {t.name}: {len(t.index())} files, fingerprint {ctx.digest(leaf)}"
                             + (f", {', '.join(extra)}" if extra else ""))
            elif isinstance(leaf, N.DatasetNode):
                st = D.status(leaf.table.info, self.index_dir)
                lines.append(f"  leaf dataset {leaf.table.name}: {st.state}")
        return lines

    def named_roots(self):
        """(kind, name, root) for every view, variable, calc, and dataset whose tree can be built."""
        out = []
        for kind, names in (("view", self.views), ("variable", self.variables)):
            for name in list(names):
                try:
                    out.append((kind, name, self.compile(lang.Name(name))[0]))
                except GlueError:
                    pass
        for name, ent in self.calcs.items():
            out.append(("calc", name, ent.root))
        for name, t in self.datasets.items():
            if t.info.reproducible:
                try:
                    out.append(("dataset", name, D.build_tree(t.info)[0]))
                except GlueError:
                    pass
        return out

    def cache_status_lines(self):
        """For status --all: the cached values under each name, and whether they match the files now."""
        ctx = self.context()
        disk = self.store.disk_dir or (os.path.join(self.glue_dir, "cache") if self.glue_dir else None)
        on_disk = {}
        if disk and os.path.isdir(disk):
            for f in os.listdir(disk):
                nid, _, rest = f.partition("-")
                on_disk.setdefault(nid, set()).add(rest.split(".")[0])
        in_mem = {}
        for key in self.store.items:
            if len(key) == 3 and not key[1]:
                in_mem.setdefault(key[0], set()).add(key[2])
        lines = []
        for kind, name, root in self.named_roots():
            fresh, stale = [], []
            for n in root.walk():
                if n.leaf or n.op not in EXPENSIVE or (n.id not in on_disk and n.id not in in_mem):
                    continue
                fp = ctx.fp(n)
                ok = fp in in_mem.get(n.id, ()) or fp[:12] in on_disk.get(n.id, ())
                (fresh if ok else stale).append(n)
            if not fresh and not stale:
                continue
            total = len(fresh) + len(stale)
            text = f"{name} ({kind}): {total} cached value{'s' if total != 1 else ''}, {len(fresh)} fresh"
            if stale:
                text += f", {len(stale)} stale"
            lines.append(text)
            for n in stale:
                changed = [lf.label() for lf in n.leaves() if isinstance(lf, N.SourceNode)]
                lines.append(f"  stale: {n.op} {n.id}" + (f" ({n.name})" if n.name else "") +
                             ", reads " + ", ".join(sorted(changed)))
        return ["cached values:"] + ["  " + x for x in lines] if lines else ["no cached values"]

    def gc(self):
        keep = set()
        for _, _, root in self.named_roots():
            keep |= {n.id for n in root.walk()}
        if self.glue_dir:
            self.store.disk_dir = os.path.join(self.glue_dir, "cache")
        removed = self.store.gc(keep)
        return [f"removed {removed} cached values that nothing uses", f"kept values for {len(keep)} nodes"]

    # ---------------------------------------------------------------- export and explain

    def export(self, node, comp, path, where=()):
        frame, ctx, _, _ = self.evaluate(node, comp, where)
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1].lower()
        key = [c for c in node.type.input_names if c != node.type.axis]
        if ext == ".csv":
            frame.to_csv(path, index=False)
        elif ext == ".parquet":
            frame.to_parquet(path, index=False)
        elif ext in (".dat", ".txt"):
            write_blocks(frame, key, path)
        else:
            raise GlueError(f"export: unknown file type {ext!r}. Use .csv, .parquet, or .dat")
        lines = ctx.report.lines(self.settings.get("report"))
        n = len(frame[key].drop_duplicates()) if key and len(frame) else (1 if len(frame) else 0)
        lines.append(f"wrote {relpath(path, os.getcwd())} ({n} curves, {len(frame)} rows)")
        return lines

    def explain(self, node, comp=None, where=()):
        preds = self.where_preds(node, where)
        ctx = self.context()
        reads, pushed = ctx.plan(node, preds)
        lines = render(node)
        if pushed:
            lines.append(f"  filters pushed to the sources: {', '.join(p.src() for p in pushed)}")
        for name, (m, n, kind) in reads.items():
            if kind == "files":
                lines.append(f"  {name}: read {m} of {n} files")
            elif kind == "sqlite":
                lines.append(f"  {name}: SQLite query")
            else:
                lines.append(f"  {name}: {kind} cache" if kind == "dataset" else f"  {name}: {kind} data")
        lines.append(f"  result: {node.type.text()}")
        return lines

    # ---------------------------------------------------------------- info

    def values(self, ast):
        if not isinstance(ast, lang.Attr):
            raise GlueError("values needs an input, as in values dmft.U")
        base, _ = self.compile(ast.obj)
        c = ast.name
        if not base.type.has_input(c):
            raise GlueError(f"{ast.obj.src()} has no input {c!r} (inputs: {', '.join(base.type.input_names)})")
        if isinstance(base, N.SourceNode) and c in base.table.path_inputs:
            vals = {ch.coords[c] for ch in base.table.index() if c in ch.coords}
        else:
            frame, _, _, _ = self.evaluate(N.points(base))
            vals = set(frame[c].tolist())
        return sorted(vals, key=lambda v: (isinstance(v, str), v))

    def info_lines(self, name):
        ent = self.lookup(name)
        if ent is None:
            raise GlueError(f"unknown name {name!r}")
        if ent.kind in ("table", "dataset"):
            t = ent.table
            lines = [f"{ent.kind} {name}: {describe_source(t)}"]
            if t.description:
                lines += [f"  {line.strip()}" for line in t.description.strip().splitlines()]
            lines.append(f"  type       {t.ftype().text()}")
            chunks = t.index()
            if t.locator_coords or t.constants:
                lines.append(f"  {_count(len(chunks), 'file')}")
            ks = t.keys([])
            lines.append(f"  {_count(len(ks), 'curve' if t.x else 'key')}")
            for c in t.inputs:
                if c == t.x:
                    continue
                src = "path" if c in t.locator_coords else "constant" if c in t.constants else "content"
                if isinstance(t, CacheTable):
                    vals = sorted(t.frame()[c].unique().tolist(), key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  input      {c:8} {len(vals):3} {'value ' if len(vals) == 1 else 'values'}: {shown}")
                elif src != "content":
                    vals = sorted({ch.coords[c] for ch in chunks}, key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  input      {c:8} {len(vals):3} {'value ' if len(vals) == 1 else 'values'}: {shown}  ({src})")
                else:
                    i = t.coords.index(c)
                    vals = sorted({k[i] for k in ks}, key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  input      {c:8} {len(vals):3} {'value ' if len(vals) == 1 else 'values'}: {shown}  ({src})")
            if t.x:
                lines.append(f"  axis       {t.x}{_unit(t, t.x)}")
            errors = {t.m(v).error for v in t.output_names() if t.m(v).error}
            for v in t.output_names():
                if v in errors:
                    continue
                err = f", error {t.m(v).error}" if t.m(v).error else ""
                lines.append(f"  output     {v}{_unit(t, v)}{err}")
            if ent.kind == "dataset":
                info = t.info
                st = D.status(info, self.index_dir)
                lines.append(f"  recipe     {info.surface}  ({len(info.nodes)} nodes)" if info.nodes
                             else f"  recipe     {info.expr or '(none)'}")
                lines.append(f"  status     {st.state}{' (pinned)' if info.pinned else ''}"
                             + (f": {st.details[0]}" if st.details else ""))
            return lines
        node, _ = self.compile(lang.Name(name))
        if ent.kind == "calc":
            lines = [f"calc {name}: {node.type.text()}", f"  from {relpath(ent.info.path, os.getcwd())}"]
            if ent.info.surface:
                lines.append(f"  surface    {ent.info.surface}")
            return lines
        lines = [f"{ent.kind} {name} = {ent.ast.src()}"]
        if ent.overrides:
            lines.append("  settings " + ", ".join(f"{k}={to_text(k, v)}" for k, v in ent.overrides.items()))
        if ent.description:
            lines.append(f"  {ent.description}")
        lines.append(f"  type {node.type.text()}" + (f", axis {node.type.axis}" if node.type.axis else ""))
        return lines

    def ls_lines(self):
        out = []
        for n, t in self.tables.items():
            what = "python" if isinstance(t, MemoryTable) else describe_source(t)
            out.append(f"table    {n:14} {what}")
        for n, e in self.views.items():
            out.append(f"view     {n:14} {e.ast.src()}")
        for n, e in self.calcs.items():
            out.append(f"calc     {n:14} {e.root.type.text()}")
        seen = {}
        for n, t in self.datasets.items():
            st = D.status(t.info, self.index_dir, seen)
            out.append(f"dataset  {n:14} {st.state}{' (pinned)' if st.pinned else ''}, {t.info.curves} curves")
        for n, e in self.variables.items():
            out.append(f"variable {n:14} {e.ast.src()}")
        return out or ["nothing loaded"]


def _unit(t, col):
    u = t.unit(col)
    return f" [{u}]" if u else ""


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write_blocks(frame, coords, path):
    """gnuplot-friendly text: one block per curve, separated by blank lines."""
    with open(path, "w") as f:
        other = [c for c in frame.columns if c not in coords]
        f.write("# " + " ".join(other) + "\n")
        if not coords:
            frame[other].to_csv(f, sep=" ", header=False, index=False)
            return
        first = True
        for key, g in frame.groupby(coords, sort=False):
            key = key if isinstance(key, tuple) else (key,)
            if not first:
                f.write("\n\n")
            first = False
            f.write("# " + ", ".join(f"{c}={v}" for c, v in zip(coords, key)) + "\n")
            g[other].to_csv(f, sep=" ", header=False, index=False)


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _int_values(pred, ftype):
    """Show filters on int inputs with int values: L=12, not L=12.0."""
    v = ftype.var(pred.name)
    if v is None or v.dtype != "int":
        return pred

    def fix(x):
        return int(x) if isinstance(x, float) and x.is_integer() else x

    value = tuple(fix(x) for x in pred.value) if isinstance(pred.value, tuple) else fix(pred.value)
    return dataclasses.replace(pred, value=value, lo=fix(pred.lo), hi=fix(pred.hi))
