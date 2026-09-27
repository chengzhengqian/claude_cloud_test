"""Custom operations registered from Python with @glue.op."""

import importlib
import os
import sys
from dataclasses import dataclass

from .errors import GlueError

REGISTRY = {}


@dataclass
class OpDef:
    name: str
    fn: object
    kind: str
    version: str
    module: str
    qualname: str

    def ref(self):
        return f"{self.module}.{self.qualname}@{self.version}"


def op(fn=None, *, kind="curve", version="1", name=None):
    """Register a custom operation.

    kind="elementwise": f(y) -> y
    kind="curve":       f(x, y, **options) -> (x, y)
    kind="reduce":      f(x, y, **options) -> float
    """
    if kind not in ("elementwise", "curve", "reduce"):
        raise GlueError("kind must be elementwise, curve, or reduce")

    def register(f):
        n = name or f.__name__
        REGISTRY[n] = OpDef(n, f, kind, str(version), f.__module__, f.__qualname__)
        return f

    return register(fn) if fn is not None else register


def load_modules(modules, paths, base_dir):
    for p in paths:
        full = os.path.normpath(os.path.join(base_dir, p))
        if full not in sys.path:
            sys.path.insert(0, full)
    for m in modules:
        try:
            importlib.import_module(m)
        except ImportError as e:
            raise GlueError(f"plugin module {m!r} could not be imported: {e}") from None


def ensure(ref):
    """Import the module behind a recipe's plugin reference like mod.fn@1."""
    target, _, version = ref.partition("@")
    module = target.rsplit(".", 1)[0]
    try:
        importlib.import_module(module)
    except ImportError as e:
        raise GlueError(f"recipe needs plugin module {module!r}: {e}") from None
