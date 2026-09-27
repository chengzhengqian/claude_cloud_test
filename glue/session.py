"""A session: loaded tables, views, datasets, variables, and settings."""

import json
import os
import sys

import numpy as np
import pandas as pd

from . import dataset as D
from . import engine as E
from . import lang
from .compiler import Compiler, Entity
from .errors import GlueError
from .plugins import load_modules
from .settings import PLOT_SETTINGS, SETTINGS, Settings, convert, convert_options, to_text
from .sources import (READ_CACHE, CacheTable, ColumnMeta, SqliteTable, check_keys, check_version,
                      describe_source, load_table)
from .util import (check_name, expand_path, read_toml, relpath, remove_toml_entry, set_toml_entry)

PROJECT_KEYS = {"glue", "kind", "name", "description", "tables", "include", "views", "datasets",
                "settings", "plot", "plugins", "meta"}


class MemoryTable(CacheTable):
    """Data registered from Python. It has no file, so it can't be part of a recipe."""

    def __init__(self, name, frame, by, x=None, y=None):
        super().__init__(name, None, "", "memory")
        self._frame = frame
        self.coords = list(by)
        self.x = x
        self._values = list(y) if y else [c for c in frame.columns if c not in by and c != x]
        self.is_dataset = False
        for c in by:
            self.m(c).type = "str" if frame[c].dtype == object or str(frame[c].dtype).startswith("str") else "float"

    def frame(self):
        return self._frame

    def _load_index(self):
        from .sources import Chunk

        return [Chunk(f"<python:{self.name}>", "", {})]

    def keys(self, selectors, explain=None):
        if explain is not None:
            explain[self.name] = (1, 1)
        return super().keys(selectors, None)

    def chunk_entries(self, mode="stat"):
        return {}


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

    def to_pandas(self):
        if self._frame is None:
            frame, ctx, _, _ = self.session.evaluate(self.node, self.comp, self.where)
            self.report = ctx.report.lines(self.session.settings.get("report"))
            self._frame = frame
        return self._frame

    def to_numpy(self):
        return self.to_pandas().to_numpy()

    def __repr__(self):
        return f"<glue result {self.node.text}: coordinates {', '.join(self.node.coords)}>"


class Session:
    def __init__(self, trust=False, log=print, warn_stale=True):
        self.warn_stale = warn_stale
        self.tables = {}
        self.datasets = {}
        self.views = {}
        self.variables = {}
        self.settings = Settings()
        self.plot_defaults = {k: v[1] for k, v in PLOT_SETTINGS.items()}
        self.project_path = None
        self.project_dir = None
        self.index_dir = None
        self.loaded = []
        self.trust = trust
        self.log = log
        self.figure = None

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
        return None

    def names(self):
        return list(self.variables) + list(self.views) + list(self.tables) + list(self.datasets)

    def _claim(self, name, what):
        check_name(name, what)
        for kind, d in (("table", self.tables), ("view", self.views), ("dataset", self.datasets)):
            if name in d:
                raise GlueError(f"{what} {name!r} clashes with the {kind} of the same name")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self.lookup(name) is None:
            raise AttributeError(f"no table, view, dataset, or variable named {name!r}")
        return Handle(self, lang.Name(name))

    # ---------------------------------------------------------------- loading

    def load(self, path):
        path = os.path.abspath(path)
        data = read_toml(path)
        kind = data.get("kind")
        if kind == "project":
            self._load_project(path, data)
        elif kind == "table":
            if self.index_dir is None:
                self.index_dir = os.path.join(os.path.dirname(path), ".glue", "index")
            t = load_table(path, index_dir=self.index_dir)
            self._claim(t.name, "table")
            self.tables[t.name] = t
            self.log(f"loaded table {t.name}")
        elif kind == "dataset":
            t = D.load_dataset(path)
            self._claim(t.name, "dataset")
            self.datasets[t.name] = t
            self.log(f"loaded dataset {t.name}")
            self._warn_status(t)
        else:
            raise GlueError(f"{path}: unknown kind {kind!r}. Use table, project, or dataset")
        self.loaded.append(path)

    def _load_project(self, path, data, prefix="", included=False):
        check_version(data, path)
        check_keys(data, PROJECT_KEYS, path)
        base = os.path.dirname(path)
        if not included:
            if self.project_path is not None and self.project_path != path:
                self.log(f"note: merging project {relpath(path, os.getcwd())} into {relpath(self.project_path, os.getcwd())}")
            if self.project_path is None:
                self.project_path = path
                self.project_dir = base
                self.index_dir = os.path.join(base, ".glue", "index")
                self.settings = Settings(data.get("settings", {}))
                plot = data.get("plot", {})
                check_keys(plot, set(PLOT_SETTINGS), f"{path} [plot]")
                for k, v in plot.items():
                    if k.startswith("x_"):
                        continue
                    conv = PLOT_SETTINGS[k][0]
                    self.plot_defaults[k] = tuple(v) if k == "size" else conv(v)
        renames = {}
        tables = data.get("tables", {})
        datasets = data.get("datasets", {})
        views = data.get("views", {})
        for n in list(tables) + list(datasets) + list(views):
            renames[n] = prefix + n
        for n, spec in tables.items():
            file = spec if isinstance(spec, str) else spec.get("file")
            if not file:
                raise GlueError(f"{path} [tables]: {n} needs a file")
            t = load_table(expand_path(file, base), prefix + n, index_dir=self.index_dir)
            self._claim(t.name, "table")
            self.tables[t.name] = t
        for n, file in datasets.items():
            t = D.load_dataset(expand_path(file, base), prefix + n)
            self._claim(t.name, "dataset")
            self.datasets[t.name] = t
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
            n_t, n_v, n_d = len(self.tables), len(self.views), len(self.datasets)
            name = data.get("name") or os.path.basename(base)
            self.log(f"loaded project {name}: {n_t} tables, {n_v} views, {n_d} datasets")
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
        for f in files:
            self.load(f)
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
                       + (f", coordinates {desc}" if desc else ""))
        return out

    # ---------------------------------------------------------------- compile and evaluate

    def compile(self, expr, name=None, overrides=None, unit=None):
        ast = lang.parse_expression(expr) if isinstance(expr, str) else expr
        comp = Compiler(self, self.settings)
        return comp.compile(ast, name=name, overrides=overrides, unit=unit), comp

    def evaluate(self, node, comp, where=(), limit=None, keys=None):
        self.settings.push({})
        try:
            READ_CACHE.limit = self.settings.get("read_cache_mb") << 20
            ctx = E.Context(self.settings, comp.like_nodes)
            frame, done, dropped, _ = E.run(node, ctx, where, keys=keys, limit=limit)
        finally:
            self.settings.pop()
        return frame, ctx, done, dropped

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
            for kind, d in (("view", self.views), ("dataset", self.datasets)):
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

    # ---------------------------------------------------------------- datasets

    def save(self, name, path=None, where=(), grid=None, fmt=None, unit=None, recipe_only=False):
        if isinstance(where, str):
            where = lang.parse_where_text(where)
        if isinstance(grid, str):
            from .settings import parse_grid

            grid = parse_grid(grid)
        if recipe_only:
            return self.save_view(name)
        info, report = D.save(self, name, path, where, grid, fmt, unit, recipe_only)
        t = D.table_from_info(info)
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
            raise GlueError(f"dataset name {info.name} clashes with a table")
        if replaced is None and info.name in self.datasets:
            replaced = "dataset"
        self.datasets[info.name] = t
        if self.project_path:
            set_toml_entry(self.project_path, "datasets", info.name,
                           f'"{relpath(info.path, self.project_dir)}"')
        lines = report.lines(self.settings.get("report"))
        lines.append(f"wrote {relpath(info.path, os.getcwd())} and {relpath(info.cache_file, os.getcwd())} "
                     f"({info.curves} curves, {info.rows} rows)")
        if replaced == "dataset":
            lines.append(f"dataset {info.name} was overwritten")
        elif replaced:
            lines.append(f"dataset {info.name} replaces the {replaced} of the same name")
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

    def status_lines(self, name=None):
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
        infos = self.dataset_infos(name)
        done = set()
        for info in infos:
            D.refresh(info, full=full, force=force, index_dir=self.index_dir, log=lines.append, _done=done)
        for n, t in list(self.datasets.items()):
            self.datasets[n] = D.table_from_info(D.load_info(t.info.path, n), n)
        return lines

    def pin(self, name, value=True):
        info = self.dataset_infos(name)[0]
        info.pinned = value
        info.write()
        return [f"{name} {'pinned' if value else 'unpinned'}"]

    # ---------------------------------------------------------------- export and explain

    def export(self, node, comp, path, where=()):
        frame, ctx, done, _ = self.evaluate(node, comp, where)
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            frame.to_csv(path, index=False)
        elif ext == ".parquet":
            frame.to_parquet(path, index=False)
        elif ext in (".dat", ".txt"):
            write_blocks(frame, list(node.coords), path)
        else:
            raise GlueError(f"export: unknown file type {ext!r}. Use .csv, .parquet, or .dat")
        lines = ctx.report.lines(self.settings.get("report"))
        lines.append(f"wrote {relpath(path, os.getcwd())} ({len(done)} curves, {len(frame)} rows)")
        return lines

    def explain(self, node, comp, where=()):
        ctx = E.Context(self.settings, comp.like_nodes)
        ctx.explain = {}
        coord_sels, _ = E.check_where(node, where)
        self.settings.push({})
        try:
            keys = ctx.keys(node, coord_sels)
            out = {"settings": self.settings}
            node.explain(out)
        finally:
            self.settings.pop()
        lines = [node.text if not isinstance(node, E.Named) else f"{node.text} = {node.node.text}"]
        for step in out.get("steps", []):
            lines.append(f"  {step}")
        cols = out.get("columns", {})
        for name, (m, n) in ctx.explain.items():
            t = self.tables.get(name) or self.datasets.get(name)
            c = ", ".join(sorted(cols.get(name, [])))
            if isinstance(t, SqliteTable):
                lines.append(f"  {name}: SQLite query, columns {c}")
            elif isinstance(t, CacheTable):
                lines.append(f"  {name}: dataset cache, columns {c}")
            else:
                lines.append(f"  {name}: read {m} of {n} files, columns {c}")
        n = "?" if keys is E.ANY else len(keys)
        lines.append(f"  result: {n} {'curves' if node.kind == 'curves' else 'keys'} on ({', '.join(node.coords)})")
        return lines

    # ---------------------------------------------------------------- info

    def info_lines(self, name):
        ent = self.lookup(name)
        if ent is None:
            raise GlueError(f"unknown name {name!r}")
        if ent.kind in ("table", "dataset"):
            t = ent.table
            lines = [f"{ent.kind} {name}: {describe_source(t)}"]
            if t.description:
                lines.append(f"  {t.description}")
            chunks = t.index()
            if t.locator_coords or t.constants:
                lines.append(f"  {len(chunks)} files")
            ks = t.keys([])
            lines.append(f"  {len(ks)} {'curves' if t.x else 'keys'}")
            for c in t.coords:
                src = "path" if c in t.locator_coords else "constant" if c in t.constants else "content"
                if isinstance(t, CacheTable):
                    vals = sorted(t.frame()[c].unique().tolist(), key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  coordinate {c:8} {len(vals):3} values: {shown}")
                elif c in t.locator_coords or c in t.constants:
                    vals = sorted({ch.coords[c] for ch in chunks}, key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  coordinate {c:8} {len(vals):3} values: {shown}  ({src})")
                else:
                    ks = t.keys([])
                    i = t.coords.index(c)
                    vals = sorted({k[i] for k in ks}, key=lambda v: (isinstance(v, str), v))
                    shown = ", ".join(str(v) for v in vals[:8]) + (", ..." if len(vals) > 8 else "")
                    lines.append(f"  coordinate {c:8} {len(vals):3} values: {shown}  ({src})")
            if t.x:
                lines.append(f"  x          {t.x}{_unit(t, t.x)}")
            for v in t.values:
                err = f", error {t.m(v).error}" if t.m(v).error else ""
                lines.append(f"  value      {v}{_unit(t, v)}{err}")
            if ent.kind == "dataset":
                info = t.info
                st = D.status(info, self.index_dir)
                lines.append(f"  recipe     {info.expr}")
                if info.where:
                    lines.append(f"  where      {info.where}")
                lines.append(f"  settings   " + ", ".join(f"{k}={v}" for k, v in info.settings.items()))
                lines.append(f"  status     {st.state}{' (pinned)' if info.pinned else ''}"
                             + (f": {st.details[0]}" if st.details else ""))
            return lines
        node, _ = self.compile(lang.Name(name))
        lines = [f"{ent.kind} {name} = {ent.ast.src()}"]
        if ent.overrides:
            lines.append("  settings " + ", ".join(f"{k}={to_text(k, v)}" for k, v in ent.overrides.items()))
        if ent.description:
            lines.append(f"  {ent.description}")
        lines.append(f"  kind {node.kind}, coordinates ({', '.join(node.coords)})"
                     + (f", x {node.xname}" if node.xname else "") + (f", unit {node.unit}" if node.unit else ""))
        return lines

    def ls_lines(self):
        out = []
        for n, t in self.tables.items():
            what = "python" if isinstance(t, MemoryTable) else describe_source(t)
            out.append(f"table    {n:14} {what}")
        for n, e in self.views.items():
            out.append(f"view     {n:14} {e.ast.src()}")
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
