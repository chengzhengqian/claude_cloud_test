"""Evaluating core trees: pushdown, the value store, fingerprints, and read plans."""

import numpy as np
import pandas as pd

from . import formula as F
from .incremental import changed_chunks, pred_sets
from .nodes import DatasetNode, SourceNode
from ..errors import GlueError
from .report import Report
from .store import Store, combine, load_epochs, salts, sels_sig
from ..sources import digest as source_digest

EXPENSIVE = {"resample", "deriv", "cumint", "swap", "reduce", "apply", "align", "legendre"}


class Context:
    def __init__(self, store=None, glue_dir=None, fp_mode="stat", report_level="short"):
        self.store = store if store is not None else Store()
        self.glue_dir = glue_dir
        self.fp_mode = fp_mode
        self.report = Report()
        self.report_level = report_level
        self.epochs = load_epochs(glue_dir)
        self._digests = {}
        self._entries = {}
        self._fps = {}
        self.nan_rows = "drop"     # "error": stop when a source has rows with missing values

    # --- fingerprints

    def digest(self, leaf):
        if leaf.id in self._digests:
            return self._digests[leaf.id]
        if isinstance(leaf, SourceNode):
            t = leaf.table
            entries = t.chunk_entries(self.fp_mode, salts(self.glue_dir, t.def_id, self.epochs))
            self._entries[leaf.id] = entries
            d = source_digest(entries, self.fp_mode)
        elif isinstance(leaf, DatasetNode):
            d = "saved:" + (leaf.table.info.sha256 or "")
        else:
            d = ""
        self._digests[leaf.id] = d
        return d

    def fp(self, node):
        if node.id not in self._fps:
            self._fps[node.id] = combine((lf.id, self.digest(lf)) for lf in node.leaves())
        return self._fps[node.id]

    # --- evaluation

    def value(self, node, sels=()):
        sels = list(sels)
        key = (node.id, sels_sig(sels), self.fp(node))
        if self.nan_rows == "error":
            key += ("nan_rows=error",)   # values cached without the check don't count as checked
        hit = self.store.get(key)
        if hit is not None:
            self._check_nan(node, hit)
            return hit
        expensive = node.op in EXPENSIVE
        frame = self._incremental(node, sels, key) if expensive else None
        if frame is None:
            ins = {}
            if node.eval_children:
                cs = node.child_sels(sels)
                for path, child in node.children():
                    s = cs.get(path, [])
                    ins[path] = None if s is None else self.value(child, s)
            frame = node.compute(self, ins, sels)
            self._check_nan(node, frame)
            applicable = [p for p in sels if p.name in frame.columns]
            frame = F.apply_preds(applicable, frame)
        self.store.put(key, frame, disk=expensive, meta=self._meta(node) if expensive else None)
        return frame

    # --- recomputing only what changed (core.md 6.6)

    def _meta(self, node):
        """What the leaves under node looked like: chunk entries for sources, digests for datasets."""
        out = {}
        for lf in node.leaves():
            self.digest(lf)
            out[lf.id] = self._entries[lf.id] if isinstance(lf, SourceNode) else self._digests[lf.id]
        return out

    def _incremental(self, node, sels, key):
        """Update an older cached value of node by recomputing only the keys whose files changed."""
        found = self.store.older(key)
        if found is None:
            return None
        old, meta = found
        changed = {}
        for lf in node.leaves():
            before = meta.get(lf.id)
            if before is None:
                return None
            self.digest(lf)
            if isinstance(lf, SourceNode):
                c, a, r = changed_chunks(before, self._entries[lf.id], self.fp_mode)
                if c | a | r:
                    changed.setdefault(lf.table.name, set()).update(c | a | r)
            elif before != self._digests[lf.id]:
                return None
        if not changed:
            return None
        sets = pred_sets(node, changed)
        if sets is None:
            return None
        drop = np.zeros(len(old), dtype=bool)
        if len(old):
            idx = old.assign(__i=np.arange(len(old)))
            for preds in sets:
                drop[F.apply_preds(preds, idx)["__i"].to_numpy()] = True
        new = [self.value(node, list(sels) + preds) for preds in sets]
        frame = pd.concat([old[~drop]] + new, ignore_index=True)
        ins = [c for c in node.type.input_names if c in frame.columns]
        frame = frame.sort_values(ins, kind="stable", ignore_index=True) if ins else frame
        keys = [c for c in ins if c != node.type.axis] or ins
        fresh = pd.concat(new, ignore_index=True) if new else frame.iloc[:0]
        k = len(fresh.drop_duplicates(keys)) if keys and len(fresh) else 0
        total = len(frame.drop_duplicates(keys)) if keys and len(frame) else len(frame)
        word = "curves" if node.type.axis in ins else "keys"
        self.report.note(f"updated cached {node.label()}: recomputed {k} of {total} {word}")
        return frame

    def _check_nan(self, node, frame):
        if self.nan_rows != "error" or not isinstance(node, SourceNode) or not len(frame):
            return
        cols = [c for c in node.type.input_names + node.type.output_names if c in frame.columns]
        bad = frame[cols].isna().any(axis=1)
        if bad.any():
            row = frame[bad].iloc[0]
            missing = [c for c in cols if row[c] != row[c]]
            key = ", ".join(f"{c}={row[c]}" for c in node.type.input_names if c in cols and c not in missing)
            raise GlueError(f"{node.table.name}: {int(bad.sum())} rows with a missing value, for example "
                            f"{', '.join(missing)} at {key}. Set nan_rows drop to skip them.")

    def run(self, root, preds=()):
        """Evaluate root, pushing down the filters that can be pushed (law L1)."""
        push = [p for p in preds if p.literal and p.name in root.part]
        rest = [p for p in preds if p not in push]
        return F.apply_preds(rest, self.value(root, push))

    # --- plans for explain

    def plan(self, root, preds=()):
        push = [p for p in preds if p.literal and p.name in root.part]
        reads = {}

        def go(node, sels):
            if isinstance(node, SourceNode):
                m, n, kind = node.table.plan(sels)
                prev = reads.get(node.table.name)
                if prev is None or prev[0] < m:
                    reads[node.table.name] = (m, n, kind)
                return
            if isinstance(node, DatasetNode):
                reads[node.table.name] = (1, 1, "dataset")
                return
            if not node.eval_children:
                go(node.expanded, sels)
                return
            cs = node.child_sels(sels)
            for path, child in node.children():
                s = cs.get(path, [])
                if s is not None:
                    go(child, s)

        go(root, push)
        return reads, push
