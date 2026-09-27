"""Command-line entry point."""

import argparse
import os
import sys

from .errors import GlueError


def _session(project, trust, out=print, warn_stale=True):
    from .session import Session

    s = Session(trust=trust, log=out, warn_stale=warn_stale)
    if project:
        s.load(project)
    elif os.path.exists("project.toml"):
        s.load("project.toml")
    return s


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv and argv[0] in ("run", "status", "refresh", "scan") else None
    ap = argparse.ArgumentParser(prog="glue", description="Describe, combine, and plot data spread across files.")
    ap.add_argument("--trust", action="store_true", help="load Python plugins without asking")
    if sub == "run":
        ap.add_argument("cmd")
        ap.add_argument("script")
        ap.add_argument("--project", default=None)
        ap.add_argument("--quiet", action="store_true", help="don't echo statements")
    elif sub == "refresh":
        ap.add_argument("cmd")
        ap.add_argument("name", nargs="?")
        ap.add_argument("--all", action="store_true")
        ap.add_argument("--full", action="store_true")
        ap.add_argument("--force", action="store_true")
        ap.add_argument("--project", default=None)
    elif sub in ("status", "scan"):
        ap.add_argument("cmd")
        ap.add_argument("project", nargs="?")
    else:
        ap.add_argument("project", nargs="?")
    args = ap.parse_args(argv)
    from .shell import Shell

    try:
        if sub == "run":
            from .session import Session

            s = Session(trust=args.trust)
            if args.project:
                s.load(args.project)
            Shell(s, echo=not args.quiet).run_file(args.script)
            return 0
        if sub == "status":
            s = _session(args.project, args.trust, warn_stale=False)
            for line in s.status_lines():
                print(line)
            return 0
        if sub == "refresh":
            s = _session(args.project, args.trust, warn_stale=False)
            if not args.name and not args.all:
                print("error: refresh needs a dataset name, or --all", file=sys.stderr)
                return 2
            for line in s.refresh(args.name, full=args.full, force=args.force):
                print(line)
            return 0
        if sub == "scan":
            s = _session(args.project, args.trust)
            for line in s.rescan():
                print(line)
            return 0
        s = _session(args.project, args.trust)
        print("glue 0.1. Type help for commands, quit to leave.")
        Shell(s).loop()
        return 0
    except GlueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
