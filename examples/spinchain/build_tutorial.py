"""Build TUTORIAL.md from TUTORIAL.src.md by running every example in it.

Nothing in TUTORIAL.md is typed by hand: each command runs, and its real output is pasted
under it. Run from anywhere:

    python build_tutorial.py            # about two minutes, most of it in make_data.py

Fenced blocks in the source:

    ```glue            statements for one glue shell that lives for the whole tutorial
    ```python          Python, in one namespace shared by all python blocks. `shell` and
                       `session` are the glue shell and its session.
    ```python hidden   the same, but not shown (used to draw the diagrams)
    ```sh              shell commands, run in this folder
    ```toml write=P    write the block to file P, and show it
    ```show P [lines=A-B] [lang=L]   show part of a real file
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
FENCE = re.compile(r"^```(\S+)?(.*)$")


def reset():
    for d in ("results", "calcs", "figs", "exports", ".glue", "tables", "ops"):
        shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)
    for f in ("project.toml",):
        if os.path.exists(os.path.join(HERE, f)):
            os.remove(os.path.join(HERE, f))
    os.makedirs(os.path.join(HERE, "figs"))


class Runner:
    def __init__(self):
        sys.path.insert(0, REPO)
        from glue.session import Session
        from glue.shell import Shell

        self.out = []
        self.shell = Shell(Session(trust=True, log=self.out.append), out=self.out.append)
        self.shell.base_dir = HERE
        self.ns = {"shell": self.shell, "session": self.shell.session, "__name__": "tutorial"}
        self.errors = []

    def glue(self, lines):
        from glue.errors import GlueError

        shown = []
        stmts, buf = [], ""
        for line in lines:
            if line.rstrip().endswith("\\"):
                buf += line.rstrip()[:-1] + " "
                continue
            stmts.append((buf + line).strip())
            buf = ""
        for stmt in stmts:
            if not stmt:
                shown.append("")
                continue
            if stmt.startswith("#"):
                shown.append(stmt)
                continue
            self.out.clear()
            try:
                self.shell.execute(stmt)
            except GlueError as e:
                self.out.append(f"error: {e}")
                self.errors.append(stmt)
            self.ns["session"] = self.shell.session
            shown.append(f"> {stmt}")
            for o in self.out:
                for part in str(o).splitlines() or [""]:
                    shown.append(part if part.startswith("  ") or not part else "  " + part)
        return ["```", *shown, "```"]

    def python(self, lines, hidden=False):
        code = "\n".join(lines)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                exec(compile(code, "<tutorial>", "exec"), self.ns)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                raise
        if hidden:
            return []
        out = ["```python", *lines, "```"]
        text = buf.getvalue().rstrip()
        if text:
            out += ["", "```", *text.splitlines(), "```"]
        return out

    def sh(self, lines):
        shown = []
        env = dict(os.environ, PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
        for line in lines:
            if not line.strip():
                continue
            r = subprocess.run(line, shell=True, cwd=HERE, capture_output=True, text=True, env=env)
            shown.append(f"$ {line}")
            text = (r.stdout + r.stderr).rstrip()
            if text:
                shown += text.splitlines()
            if r.returncode != 0:
                self.errors.append(line)
        return ["```", *shown, "```"]

    def write(self, lang, path, lines):
        full = os.path.join(HERE, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return [f"```{lang}", *lines, "```"]

    def show(self, args):
        parts = args.split()
        path = parts[0]
        opts = dict(p.split("=", 1) for p in parts[1:])
        with open(os.path.join(HERE, path)) as f:
            text = f.read().splitlines()
        if "lines" in opts:
            a, b = (int(x) for x in opts["lines"].split("-"))
            more = len(text) > b
            text = text[a - 1:b] + (["..."] if more else [])
        return [f"```{opts.get('lang', '')}", *text, "```"]


def main():
    os.chdir(HERE)
    reset()
    r = Runner()
    src = open(os.path.join(HERE, "TUTORIAL.src.md")).read().splitlines()
    out = ["<!-- Built by build_tutorial.py from TUTORIAL.src.md. Edit the source, not this file. -->", ""]
    i = 0
    while i < len(src):
        m = FENCE.match(src[i])
        if not m or m.group(1) is None:
            out.append(src[i])
            i += 1
            continue
        kind, args = m.group(1), m.group(2).strip()
        j = i + 1
        while not src[j].startswith("```"):
            j += 1
        body = src[i + 1:j]
        if kind == "glue":
            out += r.glue(body)
        elif kind == "python":
            if args.startswith("write="):
                out += r.write("python", args[len("write="):], body)
            else:
                out += r.python(body, hidden=args == "hidden")
        elif kind == "sh":
            out += r.sh(body)
        elif kind == "show":
            out += r.show(args)
        elif args.startswith("write="):
            out += r.write(kind, args[len("write="):], body)
        else:
            out += [src[i], *body, "```"]
        i = j + 1
    text = "\n".join(out).rstrip() + "\n"
    text = re.sub(r"\n{3,}(?=[^`]*$|!\[)", "\n\n", text)     # hidden blocks leave extra blank lines
    with open(os.path.join(HERE, "TUTORIAL.md"), "w") as f:
        f.write(text)
    print(f"wrote TUTORIAL.md. Statements that gave an error: {len(r.errors)}")
    for e in r.errors:
        print("  ", e)


if __name__ == "__main__":
    main()
