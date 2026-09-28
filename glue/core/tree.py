"""Calculation trees in TOML (docs/core.md, section 6)."""

from ..errors import GlueError
from .nodes import REGISTRY


def topo(root):
    """Nodes in leaves-first order, following the tree's own children (not expansions)."""
    order, seen = [], set()

    def go(n):
        if n.id in seen:
            return
        seen.add(n.id)
        for _, c in n.children():
            go(c)
        order.append(n)

    go(root)
    return order


def dump_nodes(root):
    return {n.id: n.to_toml() for n in topo(root)}


def load_nodes(nodes, root_id, resolver):
    """Build the tree under root_id. Returns (root, warnings)."""
    built, stack, warnings = {}, [], []

    def get(nid):
        if nid in built:
            return built[nid]
        if nid not in nodes:
            raise GlueError(f"calculation tree: node {nid} is referenced but not defined")
        if nid in stack:
            raise GlueError(f"calculation tree: cycle through node {nid}")
        stack.append(nid)
        d = nodes[nid]
        op = d.get("op")
        cls = REGISTRY.get(op)
        if cls is None:
            raise GlueError(f"calculation tree: node {nid} has unknown op {op!r}")
        node = cls.from_toml(d, get, resolver)
        stack.pop()
        if node.id != nid:
            warnings.append((nid, node.id, op))
        built[nid] = node
        return node

    if root_id not in nodes:
        raise GlueError(f"calculation tree: root {root_id} is not defined")
    return get(root_id), warnings


def render(root, indent="  "):
    """Text view of a tree for explain and why: one node per line, with its type."""
    lines = []
    seen = set()

    def go(n, depth):
        pad = indent * depth
        ref = f"  (same as above: {n.id})" if n.id in seen else ""
        head = n.describe()
        if n.name and n.op not in ("source", "dataset"):
            head = f"{n.name} = {head}"
        lines.append(f"{pad}{head}  {n.type.text()}{ref}")
        if ref:
            return
        seen.add(n.id)
        for _, c in n.children():
            go(c, depth + 1)

    go(root, 0)
    return lines
