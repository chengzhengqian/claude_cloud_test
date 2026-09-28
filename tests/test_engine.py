import numpy as np
import pytest

from glue import lang
from glue.errors import GlueError


def frame(session, expr, where=None, **settings):
    return session.eval(expr, where=where, **settings).to_pandas()


def test_table_scan_and_keys(session):
    t = session.tables["a"]
    assert len(t.index()) == 4
    assert t.coords == ["U", "n"]
    assert t.keys([]) == [(1.0, 0.5), (1.0, 1.0), (2.0, 0.5), (2.0, 1.0)]


def test_alignment_interpolates_onto_overlap_grid(session):
    df = frame(session, "a.E - b.E", method="cubic")
    assert set(df.columns) == {"U", "n", "T", "value"}
    assert df.groupby(["U", "n"]).size().tolist() == [200, 200, 200]
    assert np.abs(df["value"]).max() < 1e-3


def test_unmatched_keys(session):
    r = session.eval("a.E - b.E")
    r.to_pandas()
    assert any("dropped 1 unmatched key (only in a.E)" in line for line in r.report)
    with pytest.raises(GlueError, match="no match"):
        frame(session, "a.E - b.E", unmatched="error")


def test_row_aligned_columns_are_not_resampled(session):
    raw = frame(session, "a.E", where="U=1.0, n=0.5")
    df = frame(session, "a.E * a.T", where="U=1.0, n=0.5")
    assert len(df) == len(raw) == 40
    np.testing.assert_allclose(df["value"], raw["E"] * raw["T"])


def test_same_view_twice_is_row_aligned(session):
    df = frame(session, "dE - dE")
    assert (df["value"] == 0).all()


def test_keyed_broadcast_and_coordinates(session):
    raw = frame(session, "a.E", where="U=2.0, n=0.5")
    df = frame(session, "a.E / g.gap + n", where="U=2.0, n=0.5")
    np.testing.assert_allclose(df["value"], raw["E"] / 1.0 + 0.5)
    df = frame(session, "(a.E + U * n) / T", where="U=1.0, n=1.0")
    np.testing.assert_allclose(df["value"], df["T"], rtol=1e-6)


def test_bare_input_needs_a_field(session):
    with pytest.raises(GlueError, match="no field to take it from"):
        frame(session, "T * 2")
    df = frame(session, "T * a.U", where="U=2.0, n=0.5")
    np.testing.assert_allclose(df["value"], df["T"] * 2.0)


def test_reductions_and_at(session):
    df = frame(session, "max(a.E)")
    assert list(df.columns) == ["U", "n", "value"]
    np.testing.assert_allclose(df["value"], [1 - 0.5, 1 - 1.0, 1 - 1.0, 1 - 2.0])
    df = frame(session, "a.E @ T=0.5", method="cubic")
    np.testing.assert_allclose(df["E"], 0.25 - df["U"] * df["n"], atol=1e-6)
    df = frame(session, "argmax(a.E)")
    assert (df["value"] == 1.0).all()
    df = frame(session, "integral(a.E + U * n, T)", where="U=1.0")
    np.testing.assert_allclose(df["value"], (1 - 0.05**3) / 3, rtol=5e-3)


def test_derivative_and_integral(session):
    df = frame(session, "d(a.E, T, method=cubic)", where="U=1.0, n=0.5")
    np.testing.assert_allclose(df["value"], 2 * df["T"], atol=1e-6)
    df = frame(session, "d(a.E, T, method=fd, order=2)", where="U=1.0, n=0.5")
    assert abs(np.median(df["value"]) - 2) < 0.2
    df = frame(session, "int(a.E + U * n, T, method=cubic)", where="U=1.0, n=0.5")
    np.testing.assert_allclose(df["value"], (df["T"] ** 3 - 0.05**3) / 3, atol=1e-6)


def test_fixed_grid_extrapolation(session):
    df = frame(session, "resample(a.E, grid=linspace(0, 1.2, 13))", where="U=1.0, n=0.5")
    assert np.isnan(df["E"].iloc[0]) and np.isnan(df["E"].iloc[-1])
    assert not np.isnan(df["E"].iloc[5])
    with pytest.raises(GlueError, match="outside the data range"):
        frame(session, "resample(a.E, grid=linspace(0, 1.2, 13))", where="U=1.0, n=0.5", extrapolate="error")
    df = frame(session, "resample(a.E, grid=linspace(0, 1.2, 13))", where="U=1.0, n=0.5", extrapolate="clamp")
    assert not df["E"].isna().any()


def test_duplicates(proj):
    import glue

    path = proj / "data" / "b" / "U_1.0" / "n_0.5.dat"
    rows = np.loadtxt(path)
    np.savetxt(path, np.vstack([rows[:3], rows[2:3] + [0, 0.01], rows[3:]]))
    s = glue.open(str(proj / "project.toml"))
    with pytest.raises(GlueError, match=r"repeated T = .* in data/b/U_1.0/n_0.5.dat|repeated T"):
        frame(s, "a.E - b.E", where="U=1.0, n=0.5")
    df = frame(s, "a.E - b.E", where="U=1.0, n=0.5", duplicates="mean")
    assert len(df) == 200


def test_fewpoints(proj):
    import glue

    np.savetxt(proj / "data" / "b" / "U_1.0" / "n_0.5.dat", np.array([[0.1, 1.0], [0.9, 2.0]]))
    s = glue.open(str(proj / "project.toml"))
    r = s.eval("a.E - b.E", where="U=1.0, n=0.5", method="cubic")
    assert len(r.to_pandas()) == 200
    assert any("used linear" in line for line in r.report)
    r = s.eval("a.E - b.E", where="U=1.0, n=0.5", method="cubic", fewpoints="drop")
    assert len(r.to_pandas()) == 0
    assert any("dropped 1 curve" in line for line in r.report)


def test_selection_and_where(session):
    df = frame(session, "a.E[U=1.0]")
    assert set(df["U"]) == {1.0}
    df = frame(session, "a.E", where="T=0.2:0.4")
    assert df["T"].between(0.2, 0.4).all()
    df = frame(session, "a.E[n!=0.5]")
    assert set(df["n"]) == {1.0}
    with pytest.raises(GlueError, match="no input J"):
        frame(session, "a.E", where="J=0.1")
    df = frame(session, "a.E[E<0]", where="U=2.0")
    assert (df["E"] < 0).all() and len(df) > 0


def test_where_pushdown_reads_fewer_files(session):
    node, comp = session.compile("a.E - b.E")
    lines = session.explain(node, comp, lang.parse_where_text("U=1.0, n=0.5"))
    text = "\n".join(lines)
    assert "a: read 1 of 4 files" in text
    assert "b: read 1 of 3 files" in text


def test_stack(session):
    df = frame(session, 'stack(a=a.E, b=b.E, tag="method")', where="U=1.0, n=0.5")
    assert df.groupby("method").size().to_dict() == {"a": 40, "b": 15}
    df = frame(session, 'stack(a=a.E, b=b.E, tag="method")', where='method="b"')
    assert set(df["method"]) == {"b"}


def test_using_and_strict(session):
    with pytest.raises(GlueError, match="strict mode"):
        frame(session, "a.E - b.E", strict=True)
    session.settings.session["strict"] = True
    try:
        df = session.eval("using(a.E - b.E, grid=overlap(n=10))").to_pandas()
    finally:
        session.settings.session.pop("strict")
    assert df.groupby(["U", "n"]).size().max() == 10


def test_like_grid(session):
    raw = frame(session, "b.E", where="U=1.0, n=0.5")
    df = frame(session, "a.E - b.E", where="U=1.0, n=0.5", grid="like(b.E)")
    np.testing.assert_allclose(np.sort(df["T"]), np.sort(raw["T"]))


def test_name_errors(session):
    with pytest.raises(GlueError, match="Did you mean a"):
        frame(session, "aa.E")
    with pytest.raises(GlueError, match="has no column"):
        frame(session, "a.X")
    same = frame(session, "a - b.E")
    np.testing.assert_allclose(same["value"], frame(session, "a.E - b.E")["value"])


def test_python_operators(session):
    df = (session.a.E - session.b.E).to_pandas(where="U=1.0")
    assert set(df["U"]) == {1.0} and len(df) == 400
    df = (2 * session.a.E + 1).to_pandas(where="U=1.0, n=0.5")
    assert len(df) == 40


def test_register_python_data(session):
    import pandas as pd

    data = pd.DataFrame({"U": [1.0] * 3, "n": [0.5] * 3, "T": [0.1, 0.5, 0.9], "E": [0.0, 0.1, 0.2]})
    session.register("pydata", data, by=["U", "n"], x="T")
    df = frame(session, "pydata.E - a.E", where="U=1.0")
    assert set(df["n"]) == {0.5}


def test_plugin_op(session):
    import glue

    @glue.op(kind="reduce", name="npoints_test")
    def npoints(x, y):
        return len(x)

    df = frame(session, "npoints_test(a.E)")
    assert (df["value"] == 40).all()


def test_python_plot(session, proj):
    out = session.plot("a.E by n where U=1.0 > figs/api.png")
    assert out.endswith("api.png") and (proj / "figs" / "api.png").exists()
