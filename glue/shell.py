"""The interactive shell and script runner."""

import os
import re
import subprocess
import traceback

from . import lang
from .elaborate import BUILTINS
from .errors import GlueError
from .guess import guess
from .plotting import parse_plot, plot, split_options
from .settings import PLOT_SETTINGS, SETTINGS, convert_options
from .util import relpath

HELP = {
    "": """\
Statements:
  NAME = EXPR [with ...]      define a variable (nothing is computed yet)
  EXPR [where ...]            show values
Commands:
  load FILE [as NAME]              reload           guess DIR           new table NAME ...
  edit NAME        scan [NAME]     ls               info NAME           values NAME.INPUT
  show EXPR        explain STMT    del NAME         save NAME ...       export EXPR to FILE
  status [--all]   refresh NAME|--all               pin NAME            unpin NAME
  invalidate NAME [where ...]      why NAME         gc                  plot Y ...
  set [KEY VALUE]  unset KEY       run FILE         py                  help [TOPIC]   quit
Topics: help expressions, help functions, help settings, help plot, help COMMAND""",
    "expressions": """\
  dmft.E                 output E of table dmft: the field [U, J, n, T → E]
  dmft.E[U=0.1, n=0.5]   select curves; T=0.1:0.5 selects an x range
  dmft.E - ed.E          matched on shared exact inputs, and aligned along ragged T onto a common grid
  dmft.E / n             an input's values, point by point
  C / T                  a bare input name takes its points from the field it meets
  dmft.E @ T=0.1         evaluate each curve at a point
  (a - b) with grid=overlap(n=500), method=cubic
  dmft.E[E<0]            filters can also use outputs
  rename(ed.E, u=U)      rename an input or output before combining
  transform(ed.E, u, U = u * 2.0)   replace an input with a formula of it
Every expression is translated into a core tree. `explain EXPR` shows it.""",
    "functions": """\
  abs sqrt exp log log10 sin cos tan sinh cosh tanh      elementwise
  resample(y, grid=..., method=...)                     evaluate on a new grid
  d(y, T, order=1, method=fd|pchip|..., grid=...)       derivative
  int(y, T, method=trapz|...)                           cumulative integral
  integral(y, T)                                        integral over each curve (one value per key)
  max min mean sum first last count argmax argmin       reduce along the axis: max(y) or max(y, n)
  at(y, T=0.1)  or  y @ T=0.1                           value at a point
  stack(a=y1, b=y2, tag="source")                       stack fields, adding a string input
  using(expr, method=cubic, ...)                        evaluate expr with these settings
  rename(y, old=new)                                    rename inputs or outputs
  transform(y, u, U = u * 2.0)                          replace input u with a formula
  swap(y, T)                                            trade the input T with y's output
  legendre(y, T, slope=p, result=G)                     Legendre transform along T
Reductions, d, int, integral, and resample act along the default axis unless you name
another input: mean(Eg, n), resample(y, grid=..., along=n).""",
    "settings": """\
  method       linear cubic pchip akima smooth(s=...)       interpolation
  grid         overlap(n=200) overlap(step=0.01) union() like(NAME) linspace(a,b,n) logspace(a,b,n) points([...])
  extrapolate  nan error extend clamp
  duplicates   error mean first last drop                   repeated x within a curve
  fewpoints    linear drop error                            curves with too few points for the method
  unmatched    drop error                                   keys on only one side
  nan_rows drop|error   strict true|false   report short|full|off
  fingerprint stat|hash   cache_format parquet|npz   datasets_dir PATH   read_cache_mb N
  align auto|[T]   which inputs to interpolate along when combining fields
  branches error|split   flat_tol X   for swap and legendre
  disk_cache true|false  keep expensive intermediate results in .glue/cache
  plot.backend plot.style plot.cmap plot.size plot.save_script
Use `set KEY VALUE`, `unset KEY`, or `with KEY=VALUE` on one statement.""",
    "plot": """\
  plot Y [, Y ...] [vs X] [by C1 [, C2]] [where ...] [with ...] [> FILE]
  plot + Y ...                     add to the current figure
  plot dmft.E vs T by J where U=0.1, n=0.5
  plot dmft=dmft.E, ed=ed.E by J where U=0.1, n=0.5 > figs/compare.png
  plot argmax(C) vs U by J where n=0.5
  options: style=lines|points|linespoints logx logy xlim=(a,b) ylim=(a,b) title="..."
           xlabel="..." ylabel="..." cmap=NAME legend=auto|off|colorbar errorbars size=(w,h)
           backend=matplotlib|gnuplot, and settings like method=cubic
  Every input must be fixed by where, used in by, or used as vs.""",
    "load": ("load FILE [as NAME]   load a project, table, calc, or dataset file. NAME renames a single file;\n"
             "                      for a project it is a prefix, so `load old/project.toml as old` gives old_dmft"),
    "reload": "reload              reload all loaded files from disk",
    "guess": "guess DIR [name=NAME]   suggest a table definition for a directory tree",
    "new": 'new table NAME pattern="U_{U}/n_{n}.dat" columns=["T","E"] [root="data"] [x=T] [file=PATH]',
    "edit": "edit NAME           open the file behind NAME in $EDITOR, then reload",
    "scan": "scan [NAME]         rebuild the chunk index",
    "ls": "ls                  list tables, views, datasets, and variables",
    "info": "info NAME           type, inputs, outputs, and status, from the chunk index",
    "values": "values NAME.INPUT   distinct values of an input",
    "show": "show EXPR [where ...] [with ...] [limit N]",
    "explain": "explain STATEMENT   show what would be read and computed, without reading data",
    "del": "del NAME            remove a session variable",
    "save": ("save NAME [as PATH] [where ...] [grid=...] [format=parquet|npz] [unit=\"...\"]   save a dataset\n"
             "save NAME --recipe-only    save the calculation tree only, as a calc file\n"
             "save NAME --view           write the variable as a view in the project file"),
    "export": "export EXPR to FILE.csv|.parquet|.dat [where ...] [with ...]",
    "status": ("status [NAME]       dataset states: fresh, stale, orphaned, modified, invalidated\n"
               "status --all        also the cached values under each view, calc, and dataset"),
    "invalidate": ("invalidate TABLE [where ...]   mark files as changed, so what depends on them recomputes\n"
                   "invalidate NAME               drop cached values of a view or calc, or mark a dataset invalid"),
    "why": "why NAME            the calculation tree of NAME, with the state of its leaves",
    "gc": "gc                  delete cached values that no view, calc, or dataset uses",
    "refresh": "refresh NAME|--all [--full] [--force]",
    "pin": "pin NAME            never recompute NAME automatically",
    "unpin": "unpin NAME",
    "set": "set                 show settings\nset KEY VALUE       change a setting for this session",
    "unset": "unset KEY",
    "run": "run FILE.glue       run a script",
    "py": "py                  Python prompt with glue_session",
    "quit": "quit",
}


class Quit(Exception):
    pass


class Shell:
    def __init__(self, session, out=print, echo=False):
        self.session = session
        self.out = out
        self.echo = echo
        self.base_dir = os.getcwd()
        session.log = out

    # ---------------------------------------------------------------- running

    def execute(self, text):
        text = text.strip()
        if not text or text.startswith("#"):
            return
        flags = set(re.findall(r"(?<!\S)--([a-z][a-z-]*)", text))
        body = re.sub(r"(?<!\S)--[a-z][a-z-]*", "", text)
        p = lang.Parser(body)
        tok = p.peek()
        if tok.kind == "NAME" and tok.value in lang.COMMANDS:
            p.next()
            getattr(self, f"cmd_{tok.value}")(p, flags, text)
        elif tok.kind == "NAME" and p.name_then_eq():
            self.assign(p)
        else:
            self.cmd_show(p, flags, text)

    def run_lines(self, lines, source="<input>"):
        for lineno, stmt in _statements(lines):
            if self.echo:
                self.out(f"> {stmt}")
            try:
                self.execute(stmt)
            except GlueError as e:
                raise GlueError(f"{source}:{lineno}: {e}") from None

    def run_file(self, path):
        path = os.path.join(self.base_dir, path)
        if not os.path.exists(path):
            raise GlueError(f"script not found: {path}")
        old = self.base_dir
        self.base_dir = os.path.dirname(os.path.abspath(path))
        try:
            with open(path) as f:
                self.run_lines(f.read().splitlines(), relpath(path, os.getcwd()))
        finally:
            self.base_dir = old

    def loop(self):
        try:
            import readline

            hist = os.path.expanduser("~/.glue_history")
            try:
                readline.read_history_file(hist)
            except OSError:
                pass
            readline.set_completer(self._complete)
            readline.set_completer_delims(" \t\n()[],=+-*/^@")
            readline.parse_and_bind("tab: complete")
        except ImportError:
            readline = None
            hist = None
        buf = ""
        while True:
            try:
                line = input("... " if buf else "> ")
            except EOFError:
                self.out("")
                break
            except KeyboardInterrupt:
                self.out("")
                buf = ""
                continue
            if line.rstrip().endswith("\\"):
                buf += line.rstrip()[:-1] + " "
                continue
            stmt, buf = buf + line, ""
            try:
                self.execute(stmt)
            except Quit:
                break
            except GlueError as e:
                self.out(f"error: {e}")
            except KeyboardInterrupt:
                self.out("interrupted")
            except Exception as e:  # show unexpected errors without leaving the shell
                self.out(f"internal error: {type(e).__name__}: {e}")
                if os.environ.get("GLUE_DEBUG"):
                    traceback.print_exc()
        if readline is not None and hist:
            try:
                readline.write_history_file(hist)
            except OSError:
                pass

    def _complete(self, text, state):
        s = self.session
        words = list(lang.COMMANDS) + list(lang.KEYWORDS) + sorted(BUILTINS) + s.names() + list(SETTINGS)
        if "." in text:
            base, _, part = text.rpartition(".")
            ent = s.lookup(base)
            cols = []
            if ent is not None and ent.table is not None:
                t = ent.table
                cols = list(t.coords) + ([t.x] if t.x else []) + list(t.values)
            matches = [f"{base}.{c}" for c in cols if c.startswith(part)]
        else:
            matches = sorted({w for w in words if w.startswith(text)})
        return matches[state] if state < len(matches) else None

    def _report(self, ctx):
        for line in ctx.report.lines(self.session.settings.get("report")):
            self.out(f"  {line}")

    def _lines(self, lines):
        for line in lines:
            self.out(f"  {line}")

    def _path(self, p):
        return os.path.join(self.base_dir, p)

    # ---------------------------------------------------------------- statements

    def assign(self, p):
        name = p.next().value
        p.expect_op("=")
        ast = p.parse_expr()
        clauses = p.parse_clauses(("with",))
        p.expect_end()
        overrides = convert_options(clauses.get("with", []))
        self.session.define(name, ast, overrides)

    def _expr_clauses(self, p, allowed=("where", "with", "limit")):
        ast = p.parse_expr()
        clauses = p.parse_clauses(allowed)
        p.expect_end()
        overrides = convert_options(clauses.get("with", []))
        node, comp = self.session.compile(ast, overrides=overrides or None)
        return ast, node, comp, clauses.get("where", []), clauses.get("limit")

    def cmd_show(self, p, flags, text):
        ast, node, comp, where, limit = self._expr_clauses(p)
        limit = 20 if limit is None else limit
        frame, ctx, done, dropped = self.session.evaluate(node, comp, where, limit=limit + 1)
        self._report(ctx)
        if len(frame) == 0:
            self.out("  (no data)")
            return
        import pandas as pd

        with pd.option_context("display.max_columns", 20, "display.width", 120):
            for line in frame.head(limit).to_string(index=False).splitlines():
                self.out(f"  {line}")
        if len(frame) > limit:
            self.out(f"  ... more rows (use limit N, or export to a file)")

    # ---------------------------------------------------------------- commands

    def cmd_load(self, p, flags, text):
        path = self._path(p.raw_word())
        name = None
        if p.accept_word("as"):
            name = p.expect_name("a name")
        p.expect_end()
        self.session.load(path, name=name)

    def cmd_reload(self, p, flags, text):
        p.expect_end()
        self.session.reload()

    def cmd_guess(self, p, flags, text):
        d = p.raw_word("a directory")
        opts = dict(p.parse_options(spaces=True)) if not p.at_end() else {}
        name = opts.get("name")
        toml, notes = guess(self._path(d), name.value if isinstance(name, lang.Str) else getattr(name, "id", None),
                            self.session.project_dir)
        self._lines(notes)
        self.out("")
        for line in toml.splitlines():
            self.out(f"  {line}")

    def cmd_new(self, p, flags, text):
        if not p.accept_word("table"):
            p.error("expected: new table NAME pattern=... columns=[...]")
        name = p.expect_name("a table name")
        opts = dict(p.parse_options(spaces=True))
        p.expect_end()
        self.out("  " + self.session_new_table(name, opts))

    def session_new_table(self, name, opts):
        from .util import check_name, set_toml_entry

        s = self.session
        check_name(name, "table name")
        for k in opts:
            if k not in ("pattern", "columns", "root", "x", "format", "file"):
                raise GlueError(f"new table: unknown option {k!r}")
        if "pattern" not in opts or "columns" not in opts:
            raise GlueError('new table needs pattern="..." and columns=["T", "E"]')

        def text_of(node):
            if isinstance(node, lang.Str):
                return node.value
            if isinstance(node, lang.Name):
                return node.id
            raise GlueError(f"expected a string, got {node.src()}")

        cols_node = opts["columns"]
        if isinstance(cols_node, lang.ListLit):
            cols = [text_of(i) for i in cols_node.items]
        else:
            cols = [c.strip() for c in text_of(cols_node).split(",")]
        base = s.project_dir or os.getcwd()
        file = os.path.join(base, text_of(opts["file"]) if "file" in opts else f"{name}.toml")
        if os.path.exists(file):
            raise GlueError(f"{relpath(file, os.getcwd())} already exists")
        root = text_of(opts["root"]) if "root" in opts else "."
        x = text_of(opts["x"]) if "x" in opts else cols[0]
        fmt = text_of(opts["format"]) if "format" in opts else "text"
        pattern = text_of(opts["pattern"])
        path_inputs = [m for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-z]+)?\}", pattern) if m not in cols]
        quoted = lambda names: "[" + ", ".join(f'"{c}"' for c in names) + "]"  # noqa: E731
        lines = ['glue = "0.2"', 'kind = "table"', "", "[source]", 'locator = "glob"',
                 f'root = "{relpath(os.path.join(base, root), os.path.dirname(file))}"',
                 f'pattern = "{pattern}"', "", "[source.reader]", f'format = "{fmt}"',
                 "columns = " + quoted(cols), "", "[field]", "inputs = " + quoted(path_inputs + [x]),
                 "outputs = " + quoted([c for c in cols if c != x and c != "_"]), f'axis = "{x}"', ""]
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "w") as f:
            f.write("\n".join(lines))
        s.load(file)
        msg = f"wrote {relpath(file, os.getcwd())}"
        if s.project_path:
            set_toml_entry(s.project_path, "tables", name, f'"{relpath(file, s.project_dir)}"')
            msg += f" and added it to {relpath(s.project_path, os.getcwd())}"
        return msg

    def cmd_edit(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        s = self.session
        ent = s.lookup(name)
        if ent is None:
            raise GlueError(f"unknown name {name!r}")
        if ent.table is not None and ent.table.path:
            path = ent.table.path
        elif ent.kind == "view" and s.project_path:
            path = s.project_path
        else:
            raise GlueError(f"{name} has no file to edit")
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        subprocess.run([editor, path], check=False)
        s.reload()

    def cmd_scan(self, p, flags, text):
        name = None if p.at_end() else p.expect_name()
        p.expect_end()
        self._lines(self.session.rescan(name))

    def cmd_ls(self, p, flags, text):
        p.expect_end()
        self._lines(self.session.ls_lines())

    def cmd_info(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        self._lines(self.session.info_lines(name))

    def cmd_values(self, p, flags, text):
        ast = p.parse_expr()
        p.expect_end()
        from .util import fmt_value

        self.out("  " + " ".join(fmt_value(v) for v in self.session.values(ast)))

    def cmd_explain(self, p, flags, text):
        s = self.session
        if p.accept_word("plot"):
            spec = parse_plot(p, text)
            _, overrides = split_options(spec.options, s.plot_defaults)
            for item in spec.items:
                node, comp = s.compile(item.ast, overrides=overrides or None)
                self._lines(s.explain(node, comp, spec.where))
            return
        if p.accept_word("save"):
            name = p.expect_name()
            node, comp = s.compile(lang.Name(name))
            self._lines(s.explain(node, comp))
            return
        if p.at("NAME") and p.name_then_eq():
            name = p.next().value
            p.expect_op("=")
            ast = p.parse_expr()
            clauses = p.parse_clauses(("with",))
            p.expect_end()
            node, comp = s.compile(ast, name=name, overrides=convert_options(clauses.get("with", [])) or None)
            self._lines(s.explain(node, comp))
            return
        _, node, comp, where, _ = self._expr_clauses(p, ("where", "with"))
        self._lines(s.explain(node, comp, where))

    def cmd_del(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        self.session.delete(name)

    def cmd_save(self, p, flags, text):
        from .settings import parse_grid

        name = p.expect_name("a name to save")
        path, where, grid, fmt, unit = None, [], None, None, None
        while not p.at_end():
            if p.accept_word("as"):
                path = p.raw_word()
            elif p.accept_word("where"):
                where = p.parse_selectors()
            elif p.name_then_eq():
                key = p.next().value
                p.expect_op("=")
                v = p.parse_optvalue()
                if key == "grid":
                    grid = parse_grid(v)
                elif key == "format":
                    fmt = v.id if isinstance(v, lang.Name) else getattr(v, "value", None)
                    if fmt not in ("parquet", "npz"):
                        raise GlueError("format must be parquet or npz")
                elif key == "unit":
                    if not isinstance(v, lang.Str):
                        raise GlueError('unit needs a string, as in unit="t"')
                    unit = v.value
                else:
                    raise GlueError(f"save has no option {key!r}. Options: grid, format, unit")
            else:
                p.error("expected as, where, grid=, format=, or unit=")
        self._lines(self.session.save(name, path, where, grid, fmt, unit, recipe_only="recipe-only" in flags,
                                      view="view" in flags))

    def cmd_export(self, p, flags, text):
        ast = p.parse_expr()
        if not p.accept_word("to"):
            p.error("expected: export EXPR to FILE")
        path = p.raw_word()
        clauses = p.parse_clauses(("where", "with"))
        p.expect_end()
        overrides = convert_options(clauses.get("with", []))
        node, comp = self.session.compile(ast, overrides=overrides or None)
        base = self.session.project_dir or self.base_dir
        self._lines(self.session.export(node, comp, os.path.join(base, path), clauses.get("where", [])))

    def cmd_status(self, p, flags, text):
        name = None if p.at_end() else p.expect_name()
        p.expect_end()
        self._lines(self.session.status_lines(name, all="all" in flags))

    def cmd_refresh(self, p, flags, text):
        name = None if p.at_end() else p.expect_name()
        p.expect_end()
        if name is None and "all" not in flags:
            raise GlueError("refresh needs a dataset name, or --all")
        self._lines(self.session.refresh(name, full="full" in flags, force="force" in flags))

    def cmd_pin(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        self._lines(self.session.pin(name, True))

    def cmd_unpin(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        self._lines(self.session.pin(name, False))

    def cmd_invalidate(self, p, flags, text):
        name = p.expect_name()
        where = []
        if p.accept_word("where"):
            where = p.parse_selectors()
        p.expect_end()
        self._lines(self.session.invalidate(name, where))

    def cmd_why(self, p, flags, text):
        name = p.expect_name()
        p.expect_end()
        self._lines(self.session.why(name))

    def cmd_gc(self, p, flags, text):
        p.expect_end()
        self._lines(self.session.gc())

    def cmd_plot(self, p, flags, text):
        spec = parse_plot(p, text)
        plot(self.session, spec, log=self.out)

    def cmd_set(self, p, flags, text):
        if p.at_end():
            self._lines(self.session.settings_lines())
            return
        key = p.expect_name("a setting name")
        if p.accept_op("."):
            key = f"{key}.{p.expect_name()}"
        if key not in SETTINGS and not (key.startswith("plot.") and key[5:] in PLOT_SETTINGS):
            raise GlueError(f"unknown setting {key!r}. Settings: {', '.join(SETTINGS)}, plot.*")
        p.accept_op("=")
        value = p.parse_optvalue()
        p.expect_end()
        self.session.set_setting(key, value)

    def cmd_unset(self, p, flags, text):
        key = p.expect_name()
        if p.accept_op("."):
            key = f"{key}.{p.expect_name()}"
        p.expect_end()
        self.session.unset_setting(key)

    def cmd_run(self, p, flags, text):
        self.run_file(p.raw_word())

    def cmd_py(self, p, flags, text):
        import code

        import glue

        self.out("Python prompt. The session is glue_session. exit() or Ctrl-D returns to glue.")
        try:
            code.interact(local={"glue_session": self.session, "glue": glue}, banner="", exitmsg="")
        except SystemExit:
            pass

    def cmd_help(self, p, flags, text):
        topic = "" if p.at_end() else p.expect_name()
        if topic not in HELP:
            raise GlueError(f"no help for {topic!r}. Topics: {', '.join(k for k in HELP if k)}")
        for line in HELP[topic].splitlines():
            self.out(line)

    def cmd_quit(self, p, flags, text):
        raise Quit()


def _statements(lines):
    buf, start = "", None
    for i, line in enumerate(lines, 1):
        if start is None:
            start = i
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        stmt = (buf + line).strip()
        buf = ""
        if stmt and not stmt.startswith("#"):
            yield start, stmt
        start = None
