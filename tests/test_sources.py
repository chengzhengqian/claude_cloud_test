import sqlite3

import numpy as np
import pytest

import glue
from glue.errors import GlueError

from conftest import write


def open_table(path):
    s = glue.open(str(path))
    return s


def test_sqlite_source(tmp_path):
    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE runs (u REAL, beta REAL, energy REAL, err REAL)")
    for u in (1.0, 2.0):
        for beta in (1, 2, 4, 8):
            con.execute("INSERT INTO runs VALUES (?, ?, ?, ?)", (u, beta, -u / beta, 0.01))
    con.commit()
    con.close()
    write(str(tmp_path / "q.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        locator = "sqlite"
        file = "r.db"
        query = "SELECT u AS U, 1.0 / beta AS T, energy AS E, err AS dE FROM runs"
        [curves]
        by = ["U"]
        x = "T"
        [columns.E]
        error = "dE"
        """)
    s = open_table(tmp_path / "q.toml")
    t = s.tables["q"]
    assert t.values == ["E"]
    assert t.keys([]) == [(1.0,), (2.0,)]
    df = s.eval("q.E", where="U=2.0, T=0.2:1").to_pandas()
    assert sorted(df["T"]) == [0.25, 0.5, 1.0]
    np.testing.assert_allclose(df["E"], -2 * df["T"])


def test_csv_raw_npy_readers(tmp_path):
    for k in (1, 2):
        d = tmp_path / "csv"
        d.mkdir(exist_ok=True)
        (d / f"k{k}.csv").write_text("temp,energy\n0.1,1\n0.2,2\n")
        a = np.array([[0.1, 1.0], [0.2, 2.0], [0.3, 3.0]])
        (tmp_path / "raw").mkdir(exist_ok=True)
        a.astype("<f8").tofile(tmp_path / "raw" / f"k{k}.bin")
        (tmp_path / "npy").mkdir(exist_ok=True)
        np.save(tmp_path / "npy" / f"k{k}.npy", a)
    common = '\n[curves]\nx = "T"\n'
    write(str(tmp_path / "c.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "csv"
        pattern = "k{k:int}.csv"
        [source.reader]
        format = "csv"
        rename = { temp = "T", energy = "E" }
        """ + common)
    write(str(tmp_path / "r.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "raw"
        pattern = "k{k:int}.bin"
        [source.reader]
        format = "raw"
        columns = ["T", "E"]
        """ + common)
    write(str(tmp_path / "n.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "npy"
        pattern = "k{k:int}.npy"
        [source.reader]
        format = "npy"
        columns = ["T", "E"]
        """ + common)
    from glue.session import Session

    s = Session(log=lambda *_: None)
    for name in "crn":
        s.load(str(tmp_path / f"{name}.toml"))
    assert s.tables["c"].values == ["E"]
    assert s.tables["c"].ctype("k") == "int"
    for name, n in (("c", 2), ("r", 3), ("n", 3)):
        df = s.eval(f"{name}.E", where="k=2").to_pandas()
        assert len(df) == n and set(df["k"]) == {2}


def test_hdf5_source(tmp_path):
    h5py = pytest.importorskip("h5py")
    with h5py.File(tmp_path / "run.h5", "w") as f:
        for U in (1.0, 2.0):
            f[f"U_{U}/data"] = np.c_[np.linspace(0.1, 1, 5), U * np.linspace(0.1, 1, 5)]
    write(str(tmp_path / "h.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        locator = "hdf5"
        file = "run.h5"
        pattern = "U_{U}"
        [source.reader]
        dataset = "data"
        columns = ["T", "E"]
        [curves]
        x = "T"
        """)
    from glue.session import Session

    s = Session(log=lambda *_: None)
    s.load(str(tmp_path / "h.toml"))
    df = s.eval("h.E / T").to_pandas()
    np.testing.assert_allclose(df["value"], df["U"])


def test_content_coordinates_in_text(tmp_path):
    rows = "\n".join(f"{m} {t} {m * t}" for m in (1, 2) for t in (0.1, 0.2, 0.3))
    write(str(tmp_path / "data" / "run_1.dat"), rows + "\n")
    write(str(tmp_path / "t.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "data"
        pattern = "run_{r:int}.dat"
        [source.reader]
        columns = ["m", "T", "E"]
        [curves]
        by = ["m"]
        x = "T"
        """)
    from glue.session import Session

    s = Session(log=lambda *_: None)
    s.load(str(tmp_path / "t.toml"))
    t = s.tables["t"]
    assert t.coords == ["m", "r"]
    assert t.keys([]) == [(1.0, 1), (2.0, 1)]
    df = s.eval("t.E", where="m=2").to_pandas()
    assert len(df) == 3


def test_bad_files_give_clear_errors(tmp_path):
    write(str(tmp_path / "data" / "x_1.dat"), "1 2 3\n4 5 6\n")
    write(str(tmp_path / "t.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "data"
        pattern = "x_{x}.dat"
        [source.reader]
        columns = ["T", "E"]
        [curves]
        x = "T"
        """)
    from glue.session import Session

    s = Session(log=lambda *_: None)
    s.load(str(tmp_path / "t.toml"))
    with pytest.raises(GlueError, match="file has 3 columns, but the table lists 2"):
        s.eval("t.E").to_pandas()
    write(str(tmp_path / "bad.toml"), 'glue = "0.1"\nkind = "table"\n[source]\npatern = "x"\n')
    with pytest.raises(GlueError, match="unknown key 'patern'"):
        s.load(str(tmp_path / "bad.toml"))


def test_index_cache(proj):
    from glue.session import Session

    s = Session(log=lambda *_: None)
    s.load(str(proj / "project.toml"))
    s.tables["a"].index()
    assert (proj / ".glue" / "index").is_dir()
    s2 = Session(log=lambda *_: None)
    s2.load(str(proj / "project.toml"))
    assert len(s2.tables["a"].index()) == 4
    (proj / "data" / "a" / "U_3.0").mkdir()
    np.savetxt(proj / "data" / "a" / "U_3.0" / "n_0.5.dat", np.c_[[0.1, 0.2], [1, 2]])
    s3 = Session(log=lambda *_: None)
    s3.load(str(proj / "project.toml"))
    assert len(s3.tables["a"].index()) == 5
