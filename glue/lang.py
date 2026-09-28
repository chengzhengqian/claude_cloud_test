"""Lexer, parser, and AST for the glue expression and command language."""

import re
from dataclasses import dataclass, field

from .errors import ParseError

COMMANDS = {
    "load", "reload", "guess", "new", "edit", "scan", "ls", "info", "values", "show",
    "explain", "del", "save", "export", "status", "refresh", "pin", "unpin", "plot",
    "set", "unset", "run", "py", "help", "quit", "invalidate", "why", "gc",
}
KEYWORDS = {"with", "where", "vs", "by", "to", "as"}
RESERVED = COMMANDS | KEYWORDS

_NUMBER = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPS2 = ("**", "<=", ">=", "!=")
_OPS1 = set("+-*/^@=<>()[],.:")


@dataclass
class Token:
    kind: str  # NAME NUMBER STRING OP EOF
    value: object
    pos: int
    text: str = ""


# ------------------------------------------------------------------ AST

PREC = {"@": 1, "+": 2, "-": 2, "*": 3, "/": 3, "neg": 4, "^": 5}


class Node:
    prec = 9

    def src(self):
        raise NotImplementedError

    def __str__(self):
        return self.src()


def _wrap(node, min_prec):
    s = node.src()
    return f"({s})" if node.prec < min_prec else s


@dataclass(eq=False)
class Num(Node):
    value: float
    text: str

    def src(self):
        return self.text


@dataclass(eq=False)
class Str(Node):
    value: str

    def src(self):
        return '"' + self.value.replace('"', '\\"') + '"'


@dataclass(eq=False)
class Name(Node):
    id: str
    pos: int = -1

    def src(self):
        return self.id


@dataclass(eq=False)
class Attr(Node):
    obj: Node
    name: str

    def src(self):
        return f"{_wrap(self.obj, 9)}.{self.name}"


@dataclass(eq=False)
class Selector:
    name: str
    op: str  # = != < <= > >= range
    value: object = None  # number, string, or list
    lo: object = None
    hi: object = None

    def src(self):
        if self.op == "range":
            lo = "" if self.lo is None else _lit(self.lo)
            hi = "" if self.hi is None else _lit(self.hi)
            return f"{self.name}={lo}:{hi}"
        return f"{self.name}{self.op}{_lit(self.value)}"


def _lit(v):
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_lit(x) for x in v) + "]"
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return f"{v:.1f}"
    return repr(v) if isinstance(v, float) else str(v)


@dataclass(eq=False)
class Sel(Node):
    obj: Node
    selectors: list

    def src(self):
        return f"{_wrap(self.obj, 9)}[{', '.join(s.src() for s in self.selectors)}]"


@dataclass(eq=False)
class Call(Node):
    func: str
    args: list = field(default_factory=list)
    kwargs: list = field(default_factory=list)  # list of (name, Node)
    pos: int = -1

    def src(self):
        parts = [a.src() for a in self.args]
        parts += [f"{k}={v.src()}" for k, v in self.kwargs]
        return f"{self.func}({', '.join(parts)})"

    def kw(self, name, default=None):
        for k, v in self.kwargs:
            if k == name:
                return v
        return default


@dataclass(eq=False)
class ListLit(Node):
    items: list

    def src(self):
        return "[" + ", ".join(i.src() for i in self.items) + "]"


@dataclass(eq=False)
class TupleLit(Node):
    items: list

    def src(self):
        return "(" + ", ".join(i.src() for i in self.items) + ")"


@dataclass(eq=False)
class Bin(Node):
    op: str
    left: Node
    right: Node

    @property
    def prec(self):
        return PREC[self.op]

    def src(self):
        p = self.prec
        if self.op == "^":
            return f"{_wrap(self.left, p + 1)}^{_wrap(self.right, p)}"
        return f"{_wrap(self.left, p)} {self.op} {_wrap(self.right, p + 1)}"


@dataclass(eq=False)
class Neg(Node):
    op: str
    operand: Node
    prec = PREC["neg"]

    def src(self):
        return f"{self.op}{_wrap(self.operand, self.prec)}"


@dataclass(eq=False)
class AtE(Node):
    obj: Node
    name: str
    value: Node  # Num or ListLit
    prec = PREC["@"]

    def src(self):
        return f"{_wrap(self.obj, 2)} @ {self.name}={self.value.src()}"


def walk(node):
    yield node
    if isinstance(node, Attr):
        yield from walk(node.obj)
    elif isinstance(node, Sel):
        yield from walk(node.obj)
    elif isinstance(node, Call):
        for a in node.args:
            yield from walk(a)
        for _, v in node.kwargs:
            yield from walk(v)
    elif isinstance(node, (ListLit, TupleLit)):
        for i in node.items:
            yield from walk(i)
    elif isinstance(node, Bin):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Neg):
        yield from walk(node.operand)
    elif isinstance(node, AtE):
        yield from walk(node.obj)
        yield from walk(node.value)


# ------------------------------------------------------------------ parser


class Parser:
    """Recursive-descent parser. Tokens are produced on demand, so commands
    can switch to reading raw words (file paths) at any point."""

    def __init__(self, text):
        self.text = text
        self.pos = 0
        self._peeked = None
        self._prev = None

    # --- lexing

    def _lex(self):
        t = self.text
        n = len(t)
        i = self.pos
        while i < n and t[i] in " \t\r\n":
            i += 1
        if i < n and t[i] == "#":
            i = n
        if i >= n:
            self.pos = n
            return Token("EOF", None, n)
        c = t[i]
        prev = self._prev
        after_value = prev is not None and (prev.kind in ("NAME", "NUMBER", "STRING") or prev.value in (")", "]"))
        if c.isdigit() or (c == "." and i + 1 < n and t[i + 1].isdigit() and not after_value):
            m = _NUMBER.match(t, i)
            self.pos = m.end()
            return Token("NUMBER", float(m.group()), i, m.group())
        if c.isalpha() or c == "_":
            m = _NAME.match(t, i)
            self.pos = m.end()
            return Token("NAME", m.group(), i, m.group())
        if c in "\"'":
            j = i + 1
            buf = []
            while j < n and t[j] != c:
                if t[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(t[j])
                j += 1
            if j >= n:
                raise ParseError("unterminated string", t, i)
            self.pos = j + 1
            return Token("STRING", "".join(buf), i, t[i:j + 1])
        two = t[i:i + 2]
        if two in _OPS2:
            self.pos = i + 2
            return Token("OP", two, i, two)
        if c in _OPS1:
            self.pos = i + 1
            return Token("OP", c, i, c)
        raise ParseError(f"unexpected character {c!r}", t, i)

    def peek(self):
        if self._peeked is None:
            self._peeked = self._lex()
        return self._peeked

    def next(self):
        tok = self.peek()
        self._peeked = None
        self._prev = tok
        return tok

    def at(self, kind, value=None):
        tok = self.peek()
        return tok.kind == kind and (value is None or tok.value == value)

    def at_op(self, value):
        return self.at("OP", value)

    def at_word(self, word):
        return self.at("NAME", word)

    def accept_op(self, value):
        if self.at_op(value):
            return self.next()
        return None

    def accept_word(self, word):
        if self.at_word(word):
            return self.next()
        return None

    def expect_op(self, value):
        tok = self.peek()
        if not (tok.kind == "OP" and tok.value == value):
            self.error(f"expected {value!r}")
        return self.next()

    def expect_name(self, what="a name"):
        tok = self.peek()
        if tok.kind != "NAME":
            self.error(f"expected {what}")
        return self.next().value

    def error(self, message, pos=None):
        if pos is None:
            pos = self.peek().pos
        raise ParseError(message, self.text, pos)

    def at_end(self):
        return self.peek().kind == "EOF"

    def expect_end(self):
        if not self.at_end():
            tok = self.peek()
            self.error(f"unexpected {tok.text or tok.value!r}")

    def raw_word(self, what="a file name"):
        """Read a quoted string or a run of non-space characters."""
        if self._peeked is not None:
            self.pos = self._peeked.pos
            self._peeked = None
        t = self.text
        i = self.pos
        while i < len(t) and t[i] in " \t":
            i += 1
        if i >= len(t) or t[i] == "#":
            self.pos = i
            self.error(f"expected {what}", i)
        if t[i] in "\"'":
            self.pos = i
            tok = self.next()
            return tok.value
        j = i
        while j < len(t) and t[j] not in " \t":
            j += 1
        self.pos = j
        self._prev = Token("STRING", t[i:j], i)
        return t[i:j]

    def rest(self):
        if self._peeked is not None:
            self.pos = self._peeked.pos
            self._peeked = None
        s = self.text[self.pos:].strip()
        self.pos = len(self.text)
        return s

    # --- expressions

    def parse_expr(self):
        node = self.parse_additive()
        if self.at_op("@"):
            self.next()
            name = self.expect_name("an x name after @")
            self.expect_op("=")
            if self.at_op("["):
                value = self.parse_list_node()
            else:
                value = self.parse_signed_node()
            node = AtE(node, name, value)
        return node

    def parse_additive(self):
        node = self.parse_mult()
        while self.at_op("+") or self.at_op("-"):
            op = self.next().value
            node = Bin(op, node, self.parse_mult())
        return node

    def parse_mult(self):
        node = self.parse_unary()
        while self.at_op("*") or self.at_op("/"):
            op = self.next().value
            node = Bin(op, node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.at_op("-") or self.at_op("+"):
            op = self.next().value
            operand = self.parse_unary()
            if isinstance(operand, Num) and op == "-":
                return Num(-operand.value, "-" + operand.text)
            if isinstance(operand, Num):
                return operand
            return Neg(op, operand)
        return self.parse_power()

    def parse_power(self):
        base = self.parse_postfix()
        if self.at_op("^") or self.at_op("**"):
            self.next()
            return Bin("^", base, self.parse_unary())
        return base

    def parse_postfix(self):
        node = self.parse_primary()
        while True:
            if self.at_op("."):
                self.next()
                node = Attr(node, self.expect_name("a column name after '.'"))
            elif self.at_op("["):
                self.next()
                sels = [self.parse_selector()]
                while self.accept_op(","):
                    sels.append(self.parse_selector())
                self.expect_op("]")
                node = Sel(node, sels)
            else:
                return node

    def parse_primary(self):
        tok = self.peek()
        if tok.kind == "NUMBER":
            self.next()
            return Num(tok.value, tok.text)
        if tok.kind == "STRING":
            self.next()
            return Str(tok.value)
        if tok.kind == "NAME":
            if tok.value in KEYWORDS:
                self.error(f"unexpected keyword {tok.value!r}")
            self.next()
            if self.at_op("("):
                return self.parse_call(tok.value, tok.pos)
            return Name(tok.value, tok.pos)
        if tok.kind == "OP" and tok.value == "(":
            self.next()
            node = self.parse_expr()
            self.expect_op(")")
            return node
        if tok.kind == "OP" and tok.value == "[":
            return self.parse_list_node()
        if tok.kind == "EOF":
            self.error("expected an expression")
        self.error(f"unexpected {tok.text or tok.value!r}")

    def parse_call(self, func, pos):
        self.expect_op("(")
        args, kwargs = [], []
        if not self.at_op(")"):
            while True:
                tok = self.peek()
                if tok.kind == "NAME" and self._name_then_eq():
                    name = self.next().value
                    self.expect_op("=")
                    kwargs.append((name, self.parse_expr()))
                else:
                    if kwargs:
                        self.error("positional argument after keyword argument")
                    args.append(self.parse_expr())
                if not self.accept_op(","):
                    break
        self.expect_op(")")
        return Call(func, args, kwargs, pos)

    def _name_then_eq(self):
        save = (self.pos, self._peeked, self._prev)
        self.next()
        result = self.at_op("=")
        self.pos, self._peeked, self._prev = save
        return result

    def name_then_eq(self):
        return self.at("NAME") and self._name_then_eq()

    # --- literals, selectors, options

    def parse_signed(self):
        sign = 1.0
        if self.at_op("-"):
            self.next()
            sign = -1.0
        elif self.at_op("+"):
            self.next()
        tok = self.peek()
        if tok.kind != "NUMBER":
            self.error("expected a number")
        self.next()
        return sign * tok.value

    def parse_signed_node(self):
        start = self.peek().pos
        v = self.parse_signed()
        return Num(v, self.text[start:self._prev.pos + len(self._prev.text)].replace(" ", ""))

    def parse_value(self, allow_names=False):
        tok = self.peek()
        if tok.kind == "STRING":
            self.next()
            return tok.value
        if tok.kind == "OP" and tok.value == "[":
            self.next()
            items = [] if self.at_op("]") else [self.parse_value(allow_names)]
            while self.accept_op(","):
                items.append(self.parse_value(allow_names))
            self.expect_op("]")
            return items
        if allow_names and tok.kind == "NAME":
            self.next()
            return tok.value
        return self.parse_signed()

    def parse_list_node(self):
        self.expect_op("[")
        items = []
        if not self.at_op("]"):
            items.append(self.parse_optvalue())
            while self.accept_op(","):
                items.append(self.parse_optvalue())
        self.expect_op("]")
        return ListLit(items)

    def parse_selector(self):
        name = self.expect_name("an input name")
        tok = self.peek()
        if tok.kind != "OP" or tok.value not in ("=", "!=", "<", "<=", ">", ">="):
            self.error("expected =, !=, <, <=, > or >=")
        op = self.next().value
        if op == "=" and self.at_op(":"):
            self.next()
            hi = None if self._range_end() else self.parse_signed()
            return Selector(name, "range", lo=None, hi=hi)
        value = self.parse_value()
        if op == "=" and self.at_op(":"):
            self.next()
            hi = None if self._range_end() else self.parse_signed()
            if not isinstance(value, float):
                self.error("a range needs numbers")
            return Selector(name, "range", lo=value, hi=hi)
        if op != "=" and op != "!=" and not isinstance(value, float):
            self.error(f"{op} needs a number")
        return Selector(name, op, value=value)

    def _range_end(self):
        tok = self.peek()
        return tok.kind == "EOF" or (tok.kind == "OP" and tok.value in (",", "]")) or (
            tok.kind == "NAME" and tok.value in KEYWORDS)

    def parse_selectors(self):
        sels = [self.parse_selector()]
        while self.at_op(",") and self._comma_then_selector():
            self.next()
            sels.append(self.parse_selector())
        return sels

    def _comma_then_selector(self):
        save = (self.pos, self._peeked, self._prev)
        self.next()
        ok = self.at("NAME") and self.peek().value not in KEYWORDS
        if ok:
            self.next()
            ok = self.at("OP") and self.peek().value in ("=", "!=", "<", "<=", ">", ">=")
        self.pos, self._peeked, self._prev = save
        return ok

    def parse_optvalue(self):
        tok = self.peek()
        if tok.kind == "NUMBER" or (tok.kind == "OP" and tok.value in ("-", "+")):
            return self.parse_signed_node()
        if tok.kind == "STRING":
            self.next()
            return Str(tok.value)
        if tok.kind == "OP" and tok.value == "[":
            return self.parse_list_node()
        if tok.kind == "OP" and tok.value == "(":
            self.next()
            items = [self.parse_optvalue()]
            while self.accept_op(","):
                items.append(self.parse_optvalue())
            self.expect_op(")")
            return TupleLit(items)
        if tok.kind == "NAME":
            self.next()
            if self.at_op("("):
                return self.parse_call(tok.value, tok.pos)
            node = Name(tok.value, tok.pos)
            while self.at_op("."):
                self.next()
                node = Attr(node, self.expect_name())
            return node
        self.error("expected a value")

    def parse_options(self, spaces=False):
        """option { "," option } where option = NAME [ "=" optvalue ].
        With spaces=True, options may also be separated by spaces."""
        opts = []
        while True:
            name = self.expect_name("an option name")
            if self.accept_op("="):
                opts.append((name, self.parse_optvalue()))
            else:
                opts.append((name, None))
            if self.at_op(","):
                self.next()
            elif not (spaces and self.at("NAME") and self.peek().value not in KEYWORDS):
                return opts

    def parse_clauses(self, allowed=("where", "with")):
        """Parse trailing where/with/limit clauses in any order."""
        out = {}
        while not self.at_end():
            tok = self.peek()
            if tok.kind == "NAME" and tok.value in allowed and tok.value not in out:
                self.next()
                if tok.value == "where":
                    out["where"] = self.parse_selectors()
                elif tok.value == "with":
                    out["with"] = self.parse_options()
                elif tok.value == "limit":
                    out["limit"] = int(self.parse_signed())
                else:
                    out[tok.value] = True
            else:
                break
        return out


def parse_expression(text):
    p = Parser(text)
    node = p.parse_expr()
    p.expect_end()
    return node


def parse_optvalue_text(text):
    p = Parser(text)
    node = p.parse_optvalue()
    p.expect_end()
    return node


def parse_where_text(text):
    if not text or not text.strip():
        return []
    p = Parser(text)
    sels = p.parse_selectors()
    p.expect_end()
    return sels
