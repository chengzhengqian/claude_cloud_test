"""Diagrams for TUTORIAL.md. Most are drawn from a live glue session, so they show what the code does."""

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e6e5e1"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
TINT = {BLUE: "#dbe8f9", ORANGE: "#fbe3d8", AQUA: "#d5f1e6", VIOLET: "#e2dff3", YELLOW: "#fbecc7", INK2: "#ecebe8"}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.edgecolor": INK2, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
})


def _box(ax, x, y, w, h, text, color, fontsize=9, dashed=False, bold_first=True):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                linewidth=1.4, edgecolor=color, facecolor=TINT.get(color, SURFACE),
                                linestyle="--" if dashed else "-"))
    lines = text.split("\n")
    if bold_first and len(lines) > 1:
        ax.text(x, y + 0.13 * h, lines[0], ha="center", va="center", fontsize=fontsize, weight="bold", color=INK)
        ax.text(x, y - 0.2 * h, "\n".join(lines[1:]), ha="center", va="center", fontsize=fontsize - 1.5, color=INK2)
    else:
        ax.text(x, y, text, ha="center", va="center", fontsize=fontsize, color=INK)


def _arrow(ax, x0, y0, x1, y1, color=INK2):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=1.2, shrinkA=2, shrinkB=2))


# ------------------------------------------------------------------ 1. the pieces


def pipeline(path):
    fig, ax = plt.subplots(figsize=(12, 5.4))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 5.4)
    ax.axis("off")
    files = [("data/ed/L_*/Delta_*/h_*.dat", "31 text files"), ("data/ftlm.db", "SQLite"),
             ("data/gap.h5", "HDF5 groups"), ("data/exact/xx_h*.csv", "CSV")]
    tables = ["tables/ed.toml", "tables/ftlm.toml", "tables/gap.toml", "tables/exact.toml"]
    ax.text(1.4, 5.1, "raw files, left as they are", ha="center", color=INK2)
    ax.text(4.1, 5.1, "one table file each", ha="center", color=INK2)
    for i, ((f, what), t) in enumerate(zip(files, tables)):
        y = 4.3 - i * 1.05
        _box(ax, 1.4, y, 2.5, 0.72, f"{what}\n{f}", INK2, fontsize=9)
        _box(ax, 4.1, y, 2.0, 0.6, t, BLUE, fontsize=9, bold_first=False)
        _arrow(ax, 2.65, y, 3.1, y)
        _arrow(ax, 5.1, y, 5.75, 2.75)
    _box(ax, 6.55, 2.75, 1.5, 1.1, "project.toml\nnames, views,\nsettings", BLUE)
    ax.text(9.55, 5.1, "what you do with it", ha="center", color=INK2)
    right = [("expression", "ed.E - exact.E", VIOLET, 4.3),
             ("core tree", "typed operations, ids", VIOLET, 3.25),
             ("plot / show / export", "figures and files", AQUA, 2.2),
             ("dataset", "data + tree + fingerprints", ORANGE, 1.15)]
    for name, sub, c, y in right:
        _box(ax, 9.55, y, 2.7, 0.72, f"{name}\n{sub}", c)
    _arrow(ax, 7.3, 2.95, 8.2, 4.3)
    _arrow(ax, 9.55, 3.94, 9.55, 3.61)
    _arrow(ax, 9.55, 2.89, 9.55, 2.56)
    _arrow(ax, 9.55, 1.84, 9.55, 1.51)
    ax.annotate("", xy=(8.2, 3.1), xytext=(8.2, 1.3),
                arrowprops=dict(arrowstyle="-|>", color=ORANGE, lw=1.2, connectionstyle="arc3,rad=-0.4"))
    ax.text(7.45, 1.75, "refresh\nwhen files\nchange", ha="center", fontsize=8, color=ORANGE)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------ 2. ragged inputs and alignment


def alignment(session, path, key=dict(L=14, Delta=0.0, h=0.0)):
    where = ", ".join(f"{k}={v}" for k, v in key.items())
    ed = session.eval("ed.E", where=where).to_pandas()
    ex = session.eval("exact.E", where="Delta=0.0, h=0.0").to_pandas()
    lo, hi = max(ed["T"].min(), ex["T"].min()), min(ed["T"].max(), ex["T"].max())
    grid = np.linspace(lo, hi, 200)          # overlap(n=200) is evenly spaced over the shared range
    fig, (ax, bx) = plt.subplots(2, 1, figsize=(10, 5.6), height_ratios=[1.1, 1], sharex=True)
    rows = [("ed.E  (L=14)", ed["T"], BLUE), ("exact.E", ex["T"], ORANGE), ("overlap(n=200)", grid, VIOLET)]
    for i, (name, t, c) in enumerate(rows):
        y = 2 - i
        ax.vlines(t, y - 0.3, y + 0.3, color=c, lw=0.8)
        ax.text(0.0105, y, name, ha="left", va="center", color=INK, fontsize=9,
                bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1))
    ax.axvspan(lo, hi, color=GRID, zorder=-1)
    ax.set_yticks([])
    ax.set_ylim(-0.6, 2.6)
    ax.set_title("Every source has its own temperatures. T is ragged, so glue interpolates both onto one grid.",
                 fontsize=10, loc="left", color=INK)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    bx.plot(ex["T"], ex["E"], color=ORANGE, lw=2, label="exact.E, infinite chain")
    bx.plot(ed["T"], ed["E"], "o", ms=4, color=BLUE, label="ed.E, L=14, its own points")
    bx.set_xscale("log")
    bx.set_xlim(0.01, 10)
    bx.set_xlabel("temperature T [J]")
    bx.set_ylabel("energy per site [J]")
    bx.legend(frameon=False, loc="upper left")
    bx.grid(color=GRID, lw=0.6)
    for s in ("top", "right"):
        bx.spines[s].set_visible(False)
    ax.text(np.sqrt(lo * hi), -0.45, "shared range: the overlap grid lives here", ha="center", fontsize=8.5,
            color=INK2)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ------------------------------------------------------------------ 3. a core tree


CATEGORY = {
    "source": BLUE, "dataset": BLUE, "const": BLUE, "axis": BLUE,
    "select": AQUA, "rename": AQUA, "transform": AQUA, "filter": AQUA, "slice": AQUA, "join": AQUA,
    "stack": AQUA, "points": AQUA, "union": AQUA, "span": AQUA, "map": AQUA,
    "resample": ORANGE, "deriv": ORANGE, "cumint": ORANGE, "reduce": ORANGE, "swap": ORANGE,
    "apply": YELLOW,
}


def _short(node):
    d = node.describe()
    return d if len(d) <= 40 else d[:38] + "…"


def tree(node, path, title=None):
    """Draw a core tree top-down. A node used twice is drawn once, then as a dashed 'same node' box."""
    boxes, edges, seen = [], [], set()

    def layout(n, depth, x0):
        """Place n's subtree starting at column x0. Returns (next free column, n's x)."""
        if n.id in seen:
            boxes.append((n, depth, x0, True))
            return x0 + 1, x0
        seen.add(n.id)
        kids = [c for _, c in n.children()]
        if not kids:
            boxes.append((n, depth, x0, False))
            return x0 + 1, x0
        x, centers = x0, []
        for c in kids:
            x, cx = layout(c, depth + 1, x)
            centers.append(cx)
        me = sum(centers) / len(centers)
        boxes.append((n, depth, me, False))
        edges.extend(((cx, depth + 1), (me, depth)) for cx in centers)
        return x, me

    width, _ = layout(node, 0, 0)
    depth = max(b[1] for b in boxes) + 1
    fig, ax = plt.subplots(figsize=(max(7, 3.9 * width), 1.35 * depth + 0.8))
    ax.set_xlim(-0.55, width - 0.45)
    ax.set_ylim(-depth + 0.4, 0.9)
    ax.axis("off")
    for (cx, cd), (px, pd) in edges:
        _arrow(ax, cx, -cd + 0.31, px, -pd - 0.31)
    for n, d, x, dup in boxes:
        if dup:
            _box(ax, x, -d, 0.9, 0.62, f"same node\n{n.id}", INK2, dashed=True)
            continue
        color = VIOLET if n.lib else CATEGORY.get(n.op, INK2)
        label = n.name + " = " if n.name and n.name != n.op and not n.leaf and len(n.name) <= 16 else ""
        head = label + n.describe()
        head = head if len(head) <= 42 else head[:40] + "…"
        _box(ax, x, -d, 0.94, 0.62, f"{head}\n{n.type.text()}\nid {n.id}", color, fontsize=8.5)
    handles = [plt.Line2D([], [], color=c, lw=6, alpha=0.6) for c in (BLUE, AQUA, ORANGE, VIOLET, YELLOW)]
    ax.legend(handles, ["leaf: reads data", "structure and pointwise", "along an input",
                        "standard library", "plugin"], loc="upper left", frameon=False, fontsize=8, ncol=5,
              bbox_to_anchor=(0, 1.02))
    if title:
        ax.set_title(title, fontsize=10, loc="left", color=INK, pad=18)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------ 4. which files are read


def _ed_cells(table):
    Ls = sorted({ch.coords["L"] for ch in table.index()})
    cols = [(d, h) for d in (0.0, 0.5, 1.0, 1.5) for h in (0.0, 0.5)]
    have = {(ch.coords["L"], ch.coords["Delta"], ch.coords["h"]): ch.rel for ch in table.index()}
    return Ls, cols, have


def file_grid(table, path, read=None, changed=None, title=""):
    """ED files as a grid: rows L, columns (Delta, h). read/changed are sets of chunk paths."""
    Ls, cols, have = _ed_cells(table)
    fig, ax = plt.subplots(figsize=(8.6, 2.9))
    for i, L in enumerate(Ls):
        for j, (d, h) in enumerate(cols):
            rel = have.get((L, d, h))
            if rel is None:
                face, edge, txt = SURFACE, INK2, "missing"
            elif changed and rel in changed:
                face, edge, txt = ORANGE, ORANGE, "changed"
            elif (read is None and not changed) or (read is not None and rel in read):
                face, edge, txt = BLUE, BLUE, "read" if read is not None else ""
            else:
                face, edge, txt = "#ecebe8", "#ecebe8", ""
            ax.add_patch(FancyBboxPatch((j + 0.06, -i - 0.94), 0.88, 0.88, boxstyle="round,pad=0,rounding_size=0.08",
                                        facecolor=face, edgecolor=edge, linestyle="--" if rel is None else "-"))
            if txt:
                ax.text(j + 0.5, -i - 0.5, txt, ha="center", va="center", fontsize=7.5,
                        color=SURFACE if face in (BLUE, ORANGE) else INK2)
    ax.set_xlim(-0.05, len(cols) + 0.05)
    ax.set_ylim(-len(Ls) - 0.05, 0.9)
    ax.set_xticks([j + 0.5 for j in range(len(cols))])
    ax.set_xticklabels([f"Δ={d}\nh={h}" for d, h in cols], fontsize=8)
    ax.set_yticks([-i - 0.5 for i in range(len(Ls))])
    ax.set_yticklabels([f"L={L}" for L in Ls], fontsize=8.5)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, fontsize=10, loc="left", color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def files_read(session, expr, where, path, title):
    from glue import lang
    from glue.core.formula import pred_from_selector

    t = session.tables["ed"]
    preds = [pred_from_selector(s) for s in lang.parse_where_text(where)]
    r = session.eval(expr, where=where)
    ctx = session.context()
    push = [p for p in preds if p.literal and p.name in r.node.part]
    reads = set()

    def go(node, sels):
        if getattr(node, "table", None) is t and node.op == "source":
            reads.update(ch.rel for ch in t.filter_chunks(sels))
            return
        if not node.eval_children:
            return go(node.expanded, sels)
        for pth, c in node.children():
            s = node.child_sels(sels).get(pth, [])
            if s is not None:
                go(c, s)

    go(r.node, push)
    del ctx
    file_grid(t, path, read=reads, title=title)
    return len(reads)


def key_grid(frame, rows, cols, highlight, path, title=""):
    """Keys of a result as a grid. highlight: set of (row value, col value) that were recomputed."""
    rv = sorted(frame[rows].unique())
    cv = sorted(frame[cols].unique())
    have = set(zip(frame[rows], frame[cols]))
    fig, ax = plt.subplots(figsize=(1.1 * len(cv) + 1.6, 0.62 * len(rv) + 1.0))
    for i, r in enumerate(rv):
        for j, c in enumerate(cv):
            if (r, c) not in have:
                continue
            hot = (r, c) in highlight
            ax.add_patch(FancyBboxPatch((j + 0.06, -i - 0.94), 0.88, 0.88, boxstyle="round,pad=0,rounding_size=0.08",
                                        facecolor=ORANGE if hot else "#ecebe8", edgecolor=ORANGE if hot else "#ecebe8"))
            ax.text(j + 0.5, -i - 0.5, "recomputed" if hot else "kept", ha="center", va="center", fontsize=7.5,
                    color=SURFACE if hot else INK2)
    ax.set_xlim(-0.05, len(cv) + 0.05)
    ax.set_ylim(-len(rv) - 0.05, 0.3)
    ax.set_xticks([j + 0.5 for j in range(len(cv))])
    ax.set_xticklabels([f"{cols}={c}" for c in cv], fontsize=8)
    ax.set_yticks([-i - 0.5 for i in range(len(rv))])
    ax.set_yticklabels([f"{rows}={r}" for r in rv], fontsize=8.5)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, fontsize=10, loc="left", color=INK)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
