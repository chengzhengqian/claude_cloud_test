"""Tables backed by files, SQLite, HDF5, or dataset caches.

A table lists its chunks without reading them (the chunk index), and reads
one curve at a time on request."""

import json
import os
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .errors import GlueError
from .template import Template
from .util import check_name, expand_path, normalize, read_toml, relpath, sha256_file

SPEC_VERSION = (0, 1)


def check_version(data, path):
    v = data.get("glue")
    if v is None:
        raise GlueError(f"{path}: missing `glue = \"0.1\"` version line")
    try:
        major, minor = (int(p) for p in str(v).split(".")[:2])
    except ValueError:
        raise GlueError(f"{path}: bad version {v!r}") from None
    if major != SPEC_VERSION[0]:
        raise GlueError(f"{path}: spec version {v} is not supported (this is {SPEC_VERSION[0]}.{SPEC_VERSION[1]})")
    if minor > SPEC_VERSION[1]:
        print(f"warning: {path} uses spec version {v}, newer than {SPEC_VERSION[0]}.{SPEC_VERSION[1]}")


def check_keys(d, allowed, where):
    for k in d:
        if k in allowed or k.startswith("x_"):
            continue
        raise GlueError(f"{where}: unknown key {k!r}. Allowed: {', '.join(sorted(allowed))}")


# ------------------------------------------------------------------ read cache


class ReadCache:
    def __init__(self, limit_mb=512):
        self.limit = limit_mb * 1 << 20
        self.used = 0
        self.items = OrderedDict()

    def get(self, key):
        v = self.items.get(key)
        if v is None:
            return None
        self.items.move_to_end(key)
        return v[0]

    def put(self, key, df):
        size = int(df.memory_usage(deep=False).sum())
        if key in self.items:
            self.used -= self.items.pop(key)[1]
        self.items[key] = (df, size)
        self.used += size
        while self.used > self.limit and len(self.items) > 1:
            _, (_, s) = self.items.popitem(last=False)
            self.used -= s

    def clear(self):
        self.items.clear()
        self.used = 0


READ_CACHE = ReadCache()


# ------------------------------------------------------------------ chunks


@dataclass
class Chunk:
    rel: str          # path relative to the table root, used in fingerprints
    path: str         # file on disk
    coords: dict      # locator coordinates, normalized
    internal: str = ""  # object path inside an HDF5 file

    def stat(self):
        st = os.stat(self.path)
        return st.st_size, st.st_mtime_ns


def match_selector(sel, value, ctype="float", digits=10):
    op = sel.op
    if op == "range":
        if ctype == "str":
            raise GlueError(f"a range doesn't work on string coordinate {sel.name}")
        return (sel.lo is None or value >= sel.lo) and (sel.hi is None or value <= sel.hi)
    target = sel.value
    if ctype != "str" and isinstance(target, str):
        raise GlueError(f"{sel.name} is a number, but the selector compares it with the string {target!r}")
    if ctype == "str" and not isinstance(target, (str, list)):
        target = str(target)

    def norm(t):
        return normalize(t, ctype, digits) if ctype != "str" else str(t)

    if op == "=":
        if isinstance(target, list):
            return value in [norm(t) for t in target]
        return value == norm(target)
    if op == "!=":
        if isinstance(target, list):
            return value not in [norm(t) for t in target]
        return value != norm(target)
    t = norm(target)
    return {"<": value < t, "<=": value <= t, ">": value > t, ">=": value >= t}[op]


def x_mask(x, selectors):
    mask = np.ones(len(x), dtype=bool)
    for s in selectors:
        if s.op == "range":
            if s.lo is not None:
                mask &= x >= s.lo
            if s.hi is not None:
                mask &= x <= s.hi
        elif s.op == "=":
            vals = s.value if isinstance(s.value, list) else [s.value]
            mask &= np.isin(x, vals)
        elif s.op == "!=":
            vals = s.value if isinstance(s.value, list) else [s.value]
            mask &= ~np.isin(x, vals)
        else:
            mask &= {"<": x < s.value, "<=": x <= s.value, ">": x > s.value, ">=": x >= s.value}[s.op]
    return mask


# ------------------------------------------------------------------ readers

READER_KEYS = {
    "text": {"format", "columns", "delimiter", "comment", "skip_rows", "max_rows", "transpose", "na"},
    "csv": {"format", "columns", "delimiter", "comment", "skip_rows", "max_rows", "transpose", "na", "header", "rename"},
    "npy": {"format", "columns", "transpose"},
    "raw": {"format", "columns", "dtype", "header_bytes", "order", "endian"},
    "parquet": {"format", "columns", "rename"},
    "dataset": {"format", "dataset", "columns", "fields", "transpose"},
}


def _name_columns(df, names, chunk):
    if df.shape[1] != len(names):
        raise GlueError(f"{chunk.path}: file has {df.shape[1]} columns, but the table lists {len(names)} "
                        f"({', '.join(names)})")
    df.columns = names
    return df[[n for n in names if n != "_"]]


def read_text(spec, chunk, csv=False):
    header = spec.get("header", True) if csv else False
    delim = spec.get("delimiter")
    sep = delim if delim else ("," if csv else r"\s+")
    na = spec.get("na", ["nan", "NaN"])
    try:
        df = pd.read_csv(
            chunk.path, sep=sep, header=0 if header else None, comment=spec.get("comment", "#"),
            skiprows=spec.get("skip_rows", 0), nrows=spec.get("max_rows"), na_values=na,
            keep_default_na=False, skipinitialspace=True,
        )
    except pd.errors.EmptyDataError:
        cols = spec.get("columns") or []
        return pd.DataFrame({c: pd.Series(dtype=float) for c in cols if c != "_"})
    except (pd.errors.ParserError, UnicodeDecodeError) as e:
        raise GlueError(f"{chunk.path}: can't parse as text data: {e}") from None
    if spec.get("transpose"):
        df = df.T.reset_index(drop=not header)
    if header:
        df = df.rename(columns={k: v for k, v in spec.get("rename", {}).items()})
        if spec.get("columns"):
            df = _name_columns(df, spec["columns"], chunk)
        return df
    return _name_columns(df, spec["columns"], chunk)


def read_npy(spec, chunk):
    arr = np.load(chunk.path, allow_pickle=False)
    if arr.ndim == 1:
        arr = arr[:, None]
    if spec.get("transpose"):
        arr = arr.T
    return _name_columns(pd.DataFrame(arr), spec["columns"], chunk)


def read_raw(spec, chunk):
    names = spec["columns"]
    dt = np.dtype(spec.get("dtype", "float64")).newbyteorder("<" if spec.get("endian", "little") == "little" else ">")
    arr = np.fromfile(chunk.path, dtype=dt, offset=spec.get("header_bytes", 0))
    n = len(names)
    if arr.size % n:
        raise GlueError(f"{chunk.path}: {arr.size} values don't split into {n} columns")
    arr = arr.reshape(-1, n) if spec.get("order", "rows") == "rows" else arr.reshape(n, -1).T
    return _name_columns(pd.DataFrame(arr.astype(dt.newbyteorder("="))), names, chunk)


def read_parquet(spec, chunk):
    try:
        df = pd.read_parquet(chunk.path)
    except ImportError:
        raise GlueError("reading Parquet needs pyarrow: pip install pyarrow") from None
    return df.rename(columns=spec.get("rename", {}))


def read_hdf5_object(spec, chunk):
    h5py = _h5py()
    with h5py.File(chunk.path, "r") as f:
        obj = f[chunk.internal]
        if spec.get("dataset"):
            if spec["dataset"] not in obj:
                raise GlueError(f"{chunk.path}:{chunk.internal} has no dataset {spec['dataset']!r}")
            obj = obj[spec["dataset"]]
        data = obj[()]
    if data.dtype.names:
        fields = spec.get("fields") or {n: n for n in data.dtype.names}
        return pd.DataFrame({new: data[old] for old, new in fields.items()})
    if data.ndim == 1:
        data = data[:, None]
    if spec.get("transpose"):
        data = data.T
    return _name_columns(pd.DataFrame(data), spec["columns"], chunk)


def _h5py():
    try:
        import h5py
    except ImportError:
        raise GlueError("reading HDF5 needs h5py: pip install h5py") from None
    return h5py


READERS = {
    "text": read_text,
    "csv": lambda s, c: read_text(s, c, csv=True),
    "npy": read_npy,
    "raw": read_raw,
    "parquet": read_parquet,
    "dataset": read_hdf5_object,
}


# ------------------------------------------------------------------ tables


@dataclass
class ColumnMeta:
    type: str = "float"
    unit: str = ""
    label: str = ""
    description: str = ""
    digits: int = 10
    error: str = ""


class SourceTable:
    """A table read from chunks listed by a locator."""

    is_dataset = False

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
        self.description = ""
        self.meta = {}           # column name -> ColumnMeta
        self.coords = []
        self.locator_coords = []
        self.constants = {}
        self.x = None
        self._values = None
        self.reader = {}
        self._columns = None     # content column names
        self._index = None
        self._key_chunks = {}
        self.skipped = 0
        self.index_dirs = {}
        self.index_path = None

    # --- schema

    def m(self, col):
        return self.meta.setdefault(col, ColumnMeta())

    def ctype(self, col):
        return self.m(col).type

    def digits(self, col):
        return self.m(col).digits

    @property
    def kind(self):
        return "curves" if self.x else "keyed"

    @property
    def content_coords(self):
        return [c for c in self.coords if c not in self.locator_coords and c not in self.constants]

    def columns(self):
        """Content column names, reading a file header or schema if needed."""
        if self._columns is None:
            self._columns = self._discover_columns()
        return self._columns

    def _discover_columns(self):
        cols = self.reader.get("columns")
        if cols:
            return [c for c in cols if c != "_"]
        chunks = self.index()
        if not chunks:
            return []
        return list(self.read_chunk(chunks[0]).columns)

    @property
    def errors(self):
        return {c: m.error for c, m in self.meta.items() if m.error}

    @property
    def values(self):
        if self._values is not None:
            return self._values
        err = set(self.errors.values())
        return [c for c in self.columns() if c not in self.coords and c != self.x and c not in err]

    def has_column(self, col):
        return col in self.coords or col == self.x or col in self.columns()

    def label(self, col):
        m = self.m(col)
        return m.label or col

    def unit(self, col):
        return self.m(col).unit

    # --- index

    def index(self):
        if self._index is None:
            self._index = self._load_index()
        return self._index

    def rescan(self):
        self._index = None
        self._key_chunks = {}
        self._columns = None
        if self.index_path and os.path.exists(self.index_path):
            os.remove(self.index_path)
        return self.index()

    def _load_index(self):
        raise NotImplementedError

    # --- reading

    def read_chunk(self, chunk):
        size, mtime = chunk.stat()
        key = (chunk.path, chunk.internal, size, mtime)
        df = READ_CACHE.get(key)
        if df is None:
            df = READERS[self.reader.get("format", "text")](self.reader, chunk)
            for c in df.columns:
                if c in self.coords and self.ctype(c) == "str":
                    df[c] = df[c].astype(str)
                elif df[c].dtype == object or str(df[c].dtype).startswith(("str", "string")):
                    try:
                        df[c] = pd.to_numeric(df[c])
                    except (ValueError, TypeError):
                        raise GlueError(f"{chunk.path}: column {c} has values that aren't numbers") from None
            for c in self.content_coords:
                if c not in df.columns:
                    raise GlueError(f"{chunk.path}: coordinate column {c} not found")
                if self.ctype(c) == "float":
                    df[c] = df[c].astype(float).round(self.digits(c)) + 0.0
                elif self.ctype(c) == "int":
                    df[c] = df[c].astype(int)
            READ_CACHE.put(key, df)
        return df

    def filter_chunks(self, selectors):
        loc = [s for s in selectors if s.name in self.locator_coords or s.name in self.constants]
        out = []
        for ch in self.index():
            if all(match_selector(s, ch.coords[s.name], self.ctype(s.name), self.digits(s.name)) for s in loc):
                out.append(ch)
        return out

    def keys(self, selectors, explain=None):
        chunks = self.filter_chunks(selectors)
        if explain is not None:
            explain[self.name] = (len(chunks), len(self.index()))
        content = self.content_coords
        key_chunks = {}
        if not content:
            for ch in chunks:
                key_chunks.setdefault(tuple(ch.coords[c] for c in self.coords), []).append(ch)
        else:
            csel = [s for s in selectors if s.name in content]
            for ch in chunks:
                df = self.read_chunk(ch)
                for combo in df[content].drop_duplicates().itertuples(index=False, name=None):
                    vals = dict(zip(content, (_py(v) for v in combo)))
                    if not all(match_selector(s, vals[s.name], self.ctype(s.name), self.digits(s.name)) for s in csel):
                        continue
                    full = dict(ch.coords)
                    full.update(vals)
                    key_chunks.setdefault(tuple(full[c] for c in self.coords), []).append(ch)
        self._key_chunks.update(key_chunks)
        return sorted(key_chunks, key=_sort_key)

    def read_key(self, key, columns, x_selectors=()):
        """Rows of one curve. Returns (DataFrame, chunks used)."""
        chunks = self._key_chunks.get(key)
        if chunks is None:
            sels = [_Eq(c, v) for c, v in zip(self.coords, key)]
            self.keys(sels)
            chunks = self._key_chunks.get(key, [])
        frames = []
        content = self.content_coords
        for ch in chunks:
            df = self.read_chunk(ch)
            if content:
                keyd = dict(zip(self.coords, key))
                mask = np.ones(len(df), dtype=bool)
                for c in content:
                    mask &= (df[c] == keyd[c]).to_numpy()
                df = df[mask]
            missing = [c for c in columns if c not in df.columns]
            if missing:
                raise GlueError(f"{ch.path}: column {missing[0]} not found (columns: {', '.join(df.columns)})")
            frames.append(df[columns])
        if not frames:
            return pd.DataFrame({c: pd.Series(dtype=float) for c in columns}), chunks
        df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        if x_selectors and self.x:
            df = df[x_mask(df[self.x].to_numpy(dtype=float), x_selectors)]
        return df, chunks

    # --- fingerprints

    def chunk_entries(self, mode="stat"):
        """{rel: [size, mtime_ns, sha]} for every chunk."""
        out = {}
        seen = {}
        for ch in self.index():
            if ch.path in seen:
                out[ch.rel] = seen[ch.path]
                continue
            size, mtime = ch.stat()
            entry = [size, mtime, sha256_file(ch.path) if mode == "hash" else ""]
            seen[ch.path] = entry
            out[ch.rel] = entry
        return out


class _Eq:
    def __init__(self, name, value):
        self.name, self.op, self.value = name, "=", value


def _sort_key(key):
    return tuple((0, v) if not isinstance(v, str) else (1, v) for v in key)


def digest(entries, mode="stat"):
    import hashlib

    h = hashlib.sha256()
    for rel in sorted(entries):
        size, mtime, sha = entries[rel]
        h.update(f"{rel}|{size}|{sha if mode == 'hash' else mtime}\n".encode())
    return "sha256:" + h.hexdigest()[:16]


# ------------------------------------------------------------------ glob and hdf5


class GlobTable(SourceTable):
    def __init__(self, name, path, root, template):
        super().__init__(name, path)
        self.root = root
        self.template = template

    def _load_index(self):
        cached = self._read_index_cache()
        if cached is not None:
            return cached
        matches, self.skipped, self.index_dirs = self.template.scan(self.root)
        chunks = [Chunk(rel, os.path.join(self.root, rel), self._coords(raw)) for rel, raw in matches]
        self._write_index_cache(chunks)
        return chunks

    def _coords(self, raw):
        c = {k: normalize(v, self.ctype(k), self.digits(k)) for k, v in raw.items()}
        c.update(self.constants)
        return c

    def _read_index_cache(self):
        if not self.index_path or not os.path.exists(self.index_path):
            return None
        try:
            with open(self.index_path) as f:
                data = json.load(f)
            if data.get("table_mtime") != os.stat(self.path).st_mtime_ns or data.get("root") != self.root:
                return None
            for d, mt in data["dirs"].items():
                if os.stat(d).st_mtime_ns != mt:
                    return None
        except (OSError, ValueError, KeyError):
            return None
        self.index_dirs = data["dirs"]
        self.skipped = data.get("skipped", 0)
        return [Chunk(rel, os.path.join(self.root, rel), self._coords(raw)) for rel, raw in data["chunks"]]

    def _write_index_cache(self, chunks):
        if not self.index_path:
            return
        try:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            raw = [(ch.rel, self.template.match(ch.rel)) for ch in chunks]
            data = {"table_mtime": os.stat(self.path).st_mtime_ns, "root": self.root,
                    "dirs": self.index_dirs, "skipped": self.skipped, "chunks": raw}
            with open(self.index_path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass


class Hdf5Table(SourceTable):
    def __init__(self, name, path, file_template, template):
        super().__init__(name, path)
        self.file_template = file_template
        self.template = template

    def _load_index(self):
        h5py = _h5py()
        files = []
        ft = self.file_template
        if "{" in ft or "*" in ft:
            root = self.dir
            matches, _, _ = Template(os.path.relpath(ft, root) if os.path.isabs(ft) else ft).scan(root)
            files = [(os.path.join(root, rel), raw) for rel, raw in matches]
        else:
            if not os.path.exists(ft):
                raise GlueError(f"HDF5 file not found: {ft}")
            files = [(ft, {})]
        chunks = []
        for fpath, fraw in files:
            names = []
            with h5py.File(fpath, "r") as f:
                f.visit(names.append)
            for obj in sorted(names):
                raw = self.template.match(obj)
                if raw is None:
                    continue
                raw.update(fraw)
                c = {k: normalize(v, self.ctype(k), self.digits(k)) for k, v in raw.items()}
                c.update(self.constants)
                rel = f"{os.path.relpath(fpath, self.dir)}::{obj}"
                chunks.append(Chunk(rel, fpath, c, internal=obj))
        return chunks


# ------------------------------------------------------------------ sqlite


class SqliteTable(SourceTable):
    def __init__(self, name, path, db, table=None, query=None, rename=None):
        super().__init__(name, path)
        self.db = db
        if table:
            base = f'SELECT * FROM "{table}"'
        else:
            base = query
        self.base = base
        self.rename = rename or {}

    def _connect(self):
        if not os.path.exists(self.db):
            raise GlueError(f"SQLite file not found: {self.db}")
        return sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)

    def _sql_cols(self):
        with self._connect() as con:
            try:
                cur = con.execute(f"SELECT * FROM ({self.base}) LIMIT 0")
            except sqlite3.Error as e:
                raise GlueError(f"{self.db}: {e}") from None
            return [d[0] for d in cur.description]

    def _discover_columns(self):
        return [self.rename.get(c, c) for c in self._sql_cols()]

    def _db_name(self, col):
        inv = {v: k for k, v in self.rename.items()}
        return inv.get(col, col)

    def _load_index(self):
        return [Chunk(os.path.basename(self.db), self.db, dict(self.constants))]

    def _where(self, selectors, xsel=()):
        clauses, params = [], []
        for s in list(selectors) + list(xsel):
            if s.name in self.constants:
                continue
            col = f'"{self._db_name(s.name)}"'
            ctype = self.ctype(s.name) if s.name != self.x else "float"
            expr = f"ROUND({col}, {self.digits(s.name)})" if ctype == "float" and s.name != self.x else col
            if s.op == "range":
                if s.lo is not None:
                    clauses.append(f"{expr} >= ?")
                    params.append(s.lo)
                if s.hi is not None:
                    clauses.append(f"{expr} <= ?")
                    params.append(s.hi)
            elif isinstance(s.value, list):
                qs = ", ".join("?" for _ in s.value)
                clauses.append(f"{expr} {'NOT IN' if s.op == '!=' else 'IN'} ({qs})")
                params += list(s.value)
            else:
                clauses.append(f"{expr} {s.op if s.op != '=' else '='} ?")
                params.append(s.value)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def keys(self, selectors, explain=None):
        chunks = self.filter_chunks(selectors)
        if explain is not None:
            explain[self.name] = (len(chunks), 1)
        if not chunks:
            return []
        content = self.content_coords
        const = tuple(self.constants.get(c) for c in self.coords)
        if not content:
            keys = [const]
        else:
            where, params = self._where([s for s in selectors if s.name in content])
            cols = ", ".join(f'"{self._db_name(c)}"' for c in content)
            with self._connect() as con:
                rows = con.execute(f"SELECT DISTINCT {cols} FROM ({self.base}){where}", params).fetchall()
            keys = set()
            for r in rows:
                vals = {c: normalize(v, self.ctype(c), self.digits(c)) for c, v in zip(content, r)}
                vals.update(self.constants)
                keys.add(tuple(vals[c] for c in self.coords))
            keys = sorted(keys, key=_sort_key)
        for k in keys:
            self._key_chunks[k] = chunks
        return keys

    def read_key(self, key, columns, x_selectors=()):
        keyd = dict(zip(self.coords, key))
        sels = [_Eq(c, keyd[c]) for c in self.content_coords]
        where, params = self._where(sels, x_selectors if self.x else ())
        cols = [c for c in columns if c not in self.constants]
        sql_cols = ", ".join(f'"{self._db_name(c)}"' for c in cols)
        with self._connect() as con:
            try:
                rows = con.execute(f"SELECT {sql_cols} FROM ({self.base}){where}", params).fetchall()
            except sqlite3.Error as e:
                raise GlueError(f"{self.db}: {e}") from None
        df = pd.DataFrame(rows, columns=cols)
        for c in columns:
            if c in self.constants:
                df[c] = self.constants[c]
            elif c not in self.coords or self.ctype(c) != "str":
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df[columns], self.index()


# ------------------------------------------------------------------ dataset caches


class CacheTable(SourceTable):
    """A saved dataset, read from its cache file."""

    is_dataset = True

    def __init__(self, name, path, cache_file, fmt):
        super().__init__(name, path)
        self.cache_file = cache_file
        self.format = fmt
        self.info = None  # set by dataset loader

    def _load_index(self):
        return [Chunk(os.path.basename(self.cache_file), self.cache_file, {})]

    def frame(self):
        if not os.path.exists(self.cache_file):
            raise GlueError(f"dataset {self.name}: cache file not found: {self.cache_file}")
        ch = self.index()[0]
        size, mtime = ch.stat()
        key = (ch.path, "", size, mtime)
        df = READ_CACHE.get(key)
        if df is None:
            from .dataset import read_cache_file
            df, _ = read_cache_file(self.cache_file, self.format)
            READ_CACHE.put(key, df)
        return df

    def _discover_columns(self):
        return list(self.frame().columns)

    def keys(self, selectors, explain=None):
        if explain is not None:
            explain[self.name] = (1, 1)
        df = self.frame()
        if not self.coords:
            return [()] if len(df) else []
        combos = df[self.coords].drop_duplicates().itertuples(index=False, name=None)
        out = []
        for combo in combos:
            vals = dict(zip(self.coords, combo))
            if all(match_selector(s, vals[s.name], self.ctype(s.name), self.digits(s.name))
                   for s in selectors if s.name in vals):
                out.append(tuple(_py(v) for v in combo))
        chunks = self.index()
        for k in out:
            self._key_chunks[k] = chunks
        return sorted(out, key=_sort_key)

    def read_key(self, key, columns, x_selectors=()):
        df = self.frame()
        mask = np.ones(len(df), dtype=bool)
        for c, v in zip(self.coords, key):
            mask &= (df[c] == v).to_numpy()
        sub = df[mask]
        if x_selectors and self.x:
            sub = sub[x_mask(sub[self.x].to_numpy(dtype=float), x_selectors)]
        return sub[columns], self.index()


def _py(v):
    return v.item() if hasattr(v, "item") else v


# ------------------------------------------------------------------ loading table files


def _meta_from(d, where):
    check_keys(d, {"type", "unit", "label", "description", "digits", "error"}, where)
    m = ColumnMeta()
    for k, v in d.items():
        if k.startswith("x_"):
            continue
        if k == "type" and v not in ("float", "int", "str"):
            raise GlueError(f"{where}: type must be float, int, or str")
        setattr(m, k, v)
    return m


def load_table(path, name=None, index_dir=None):
    data = read_toml(path)
    check_version(data, path)
    check_keys(data, {"glue", "kind", "name", "description", "source", "curves", "columns", "meta"}, path)
    if data.get("kind") != "table":
        raise GlueError(f"{path}: expected kind = \"table\", found {data.get('kind')!r}")
    name = name or data.get("name") or os.path.splitext(os.path.basename(path))[0]
    check_name(name, "table name")
    base = os.path.dirname(os.path.abspath(path))
    src = data.get("source")
    if not isinstance(src, dict):
        raise GlueError(f"{path}: missing [source]")
    locator = src.get("locator", "glob")
    reader = dict(src.get("reader", {}))
    constants = src.get("constants", {})

    if locator == "glob":
        check_keys(src, {"locator", "root", "pattern", "constants", "reader"}, f"{path} [source]")
        if "pattern" not in src:
            raise GlueError(f"{path}: [source] needs a pattern")
        tmpl = Template(src["pattern"])
        table = GlobTable(name, path, expand_path(src.get("root", "."), base), tmpl)
    elif locator == "hdf5":
        check_keys(src, {"locator", "file", "pattern", "constants", "reader"}, f"{path} [source]")
        if "file" not in src or "pattern" not in src:
            raise GlueError(f"{path}: an hdf5 source needs file and pattern")
        tmpl = Template(src["pattern"])
        table = Hdf5Table(name, path, expand_path(src["file"], base), tmpl)
        reader.setdefault("format", "dataset")
    elif locator == "sqlite":
        check_keys(src, {"locator", "file", "table", "query", "rename", "constants"}, f"{path} [source]")
        if ("table" in src) == ("query" in src):
            raise GlueError(f"{path}: a sqlite source needs exactly one of table or query")
        tmpl = None
        table = SqliteTable(name, path, expand_path(src["file"], base), src.get("table"), src.get("query"),
                            src.get("rename"))
    else:
        raise GlueError(f"{path}: unknown locator {locator!r}. Use glob, hdf5, or sqlite")

    if locator != "sqlite":
        fmt = reader.get("format", "text")
        if fmt not in READER_KEYS:
            raise GlueError(f"{path}: unknown reader format {fmt!r}. Use {', '.join(READER_KEYS)}")
        check_keys(reader, READER_KEYS[fmt], f"{path} [source.reader]")
        if fmt in ("text", "npy", "raw") and not reader.get("columns"):
            raise GlueError(f"{path}: the {fmt} reader needs columns = [...]")
        if fmt == "csv" and not reader.get("header", True) and not reader.get("columns"):
            raise GlueError(f"{path}: a csv reader with header = false needs columns = [...]")
        if fmt == "dataset" and not reader.get("columns") and not reader.get("fields"):
            pass
    table.reader = reader
    table.description = data.get("description", "")

    for col, d in data.get("columns", {}).items():
        table.meta[col] = _meta_from(d, f"{path} [columns.{col}]")
    if tmpl is not None:
        for n in tmpl.names:
            m = table.meta.get(n)
            if m is not None and "type" in data["columns"][n] and m.type != tmpl.types[n]:
                raise GlueError(f"{path}: {n} is {tmpl.types[n]} in the pattern but {m.type} in [columns.{n}]")
            table.m(n).type = tmpl.types[n]
        table.locator_coords = list(tmpl.names)
    for k, v in constants.items():
        table.m(k).type = "str" if isinstance(v, str) else "int" if isinstance(v, int) else "float"
        table.constants[k] = normalize(v, table.ctype(k), table.digits(k))

    curves = data.get("curves", {})
    check_keys(curves, {"by", "x", "y"}, f"{path} [curves]")
    by = list(curves.get("by", []))
    coords = list(by)
    for c in table.locator_coords + list(table.constants):
        if c not in coords:
            coords.append(c)
    if locator == "sqlite" and not by:
        pass
    table.coords = coords
    table.x = curves.get("x")
    if table.x in coords:
        raise GlueError(f"{path}: {table.x} can't be both a coordinate and x")
    if "y" in curves:
        table._values = list(curves["y"])
    for c in by:
        if c not in table.locator_coords and c not in table.constants:
            table.m(c)
    for col, m in table.meta.items():
        if m.error and m.error == col:
            raise GlueError(f"{path}: column {col} can't be its own error column")
    if index_dir and isinstance(table, GlobTable):
        import hashlib

        tag = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:10]
        table.index_path = os.path.join(index_dir, f"{name}-{tag}.json")
    return table


def describe_source(table):
    if isinstance(table, GlobTable):
        return f"{relpath(table.root, os.getcwd())}/{table.template.text}"
    if isinstance(table, SqliteTable):
        return f"sqlite {relpath(table.db, os.getcwd())}"
    if isinstance(table, Hdf5Table):
        return f"hdf5 {relpath(table.file_template, os.getcwd())}:{table.template.text}"
    if isinstance(table, CacheTable):
        return f"dataset cache {relpath(table.cache_file, os.getcwd())}"
    return "?"
