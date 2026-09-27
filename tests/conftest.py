import os
import textwrap

import numpy as np
import pytest

import glue


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(text))


def curve_file(path, T, E):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savetxt(path, np.c_[T, E], header="T E")


def table_toml(root, pattern="U_{U}/n_{n}.dat", columns='["T", "E"]', extra=""):
    return f"""\
glue = "0.1"
kind = "table"

[source]
root = "{root}"
pattern = "{pattern}"

[source.reader]
format = "text"
columns = {columns}

[curves]
x = "T"

[columns.E]
unit = "t"
{extra}"""


@pytest.fixture
def proj(tmp_path):
    """Two tables a and b of E(T) = T^2 - U*n on different grids. b lacks (U=2, n=1)."""
    rng = np.random.default_rng(1)
    for name, npts in (("a", 40), ("b", 15)):
        for U in (1.0, 2.0):
            for n in (0.5, 1.0):
                if name == "b" and (U, n) == (2.0, 1.0):
                    continue
                T = np.sort(rng.uniform(0.05, 1.0, npts))
                T[0], T[-1] = 0.05, 1.0
                curve_file(str(tmp_path / "data" / name / f"U_{U}" / f"n_{n}.dat"), T, T**2 - U * n)
        write(str(tmp_path / f"{name}.toml"), table_toml(f"data/{name}"))
    for U in (1.0, 2.0):
        for n in (0.5, 1.0):
            write(str(tmp_path / "data" / "g" / f"U_{U}" / f"n_{n}.dat"), f"# gap\n{U * n}\n")
    write(str(tmp_path / "g.toml"), """\
        glue = "0.1"
        kind = "table"
        [source]
        root = "data/g"
        pattern = "U_{U}/n_{n}.dat"
        [source.reader]
        format = "text"
        columns = ["gap"]
        """)
    write(str(tmp_path / "project.toml"), """\
        glue = "0.1"
        kind = "project"
        name = "test"

        [tables]
        a = "a.toml"
        b = "b.toml"
        g = "g.toml"

        [views]
        dE = "a.E - b.E"
        """)
    return tmp_path


@pytest.fixture
def session(proj):
    return glue.open(str(proj / "project.toml"))
