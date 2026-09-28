"""Formulas (for map and transform) and predicates (for filter)."""

from dataclasses import dataclass

import numpy as np

from .. import lang
from ..errors import GlueError
from ..util import normalize

FUNCS1 = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log, "log10": np.log10,
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
}
FUNCS2 = {"min": np.minimum, "max": np.maximum}
OPS = {"+": np.add, "-": np.subtract, "*": np.multiply, "/": np.divide, "^": np.power}


def parse(text):
    try:
        ast = lang.parse_expression(text)
    except GlueError as e:
        raise GlueError(f"bad formula {text!r}: {e}") from None
    _check(ast, text)
    return ast


def canonical(text):
    return parse(text).src()


def column_name(node):
    if isinstance(node, lang.Name):
        return node.id
    if isinstance(node, lang.Attr):
        base = column_name(node.obj)
        return None if base is None else f"{base}.{node.name}"
    return None


def _check(a, text):
    if isinstance(a, (lang.Num, lang.Name)):
        return
    if isinstance(a, lang.Attr):
        if column_name(a) is None:
            raise GlueError(f"formula {text!r}: {a.src()} isn't a column name")
        return
    if isinstance(a, lang.Bin):
        _check(a.left, text)
        _check(a.right, text)
        return
    if isinstance(a, lang.Neg):
        _check(a.operand, text)
        return
    if isinstance(a, lang.Call):
        if a.kwargs:
            raise GlueError(f"formula {text!r}: {a.func}() takes no named options")
        if a.func in FUNCS1 and len(a.args) == 1:
            _check(a.args[0], text)
            return
        if a.func in FUNCS2 and len(a.args) == 2:
            for x in a.args:
                _check(x, text)
            return
        raise GlueError(f"formula {text!r}: unknown function {a.func}() with {len(a.args)} argument(s). "
                        f"Functions: {', '.join(sorted(FUNCS1))}, min(a, b), max(a, b)")
    raise GlueError(f"formula {text!r}: {a.src()} can't be used in a formula")


def names(ast):
    out = set()

    def go(a):
        if isinstance(a, (lang.Name, lang.Attr)):
            out.add(column_name(a))
        elif isinstance(a, lang.Bin):
            go(a.left)
            go(a.right)
        elif isinstance(a, lang.Neg):
            go(a.operand)
        elif isinstance(a, lang.Call):
            for x in a.args:
                go(x)

    go(ast)
    return out


def evaluate(ast, frame):
    n = len(frame)

    def ev(a):
        if isinstance(a, lang.Num):
            return np.full(n, a.value, dtype=float)
        if isinstance(a, (lang.Name, lang.Attr)):
            c = column_name(a)
            if c not in frame.columns:
                raise GlueError(f"formula: no column {c!r} (columns: {', '.join(map(str, frame.columns))})")
            col = frame[c]
            if col.dtype == object or str(col.dtype).startswith(("str", "string")):
                raise GlueError(f"formula: {c} holds strings and can't be used in arithmetic")
            return col.to_numpy(dtype=float)
        if isinstance(a, lang.Bin):
            return OPS[a.op](ev(a.left), ev(a.right))
        if isinstance(a, lang.Neg):
            v = ev(a.operand)
            return -v if a.op == "-" else v
        if isinstance(a, lang.Call):
            if a.func in FUNCS1:
                return FUNCS1[a.func](ev(a.args[0]))
            return FUNCS2[a.func](ev(a.args[0]), ev(a.args[1]))
        raise AssertionError(a)

    with np.errstate(all="ignore"):
        return ev(ast)


# ------------------------------------------------------------------ predicates


@dataclass(frozen=True)
class Pred:
    """One comparison. Compatible with lang.Selector for pushing down to sources."""

    name: str
    op: str             # = != < <= > >= range
    value: object = None
    lo: object = None
    hi: object = None
    ref: str = None     # compare with another column instead of a literal

    def src(self):
        if self.ref is not None:
            return f"{self.name}{self.op}{self.ref}"
        return lang.Selector(self.name, self.op, self.value, self.lo, self.hi).src()

    @property
    def literal(self):
        return self.ref is None


def pred_from_selector(s):
    v = s.value
    if isinstance(v, list):
        v = tuple(v)
    return Pred(s.name, s.op, v, s.lo, s.hi)


def parse_predicate(text):
    """`U=2.0, T=0.1:0.5, a.E<0, T>=r.lo` → list of Pred."""
    if not text or not text.strip():
        return []
    p = lang.Parser(text)
    out = []
    while True:
        name = p.expect_name("a column name")
        while p.accept_op("."):
            name += "." + p.expect_name()
        tok = p.peek()
        if tok.kind != "OP" or tok.value not in ("=", "!=", "<", "<=", ">", ">="):
            p.error("expected =, !=, <, <=, > or >=")
        op = p.next().value
        if p.at("NAME") and p.peek().value not in lang.KEYWORDS:
            ref = p.next().value
            while p.accept_op("."):
                ref += "." + p.expect_name()
            out.append(Pred(name, op, ref=ref))
        elif op == "=" and p.at_op(":"):
            p.next()
            hi = None if p._range_end() else p.parse_signed()
            out.append(Pred(name, "range", lo=None, hi=hi))
        else:
            value = p.parse_value()
            if op == "=" and p.at_op(":"):
                p.next()
                hi = None if p._range_end() else p.parse_signed()
                out.append(Pred(name, "range", lo=value, hi=hi))
            else:
                out.append(Pred(name, op, tuple(value) if isinstance(value, list) else value))
        if not p.accept_op(","):
            break
    p.expect_end()
    return out


def predicate_text(preds):
    return ", ".join(p.src() for p in preds)


def pred_mask(pred, frame, digits=10):
    if pred.name not in frame.columns:
        raise GlueError(f"filter {pred.src()}: no column {pred.name!r} "
                        f"(columns: {', '.join(map(str, frame.columns))})")
    col = frame[pred.name]
    is_str = col.dtype == object or str(col.dtype).startswith(("str", "string"))
    if pred.ref is not None:
        if pred.ref not in frame.columns:
            raise GlueError(f"filter {pred.src()}: no column {pred.ref!r}")
        a, b = col.to_numpy(), frame[pred.ref].to_numpy()
        return {"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[pred.op]
    vals = col.to_numpy()
    if pred.op == "range":
        if is_str:
            raise GlueError(f"a range doesn't work on the string column {pred.name}")
        m = np.ones(len(vals), dtype=bool)
        if pred.lo is not None:
            m &= vals >= pred.lo
        if pred.hi is not None:
            m &= vals <= pred.hi
        return m

    def norm(t):
        if is_str:
            return str(t)
        if isinstance(t, str):
            raise GlueError(f"{pred.name} is a number, but the filter compares it with the string {t!r}")
        return normalize(t, "float", digits)

    if pred.op in ("=", "!="):
        targets = pred.value if isinstance(pred.value, tuple) else (pred.value,)
        targets = [norm(t) for t in targets]
        if is_str:
            m = np.isin(vals.astype(str), targets)
        else:
            m = np.isin(np.round(vals.astype(float), digits), targets)
        return m if pred.op == "=" else ~m
    t = norm(pred.value)
    return {"<": vals < t, "<=": vals <= t, ">": vals > t, ">=": vals >= t}[pred.op]


def apply_preds(preds, frame, digits=10):
    if not preds or len(frame) == 0:
        return frame
    m = np.ones(len(frame), dtype=bool)
    for p in preds:
        m &= pred_mask(p, frame, digits)
    return frame[m].reset_index(drop=True)
