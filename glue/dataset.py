"""Saved datasets: a calculation tree, its type, its cached value, and fingerprints (core.md 6)."""

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import __version__
from . import lang
from .core import LIB
from .core import nodes as N
from .core.context import Context
from .core.formula import apply_preds, pred_from_selector, predicate_text
from .core.incremental import changed_chunks, pred_sets
from .core.store import Store, find_glue_dir, load_epochs, salts
from .core.tree import dump_nodes, load_nodes
from .core.types import FType, Out, Var, curve_key
from .elaborate import Elaborator, Entity
from .errors import GlueError
from .settings import Settings, convert
from .sources import CacheTable, ColumnMeta, check_keys, check_version, digest, load_table, _meta_from
from .util import check_name, read_toml, relpath, sha256_file, toml_dumps

TOP_KEYS = {"glue", "kind", "name", "created", "tool", "pinned", "description", "reproducible", "invalid",
            "type", "recipe", "curves", "columns", "cache", "fingerprint", "meta"}


@dataclass
class DatasetInfo:
    path: str
    name: str
    kind: str = "dataset"   # dataset or calc
    version: str = "0.2"
    created: object = None
    tool: str = ""
    pinned: bool = False
    invalid: bool = False
    description: str = ""
    reproducible: bool = True
    surface: str = ""
    root: str = ""
    lib: str = LIB
    inputs: dict = field(default_factory=dict)   # label -> path relative to this file
    nodes: dict = field(default_factory=dict)    # node id -> node table
    type: FType = None
    cache_file: str = ""
    format: str = "parquet"
    sha256: str = ""
    points: int = 0
    curves: int = 0
    dropped: int = 0
    fp_mode: str = "stat"
    fingerprints: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    # version 0.1 recipes, translated on first refresh
    expr: str = ""
    settings: dict = field(default_factory=dict)
    where: str = ""

    @property
    def dir(self):
        return os.path.dirname(os.path.abspath(self.path))

    @property
    def rows(self):
        return self.points

    def input_path(self, label):
        return os.path.normpath(os.path.join(self.dir, self.inputs[label]))

    def to_toml(self):
        d = {"glue": "0.2", "kind": self.kind, "name": self.name,
             "created": self.created or dt.datetime.now(dt.timezone.utc), "tool": self.tool}
        if self.kind == "dataset":
            d["pinned"] = self.pinned
            if self.invalid:
                d["invalid"] = True
        d["description"] = self.description
        if not self.reproducible:
            d["reproducible"] = False
        d["type"] = self.type.to_toml()
        if self.reproducible:
            d["recipe"] = {"lib": self.lib, "surface": self.surface, "root": self.root,
                           "inputs": dict(self.inputs), "nodes": self.nodes}
        if self.kind == "dataset":
            d["cache"] = {"file": os.path.basename(self.cache_file), "format": self.format, "sha256": self.sha256,
                          "points": self.points, "curves": self.curves, "dropped": self.dropped}
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
    kind = data.get("kind")
    if kind not in ("dataset", "calc"):
        raise GlueError(f"{path}: expected kind = \"dataset\" or \"calc\"")
    info = DatasetInfo(path=os.path.abspath(path), name=name or data.get("name") or
                       os.path.splitext(os.path.basename(path))[0], kind=kind)
    info.version = str(data.get("glue"))
    info.created = data.get("created")
    info.tool = data.get("tool", "")
    info.pinned = bool(data.get("pinned", False))
    info.invalid = bool(data.get("invalid", False))
    info.description = data.get("description", "")
    info.reproducible = bool(data.get("reproducible", True)) and "recipe" in data
    r = data.get("recipe", {})
    info.inputs = dict(r.get("inputs", {}))
    if "nodes" in r:
        check_keys(r, {"lib", "surface", "root", "inputs", "nodes"}, f"{path} [recipe]")
        info.lib = r.get("lib", LIB)
        info.surface = r.get("surface", "")
        info.root = r.get("root", "")
        info.nodes = {k: dict(v) for k, v in r.get("nodes", {}).items()}
    else:
        check_keys(r, {"expr", "source_expr", "where", "inputs", "settings", "plugins"}, f"{path} [recipe]")
        info.version = "0.1"
        info.expr = r.get("expr", "")
        info.surface = r.get("source_expr", "") or info.expr
        info.where = r.get("where", "")
        info.settings = {k: str(v) if not isinstance(v, bool) else ("true" if v else "false")
                         for k, v in r.get("settings", {}).items()}
    if "type" in data:
        info.type = FType.from_toml(data["type"])
    else:
        c = data.get("curves", {})
        cols = {k: _meta_from(v, f"{path} [columns.{k}]") for k, v in data.get("columns", {}).items()}
        by, x = list(c.get("by", [])), c.get("x")
        ins = [Var(b, cols[b].type if b in cols else "float", True) for b in by]
        if x:
            ins.append(Var(x, "float", False, cols[x].unit if x in cols else ""))
        outs = [Out(y, cols[y].unit if y in cols else "") for y in c.get("y", [])]
        info.type = FType(tuple(ins), tuple(outs), x)
    cache = data.get("cache", {})
    info.cache_file = os.path.join(info.dir, cache.get("file", info.name + ".parquet"))
    info.format = cache.get("format", "parquet")
    info.sha256 = cache.get("sha256", "")
    info.points = cache.get("points", cache.get("rows", 0))
    info.curves = cache.get("curves", 0)
    info.dropped = cache.get("dropped", 0)
    fp = dict(data.get("fingerprint", {}))
    info.fp_mode = fp.pop("mode", "stat")
    info.fingerprints = fp
    info.meta = data.get("meta", {})
    if not info.root:
        info.root = "legacy" + hashlib.sha256((info.sha256 or info.path).encode()).hexdigest()[:6]
    return info


def table_from_info(info, name=None):
    t = CacheTable(name or info.name, info.path, info.cache_file, info.format)
    ft = info.type
    t.inputs = ft.input_names
    t.outputs_decl = ft.output_names
    t.x = ft.axis
    t.coords = [c for c in ft.input_names if c != ft.axis]
    t._values = ft.output_names
    for v in ft.inputs:
        t.meta[v.name] = ColumnMeta(type=v.dtype, unit=v.unit)
    for o in ft.outputs:
        t.meta[o.name] = ColumnMeta(unit=o.unit, label=o.label)
    t.description = info.description
    t.def_id = "dataset:" + info.root
    t.info = info
    t.ftype = lambda: info.type
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
            is_str = col.dtype == object or str(col.dtype).startswith("str")
            arrays[f"col:{c}"] = col.to_numpy(dtype=str) if is_str else col.to_numpy()
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
        return table.to_pandas(), json.loads(meta.get(b"glue.chunks", b"{}"))
    with np.load(path, allow_pickle=False) as z:
        cols = [str(c) for c in z["__glue_columns__"]]
        frame = pd.DataFrame({c: z[f"col:{c}"] for c in cols})
        chunks = json.loads(str(z["__glue_chunks__"]))
    return frame, chunks


# ------------------------------------------------------------------ building trees from files


def project_dirs(path):
    g = find_glue_dir(path)
    if g is None:
        return None, os.path.dirname(os.path.abspath(path))
    return g, os.path.dirname(g)


class RecipeNamespace:
    """Resolves a recipe's leaves using only its [recipe.inputs]."""

    def __init__(self, info):
        self.info = info
        self.glue_dir, self.base = project_dirs(info.path)
        self.index_dir = os.path.join(self.glue_dir, "index") if self.glue_dir else None
        self.entities = {}
        for label in info.inputs:
            path = info.input_path(label)
            if not os.path.exists(path):
                raise GlueError(f"{info.name}: input {label} not found at {relpath(path, os.getcwd())}")
            kind = read_toml(path).get("kind")
            if kind == "dataset":
                self.entities[label] = Entity("dataset", label, table=load_dataset(path, label))
            else:
                self.entities[label] = Entity("table", label,
                                              table=load_table(path, label, self.index_dir, base_dir=self.base))

    def lookup(self, name):
        return self.entities.get(name)

    def names(self):
        return list(self.entities)

    def resolver(self, kind, label):
        ent = self.entities.get(label)
        if ent is None:
            raise GlueError(f"{self.info.name}: the tree uses {kind} {label!r}, which isn't in [recipe.inputs]")
        return ent.table


def build_tree(info):
    """(root node, namespace, warnings) for a saved dataset or calc, from current table files."""
    ns = RecipeNamespace(info)
    if info.version == "0.1":
        from .plugins import ensure

        settings = Settings()
        for k, v in info.settings.items():
            settings.session[k] = convert(k, v)
        el = Elaborator(ns, settings)
        root = el.build(lang.parse_expression(info.expr))
        if info.where:
            root = N.filt(root, predicate_text([pred_from_selector(s) for s in lang.parse_where_text(info.where)]))
        root = el.named(root, info.type.output_names[0] if info.type.outputs else info.name)
        return root, ns, []
    root, warnings = load_nodes(info.nodes, info.root, ns.resolver)
    return root, ns, warnings


# ------------------------------------------------------------------ saving


def _entries_for(ent, mode, glue_dir, epochs):
    t = ent.table
    if ent.kind == "dataset":
        return {os.path.basename(t.cache_file): [os.path.getsize(t.cache_file), 0, t.info.sha256, ""]}
    return t.chunk_entries(mode, salts(glue_dir, t.def_id, epochs))


def _digest_for(ent, entries, mode):
    if ent.kind == "dataset":
        return "sha256:" + ent.table.info.sha256[:16]
    return digest(entries, mode)


def _count_keys(frame, root):
    part = [c for c in root.type.input_names if c in root.part]
    if not part:
        return 1 if len(frame) else 0
    return len(frame[part].drop_duplicates())


def sort_frame(frame, ftype):
    ins = [c for c in ftype.input_names if c in frame.columns]
    if ins and len(frame):
        return frame.sort_values(ins, kind="stable").reset_index(drop=True)
    return frame.reset_index(drop=True)


def _write(info, root, frame, entities, glue_dir):
    frame = sort_frame(frame, root.type)
    epochs = load_epochs(glue_dir)
    entries = {label: _entries_for(ent, info.fp_mode, glue_dir, epochs) for label, ent in entities.items()}
    info.fingerprints = {label: _digest_for(entities[label], e, info.fp_mode) for label, e in entries.items()}
    info.nodes = dump_nodes(root)
    info.root = root.id
    info.type = root.type
    info.sha256 = write_cache_file(info.cache_file, info.format, frame, {"inputs": entries})
    info.points = len(frame)
    info.curves = _count_keys(frame, root)
    info.created = dt.datetime.now(dt.timezone.utc)
    info.tool = f"glue {__version__}"
    info.invalid = False
    info.version = "0.2"
    info.write()


def _relative_inputs(entities, where):
    out = {}
    for label, ent in entities.items():
        out[label] = relpath(os.path.abspath(ent.table.path), where)
    return out


def save(session, name, path=None, where=(), grid=None, fmt=None, unit=None, recipe_only=False):
    ent = session.lookup(name)
    if ent is None:
        raise GlueError(f"unknown name {name!r}")
    if ent.kind in ("table", "dataset", "calc"):
        raise GlueError(f"{name} is a {ent.kind}. save works on variables and views. "
                        + ("Use refresh to recompute a dataset." if ent.kind == "dataset" else ""))
    base = session.project_dir or os.getcwd()
    if path is None:
        folder = "calcs" if recipe_only else session.settings.get("datasets_dir")
        path = os.path.join(folder, name)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    if path.endswith(".toml"):
        path = path[:-5]
    ds_name = os.path.basename(path)
    check_name(ds_name, "dataset name")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = fmt or session.settings.get("cache_format")

    el = Elaborator(session, session.settings)
    session.settings.push({"grid": grid} if grid is not None else {})
    try:
        root = el.build(ent.ast, overrides=ent.overrides)
    finally:
        session.settings.pop()
    if where:
        root = N.filt(root, predicate_text([pred_from_selector(s) for s in where]))
    root = el.named(root, ds_name, unit)

    info = DatasetInfo(path=os.path.abspath(path + ".toml"), name=ds_name, kind="calc" if recipe_only else "dataset")
    info.surface = ent.ast.src() if ent.kind == "var" else name
    info.description = ent.description or ""
    info.fp_mode = session.settings.get("fingerprint")
    info.cache_file = f"{path}.{fmt}"
    info.format = fmt
    ctx = session.context()
    if el.nan_error:
        ctx.nan_rows = "error"
    missing = [label for label, e in el.inputs.items() if not e.table.path]
    if missing:
        if recipe_only:
            raise GlueError(f"{name} uses {', '.join(missing)}, which came from Python and has no file, "
                            "so it can't be saved as a calc")
        return _save_snapshot(info, root, ctx)
    info.inputs = _relative_inputs(el.inputs, info.dir)
    if recipe_only:
        info.nodes = dump_nodes(root)
        info.root = root.id
        info.type = root.type
        info.created = dt.datetime.now(dt.timezone.utc)
        info.tool = f"glue {__version__}"
        info.write()
        return info, ctx.report
    frame = ctx.run(root)
    _write(info, root, frame, el.inputs, session.glue_dir)
    return info, ctx.report


def _save_snapshot(info, root, ctx):
    frame = ctx.run(root)
    info.reproducible = False
    info.type = root.type
    info.root = root.id
    info.description = info.description or "saved from Python data, no recipe"
    frame = sort_frame(frame, root.type)
    info.sha256 = write_cache_file(info.cache_file, info.format, frame, {})
    info.points = len(frame)
    info.curves = _count_keys(frame, root)
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
    changed: dict = field(default_factory=dict)  # input label -> set of changed chunk paths
    pinned: bool = False
    rebuild: bool = False                        # the tree itself changed, so recompute everything


def status(info, index_dir=None, _seen=None):
    _seen = _seen if _seen is not None else {}
    if info.path in _seen:
        return _seen[info.path]
    st = Status("fresh", pinned=info.pinned)
    _seen[info.path] = st
    if not info.reproducible:
        st.state = "no recipe"
        return st
    if info.invalid:
        st.state = "invalidated"
        st.details.append("marked with invalidate")
        return st
    if not os.path.exists(info.cache_file):
        st.state = "modified"
        st.details.append("cache file is missing")
        return st
    if sha256_file(info.cache_file) != info.sha256:
        st.state = "modified"
        st.details.append("cache file was changed after it was saved")
        return st
    glue_dir, base = project_dirs(info.path)
    epochs = load_epochs(glue_dir)
    _, meta = read_cache_file(info.cache_file, info.format)
    stored = meta.get("inputs", {})
    defs = {d.get("table"): d.get("def") for d in info.nodes.values() if d.get("op") == "source"}
    for label in info.inputs:
        path = info.input_path(label)
        if not os.path.exists(path):
            st.state = "orphaned"
            st.details.append(f"input {label} not found: {relpath(path, os.getcwd())}")
            continue
        try:
            kind = read_toml(path).get("kind")
            if kind == "dataset":
                sub = load_info(path, label)
                sub_st = status(sub, index_dir, _seen)
                if ("sha256:" + sub.sha256[:16]) != info.fingerprints.get(label):
                    st.changed[label] = {"(dataset changed)"}
                    st.details.append(f"{label}: dataset was recomputed")
                elif sub_st.state not in ("fresh", "no recipe"):
                    st.changed[label] = {"(dataset stale)"}
                    st.details.append(f"{label}: depends on a stale dataset")
                continue
            idx = os.path.join(glue_dir, "index") if glue_dir else None
            t = load_table(path, label, idx, base_dir=base)
            cur = t.chunk_entries(info.fp_mode, salts(glue_dir, t.def_id, epochs))
        except GlueError as e:
            st.state = "orphaned"
            st.details.append(f"input {label}: {e}")
            continue
        if label in defs and defs[label] and defs[label] != t.def_id:
            st.changed[label] = {"(definition changed)"}
            st.rebuild = True
            st.details.append(f"{label}: table definition changed")
            continue
        changed, added, removed = changed_chunks(stored.get(label, {}), cur, info.fp_mode)
        if changed or added or removed:
            st.changed[label] = changed | added | removed
            parts = [f"{len(v)} {w}" for v, w in ((changed, "changed"), (added, "added"), (removed, "removed")) if v]
            example = sorted(changed | added | removed)[0]
            more = ", ..." if len(st.changed[label]) > 1 else ""
            st.details.append(f"{label}: {', '.join(parts)} ({example}{more})")
    if st.state == "fresh" and st.changed:
        st.state = "stale"
    if st.state == "fresh" and info.version == "0.1":
        st.details.append("saved by glue 0.1. refresh rewrites it as a 0.2 tree")
    return st


def _pred_sets(root, st, ns):
    """For each changed chunk, the filter on root inputs that selects the curves it affects."""
    for label in st.changed:
        ent = ns.entities.get(label)
        if ent is None or ent.kind != "table":
            return None
    return pred_sets(root, st.changed)


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
    for label in info.inputs:
        path = info.input_path(label)
        if os.path.exists(path) and read_toml(path).get("kind") == "dataset":
            sub = load_info(path, label)
            if status(sub, index_dir).state in ("stale", "modified", "invalidated"):
                refresh(sub, full=full, force=force, index_dir=index_dir, log=log, _done=_done)
    st = status(info, index_dir)
    if st.state == "orphaned":
        raise GlueError(f"{info.name} can't be recomputed: " + "; ".join(st.details))
    if st.state == "fresh" and not full and info.version != "0.1":
        log(f"{info.name}: fresh, nothing to do")
        return info
    root, ns, warnings = build_tree(info)
    for old, new, op in warnings:
        log(f"{info.name}: node {old} ({op}) was edited or its source changed; its id is now {new}")
    glue_dir, _ = project_dirs(info.path)
    ctx = Context(Store(), glue_dir, info.fp_mode)
    full = (full or st.state in ("modified", "invalidated") or st.rebuild or info.version == "0.1"
            or root.id != info.root)
    sets = None if full else _pred_sets(root, st, ns)
    if sets is None:
        frame = ctx.run(root)
        _write(info, root, frame, ns.entities, glue_dir)
        log(f"{info.name}: recomputed all {info.curves} {_unit_word(root)}")
    else:
        old, _ = read_cache_file(info.cache_file, info.format)
        drop = np.zeros(len(old), dtype=bool)
        new_frames = []
        for preds in sets:
            m = np.ones(len(old), dtype=bool)
            if len(old):
                sub = apply_preds(preds, old.assign(__i=np.arange(len(old))))
                m = np.isin(np.arange(len(old)), sub["__i"].to_numpy())
            drop |= m
            new_frames.append(ctx.run(root, preds))
        frame = pd.concat([old[~drop]] + new_frames, ignore_index=True)
        _write(info, root, frame, ns.entities, glue_dir)
        fresh = pd.concat(new_frames, ignore_index=True) if new_frames else pd.DataFrame()
        keys = [c for c in curve_key(root.type) if c in fresh.columns]
        k = len(fresh.drop_duplicates(keys)) if keys and len(fresh) else min(len(fresh), 1)
        log(f"{info.name}: recomputed {k} of {info.curves} {_unit_word(root)}, {max(info.curves - k, 0)} unchanged")
    for line in ctx.report.lines():
        log("  " + line)
    return info


def _unit_word(root):
    part = [u for u in root.type.input_names if u in root.part]
    ragged = [u for u in root.type.input_names if u not in root.part]
    return "curves" if part and ragged else "keys"


def invalidate(info):
    info.invalid = True
    info.write()
