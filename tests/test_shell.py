import os

import pytest

from glue.errors import GlueError
from glue.session import Session
from glue.shell import Shell


@pytest.fixture
def sh(proj):
    out = []
    s = Session(log=out.append)
    shell = Shell(s, out=out.append)
    shell.base_dir = str(proj)
    shell.execute("load project.toml")
    shell.output = out
    return shell


def run(sh, text):
    sh.output.clear()
    sh.execute(text)
    return "\n".join(sh.output)


def test_show_and_assignment(sh):
    out = run(sh, "dE where U=1.0 limit 3")
    assert "aligned a.E, b.E: 2 curves matched" in out
    assert "more rows" in out
    run(sh, "m = max(a.E) with method=cubic")
    out = run(sh, "show m")
    assert out.split("\n")[0].split() == ["U", "n", "m"]
    out = run(sh, "ls")
    assert "variable m" in out and "view     dE" in out


def test_info_values_explain(sh):
    assert "4 curves" in run(sh, "info a")
    assert run(sh, "values a.U").strip() == "1.0 2.0"
    out = run(sh, "explain plot dE by n where U=1.0")
    assert "a: read 2 of 4 files" in out


def test_plot_writes_png(sh, proj):
    out = run(sh, "plot a=a.E, b=b.E by n where U=1.0 > figs/x.png")
    assert os.path.exists(proj / "figs" / "x.png")
    assert "saved" in out
    run(sh, "plot max(a.E) vs U by n > figs/k.png")
    assert os.path.exists(proj / "figs" / "k.png")


def test_plot_refuses_overlapping_curves(sh):
    with pytest.raises(GlueError, match="not fixed"):
        run(sh, "plot a.E by U > figs/bad.png")


def test_plot_gnuplot_backend(sh, proj):
    run(sh, "plot a.E by n where U=1.0 with backend=gnuplot > figs/g.gp")
    script = (proj / "figs" / "g.gp").read_text()
    assert "index 1 using 1:2" in script
    data = (proj / "figs" / "g_0.dat").read_text()
    assert "# n=0.5" in data and "# n=1.0" in data


def test_save_status_refresh_commands(sh, proj):
    out = run(sh, "save dE as results/d2 grid=overlap(n=20) format=npz")
    assert "d2.toml" in out and "d2.npz" in out
    assert "fresh" in run(sh, "status")
    assert "fresh, nothing to do" in run(sh, "refresh d2")
    with pytest.raises(GlueError, match="needs a dataset name"):
        run(sh, "refresh")
    assert "recomputed all 3" in run(sh, "refresh --all --full")


def test_export(sh, proj):
    run(sh, "export dE to out/d.dat where U=1.0")
    text = (proj / "out" / "d.dat").read_text()
    assert text.startswith("# T dE") and "# U=1.0, n=0.5" in text
    run(sh, "export max(a.E) to out/m.csv")
    assert (proj / "out" / "m.csv").read_text().startswith("U,n,value")


def test_set_and_errors(sh):
    run(sh, "set method cubic")
    assert "cubic" in run(sh, "set")
    run(sh, "set grid overlap(n=7)")
    assert "7 " not in run(sh, "dE where U=1.0, n=0.5 limit 100").split("\n")[0]
    run(sh, "unset grid")
    with pytest.raises(GlueError, match="unknown setting"):
        run(sh, "set nope 1")
    with pytest.raises(GlueError):
        run(sh, "plot = 3")


def test_script_with_continuation(sh, proj):
    (proj / "s.glue").write_text("# comment\nq = a.E - \\\n    b.E\nshow q limit 1\n")
    run(sh, "run s.glue")
    assert "variable q" in run(sh, "ls")


def test_new_table_and_guess(sh, proj):
    out = run(sh, "guess data/a")
    assert 'pattern = "U_{U}/n_{n}.dat"' in out
    assert 'columns = ["T", "E"]' in out
    run(sh, 'new table a2 pattern="U_{U}/n_{n}.dat" columns=["T", "E"] root="data/a"')
    assert "a2" in (proj / "project.toml").read_text()
    assert "4 curves" in run(sh, "info a2")


def test_save_view_writes_surface_text(sh, proj):
    run(sh, "r = a.E / g.gap")
    out = run(sh, "save r --view")
    assert "wrote view r" in out
    assert 'r = "a.E / g.gap"' in (proj / "project.toml").read_text()
    assert "view     r" in run(sh, "ls")


def test_save_recipe_only_writes_calc(sh, proj):
    run(sh, "r = a.E / g.gap with method=cubic")
    out = run(sh, "save r --recipe-only")
    assert "wrote calc" in out
    assert 'r = "calcs/r.toml"' in (proj / "project.toml").read_text()
    calc = (proj / "calcs" / "r.toml").read_text()
    assert 'kind = "calc"' in calc and "[cache]" not in calc
    assert "calc     r" in run(sh, "ls")
    assert "r" in run(sh, "r where U=1.0 limit 2")
