"""The plot command: a plot spec, drawn with matplotlib or written for gnuplot."""

import os
import shutil
import subprocess
from dataclasses import dataclass, field

import numpy as np

from . import lang
from .errors import GlueError
from .settings import PLOT_SETTINGS, SETTINGS, convert
from .util import fmt_value, relpath

PLOT_OPTIONS = {"style", "logx", "logy", "xlim", "ylim", "title", "xlabel", "ylabel", "cmap", "legend",
                "errorbars", "size", "backend"}
LINESTYLES = ["-", "--", ":", "-."]
MARKERS = ["o", "s", "^", "D", "v"]


@dataclass
class PlotItem:
    ast: object
    label: str = None


@dataclass
class PlotSpec:
    items: list
    vs: str = None
    by: list = field(default_factory=list)
    where: list = field(default_factory=list)
    options: list = field(default_factory=list)
    outfile: str = None
    add: bool = False
    text: str = ""


def parse_plot(p, text=""):
    """Parse after the `plot` word."""
    spec = PlotSpec(items=[], text=text)
    if p.accept_op("+"):
        spec.add = True
    while True:
        if p.name_then_eq():
            label = p.next().value
            p.expect_op("=")
            spec.items.append(PlotItem(p.parse_expr(), label))
        else:
            spec.items.append(PlotItem(p.parse_expr()))
        if not p.accept_op(","):
            break
    while not p.at_end():
        if p.accept_word("vs"):
            spec.vs = p.expect_name("an x name after vs")
        elif p.accept_word("by"):
            spec.by = [p.expect_name("an input after by")]
            while p.at_op(",") and _comma_then_plain_name(p):
                p.next()
                spec.by.append(p.expect_name())
        elif p.accept_word("where"):
            spec.where = p.parse_selectors()
        elif p.accept_word("with"):
            spec.options = p.parse_options()
        elif p.accept_op(">"):
            spec.outfile = p.raw_word()
        else:
            tok = p.peek()
            p.error(f"unexpected {tok.text or tok.value!r}. Expected vs, by, where, with, or > FILE")
    return spec


def _comma_then_plain_name(p):
    save = (p.pos, p._peeked, p._prev)
    p.next()
    tok = p.peek()
    ok = tok.kind == "NAME" and tok.value not in lang.KEYWORDS
    if ok:
        p.next()
        ok = not (p.at("OP") and p.peek().value in ("=", "<", ">", "!=", "<=", ">="))
    p.pos, p._peeked, p._prev = save
    return ok


def _optval(node):
    if node is None:
        return True
    if isinstance(node, lang.Num):
        return node.value
    if isinstance(node, lang.Str):
        return node.value
    if isinstance(node, lang.Name):
        return node.id
    if isinstance(node, lang.TupleLit):
        return tuple(_optval(i) for i in node.items)
    raise GlueError(f"bad plot option value {node.src()}")


def split_options(options, defaults):
    plot = dict(defaults)
    settings = {}
    for name, node in options:
        if name in SETTINGS:
            if node is None:
                raise GlueError(f"setting {name} needs a value")
            settings[name] = convert(name, node)
        elif name in PLOT_OPTIONS:
            v = _optval(node)
            if name in ("style", "backend", "cmap"):
                v = PLOT_SETTINGS[name][0](v) if name in PLOT_SETTINGS else v
            if name in ("xlim", "ylim", "size") and not (isinstance(v, tuple) and len(v) == 2):
                raise GlueError(f"{name} needs (a, b)")
            if name == "legend" and v not in ("auto", "off", "colorbar"):
                raise GlueError("legend must be auto, off, or colorbar")
            plot[name] = v
        else:
            raise GlueError(f"unknown plot option {name!r}. Options: {', '.join(sorted(PLOT_OPTIONS))}, "
                            f"or a setting like method or grid")
    return plot, settings


@dataclass
class Series:
    label: str
    node: object
    frame: object
    err: object = None

    @property
    def yname(self):
        return self.node.type.output_names[0]

    @property
    def unit(self):
        return self.node.type.outputs[0].unit

    @property
    def ylabel(self):
        o = self.node.type.outputs[0]
        return o.label or (self.node.name if self.node.name and len(self.node.name) < 30 else o.name)


def _source_select(node):
    """(table, column) if node is a column read straight from a table, else None."""
    from .core import nodes as N

    while isinstance(node, (N.FilterNode, N.RenameNode)):
        node = node.kid()
    if isinstance(node, N.SelectNode) and isinstance(node.kid(), N.SourceNode):
        return node.kid(), node.params["outputs"][0]
    return None


def _column_text(session, name, unit=""):
    label, u = name, unit
    for t in list(session.tables.values()) + list(session.datasets.values()):
        if name in t.meta and (t.x == name or name in t.coords or name in t.meta):
            m = t.meta[name]
            label = m.label or name
            u = u or m.unit
            break
    return f"{label} [{u}]" if u else label


def plot(session, spec, log=print):
    from .core import nodes as N

    opts, overrides = split_options(spec.options, session.plot_defaults)
    series = []
    for item in spec.items:
        node, comp = session.compile(item.ast, overrides=overrides or None)
        if len(node.type.outputs) != 1:
            outs = node.type.output_names
            raise GlueError(f"plot needs one output, but {item.ast.src()} has {len(outs)} "
                            f"({', '.join(outs) or 'none'}). Pick one, as in {item.ast.src()}.{outs[0] if outs else 'E'}")
        frame, ctx, _, _ = session.evaluate(node, comp, spec.where)
        for line in ctx.report.lines(session.settings.get("report")):
            log("  " + line)
        if len(frame) == 0:
            raise GlueError(f"{item.ast.src()} has no data" + (" for " + ", ".join(s.src() for s in spec.where)
                                                                if spec.where else ""))
        s = Series(item.label or item.ast.src(), node, frame)
        if opts.get("errorbars"):
            found = _source_select(node)
            if found and found[0].table.m(found[1]).error:
                src, col = found
                enode = N.select(src, [src.table.m(col).error])
                eframe, _, _, _ = session.evaluate(enode, None, spec.where)
                ins = node.type.input_names
                merged = frame[ins].merge(eframe, on=ins, how="left")
                s.err = merged[src.table.m(col).error].to_numpy(dtype=float)
        series.append(s)

    by = list(spec.by)
    inputs = []
    for s in series:
        for c in s.node.type.input_names:
            if c not in inputs:
                inputs.append(c)
    for c in by:
        if c not in inputs:
            raise GlueError(f"by {c}: the plotted values have no input {c} (they have {', '.join(inputs)})")

    def varying(c):
        return any(c in s.frame.columns and s.frame[c].nunique() > 1 for s in series)

    xname = spec.vs
    if xname is None:
        axes = {s.node.type.axis for s in series}
        if len(axes) == 1 and None not in axes:
            xname = axes.pop()
        else:
            free = [c for c in inputs if c not in by and varying(c)]
            if len(free) != 1:
                raise GlueError("choose the horizontal axis with `vs INPUT`, as in vs " + (free[0] if free else "U"))
            xname = free[0]
    if xname not in inputs:
        raise GlueError(f"vs {xname}: not an input of the plotted values (inputs: {', '.join(inputs)})")
    if any(not s.node.type.has_input(xname) for s in series):
        raise GlueError(f"vs {xname}: every plotted value needs the input {xname}")
    exact_x = all(s.node.type.var(xname).exact for s in series)
    kind = "keyed" if exact_x else "curves"

    free = [c for c in inputs if c not in by and c != xname and varying(c)]
    if free and not by and len(free) == 1:
        by = free
        free = []
    if free:
        more = ", ".join(free)
        raise GlueError(f"input{'s' if len(free) > 1 else ''} {more} not fixed. Curves for different values "
                        f"would overlap. Add {free[0]} to where, or use: by {', '.join(by + free)}")
    if len(by) > 2:
        raise GlueError("by takes at most two inputs (color and line style)")
    if len(by) == 2 and len(series) > 1:
        raise GlueError("with several plotted values, line style marks the value, so by takes one input")

    fixed = {}
    for c in inputs:
        if c in by or c == xname:
            continue
        vals = set()
        for s in series:
            if c in s.frame.columns:
                vals |= set(s.frame[c].unique())
        if len(vals) == 1:
            fixed[c] = vals.pop()

    backend = opts.get("backend", "matplotlib")
    if backend == "gnuplot":
        return _gnuplot(session, spec, series, kind, xname, by, fixed, opts, log)
    return _matplotlib(session, spec, series, kind, xname, by, fixed, opts, log)


def _values(series, c):
    vals = set()
    for s in series:
        if c in s.frame.columns:
            vals |= set(s.frame[c].unique())
    return sorted(vals, key=lambda v: (isinstance(v, str), v))


def _matplotlib(session, spec, series, kind, xname, by, fixed, opts, log):
    import matplotlib

    if spec.outfile or not _interactive():
        matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    if spec.add and session.figure is not None:
        fig, ax, state = session.figure
    else:
        fig, ax = plt.subplots(figsize=opts.get("size", (6, 4)), layout="constrained")
        state = {"handles": [], "labels": [], "series": 0, "colorbar": False}
        session.figure = (fig, ax, state)

    style = opts.get("style", "lines")
    cmap = matplotlib.colormaps[opts.get("cmap", "viridis")]
    color_by = by[0] if by else None
    style_by = by[1] if len(by) > 1 else None
    cvals = _values(series, color_by) if color_by else []
    numeric = bool(cvals) and not any(isinstance(v, str) for v in cvals)
    if numeric and len(cvals) > 1:
        lo, hi = float(min(cvals)), float(max(cvals))
        norm = matplotlib.colors.Normalize(lo, hi)

        def color(v):
            return cmap(0.05 + 0.8 * norm(float(v)))
    elif cvals:
        palette = matplotlib.colormaps["tab10"]
        index = {v: i for i, v in enumerate(cvals)}

        def color(v):
            return palette(index[v] % 10)
    else:
        def color(v):
            return matplotlib.colormaps["tab10"](state["series"] % 10) if len(series) == 1 else "0.15"

    svals = _values(series, style_by) if style_by else []
    for si, s in enumerate(series):
        f = s.frame
        yname = s.yname
        groups = by if by else []
        grouped = f.groupby(groups, sort=True) if groups else [((), f)]
        ls_series = LINESTYLES[(state["series"] + si) % len(LINESTYLES)] if len(series) > 1 or spec.add else "-"
        for key, g in grouped:
            key = key if isinstance(key, tuple) else (key,)
            kd = dict(zip(groups, key))
            c = color(kd[color_by]) if color_by else matplotlib.colormaps["tab10"]((state["series"] + si) % 10)
            ls = LINESTYLES[svals.index(kd[style_by]) % len(LINESTYLES)] if style_by else ls_series
            g = g.sort_values(xname)
            x = g[xname].to_numpy(dtype=float)
            y = g[yname].to_numpy(dtype=float)
            marker = None
            if style in ("points", "linespoints") or kind == "keyed":
                marker = MARKERS[si % len(MARKERS)] if len(series) > 1 else "o"
            lw = 0 if style == "points" else 1.6
            if s.err is not None:
                err = s.err[g.index.to_numpy()]
                points_only = lw == 0 or not any(k == "style" for k, _ in spec.options)
                ax.errorbar(x, y, yerr=err, color=c, ls="none" if points_only else ls, lw=lw,
                            marker=marker or "o", ms=3.5, capsize=2, elinewidth=0.9)
            else:
                ax.plot(x, y, color=c, ls=ls if lw else "none", lw=lw, marker=marker, ms=3.5)
        if len(series) > 1 or spec.add:
            hc = "0.15" if color_by else matplotlib.colormaps["tab10"]((state["series"] + si) % 10)
            if s.err is not None and not any(k == "style" for k, _ in spec.options):
                handle = Line2D([], [], color=hc, ls="none", marker="o", ms=4)
            else:
                handle = Line2D([], [], color=hc, ls=ls_series, lw=1.6,
                                marker=MARKERS[si % len(MARKERS)] if kind == "keyed" else None)
            state["handles"].append(handle)
            state["labels"].append(s.label)
    state["series"] += len(series)

    legend = opts.get("legend", "auto")
    handles, labels = list(state["handles"]), list(state["labels"])
    use_colorbar = numeric and len(cvals) > 1 and (legend == "colorbar" or (legend == "auto" and len(cvals) > 6))
    if color_by and legend != "off":
        if use_colorbar and not state["colorbar"]:
            import matplotlib.cm as cm

            if len(cvals) <= 12:
                bounds = _bounds(cvals)
                sub = matplotlib.colors.ListedColormap([color(v) for v in cvals])
                sm = cm.ScalarMappable(norm=matplotlib.colors.BoundaryNorm(bounds, len(cvals)), cmap=sub)
                cb = fig.colorbar(sm, ax=ax, ticks=[float(v) for v in cvals], pad=0.02)
                cb.ax.set_yticklabels([fmt_value(v) for v in cvals])
            else:
                sm = cm.ScalarMappable(norm=matplotlib.colors.Normalize(min(cvals), max(cvals)),
                                       cmap=matplotlib.colors.ListedColormap([color(v) for v in np.linspace(min(cvals), max(cvals), 256)]))
                cb = fig.colorbar(sm, ax=ax, pad=0.02)
            cb.set_label(_column_text(session, color_by))
            state["colorbar"] = True
        elif not use_colorbar and not state["colorbar"]:
            for v in cvals:
                handles.append(Line2D([], [], color=color(v), lw=2.5))
                labels.append(f"{color_by}={fmt_value(v)}")
    if style_by and legend != "off":
        for v in svals:
            handles.append(Line2D([], [], color="0.15", ls=LINESTYLES[svals.index(v) % len(LINESTYLES)], lw=1.6))
            labels.append(f"{style_by}={fmt_value(v)}")
    if handles and legend != "off":
        ax.legend(handles, labels, frameon=False, fontsize=9)

    s0 = series[0]
    ax.set_xlabel(opts.get("xlabel") or _column_text(session, xname))
    if opts.get("ylabel"):
        ax.set_ylabel(opts["ylabel"])
    elif len(series) == 1 and not spec.add:
        name = spec.items[0].label or s0.ylabel
        ax.set_ylabel(f"{name} [{s0.unit}]" if s0.unit else name)
    else:
        units = {s.unit for s in series}
        names = {s.yname for s in series}
        unit = units.pop() if len(units) == 1 else ""
        name = names.pop() if len(names) == 1 else ""
        if name or unit:
            ax.set_ylabel(f"{name} [{unit}]".strip() if unit else name)
    title = opts.get("title")
    if title is None and fixed:
        title = ", ".join(f"{c}={fmt_value(v)}" for c, v in fixed.items())
    if title:
        ax.set_title(title, fontsize=11)
    if opts.get("logx"):
        ax.set_xscale("log")
    if opts.get("logy"):
        ax.set_yscale("log")
    if opts.get("xlim"):
        ax.set_xlim(*opts["xlim"])
    if opts.get("ylim"):
        ax.set_ylim(*opts["ylim"])
    ax.grid(True, color="0.9", lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    out = spec.outfile
    if out is None and not _interactive():
        base = session.project_dir or os.getcwd()
        out = os.path.join(base, ".glue", "last_plot.png")
    if out:
        out = os.path.abspath(os.path.join(session.project_dir or os.getcwd(), out)) if not os.path.isabs(out) else out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fig.savefig(out, dpi=150)
        log(f"  saved {relpath(out, os.getcwd())}")
        if session.plot_defaults.get("save_script") and spec.outfile:
            _write_script(session, spec, out)
        return out
    plt.show(block=False)
    plt.pause(0.05)
    return None


def _bounds(vals):
    v = [float(x) for x in vals]
    mids = [(a + b) / 2 for a, b in zip(v, v[1:])]
    first = v[0] - (mids[0] - v[0])
    last = v[-1] + (v[-1] - mids[-1])
    return [first] + mids + [last]


def _interactive():
    import matplotlib

    backend = matplotlib.get_backend().lower()
    if backend in ("agg", "pdf", "svg", "ps", "cairo", "template") or "inline" in backend:
        return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt"
                or backend.startswith("macosx"))


def _write_script(session, spec, out):
    script = os.path.splitext(out)[0] + ".glue"
    with open(script, "w") as f:
        if session.project_path:
            f.write(f"load {relpath(session.project_path, os.path.dirname(script))}\n")
        f.write(spec.text.strip() + "\n")


def _gnuplot(session, spec, series, kind, xname, by, fixed, opts, log):
    from .session import write_blocks

    base = session.project_dir or os.getcwd()
    out = os.path.abspath(os.path.join(base, spec.outfile or os.path.join(".glue", "last_plot.gp")))
    stem, ext = os.path.splitext(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = []
    image = ext.lower() in (".png", ".pdf", ".svg")
    if image:
        term = {".png": "pngcairo size 900,600", ".pdf": "pdfcairo size 6in,4in", ".svg": "svg size 900,600"}[ext.lower()]
        lines += [f"set terminal {term}", f"set output '{os.path.basename(out)}'"]
    lines.append(f"set xlabel '{_column_text(session, xname)}'")
    if opts.get("logx"):
        lines.append("set logscale x")
    if opts.get("logy"):
        lines.append("set logscale y")
    title = opts.get("title") or ", ".join(f"{c}={fmt_value(v)}" for c, v in fixed.items())
    if title:
        lines.append(f"set title '{title}'")
    plots = []
    for i, s in enumerate(series):
        data = f"{stem}_{i}.dat"
        frame = s.frame[by + [xname, s.yname]].sort_values(by + [xname])
        write_blocks(frame, list(by), data)
        keys = [k for k, _ in frame.groupby(by, sort=False)] if by else [()]
        with_ = "points" if opts.get("style") == "points" or kind == "keyed" else "lines"
        for k, key in enumerate(keys):
            key = key if isinstance(key, tuple) else (key,)
            ttl = s.label + ("" if not by else " " + ", ".join(f"{c}={fmt_value(v)}" for c, v in zip(by, key)))
            plots.append(f"'{os.path.basename(data)}' index {k} using 1:2 with {with_} title '{ttl}'")
    lines.append("plot " + ", \\\n     ".join(plots))
    script = stem + ".gp"
    with open(script, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"  wrote {relpath(script, os.getcwd())}")
    if image:
        if shutil.which("gnuplot"):
            subprocess.run(["gnuplot", os.path.basename(script)], cwd=os.path.dirname(script), check=True)
            log(f"  saved {relpath(out, os.getcwd())}")
        else:
            log("  gnuplot is not installed, so only the script was written")
    return out
