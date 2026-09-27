"""Path templates like U_{U}/J_{J}/n_{n}.dat."""

import os
import re

from .errors import GlueError

TYPE_RE = {
    "float": r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",
    "int": r"[-+]?\d+",
    "str": r"[^/]+?",
}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([a-z]+))?\}")


class Template:
    def __init__(self, text):
        self.text = text
        parts = text.strip("/").split("/")
        if not parts or any(p == "" for p in parts):
            raise GlueError(f"bad path template {text!r}")
        self.names = []
        self.types = {}
        self.segments = []
        for part in parts:
            regex = ""
            pos = 0
            for m in _PLACEHOLDER.finditer(part):
                regex += self._literal(part[pos:m.start()])
                name, ctype = m.group(1), m.group(2) or "float"
                if ctype not in TYPE_RE:
                    raise GlueError(f"unknown placeholder type {ctype!r} in {text!r}. Use float, int, or str")
                if name in self.types:
                    raise GlueError(f"placeholder {{{name}}} appears twice in {text!r}")
                self.names.append(name)
                self.types[name] = ctype
                regex += f"(?P<{name}>{TYPE_RE[ctype]})"
                pos = m.end()
            regex += self._literal(part[pos:])
            if "{" in part[pos:] or "}" in part[pos:]:
                raise GlueError(f"bad placeholder in {text!r}")
            self.segments.append(re.compile(f"^{regex}$"))
        self.full = re.compile("^" + "/".join(s.pattern[1:-1] for s in self.segments) + "$")

    @staticmethod
    def _literal(s):
        return ".*?".join(re.escape(x) for x in s.split("*")).replace(".*?", "[^/]*?")

    def match(self, relpath):
        m = self.full.match(relpath.strip("/"))
        if not m:
            return None
        return {k: m.group(k) for k in self.names}

    def scan(self, root):
        """Walk only the directories the template can reach.

        Returns (matches, skipped, dir_mtimes) where matches is a list of
        (relpath, raw coordinate strings)."""
        if not os.path.isdir(root):
            raise GlueError(f"data directory not found: {root}")
        matches, dirs = [], {}
        skipped = [0]
        last = len(self.segments) - 1

        def walk(dirpath, depth, rel, coords):
            try:
                dirs[dirpath] = os.stat(dirpath).st_mtime_ns
                entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
            except OSError:
                return
            seg = self.segments[depth]
            for e in entries:
                if e.name.startswith("."):
                    continue
                m = seg.match(e.name)
                is_dir = e.is_dir()
                if not m or (depth < last and not is_dir) or (depth == last and is_dir):
                    if depth == last and not is_dir:
                        skipped[0] += 1
                    continue
                c = dict(coords)
                c.update(m.groupdict())
                r = f"{rel}/{e.name}" if rel else e.name
                if depth == last:
                    matches.append((r, c))
                else:
                    walk(e.path, depth + 1, r, c)

        walk(root, 0, "", {})
        return matches, skipped[0], dirs
