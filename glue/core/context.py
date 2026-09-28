"""Evaluating core trees: pushdown, the value store, fingerprints, and read plans."""

from . import formula as F
from .nodes import DatasetNode, SourceNode
from .report import Report
from .store import Store, combine, load_epochs, salts, sels_sig

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
        self._fps = {}

    # --- fingerprints

    def digest(self, leaf):
        if leaf.id in self._digests:
            return self._digests[leaf.id]
        if isinstance(leaf, SourceNode):
            t = leaf.table
            d = t.digest(self.fp_mode, salts(self.glue_dir, t.def_id, self.epochs))
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
        hit = self.store.get(key)
        if hit is not None:
            return hit
        ins = {}
        if node.eval_children:
            cs = node.child_sels(sels)
            for path, child in node.children():
                s = cs.get(path, [])
                ins[path] = None if s is None else self.value(child, s)
        frame = node.compute(self, ins, sels)
        applicable = [p for p in sels if p.name in frame.columns]
        frame = F.apply_preds(applicable, frame)
        self.store.put(key, frame, disk=node.op in EXPENSIVE)
        return frame

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
