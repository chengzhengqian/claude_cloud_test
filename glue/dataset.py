"""Saved datasets: cached data plus the recipe that made it."""

import datetime as dt
import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import __version__
from . import engine as E
from . import lang
from .compiler import Compiler, Entity, Expander
from .errors import GlueError
from .settings import RECIPE_KEYS, GridSpec, Settings, convert, to_text
from .sources import (CacheTable, ColumnMeta, check_keys, check_version, digest, load_table, _meta_from)
from .util import check_name, read_toml, relpath, sha256_file, toml_dumps

TOP_KEYS = {"glue", "kind", "name", "created", "tool", "pinned", "description", "reproducible",
            "recipe", "curves", "columns", "cache", "fingerprint", "meta"}


@dataclass
class DatasetInfo:
    path: str
    name: str
    created: object = None
    tool: str = ""
    pinned: bool = False
    description: str = ""
    reproducible: bool = True
    expr: str = ""
    source_expr: str = ""
    where: str = ""
    inputs: dict = field(default_factory=dict)      # name -> path relative to the dataset file
    settings: dict = field(default_factory=dict)    # key -> text
    plugins: dict = field(default_factory=dict)     # name -> module.fn@version
    by: list = field(default_factory=list)
    x: str = None
    y: list = field(default_factory=list)
    columns: dict = field(default_factory=dict)     # name -> ColumnMeta
    cache_file: str = ""
    format: str = "parquet"
    sha256: str = ""
    rows: int = 0
    curves: int = 0
    dropped: int = 0
    fp_mode: str = "stat"
    fingerprints: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    @property
    def dir(self):
        return os.path.dirname(os.path.abspath(self.path))

    def input_path(self, name):
        return os.path.normpath(os.path.join(self.dir, self.inputs[name]))

    def to_toml(self):
        d = {"glue": "0.1", "kind": "dataset", "name": self.name,
             "created": self.created or dt.datetime.now(dt.timezone.utc), "tool": self.tool,
             "pinned": self.pinned, "description": self.description}
        if not self.reproducible:
            d["reproducible"] = False
        else:
            recipe = {"expr": self.expr, "source_expr": self.source_expr, "where": self.where,
                      "inputs": dict(self.inputs), "settings": dict(self.settings)}
            if self.plugins:
                recipe["plugins"] = dict(self.plugins)
            d["recipe"] = recipe
        curves = {"by": list(self.by)}
        if self.x:
            curves["x"] = self.x
        curves["y"] = list(self.y)
        d["curves"] = curves
        cols = {}
        for c, m in self.columns.items():
            entry = {k: getattr(m, k) for k in ("type", "unit", "label") if getattr(m, k) and not (k == "type" and m.type == "float")}
            if entry:
                cols[c] = entry
        if cols:
            d["columns"] = cols
        d["cache"] = {"file": os.path.basename(self.cache_file), "format": self.format, "sha256": self.sha256,
                      "rows": self.rows, "curves": self.curves, "dropped": self.dropped}
        if self.reproducible:
            d["fingerprint"] = {"mode": self.fp_mode, **self.fingerprints}
        if self.meta:
            d["meta"] = self.meta
        return toml_dumps(d)

    def write(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.to_toml())


def load_info(path, name=None):
    data = read_toml(path)
    check_version(data, path)
    check_keys(data, TOP_KEYS, path)
    if data.get("kind") != "dataset":
        raise GlueError(f"{path}: expected kind = \"dataset\"")
    info = DatasetInfo(path=os.path.abspath(path), name=name or data.get("name") or
                       os.path.splitext(os.path.basename(path))[0])
    info.created = data.get("created")
    info.tool = data.get("tool", "")
    info.pinned = bool(data.get("pinned", False))
    info.description = data.get("description", "")
    info.reproducible = bool(data.get("reproducible", True)) and "recipe" in data
    r = data.get("recipe", {})
    check_keys(r, {"expr", "source_expr", "where", "inputs", "settings", "plugins"}, f"{path} [recipe]")
    info.expr = r.get("expr", "")
    info.source_expr = r.get("source_expr", "")
    info.where = r.get("where", "")
    info.inputs = dict(r.get("inputs", {}))
    info.settings = {k: str(v) if not isinstance(v, bool) else ("true" if v else "false")
                     for k, v in r.get("settings", {}).items()}
    info.plugins = dict(r.get("plugins", {}))
    c = data.get("curves", {})
    info.by = list(c.get("by", []))
    info.x = c.get("x")
    info.y = list(c.get("y", []))
    for col, d in data.get("columns", {}).items():
        info.columns[col] = _meta_from(d, f"{path} [columns.{col}]")
    cache = data.get("cache", {})
    info.cache_file = os.path.join(info.dir, cache.get("file", info.name + ".parquet"))
    info.format = cache.get("format", "parquet")
    info.sha256 = cache.get("sha256", "")
    info.rows = cache.get("rows", 0)
    info.curves = cache.get("curves", 0)
    info.dropped = cache.get("dropped", 0)
    fp = dict(data.get("fingerprint", {}))
    info.fp_mode = fp.pop("mode", "stat")
    info.fingerprints = fp
    info.meta = data.get("meta", {})
    return info


def table_from_info(info, name=None):
    t = CacheTable(name or info.name, info.path, info.cache_file, info.format)
    t.coords = list(info.by)
    t.x = info.x
    t._values = list(info.y)
    t.meta = {k: ColumnMeta(**vars(v)) for k, v in info.columns.items()}
    t.description = info.description
    t.info = info
    return t


def load_dataset(path, name=None):
    return table_from_info(load_info(path, name), name)


# ------------------------------------------------------------------ cache files


def write_cache_file(path, fmt, frame, chunks_meta):
    blob = json.dumps(chunks_meta, separators=(",", ":"))
    if fmt == "parquet":
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise GlueError("saving Parquet needs pyarrow: pip install pyarrow, or set cache_format npz") from None
        table = pa.Table.from_pandas(frame, preserve_index=False)
        meta = dict(table.schema.metadata or {})
        meta[b"glue.chunks"] = blob.encode()
        pq.write_table(table.replace_schema_metadata(meta), path)
    elif fmt == "npz":
        arrays = {}
        for c in frame.columns:
            col = frame[c]
            arrays[f"col:{c}"] = col.to_numpy(dtype=str) if col.dtype == object or str(col.dtype).startswith("str") else col.to_numpy()
        with open(path, "wb") as f:
            np.savez(f, __glue_columns__=np.array(list(frame.columns)), __glue_chunks__=np.array(blob), **arrays)
    else:
        raise GlueError(f"unknown cache format {fmt!r}")
    return sha256_file(path)


def read_cache_file(path, fmt):
    if fmt == "parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise GlueError("reading Parquet needs pyarrow: pip install pyarrow") from None
        table = pq.read_table(path)
        meta = table.schema.metadata or {}
        frame = table.to_pandas()
        chunks = json.loads(meta.get(b"glue.chunks", b"{}"))
        return frame, chunks
    with np.load(path, allow_pickle=False) as z:
        cols = [str(c) for c in z["__glue_columns__"]]
        frame = pd.DataFrame({c: z[f"col:{c}"] for c in cols})
        chunks = json.loads(str(z["__glue_chunks__"]))
    return frame, chunks


# ------------------------------------------------------------------ recipes


class RecipeNamespace:
    """Resolves names in a recipe using only its [recipe.inputs]."""

    def __init__(self, info, index_dir=None):
        self.info = info
        self.entities = {}
        for name in info.inputs:
            path = info.input_path(name)
            if not os.path.exists(path):
                raise GlueError(f"dataset {info.name}: input {name} not found at {path}")
            kind = read_toml(path).get("kind")
            if kind == "dataset":
                t = load_dataset(path, name)
                self.entities[name] = Entity("dataset", name, table=t)
            else:
                t = load_table(path, name, index_dir=index_dir)
                self.entities[name] = Entity("table", name, table=t)

    def lookup(self, name):
        return self.entities.get(name)

    def names(self):
        return list(self.entities)


def recipe_settings(info):
    s = Settings()
    for k, v in info.settings.items():
        s.session[k] = convert(k, v)
    return s


def compile_recipe(info, index_dir=None):
    from .plugins import ensure

    for ref in info.plugins.values():
        ensure(ref)
    ns = RecipeNamespace(info, index_dir)
    settings = recipe_settings(info)
    comp = Compiler(ns, settings)
    node = comp.compile(lang.parse_expression(info.expr), name=info.y[0] if info.y else info.name)
    where = lang.parse_where_text(info.where)
    return node, comp, ns, settings, where


def entries_for(ent, mode):
    t = ent.table
    if ent.kind == "dataset":
        return {os.path.basename(t.cache_file): [os.path.getsize(t.cache_file), 0, t.info.sha256]}
    return t.chunk_entries(mode)


def input_digest(ent, entries, mode):
    if ent.kind == "dataset":
        return "sha256:" + ent.table.info.sha256[:16]
    return digest(entries, mode)


def _key_list(k):
    return [v for v in k]


def _write_result(info, node, frame, keys, dropped, deps, ns, report_extra=None):
    """Write cache and TOML for a finished run."""
    entries = {name: entries_for(ent, info.fp_mode) for name, ent in ns.entities.items()}
    info.fingerprints = {name: input_digest(ns.entities[name], entries[name], info.fp_mode) for name in entries}
    chunks_meta = {
        "coords": list(node.coords),
        "inputs": entries,
        "keys": [[_key_list(k), sorted([list(d) for d in deps.get(tuple(k), ())])] for k in keys],
        "dropped": [[_key_list(k), sorted([list(d) for d in deps.get(tuple(k), ())])] for k in dropped],
    }
    info.sha256 = write_cache_file(info.cache_file, info.format, frame, chunks_meta)
    info.rows = len(frame)
    info.curves = len(keys)
    info.dropped = len(dropped)
    info.created = dt.datetime.now(dt.timezone.utc)
    info.tool = f"glue {__version__}"
    info.write()


def save(session, name, path=None, where=(), grid=None, fmt=None, unit=None, recipe_only=False):
    ent = session.lookup(name)
    if ent is None:
        raise GlueError(f"unknown name {name!r}")
    if ent.kind in ("table", "dataset"):
        raise GlueError(f"{name} is a {ent.kind}. save works on variables and views. "
                        + ("Use refresh to recompute a dataset." if ent.kind == "dataset" else ""))
    if recipe_only:
        return session.save_view(name)

    expander = Expander(session)
    expr_ast = expander.expand(ent.ast)
    settings = session.settings
    settings.push(ent.overrides or {})
    if grid is not None:
        settings.push({"grid": grid})
    try:
        eff = {k: settings.get(k) for k in RECIPE_KEYS}
    finally:
        if grid is not None:
            settings.pop()
        settings.pop()
    if eff["grid"].kind == "like":
        eff["grid"] = GridSpec("like", like=expander.expand(eff["grid"].like))
        for n in lang.walk(eff["grid"].like):
            if isinstance(n, lang.Name):
                e2 = session.lookup(n.id)
                if e2 is not None and e2.kind in ("table", "dataset"):
                    expander.inputs[n.id] = e2

    base = session.project_dir or os.getcwd()
    if path is None:
        path = os.path.join(session.settings.get("datasets_dir"), name)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    if path.endswith(".toml"):
        path = path[:-5]
    ds_name = os.path.basename(path)
    check_name(ds_name, "dataset name")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = fmt or session.settings.get("cache_format")
    toml_path = path + ".toml"

    info = DatasetInfo(path=os.path.abspath(toml_path), name=ds_name)
    missing = [n for n, e in expander.inputs.items() if not e.table.path]
    if missing:
        return _save_snapshot(session, ent, info, fmt, where, unit, grid)
    info.expr = expr_ast.src()
    info.source_expr = ent.ast.src() if ent.kind == "var" else name
    info.where = ", ".join(s.src() for s in where)
    info.inputs = {n: relpath(os.path.abspath(e.table.path), info.dir) for n, e in expander.inputs.items()}
    info.settings = {k: to_text(k, v) for k, v in eff.items()}
    info.plugins = {n: o.ref() for n, o in expander.plugins.items()}
    info.cache_file = f"{path}.{fmt}"
    info.format = fmt
    info.fp_mode = session.settings.get("fingerprint")
    info.description = ent.description or ""

    node, comp, ns, rs, where_sels = compile_recipe(info, session.index_dir)
    ctx = E.Context(rs, comp.like_nodes)
    frame, keys, dropped, deps = E.run(node, ctx, where_sels)
    info.by = list(node.coords)
    info.x = node.xname
    info.y = [E.result_names(node)]
    info.columns = {}
    for c in node.coords:
        info.columns[c] = ColumnMeta(type=node.ctype(c))
    for t in ns.entities.values():
        tab = t.table
        if node.xname and tab.x == node.xname and node.xname not in info.columns:
            info.columns[node.xname] = ColumnMeta(unit=tab.unit(node.xname), label=tab.m(node.xname).label)
    info.columns[info.y[0]] = ColumnMeta(unit=unit if unit is not None else node.unit)
    _write_result(info, node, frame, keys, dropped, deps, ns)
    return info, ctx.report


def _save_snapshot(session, ent, info, fmt, where, unit, grid):
    """Save data that depends on Python-registered tables: no recipe, can't be recomputed."""
    node, comp = session.compile(lang.Name(ent.name), overrides={"grid": grid} if grid is not None else None)
    frame, ctx, keys, dropped = session.evaluate(node, comp, where)
    info.reproducible = False
    info.by = list(node.coords)
    info.x = node.xname
    info.y = [E.result_names(node)]
    if info.y[0] != info.name and info.y[0] in frame.columns:
        frame = frame.rename(columns={info.y[0]: info.name})
        info.y = [info.name]
    info.columns = {c: ColumnMeta(type=node.ctype(c)) for c in node.coords}
    info.columns[info.y[0]] = ColumnMeta(unit=unit if unit is not None else node.unit)
    info.cache_file = os.path.splitext(info.path)[0] + f".{fmt}"
    info.format = fmt
    info.description = ent.description or "saved from Python data, no recipe"
    info.sha256 = write_cache_file(info.cache_file, fmt, frame, {"coords": list(node.coords)})
    info.rows, info.curves, info.dropped = len(frame), len(keys), len(dropped)
    info.created = dt.datetime.now(dt.timezone.utc)
    info.tool = f"glue {__version__}"
    info.write()
    ctx.report.note("saved without a recipe, because it uses data registered from Python")
    return info, ctx.report


# ------------------------------------------------------------------ status and refresh


@dataclass
class Status:
    state: str
    details: list = field(default_factory=list)
    changed: dict = field(default_factory=dict)  # input -> set of changed rels
    pinned: bool = False


def status(info, index_dir=None, _seen=None):
    _seen = _seen if _seen is not None else {}
    if info.path in _seen:
        return _seen[info.path]
    st = Status("fresh", pinned=info.pinned)
    _seen[info.path] = st
    if not info.reproducible:
        st.state = "no recipe"
        return st
    if not os.path.exists(info.cache_file):
        st.state = "modified"
        st.details.append("cache file is missing")
        return st
    if sha256_file(info.cache_file) != info.sha256:
        st.state = "modified"
        st.details.append("cache file was changed after it was saved")
        return st
    _, meta = read_cache_file(info.cache_file, info.format)
    stored = meta.get("inputs", {})
    for name in info.inputs:
        path = info.input_path(name)
        if not os.path.exists(path):
            st.state = "orphaned"
            st.details.append(f"input {name} not found: {relpath(path, os.getcwd())}")
            continue
        try:
            kind = read_toml(path).get("kind")
            if kind == "dataset":
                sub = load_info(path, name)
                sub_st = status(sub, index_dir, _seen)
                if ("sha256:" + sub.sha256[:16]) != info.fingerprints.get(name):
                    st.changed[name] = {"(dataset changed)"}
                    st.details.append(f"{name}: dataset was recomputed")
                elif sub_st.state == "stale":
                    st.changed[name] = {"(dataset stale)"}
                    st.details.append(f"{name}: depends on a stale dataset")
                continue
            t = load_table(path, name, index_dir=index_dir)
            cur = t.chunk_entries(info.fp_mode)
        except GlueError as e:
            st.state = "orphaned"
            st.details.append(f"input {name}: {e}")
            continue
        old = stored.get(name, {})
        idx = 2 if info.fp_mode == "hash" else 1

        def same(a, b):
            return a[0] == b[0] and a[idx] == b[idx]

        changed = {r for r in cur if r in old and not same(cur[r], old[r])}
        added = set(cur) - set(old)
        removed = set(old) - set(cur)
        if changed or added or removed:
            st.changed[name] = changed | added | removed
            parts = []
            if changed:
                parts.append(f"{len(changed)} changed")
            if added:
                parts.append(f"{len(added)} added")
            if removed:
                parts.append(f"{len(removed)} removed")
            example = sorted(changed | added | removed)[0]
            st.details.append(f"{name}: {', '.join(parts)} ({example}{', ...' if len(st.changed[name]) > 1 else ''})")
    if st.state == "fresh" and st.changed:
        st.state = "stale"
    return st


def refresh(info, full=False, force=False, index_dir=None, log=print, _done=None):
    _done = _done if _done is not None else set()
    if info.path in _done:
        return info
    _done.add(info.path)
    if info.pinned and not force:
        log(f"{info.name}: pinned, skipped (use --force)")
        return info
    if not info.reproducible:
        raise GlueError(f"{info.name} has no recipe and can't be recomputed")
    for name in info.inputs:
        path = info.input_path(name)
        if os.path.exists(path) and read_toml(path).get("kind") == "dataset":
            sub = load_info(path, name)
            if status(sub, index_dir).state in ("stale", "modified"):
                refresh(sub, full=full, force=force, index_dir=index_dir, log=log, _done=_done)
    st = status(info, index_dir)
    if st.state == "orphaned":
        raise GlueError(f"{info.name} can't be recomputed: " + "; ".join(st.details))
    if st.state == "fresh" and not full:
        log(f"{info.name}: fresh, nothing to do")
        return info
    full = full or st.state == "modified"

    node, comp, ns, rs, where = compile_recipe(info, index_dir)
    ctx = E.Context(rs, comp.like_nodes)
    coord_sels, _ = E.check_where(node, where)
    plan = ctx.keys(node, coord_sels)
    if full:
        frame, keys, dropped, deps = E.run(node, ctx, where, keys=plan)
        _write_result(info, node, frame, keys, dropped, deps, ns)
        log(f"{info.name}: recomputed all {len(keys)} curves")
        return info

    old_frame, meta = read_cache_file(info.cache_file, info.format)
    old_deps = {tuple(k): {tuple(d) for d in ds} for k, ds in meta.get("keys", [])}
    old_dropped = {tuple(k): {tuple(d) for d in ds} for k, ds in meta.get("dropped", [])}
    known = {**old_deps, **old_dropped}
    changed = {(name, r) for name, rels in st.changed.items() for r in rels}
    changed_inputs = {name for name, rels in st.changed.items() if any(r.startswith("(") for r in rels)}

    def dirty(k):
        if k not in known:
            return True
        ds = known[k]
        return bool(ds & changed) or any(d[0] in changed_inputs for d in ds)

    todo = [k for k in plan if dirty(k)]
    keep = [k for k in plan if not dirty(k) and k in old_deps]
    new_frame, new_keys, new_dropped, new_deps = E.run(node, ctx, where, keys=todo)
    coords = list(node.coords)
    keep_set = set(keep)
    if coords and len(old_frame):
        tuples = [tuple(E._py_key(v) for v in row) for row in old_frame[coords].itertuples(index=False, name=None)]
        mask = np.array([t in keep_set for t in tuples], dtype=bool)
        kept = old_frame[mask]
    else:
        kept = old_frame if keep else old_frame.iloc[0:0]
    frame = pd.concat([kept, new_frame], ignore_index=True) if len(new_frame) else kept.reset_index(drop=True)
    if coords and len(frame):
        order = {k: i for i, k in enumerate(plan)}
        frame["__o"] = [order.get(tuple(E._py_key(v) for v in row), 1 << 30)
                        for row in frame[coords].itertuples(index=False, name=None)]
        frame = frame.sort_values("__o", kind="stable").drop(columns="__o").reset_index(drop=True)
    all_keys = [k for k in plan if k in keep_set or k in set(new_keys)]
    deps = {k: old_deps[k] for k in keep}
    deps.update(new_deps)
    dropped_all = [k for k in plan if k in set(new_dropped) or (k in old_dropped and not dirty(k))]
    for k in dropped_all:
        deps.setdefault(k, old_dropped.get(k, set()))
    _write_result(info, node, frame, all_keys, dropped_all, deps, ns)
    removed = len([k for k in old_deps if k not in set(plan)])
    msg = f"{info.name}: recomputed {len(todo)} of {len(plan)} curves, {len(keep)} unchanged"
    if removed:
        msg += f", {removed} removed"
    log(msg)
    for line in ctx.report.lines():
        log("  " + line)
    return info
