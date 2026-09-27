# glue

A small tool for scientific data spread across many files, like
`U_0.1/J_0.2/n_0.5.dat`, where each file holds a few columns such as T and E.

- **Describe** the files with TOML interface files. The data stays where it is.
- **Compute** across datasets with a short expression language. `dmft.E - ed.E`
  matches curves by their parameters and interpolates both onto a common grid.
- **Save** results as datasets that store the data and the recipe, so the tool
  can tell when the inputs changed and recompute only the affected curves.

It connects existing tools (NumPy, pandas, SciPy, matplotlib, gnuplot) rather
than replacing them. How to use it: [docs/reference.md](docs/reference.md).
The design: [docs/spec.md](docs/spec.md).

## Install

```
pip install -e .              # Python 3.11+
pip install -e ".[test]"      # adds pyarrow, h5py, pytest
```

Parquet caches need `pyarrow`. Set `cache_format npz` to avoid it.

## A short session

```
$ cd examples/hubbard && glue --trust
> info dmft
  table dmft: data/dmft/U_{U}/J_{J}/n_{n}.dat
    48 files
    coordinate U   4 values: 1.0, 2.0, 3.0, 4.0  (path)
    ...
> explain plot dE by U where J=0.1, n=1.0
  dE = dmft.E - ed.E
    align dmft.E and ed.E: grid overlap(n=200), method pchip
    dmft: read 4 of 48 files, columns E, T
    ed: read 4 of 44 files, columns E, T
    result: 4 curves on (U, J, n)
> plot dE by U where J=0.1, n=1.0 > figs/dE.png
  aligned dmft.E, ed.E: 4 curves matched on (U, J, n), grid overlap(n=200), method pchip
  saved figs/dE.png
> plot argmax(C) / gap.gap vs U by J where n=1.0 > figs/ratio.png
> save dE as results/dE grid=overlap(n=400)
  error: curve U=2.0, J=0.1, n=0.9 in data/ed/U_2.0/J_0.1/n_0.9.dat has repeated T = 0.295897.
  Set duplicates to mean, first, last, or drop to continue.
> set duplicates mean
> save dE as results/dE grid=overlap(n=400)
  dropped 4 unmatched keys (only in dmft.E)
  aligned dmft.E, ed.E: 44 curves matched on (U, J, n), grid overlap(n=400), method pchip
  wrote results/dE.toml and results/dE.parquet (44 curves, 17600 rows)
```

`dE` and `C` are views defined in the project file: `dE = dmft.E - ed.E` and
`C = d(dmft.E, T)`.

From Python:

```python
import glue

s = glue.open("examples/hubbard/project.toml", trust=True)
df = s.eval("dmft.E - ed.E", where="J=0.1, n=1.0").to_pandas()
df2 = (s.dmft.E - s.ed.E).to_pandas(where="U=2.0", method="cubic", duplicates="mean")
```

## Layout

- `glue/`: the library. `sources.py` (files, SQLite, HDF5, caches), `lang.py`
  (parser), `compiler.py` and `engine.py` (the operation tree, alignment,
  curve-by-curve evaluation), `dataset.py` (recipes, fingerprints, refresh),
  `plotting.py`, `shell.py`, `cli.py`
- `examples/hubbard/`: a realistic sweep with four sources, an analysis
  script, figures, and saved datasets
- `tests/`: `python -m pytest`
- `docs/reference.md`: the usage reference
- `docs/spec.md`: the specification
