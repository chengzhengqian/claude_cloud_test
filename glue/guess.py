"""Suggest a table definition by looking at a directory tree."""

import os
import re
from collections import Counter

from .errors import GlueError
from .util import relpath

SEG = re.compile(r"^(?P<name>[A-Za-z][A-Za-z]*?)_?(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
                 r"(?P<ext>\.[A-Za-z][A-Za-z0-9]*)?$")


def guess(directory, name=None, table_dir=None):
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        raise GlueError(f"not a directory: {directory}")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            if not f.startswith("."):
                files.append(os.path.relpath(os.path.join(dirpath, f), root))
        if len(files) > 5000:
            break
    if not files:
        raise GlueError(f"no files under {directory}")
    depth = Counter(f.count(os.sep) for f in files).most_common(1)[0][0]
    files = [f for f in files if f.count(os.sep) == depth]
    parts = [f.split(os.sep) for f in files]
    pattern = []
    names = []
    for level in range(depth + 1):
        segs = [p[level] for p in parts]
        ms = [SEG.match(s) for s in segs]
        if all(ms) and len({m.group("name") for m in ms}) == 1 and len({m.group("ext") for m in ms}) == 1:
            n = ms[0].group("name")
            if n in names:
                n = f"{n}{level}"
            names.append(n)
            sep = "_" if "_" in segs[0][:len(ms[0].group("name")) + 1] else ""
            pattern.append(f"{ms[0].group('name')}{sep}{{{n}}}{ms[0].group('ext') or ''}")
        elif len(set(segs)) == 1:
            pattern.append(segs[0])
        else:
            ext = os.path.splitext(segs[0])[1] if level == depth else ""
            same_ext = all(s.endswith(ext) for s in segs)
            pattern.append("*" + ext if same_ext else "*")
    ncols, header = _sniff(os.path.join(root, files[0]))
    if header and len(header) == ncols:
        columns = header
    else:
        columns = [f"c{i}" for i in range(ncols)]
    name = name or os.path.basename(root)
    base = os.path.abspath(table_dir) if table_dir else os.getcwd()
    lines = [
        'glue = "0.2"',
        'kind = "table"',
        f'name = "{name}"',
        "",
        "[source]",
        'locator = "glob"',
        f'root = "{relpath(root, base)}"',
        f'pattern = "{"/".join(pattern)}"',
        "",
        "[source.reader]",
        'format = "text"',
        "columns = [" + ", ".join(f'"{c}"' for c in columns) + "]",
        "",
        "[field]",
        "inputs = [" + ", ".join(f'"{c}"' for c in names + columns[:1]) + "]",
        "outputs = [" + ", ".join(f'"{c}"' for c in columns[1:]) + "]",
        f'axis = "{columns[0]}"',
    ]
    notes = [f"{len(files)} files matched, path inputs: {', '.join(names) or 'none'}",
             f"{ncols} columns in {files[0]}" + (" (names from its header comment)" if columns == header else
                                                  ", rename c0, c1, ... to real names")]
    return "\n".join(lines) + "\n", notes


def _sniff(path):
    header = None
    with open(path, errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                words = s.lstrip("#").split()
                if words and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", w) for w in words):
                    header = words
                continue
            return len(s.replace(",", " ").split()), header
    return 0, header
