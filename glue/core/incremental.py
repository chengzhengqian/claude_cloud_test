"""Recomputing only the curves a changed file affects (core.md 6.6).

Shared by `refresh` for saved datasets and by the value store for cached
intermediate values.
"""

from .formula import Pred


def trace(node, u):
    """Leaves an input of `node` comes from: [(leaf node, leaf input name)]."""
    if node.leaf:
        return [(node, u)]
    if not node.eval_children:
        return trace(node.expanded, u)
    out = []
    for path, c in node.children():
        m = node.map_in(path, u)
        if m is not None and m in c.part:
            out += trace(c, m)
    return out


def changed_chunks(old, cur, mode):
    """Chunks that changed, were added, or were removed between two {rel: entry} maps."""
    i = 2 if mode == "hash" else 1

    def same(a, b):
        a, b = list(a) + [""] * 4, list(b) + [""] * 4
        return a[0] == b[0] and a[i] == b[i] and (a[3] or "") == (b[3] or "")

    changed = {r for r in cur if r in old and not same(cur[r], old[r])}
    return changed, set(cur) - set(old), set(old) - set(cur)


def chunk_coords(table, rel, index):
    """The path input values of a chunk, also for a chunk that no longer exists."""
    from ..sources import GlobTable, Hdf5Table
    from ..util import normalize

    for ch in index:
        if ch.rel == rel:
            return dict(table.constants, **ch.coords)
    if isinstance(table, GlobTable):
        raw = table.template.match(rel)
        return dict(table.constants, **table._coords(raw)) if raw else None
    if isinstance(table, Hdf5Table) and "::" in rel:
        raw = table.template.match(rel.split("::", 1)[1])
        if raw:
            c = {k: normalize(v, table.ctype(k), table.digits(k)) for k, v in raw.items()}
            c.update(table.constants)
            return table.apply_path_transforms(c)
    return None


def pred_sets(node, changed):
    """For each changed chunk, the filter on node's inputs that selects the points it affects.

    `changed` maps a table name to the set of changed chunk paths. Returns a list of
    filter lists, or None when some change can't be traced to node's inputs, in which
    case everything must be recomputed.
    """
    part = [u for u in node.type.input_names if u in node.part]
    if not part:
        return None
    traces = {u: trace(node, u) for u in part}
    from .nodes import SourceNode

    sources = {lf.table.name: lf.table for lf in node.leaves() if isinstance(lf, SourceNode)}
    sets = {}
    for label, rels in changed.items():
        t = sources.get(label)
        if t is None or any(r.startswith("(") for r in rels):
            return None
        index = t.index()
        for rel in rels:
            coords = chunk_coords(t, rel, index)
            if coords is None:
                return None
            preds = []
            for u in part:
                for leaf, v in traces[u]:
                    if isinstance(leaf, SourceNode) and leaf.table.name == label and v in coords:
                        preds.append(Pred(u, "=", coords[v]))
                        break
            if not preds:
                return None
            sets[tuple(p.src() for p in preds)] = preds
    return list(sets.values())
