import datetime as _dt
import hashlib
import math
import os
import re
import tomllib

from .errors import GlueError

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def normalize(value, ctype="float", digits=10):
    if ctype == "str":
        return str(value)
    if ctype == "int":
        return int(value)
    v = float(value)
    if math.isnan(v):
        return v
    v = round(v, digits)
    return 0.0 if v == 0 else v


def fmt_value(v):
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float):
        return repr(v) if v != int(v) or abs(v) >= 1e15 else f"{v:.1f}"
    return str(v)


def fmt_key(coords, key):
    return ", ".join(f"{c}={fmt_value(v)}" for c, v in zip(coords, key))


def expand_path(path, base_dir):
    def sub(m):
        name = m.group(1)
        if name not in os.environ:
            raise GlueError(f"environment variable {name} is not set (used in path {path!r})")
        return os.environ[name]

    p = _ENV_RE.sub(sub, str(path))
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)
    return os.path.normpath(p)


def read_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise GlueError(f"file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise GlueError(f"{path}: {e}") from None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_name(name, what="name"):
    from .lang import RESERVED

    if not NAME_RE.match(name):
        raise GlueError(f"invalid {what} {name!r}: use letters, digits, and _")
    if name in RESERVED:
        raise GlueError(f"{name!r} is a reserved word and can't be used as a {what}")


# ---------------------------------------------------------------- TOML output

def _toml_str(s):
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def _toml_key(k):
    return k if re.match(r"^[A-Za-z0-9_-]+$", k) else _toml_str(k)


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def toml_dumps(data):
    lines = []

    def emit(table, prefix):
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict) or _inline(v)}
        subs = {k: v for k, v in table.items() if isinstance(v, dict) and not _inline(v)}
        if prefix and (scalars or not subs):
            if lines:
                lines.append("")
            lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            lines.append(f"{_toml_key(k)} = {toml_value(v)}")
        for k, v in subs.items():
            emit(v, f"{prefix}.{_toml_key(k)}" if prefix else _toml_key(k))

    emit(data, "")
    return "\n".join(lines) + "\n"


class Inline(dict):
    """A dict written as a TOML inline table."""


def _inline(v):
    return isinstance(v, Inline)


def set_toml_entry(path, section, key, value_text):
    """Add or replace `key = value` in `[section]` of a TOML file, keeping everything else."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    new_line = f"{key} = {value_text}"
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [header, new_line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        for i in range(start + 1, end):
            if key_re.match(lines[i]):
                lines[i] = new_line
                break
        else:
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, new_line)
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def remove_toml_entry(path, section, key):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    lines = [ln for i, ln in enumerate(lines) if not (start < i < end and key_re.match(ln))]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def relpath(path, start):
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return path
