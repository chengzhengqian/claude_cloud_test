"""The core calculus: identity, trees, the new operations, and invalidation (docs/core.md)."""

import os
import shutil
import time

import numpy as np
import pytest

import glue
from glue import dataset as D
from glue import lang
from glue.core import nodes as N
from glue.core.tree import dump_nodes, load_nodes
from glue.errors import GlueError
from glue.session import Session
from glue.sources import load_table

from conftest import table_toml, write


def quiet(path):
    s = Session(log=lambda *_: None)
    s.load(str(path))
    return s


def frame(s, expr, where=None, **settings):
    return s.eval(expr, where=where, **settings).to_pandas()


# ------------------------------------------------------------------ identity


def test_leaf_id_ignores_name_and_file_location(proj):
    a = load_table(str(proj / "a.toml"), base_dir=str(proj))
    (proj / "tables").mkdir()
    write(str(proj / "tables" / "renamed.toml"), table_toml("../data/a"))
    moved = load_table(str(proj / "tables" / "renamed.toml"), "dmft_other_name", base_dir=str(proj))
    assert a.def_id == moved.def_id
    b = load_table(str(proj / "b.toml"), "a", base_dir=str(proj))
    assert b.def_id != a.def_id


def test_same_name_different_folders_never_share(proj, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    shutil.copytree(proj / "data", other / "data")
    rows = np.loadtxt(other / "data" / "a" / "U_1.0" / "n_0.5.dat")
    rows[:, 1] += 1.0
    np.savetxt(other / "data" / "a" / "U_1.0" / "n_0.5.dat", rows)
    write(str(other / "a.toml"), table_toml("data/b"))
    s1 = quiet(proj / "a.toml")
    s2 = quiet(other / "a.toml")
    n1, _ = s1.compile("a.E")
    n2, _ = s2.compile("a.E")
    assert n1.id != n2.id


def test_node_ids_are_deterministic(session):
    n1, _ = session.compile("a.E - b.E")
    n2, _ = session.compile("a.E  -  b.E")
    assert n1.id == n2.id
    n3, _ = session.compile("a.E - b.E", overrides={"method": glue.session.convert("method", "cubic")})
    assert n3.id != n1.id


def test_saved_dataset_is_transparent(proj):
    s = quiet(proj / "project.toml")
    s.define("C", "d(a.E, T)")
    expected, _ = s.compile(lang.Name("C"))
    s.save("C")
    info = s.datasets["C"].info
    assert info.root == expected.id
    node, _ = s.compile("argmax(C)")
    leaf = next(iter(node.leaves()))
    assert isinstance(leaf, N.DatasetNode) and leaf.id == expected.id


# ------------------------------------------------------------------ trees


def test_tree_round_trip_and_hand_edits(proj):
    s = quiet(proj / "project.toml")
    root, el = s.compile("(a.E - b.E) / g.gap")
    nodes = dump_nodes(root)
    tables = {k: e.table for k, e in el.inputs.items()}
    back, warnings = load_nodes(nodes, root.id, lambda kind, label: tables[label])
    assert back.id == root.id and not warnings
    mid = next(k for k, v in nodes.items() if v["op"] == "align")
    nodes[mid] = dict(nodes[mid], method="linear")
    edited, warnings = load_nodes(nodes, root.id, lambda kind, label: tables[label])
    assert warnings and warnings[0][0] == mid
    assert edited.id != root.id


def test_tree_errors(proj):
    s = quiet(proj / "project.toml")
    root, el = s.compile("a.E * 2")
    nodes = dump_nodes(root)
    with pytest.raises(GlueError, match="not defined"):
        load_nodes(nodes, "nope", lambda *a: None)
    broken = {k: dict(v) for k, v in nodes.items()}
    top = broken[root.id]
    top["of"] = "missing000000"
    with pytest.raises(GlueError, match="referenced but not defined"):
        load_nodes(broken, root.id, lambda *a: None)


def test_explain_shows_core_tree(session):
    node, comp = session.compile("a.E - b.E")
    text = "\n".join(session.explain(node, comp, lang.parse_where_text("U=1.0")))
    assert "align(along T, grid overlap(n=200), method pchip)" in text
    assert "overlap" in text and "source(a" in text
    assert "filters pushed to the sources: U=1.0" in text


# ------------------------------------------------------------------ operations


def test_join_without_alignment_when_points_are_shared(session):
    node, _ = session.compile("d(a.E, T) * a.E")
    assert not any(n.op == "align" for n in node.walk())
    node, _ = session.compile("a.E / g.gap")
    assert not any(n.op == "align" for n in node.walk())
    node, _ = session.compile("(a.E - b.E) / abs(a.E)")
    assert sum(n.op == "align" for n in node.walk()) == 1


def test_align_setting(session):
    # align=[] means an exact join: only T values present in both tables survive
    df = frame(session, "a.E - b.E", align="[]")
    assert set(df["T"]) <= {0.05, 1.0}
    assert "align" not in session.eval("a.E - b.E", align="[]").tree()
    assert "align" in session.eval("a.E - b.E").tree()
    with pytest.raises(GlueError, match="no input X"):
        frame(session, "a.E - b.E", align="[X]")


def test_transform_and_rename(proj):
    for U in (1.0, 2.0):
        for n in (0.5, 1.0):
            T = np.linspace(0.05, 1.0, 12)
            p = proj / "data" / "c" / f"u_{U / 4}" / f"n_{n}.dat"
            p.parent.mkdir(parents=True, exist_ok=True)
            np.savetxt(p, np.c_[T, (T**2 - U * n) / 2.0])
    write(str(proj / "c.toml"), """\
        glue = "0.2"
        kind = "table"
        [source]
        root = "data/c"
        pattern = "u_{u}/n_{n}.dat"
        [source.reader]
        columns = ["T", "E_half"]
        [field]
        inputs = ["U", "n", "T"]
        outputs = ["E"]
        axis = "T"
        [field.transform]
        U = { from = "u", formula = "u * 4.0" }
        [field.map]
        E = "E_half * 2.0"
        """)
    s = quiet(proj / "project.toml")
    s.load(str(proj / "c.toml"))
    t = s.tables["c"]
    assert t.path_inputs == ["U", "n"]
    assert sorted({ch.coords["U"] for ch in t.index()}) == [1.0, 2.0]
    node, _ = s.compile("c.E")
    assert str(node.type) == "[U, n, T:ragged → E]"
    assert s.explain(node, None, lang.parse_where_text("U=2.0"))[-2] == "  c: read 2 of 4 files"
    df = frame(s, "a.E - c.E", method="cubic")
    assert np.abs(df["value"]).max() < 1e-6
    df = frame(s, "transform(c.E, T, t = T * 10)", where="U=1.0, n=0.5")
    assert list(df.columns) == ["U", "n", "t", "E"] and df["t"].max() == pytest.approx(10.0)
    df = frame(s, "rename(c.E, U=V)")
    assert "V" in df.columns
    with pytest.raises(GlueError, match="collide"):
        frame(s, "transform(c.E, U, U = U * 0)")


def test_transform_with_another_field_scaling_collapse(session):
    df = frame(session, "transform(a.E, T, t = T / g.gap)", where="n=1.0")
    assert list(df.columns) == ["U", "n", "t", "E"]
    for U, grp in df.groupby("U"):
        assert grp["t"].max() == pytest.approx(1.0 / (U * 1.0))


def test_swap_and_branches(session):
    df = frame(session, "swap(a.E, T)", where="U=1.0, n=0.5")
    assert list(df.columns) == ["U", "n", "E", "T"]
    np.testing.assert_allclose(df["E"], df["T"] ** 2 - 0.5, atol=1e-8)
    with pytest.raises(GlueError, match="isn't monotonic"):
        frame(session, "swap(a.E * (a.T - 0.5), T)", where="U=1.0, n=0.5")
    df = frame(session, "swap(a.E * (a.T - 0.5), T, branches=split)", where="U=1.0, n=0.5")
    assert "branch" in df.columns and df["branch"].nunique() >= 2


def test_legendre(session):
    df = frame(session, "legendre(a.E, T, slope=p, result=G, method=cubic)", where="U=1.0, n=0.5")
    assert list(df.columns) == ["U", "n", "p", "T", "G"]
    np.testing.assert_allclose(df["p"], 2 * df["T"], atol=1e-5)
    np.testing.assert_allclose(df["G"], df["T"] ** 2 + 0.5, atol=1e-5)


def test_reduce_along_a_path_input(session):
    df = frame(session, "mean(resample(a.E, grid=linspace(0.1, 0.9, 5)), n)")
    assert list(df.columns) == ["U", "T", "value"] and len(df) == 10
    row = df[(df["U"] == 1.0)].iloc[0]
    assert row["value"] == pytest.approx(row["T"] ** 2 - 0.75, abs=1e-3)


def test_value_filters_and_multiple_outputs(session):
    df = frame(session, "a.E[E>0]")
    assert (df["E"] > 0).all()
    df = frame(session, "at(a.E, T=[0.25, 0.5])", method="cubic")
    assert sorted(df["T"].unique()) == [0.25, 0.5]


# ------------------------------------------------------------------ calcs, prefixes, invalidation


def test_calc_files_and_prefixes(proj):
    s = quiet(proj / "project.toml")
    s.define("r", "a.E / g.gap", method="cubic")
    s.save("r", recipe_only=True)
    s2 = quiet(proj / "project.toml")
    assert "r" in s2.calcs
    df = frame(s2, "r * 2", where="U=1.0")
    assert len(df) == 80
    s2.load(str(proj / "project.toml"), prefix="old_")
    assert "old_a" in s2.tables and "old_dE" in s2.views
    node, _ = s2.compile("old_dE")
    assert {lf.table.name for lf in node.leaves()} == {"old_a", "old_b"}


def test_invalidate_table_and_partial(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    s.invalidate("b", lang.parse_where_text("U=1.0, n=0.5"))
    info = s.datasets["dE"].info
    st = D.status(info)
    assert st.state == "stale" and st.changed == {"b": {"U_1.0/n_0.5.dat"}}
    lines = s.refresh("dE")
    assert "recomputed 1 of 3 curves" in lines[0]
    s.invalidate("a")
    assert D.status(s.datasets["dE"].info).state == "stale"
    assert "recomputed" in s.refresh("dE")[0]
    s.invalidate("dE")
    assert D.status(D.load_info(info.path)).state == "invalidated"
    assert "recomputed all" in s.refresh("dE")[0]
    assert D.status(D.load_info(info.path)).state == "fresh"


def test_definition_change_rebuilds(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    text = (proj / "b.toml").read_text().replace("[source.reader]", "[source.reader]\nmax_rows = 1000")
    (proj / "b.toml").write_text(text)
    info = D.load_info(s.datasets["dE"].info.path)
    st = D.status(info)
    assert st.state == "stale" and "definition changed" in st.details[0]
    s2 = quiet(proj / "project.toml")
    out = "\n".join(s2.refresh("dE"))
    assert "was edited or its source changed" in out and "recomputed all" in out


def test_why_and_gc(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    text = "\n".join(s.why("dE"))
    assert "dataset dE: fresh" in text and "leaf a" in text and "align" in text
    s.settings.session["disk_cache"] = True
    frame(s, "d(a.E, T)")
    cache = proj / ".glue" / "cache"
    assert cache.is_dir() and os.listdir(cache)
    lines = s.gc()
    assert lines[0].startswith("removed")
    assert not os.listdir(cache)


def test_version_01_datasets_still_load_and_refresh(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    info = s.datasets["dE"].info
    frame_now, meta = D.read_cache_file(info.cache_file, info.format)
    legacy = f'''glue = "0.1"
kind = "dataset"
name = "dE"
pinned = false

[recipe]
expr = "a.E - b.E"
source_expr = "dE"
where = ""

[recipe.inputs]
a = "../a.toml"
b = "../b.toml"

[recipe.settings]
method = "pchip"
grid = "overlap(n=200)"
duplicates = "error"

[curves]
by = ["U", "n"]
x = "T"
y = ["dE"]

[cache]
file = "dE.parquet"
format = "parquet"
sha256 = "{info.sha256}"
rows = {info.points}
curves = 3

[fingerprint]
mode = "stat"
'''
    (proj / "results" / "dE.toml").write_text(legacy)
    old = D.load_info(str(proj / "results" / "dE.toml"))
    assert old.version == "0.1" and str(old.type) == "[U, n, T:ragged → dE]"
    st = D.status(old)
    assert st.state == "fresh" and "saved by glue 0.1" in st.details[0]
    D.refresh(old, log=lambda *_: None)
    new = D.load_info(str(proj / "results" / "dE.toml"))
    assert new.version == "0.2" and new.nodes
    after, _ = D.read_cache_file(new.cache_file, new.format)
    np.testing.assert_allclose(after["dE"].to_numpy(), frame_now["dE"].to_numpy())


def test_align_broadcasts_an_operand_with_fewer_inputs(session):
    # ref has no n, so the same reference curve is used for every n
    session.define("ref", "mean(resample(a.E, grid=linspace(0.05, 1.0, 20)), n)")
    r = session.eval("a.E - ref", where="U=1.0")
    df = r.to_pandas()
    assert str(r.type).startswith("[U, n, T")
    assert sorted(df["n"].unique()) == [0.5, 1.0]
    # E = T^2 - U*n, so E - mean over n of E = -U*(n - 0.75)
    for n, g in df.groupby("n"):
        np.testing.assert_allclose(g["value"], -(n - 0.75), atol=2e-3)


def test_nan_rows_error_stops_but_drop_gives_same_values(proj):
    path = proj / "data" / "a" / "U_1.0" / "n_0.5.dat"
    rows = path.read_text().splitlines()
    rows.insert(3, "0.5 nan")
    path.write_text("\n".join(rows) + "\n")
    s = quiet(proj / "project.toml")
    # a missing output stays a point with no value, and fits ignore it (core.md 1.4)
    dropped = frame(s, "d(a.E, T)", where="U=1.0, n=0.5")
    assert dropped["value"].isna().sum() == 1
    ok = dropped.dropna()
    np.testing.assert_allclose(ok["value"], 2 * ok["T"], atol=0.05)
    with pytest.raises(GlueError, match="a: 1 rows with a missing value, for example E at U=1.0"):
        frame(s, "d(a.E, T)", where="U=1.0, n=0.5", nan_rows="error")
    # a check, not a parameter: the tree is the same either way
    assert s.eval("d(a.E, T)").node.id == s.eval("d(a.E, T)", nan_rows="error").node.id


def test_disk_cache_size_limit(proj):
    s = quiet(proj / "project.toml")
    s.set_setting("disk_cache", "true")
    cache = proj / ".glue" / "cache"
    frame(s, "d(a.E, T)")
    assert cache.is_dir() and os.listdir(cache)
    s.set_setting("disk_cache_mb", "0")
    s.store.clear()
    frame(s, "d(b.E, T)")
    assert os.listdir(cache) == []


def _reads(s, expr, where):
    r = s.eval(expr, where=where)
    return [l.strip() for l in s.explain(r.node, r.comp, lang.parse_where_text(where)) if " read " in l]


@pytest.mark.parametrize("expr, where, reads, U", [
    ("transform(a.E, U, u = U * 10)", "u=20", "a: read 2 of 4 files", [20.0]),
    ("transform(a.E, U, U = U * 10)", "U=10", "a: read 2 of 4 files", [10.0]),
    ("transform(a.E - b.E, U, u = U * 10)", "u=10, n=0.5", "a: read 1 of 4 files", [10.0]),
    ("transform(a.E, U, u = U * 10)", "u=7", "a: read 0 of 4 files", []),
])
def test_filter_on_transformed_input_skips_files(proj, expr, where, reads, U):
    s = quiet(proj / "project.toml")
    assert reads in _reads(s, expr, where)
    df = frame(s, expr, where=where)
    name = s.eval(expr).type.input_names[0]
    assert sorted(set(df[name])) == U
    # the same rows as filtering after computing everything
    full = frame(s, expr)
    from glue.core.formula import apply_preds, pred_from_selector
    expect = apply_preds([pred_from_selector(x) for x in lang.parse_where_text(where)], full)
    assert len(df) == len(expect)
    np.testing.assert_allclose(np.sort(df.iloc[:, -1]), np.sort(expect.iloc[:, -1]))


def test_status_all_lists_cached_values(proj):
    s = quiet(proj / "project.toml")
    s.set_setting("disk_cache", "true")
    frame(s, "dE")
    text = "\n".join(s.status_lines(all=True))
    assert "dE (view):" in text and "stale" not in text.split("cached values:")[1]
    time.sleep(0.01)
    p = proj / "data" / "b" / "U_1.0" / "n_0.5.dat"
    p.write_text(p.read_text() + "\n")
    s2 = quiet(proj / "project.toml")        # a new session: only the disk cache is left
    text = "\n".join(s2.status_lines(all=True))
    assert "dE (view):" in text and "stale: align" in text and "reads a, b" in text


def _touch_b(proj):
    time.sleep(0.01)
    p = proj / "data" / "b" / "U_1.0" / "n_0.5.dat"
    a = np.loadtxt(p)
    a[:, 1] += 0.5
    np.savetxt(p, a)


def test_cached_values_update_only_changed_curves(proj):
    s = quiet(proj / "project.toml")
    s.eval("dE").to_pandas()
    _touch_b(proj)
    r = s.eval("dE")
    df = r.to_pandas()
    assert "updated cached a.E - b.E: recomputed 1 of 3 curves" in r.report
    fresh = quiet(proj / "project.toml").eval("dE").to_pandas()
    np.testing.assert_allclose(df["dE"], fresh["dE"])
    assert list(df.columns) == list(fresh.columns) and len(df) == len(fresh)


def test_cached_values_update_across_sessions(proj):
    def sess():
        s = quiet(proj / "project.toml")
        s.set_setting("disk_cache", "true")
        return s

    sess().eval("max(dE)").to_pandas()
    _touch_b(proj)
    r = sess().eval("max(dE)")
    df = r.to_pandas()
    assert any(line.startswith("updated cached") and "1 of 3" in line for line in r.report)
    fresh = quiet(proj / "project.toml").eval("max(dE)").to_pandas()
    np.testing.assert_allclose(df["value"], fresh["value"])
    # only the newest value of each node stays on disk
    ids = [f.split("-")[0] for f in os.listdir(proj / ".glue" / "cache") if f.endswith(".parquet")]
    assert len(ids) == len(set(ids))


def test_update_after_a_mean_over_n(proj):
    # the changed file has U=1.0, so the mean for U=1.0 is recomputed from every n
    s = quiet(proj / "project.toml")
    r0 = s.eval("mean(resample(a.E, grid=linspace(0.1, 0.9, 5)), n)")
    r0.to_pandas()
    time.sleep(0.01)
    p = proj / "data" / "a" / "U_1.0" / "n_0.5.dat"
    a = np.loadtxt(p)
    a[:, 1] += 1.0
    np.savetxt(p, a)
    r = s.eval("mean(resample(a.E, grid=linspace(0.1, 0.9, 5)), n)")
    df = r.to_pandas()
    fresh = quiet(proj / "project.toml").eval("mean(resample(a.E, grid=linspace(0.1, 0.9, 5)), n)").to_pandas()
    np.testing.assert_allclose(df["value"], fresh["value"])
