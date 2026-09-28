import os
import time

import numpy as np
import pytest

import glue
from glue import dataset as D
from glue import lang
from glue.errors import GlueError


def quiet(path):
    from glue.session import Session

    s = Session(log=lambda *_: None)
    s.load(str(path))
    return s


def touch_curve(path, factor=1.01):
    time.sleep(0.01)
    rows = np.loadtxt(path)
    rows[:, 1] *= factor
    np.savetxt(path, rows)


def test_save_writes_recipe_and_cache(proj):
    s = quiet(proj / "project.toml")
    s.define("x", lang.parse_expression("dE / 2"), {"method": glue.session.convert("method", "cubic")})
    s.save("x", grid=None)
    info = D.load_info(str(proj / "results" / "x.toml"))
    assert info.surface == "dE / 2"
    assert info.version == "0.2" and info.root in info.nodes
    ops = [n["op"] for n in info.nodes.values()]
    assert ops.count("source") == 2 and "align" in ops and "overlap" in ops
    align = next(n for n in info.nodes.values() if n["op"] == "align")
    assert align["method"] == "cubic" and align["along"] == "T"
    assert set(info.inputs) == {"a", "b"}
    assert info.type.text() == "[U, n, T:ragged → x]"
    assert info.curves == 3 and info.rows == 600
    assert D.status(info).state == "fresh"
    project = (proj / "project.toml").read_text()
    assert 'x = "results/x.toml"' in project
    df = s.eval("x").to_pandas()
    assert len(df) == 600 and "x" in df.columns


def test_view_replaced_by_dataset(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    assert "dE" in s.datasets and "dE" not in s.views
    assert "dE =" not in (proj / "project.toml").read_text().split("[datasets]")[0].split("[views]")[1]
    s2 = quiet(proj / "project.toml")
    assert "dE" in s2.datasets


def test_stale_and_incremental_refresh(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    info = s.datasets["dE"].info
    touch_curve(proj / "data" / "b" / "U_1.0" / "n_0.5.dat")
    st = D.status(info)
    assert st.state == "stale"
    assert st.changed == {"b": {"U_1.0/n_0.5.dat"}}
    lines = s.refresh("dE")
    assert "recomputed 1 of 3 curves, 2 unchanged" in lines[0]
    info = s.datasets["dE"].info
    assert D.status(info).state == "fresh"
    fresh = quiet(proj / "project.toml").eval("a.E - b.E").to_pandas()
    saved, _ = D.read_cache_file(info.cache_file, info.format)
    np.testing.assert_allclose(saved["dE"].to_numpy(), fresh["value"].to_numpy())


def test_refresh_counts_result_curves_not_changed_files(proj):
    # a has 4 files, but dE only has 3 curves because b lacks (U=2, n=1)
    s = quiet(proj / "project.toml")
    s.save("dE")
    later = time.time() + 10
    for p in (proj / "data" / "a").rglob("*.dat"):
        os.utime(p, (later, later))
    lines = s.refresh("dE")
    assert "recomputed 3 of 3 curves, 0 unchanged" in lines[0]


def test_new_input_file_adds_curve(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    a = np.loadtxt(proj / "data" / "a" / "U_1.0" / "n_0.5.dat")
    np.savetxt(proj / "data" / "b" / "U_2.0" / "n_1.0.dat", a)
    lines = s.refresh("dE")
    assert "recomputed 1 of 4 curves, 3 unchanged" in lines[0]


def test_modified_orphaned_and_pinned(proj):
    s = quiet(proj / "project.toml")
    s.save("dE", fmt="npz")
    info = s.datasets["dE"].info
    assert info.cache_file.endswith(".npz")
    with open(info.cache_file, "ab") as f:
        f.write(b"x")
    assert D.status(info).state == "modified"
    s.refresh("dE")
    assert D.status(s.datasets["dE"].info).state == "fresh"

    s.pin("dE")
    touch_curve(proj / "data" / "a" / "U_1.0" / "n_0.5.dat")
    info = D.load_info(s.datasets["dE"].info.path)
    assert info.pinned and D.status(info).state == "stale"
    lines = s.refresh("dE")
    assert "pinned, skipped" in lines[0]
    s.pin("dE", False)

    os.rename(proj / "b.toml", proj / "b_moved.toml")
    info = D.load_info(s.datasets["dE"].info.path)
    assert D.status(info).state == "orphaned"
    with pytest.raises(GlueError, match="can't be recomputed"):
        D.refresh(info, log=lambda *_: None)


def test_dataset_chain(proj):
    s = quiet(proj / "project.toml")
    s.define("peak", lang.parse_expression("max(a.E)"))
    s.save("peak")
    s.define("scaled", lang.parse_expression("peak * 2"))
    s.save("scaled")
    info = s.datasets["scaled"].info
    assert info.inputs == {"peak": "peak.toml"}
    touch_curve(proj / "data" / "a" / "U_2.0" / "n_0.5.dat", 0.5)
    assert D.status(info).state == "stale"
    lines = s.refresh("scaled")
    assert any(line.startswith("peak: recomputed 1 of 4") for line in lines)
    df = quiet(proj / "project.toml").eval("scaled").to_pandas()
    raw = quiet(proj / "project.toml").eval("max(a.E) * 2").to_pandas()
    np.testing.assert_allclose(df["scaled"], raw["value"])


def test_save_with_where_and_grid(proj):
    from glue.settings import parse_grid

    s = quiet(proj / "project.toml")
    s.save("dE", path="out/dE_u1", where=lang.parse_where_text("U=1.0"), grid=parse_grid("overlap(n=50)"))
    info = s.datasets["dE_u1"].info
    ops = {n["op"]: n for n in info.nodes.values()}
    assert ops["filter"]["predicate"] == "U=1.0"
    assert ops["overlap"]["count"] == 50
    assert info.curves == 2 and info.rows == 100


def test_snapshot_from_python_data(proj):
    import pandas as pd

    s = quiet(proj / "project.toml")
    data = pd.DataFrame({"U": [1.0, 1.0], "n": [0.5, 0.5], "T": [0.1, 0.9], "E": [0.0, 1.0]})
    s.register("pydata", data, by=["U", "n"], x="T")
    s.define("snap", lang.parse_expression("pydata.E * 2"))
    s.save("snap")
    info = s.datasets["snap"].info
    assert not info.reproducible
    assert D.status(info).state == "no recipe"
    with pytest.raises(GlueError, match="no recipe"):
        D.refresh(info, log=lambda *_: None)
    assert quiet(proj / "project.toml").eval("snap").to_pandas()["snap"].tolist() == [0.0, 2.0]


def test_missing_dataset_file_is_skipped_with_a_warning(proj):
    s = quiet(proj / "project.toml")
    s.save("dE")
    os.remove(proj / "results" / "dE.toml")
    out = []
    from glue.session import Session

    s2 = Session(log=out.append)
    s2.load(str(proj / "project.toml"))
    assert "dE" not in s2.datasets
    assert any("dataset dE: results/dE.toml not found, skipped" in line for line in out)
    s2.define("dE", "a.E - b.E")
    s2.save("dE")
    assert "dE" in s2.datasets
