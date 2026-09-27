"""Settings, grid specs, and interpolation method specs."""

from dataclasses import dataclass

from . import lang
from .errors import GlueError

METHODS = {
    # name: (minimum points, passes through data points)
    "linear": (2, True),
    "cubic": (4, True),
    "pchip": (2, True),
    "akima": (5, True),
    "smooth": (4, False),
}


@dataclass(frozen=True)
class MethodSpec:
    name: str
    s: float = None

    @property
    def min_points(self):
        return METHODS[self.name][0]

    def text(self):
        if self.name == "smooth":
            return f"smooth(s={self.s!r})" if self.s is not None else "smooth()"
        return self.name


@dataclass(frozen=True)
class GridSpec:
    kind: str  # overlap union like linspace logspace points
    n: int = None
    step: float = None
    a: float = None
    b: float = None
    points: tuple = None
    like: object = None  # AST node for like(...)

    def text(self):
        k = self.kind
        if k == "overlap":
            return f"overlap(step={self.step!r})" if self.step is not None else f"overlap(n={self.n})"
        if k == "union":
            return "union()"
        if k == "like":
            return f"like({self.like.src()})"
        if k in ("linspace", "logspace"):
            return f"{k}({self.a!r}, {self.b!r}, {self.n})"
        if k == "points":
            return "points([" + ", ".join(repr(p) for p in self.points) + "])"
        raise AssertionError(k)

    @property
    def fixed(self):
        return self.kind in ("linspace", "logspace", "points")


def _num(node, what):
    if isinstance(node, lang.Num):
        return node.value
    raise GlueError(f"{what} must be a number, got {node.src()}")


def _int(node, what):
    v = _num(node, what)
    if v != int(v) or v < 1:
        raise GlueError(f"{what} must be a positive whole number")
    return int(v)


def parse_method(node, allow_fd=False):
    if isinstance(node, str):
        node = lang.parse_optvalue_text(node)
    if isinstance(node, lang.Str):
        node = lang.parse_optvalue_text(node.value)
    names = sorted(METHODS) + (["fd"] if allow_fd else [])
    if isinstance(node, lang.Name):
        if node.id == "smooth":
            return MethodSpec("smooth", None)
        if node.id == "fd" and allow_fd:
            return MethodSpec("fd")
        if node.id in METHODS:
            return MethodSpec(node.id)
    if isinstance(node, lang.Call) and node.func == "smooth":
        s = node.kw("s")
        if s is None and node.args:
            s = node.args[0]
        return MethodSpec("smooth", None if s is None else _num(s, "s"))
    raise GlueError(f"unknown method {node.src()}. Use one of: {', '.join(names)}, smooth(s=...)")


def parse_grid(node):
    if isinstance(node, str):
        node = lang.parse_optvalue_text(node)
    if isinstance(node, lang.Str):
        node = lang.parse_optvalue_text(node.value)
    if isinstance(node, lang.Name) and node.id == "union":
        return GridSpec("union")
    if not isinstance(node, lang.Call):
        raise GlueError(f"unknown grid {node.src()}. Use overlap(n=...), union(), like(...), "
                        "linspace(a, b, n), logspace(a, b, n), or points([...])")
    f, args = node.func, node.args
    if f == "overlap":
        n, step = node.kw("n"), node.kw("step")
        if args:
            n = args[0]
        if step is not None:
            v = _num(step, "step")
            if v <= 0:
                raise GlueError("step must be positive")
            return GridSpec("overlap", step=v)
        return GridSpec("overlap", n=200 if n is None else _int(n, "n"))
    if f == "union":
        return GridSpec("union")
    if f == "like":
        if len(args) != 1:
            raise GlueError("like() takes one argument, as in like(dmft.E)")
        return GridSpec("like", like=args[0])
    if f in ("linspace", "logspace"):
        vals = list(args) + [v for k, v in node.kwargs if k in ("start", "stop", "n")]
        if len(vals) != 3:
            raise GlueError(f"{f} takes three arguments: {f}(start, stop, n)")
        a, b = _num(vals[0], "start"), _num(vals[1], "stop")
        if f == "logspace" and (a <= 0 or b <= 0):
            raise GlueError("logspace needs positive start and stop")
        return GridSpec(f, a=a, b=b, n=_int(vals[2], "n"))
    if f == "points":
        if len(args) != 1 or not isinstance(args[0], lang.ListLit):
            raise GlueError("points takes a list, as in points([0.1, 0.2])")
        return GridSpec("points", points=tuple(_num(i, "point") for i in args[0].items))
    raise GlueError(f"unknown grid {f}()")


def _choice(options):
    def conv(node):
        if isinstance(node, str):
            v = node
        elif isinstance(node, (lang.Name, lang.Str)):
            v = node.id if isinstance(node, lang.Name) else node.value
        else:
            v = node.src()
        if v not in options:
            raise GlueError(f"must be one of: {', '.join(options)} (got {v})")
        return v
    return conv


def _bool(node):
    if isinstance(node, bool):
        return node
    v = node if isinstance(node, str) else getattr(node, "id", getattr(node, "value", node.src() if hasattr(node, "src") else node))
    if v in ("true", "on", "yes", True):
        return True
    if v in ("false", "off", "no", False):
        return False
    raise GlueError("must be true or false")


def _intval(node):
    if isinstance(node, int) and not isinstance(node, bool):
        return node
    if isinstance(node, lang.Num) and node.value == int(node.value):
        return int(node.value)
    raise GlueError("must be a whole number")


def _string(node):
    if isinstance(node, str):
        return node
    if isinstance(node, lang.Str):
        return node.value
    if isinstance(node, lang.Name):
        return node.id
    raise GlueError("must be a string")


SETTINGS = {
    # key: (converter, default, to_text)
    "method": (parse_method, MethodSpec("pchip"), lambda v: v.text()),
    "grid": (parse_grid, GridSpec("overlap", n=200), lambda v: v.text()),
    "extrapolate": (_choice(["nan", "error", "extend", "clamp"]), "nan", str),
    "duplicates": (_choice(["error", "mean", "first", "last", "drop"]), "error", str),
    "fewpoints": (_choice(["linear", "drop", "error"]), "linear", str),
    "unmatched": (_choice(["drop", "error"]), "drop", str),
    "nan_rows": (_choice(["drop", "error"]), "drop", str),
    "strict": (_bool, False, lambda v: "true" if v else "false"),
    "report": (_choice(["short", "full", "off"]), "short", str),
    "fingerprint": (_choice(["stat", "hash"]), "stat", str),
    "cache_format": (_choice(["parquet", "npz"]), "parquet", str),
    "datasets_dir": (_string, "results", str),
    "read_cache_mb": (_intval, 512, str),
}

RECIPE_KEYS = ["method", "grid", "extrapolate", "duplicates", "fewpoints", "unmatched", "nan_rows"]

PLOT_SETTINGS = {
    "backend": (_choice(["matplotlib", "gnuplot"]), "matplotlib"),
    "style": (_choice(["lines", "points", "linespoints"]), "lines"),
    "cmap": (_string, "viridis"),
    "size": (None, (6.0, 4.0)),
    "save_script": (_bool, False),
}


def convert(key, value):
    """Convert a setting from an AST node, a TOML value, or text."""
    if key not in SETTINGS:
        raise GlueError(f"unknown setting {key!r}. Settings: {', '.join(SETTINGS)}")
    conv = SETTINGS[key][0]
    try:
        if isinstance(value, str) and key in ("method", "grid"):
            return conv(lang.parse_optvalue_text(value))
        if isinstance(value, (bool, int, float)) and not isinstance(value, lang.Node):
            if key in ("strict",):
                return _bool(value)
            if key == "read_cache_mb":
                return _intval(int(value))
        return conv(value)
    except GlueError as e:
        raise GlueError(f"setting {key}: {e}") from None


def to_text(key, value):
    return SETTINGS[key][2](value)


class Settings:
    """Four levels: statement overrides > session > project > built-in."""

    LEVELS = ("statement", "session", "project", "default")

    def __init__(self, project=None):
        self.project = {}
        self.session = {}
        self._stack = []
        for k, v in (project or {}).items():
            self.project[k] = convert(k, v)

    def get(self, key):
        for layer in reversed(self._stack):
            if key in layer:
                return layer[key]
        if key in self.session:
            return self.session[key]
        if key in self.project:
            return self.project[key]
        return SETTINGS[key][1]

    def source(self, key):
        for layer in reversed(self._stack):
            if key in layer:
                return "statement"
        if key in self.session:
            return "session"
        if key in self.project:
            return "project"
        return "default"

    def push(self, overrides):
        self._stack.append(overrides)

    def pop(self):
        self._stack.pop()

    def effective(self, keys=None):
        return {k: self.get(k) for k in (keys or SETTINGS)}


def convert_options(options, allowed=None):
    """Turn [(name, ast)] from a with-clause into converted settings."""
    out = {}
    for name, node in options:
        if allowed is not None and name not in allowed:
            raise GlueError(f"option {name!r} isn't allowed here")
        if node is None:
            raise GlueError(f"setting {name} needs a value, as in {name}=...")
        out[name] = convert(name, node)
    return out
