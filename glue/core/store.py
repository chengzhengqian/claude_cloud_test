"""The value store: values keyed by (node id, fingerprint), plus epochs for manual invalidation."""

import hashlib
import json
import os
import tomllib
from collections import OrderedDict

import pandas as pd

from ..util import toml_dumps


def combine(pairs):
    """Fingerprint of a node from its leaves' (leaf id, digest) pairs."""
    h = hashlib.sha256()
    for leaf_id, dig in sorted(pairs):
        h.update(f"{leaf_id}={dig}\n".encode())
    return h.hexdigest()[:16]


def find_glue_dir(start):
    """The .glue folder of the project containing `start` (a file or folder)."""
    d = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
    while True:
        if os.path.exists(os.path.join(d, "project.toml")) or os.path.isdir(os.path.join(d, ".glue")):
            return os.path.join(d, ".glue")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# ------------------------------------------------------------------ epochs


def _epochs_path(glue_dir):
    return os.path.join(glue_dir, "epochs.toml")


def load_epochs(glue_dir):
    if not glue_dir:
        return {}
    path = _epochs_path(glue_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_epochs(glue_dir, data):
    os.makedirs(glue_dir, exist_ok=True)
    with open(_epochs_path(glue_dir), "w") as f:
        f.write(toml_dumps(data))


def bump(glue_dir, leaf_id, chunks=None):
    data = load_epochs(glue_dir)
    entry = data.setdefault(leaf_id, {"epoch": 0, "chunks": {}})
    entry.setdefault("chunks", {})
    if chunks is None:
        entry["epoch"] = int(entry.get("epoch", 0)) + 1
    else:
        for rel in chunks:
            entry["chunks"][rel] = int(entry["chunks"].get(rel, 0)) + 1
    save_epochs(glue_dir, data)
    return entry


def salts(glue_dir, leaf_id, epochs=None):
    """(epoch, {rel: n}) for one leaf."""
    data = epochs if epochs is not None else load_epochs(glue_dir)
    e = data.get(leaf_id, {})
    return int(e.get("epoch", 0)), {k: int(v) for k, v in e.get("chunks", {}).items()}


# ------------------------------------------------------------------ memory and disk store


class Store:
    def __init__(self, limit_mb=512, disk_dir=None, disk_limit_mb=1024):
        self.limit = limit_mb << 20
        self.used = 0
        self.items = OrderedDict()
        self.disk_dir = disk_dir
        self.disk_limit = disk_limit_mb << 20

    def _on_disk(self, key):
        # only whole, unfiltered values in the plain (id, sels, fp) form go to disk
        return bool(self.disk_dir) and len(key) == 3 and not key[1]

    def get(self, key):
        hit = self.items.get(key)
        if hit is not None:
            self.items.move_to_end(key)
            return hit[0]
        if self._on_disk(key):
            path = self._path(key)
            if os.path.exists(path):
                try:
                    frame = pd.read_parquet(path)
                    os.utime(path)          # recently used, for the size limit
                except Exception:
                    return None
                self._put_mem(key, frame)
                return frame
        return None

    def put(self, key, frame, disk=False):
        self._put_mem(key, frame)
        if disk and self._on_disk(key):
            try:
                os.makedirs(self.disk_dir, exist_ok=True)
                frame.to_parquet(self._path(key), index=False)
            except Exception:
                return
            self.prune_disk()

    def prune_disk(self):
        """Remove the least recently used cache files until the folder is under the size limit."""
        if not self.disk_dir or not os.path.isdir(self.disk_dir):
            return 0
        files = []
        for f in os.listdir(self.disk_dir):
            path = os.path.join(self.disk_dir, f)
            st = os.stat(path)
            files.append((st.st_mtime_ns, st.st_size, path))
        total = sum(f[1] for f in files)
        removed = 0
        for _, size, path in sorted(files):
            if total <= self.disk_limit:
                break
            os.remove(path)
            total -= size
            removed += 1
        return removed

    def _put_mem(self, key, frame):
        size = int(frame.memory_usage(deep=False).sum()) if len(frame.columns) else 64
        if key in self.items:
            self.used -= self.items.pop(key)[1]
        self.items[key] = (frame, size)
        self.used += size
        while self.used > self.limit and len(self.items) > 1:
            _, (_, s) = self.items.popitem(last=False)
            self.used -= s

    def _path(self, key):
        node_id, sels, fp = key
        return os.path.join(self.disk_dir, f"{node_id}-{fp[:12]}.parquet")

    def drop(self, node_ids):
        node_ids = set(node_ids)
        for key in [k for k in self.items if k[0] in node_ids]:
            self.used -= self.items.pop(key)[1]
        removed = 0
        if self.disk_dir and os.path.isdir(self.disk_dir):
            for f in os.listdir(self.disk_dir):
                if f.split("-")[0] in node_ids:
                    os.remove(os.path.join(self.disk_dir, f))
                    removed += 1
        return removed

    def gc(self, keep_ids):
        keep_ids = set(keep_ids)
        removed = 0
        if self.disk_dir and os.path.isdir(self.disk_dir):
            for f in os.listdir(self.disk_dir):
                if f.split("-")[0] not in keep_ids:
                    os.remove(os.path.join(self.disk_dir, f))
                    removed += 1
        for key in [k for k in self.items if k[0] not in keep_ids]:
            self.used -= self.items.pop(key)[1]
        return removed

    def clear(self):
        self.items.clear()
        self.used = 0


def sels_sig(sels):
    return tuple(sorted(p.src() for p in sels))


def dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o):
    if isinstance(o, tuple):
        return list(o)
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))
