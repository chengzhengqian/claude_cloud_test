import pytest

from glue import lang
from glue.errors import ParseError
from glue.template import Template


def src(text):
    return lang.parse_expression(text).src()


@pytest.mark.parametrize("text,expected", [
    ("a.E - b.E", "a.E - b.E"),
    ("-T^2", "-T^2"),
    ("(a.E - b.E) / b.E", "(a.E - b.E) / b.E"),
    ("a - (b - c)", "a - (b - c)"),
    ("2^3^2", "2^3^2"),
    ("a.E - b.E @ T=0.1", "a.E - b.E @ T=0.1"),
    ("a.E[U=0.1, n=0.5]", "a.E[U=0.1, n=0.5]"),
    ("a.E[T=0.1:0.5]", "a.E[T=0.1:0.5]"),
    ("a.E[T=:0.5]", "a.E[T=:0.5]"),
    ("a.E[U=[0.1, 0.2]]", "a.E[U=[0.1, 0.2]]"),
    ("resample(a.E, grid=overlap(n=500), method=smooth(s=0.1))",
     "resample(a.E, grid=overlap(n=500), method=smooth(s=0.1))"),
    ("d(a.E, T) / T", "d(a.E, T) / T"),
    ("a.E ** 2", "a.E^2"),
])
def test_roundtrip(text, expected):
    assert src(text) == expected
    assert src(expected) == expected


def test_precedence():
    node = lang.parse_expression("a.E - b.E @ T=0.1")
    assert isinstance(node, lang.AtE) and isinstance(node.obj, lang.Bin)
    node = lang.parse_expression("-T^2")
    assert isinstance(node, lang.Neg) and isinstance(node.operand, lang.Bin)
    node = lang.parse_expression("a + b * c")
    assert node.op == "+" and node.right.op == "*"


def test_negative_numbers_and_selectors():
    node = lang.parse_expression("a.E[U>=-1, model=\"x\"]")
    s1, s2 = node.selectors
    assert (s1.op, s1.value) == (">=", -1.0)
    assert (s2.op, s2.value) == ("=", "x")
    assert lang.parse_expression("a - 2").op == "-"
    assert lang.parse_expression("-2").value == -2.0


def test_clauses_and_flags():
    p = lang.Parser("dmft.E where n=0.5, U=0.1:0.3 with logx, grid=overlap(200)")
    p.parse_expr()
    c = p.parse_clauses()
    assert [s.name for s in c["where"]] == ["n", "U"]
    assert c["with"][0] == ("logx", None)
    assert c["with"][1][0] == "grid"


def test_raw_word_reads_paths():
    p = lang.Parser("load data/U_0.1/project.toml")
    p.next()
    assert p.raw_word() == "data/U_0.1/project.toml"


def test_errors_point_at_position():
    with pytest.raises(ParseError) as e:
        lang.parse_expression("a.E +")
    assert "expected an expression" in str(e.value)
    with pytest.raises(ParseError):
        lang.parse_expression("a.E[U 1]")
    with pytest.raises(ParseError):
        lang.parse_expression("where")


def test_template_matching():
    t = Template("U_{U}/J_{J}/n_{n}.dat")
    assert t.match("U_0.1/J_-0.2/n_1e-3.dat") == {"U": "0.1", "J": "-0.2", "n": "1e-3"}
    assert t.match("U_0.1/J_0.2/m_0.5.dat") is None
    assert t.match("U_x/J_0.2/n_0.5.dat") is None
    t = Template("run_*/{model:str}/k{k:int}.dat")
    assert t.match("run_03/hubbard/k4.dat") == {"model": "hubbard", "k": "4"}
    assert t.match("run_03/hubbard/k4.5.dat") is None
    assert t.types == {"model": "str", "k": "int"}


def test_template_errors():
    from glue.errors import GlueError

    with pytest.raises(GlueError):
        Template("U_{U}/n_{U}.dat")
    with pytest.raises(GlueError):
        Template("U_{U:complex}.dat")
