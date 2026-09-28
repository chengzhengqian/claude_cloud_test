# glue

A small tool for scientific data spread across many files, like
`U_0.1/J_0.2/n_0.5.dat`, where each file holds a few columns such as T and E.

- **Describe** the files with TOML interface files. The data stays where it is.
- **Compute** across datasets with a short expression language. `dmft.E - ed.E`
  matches curves by their parameters and interpolates both onto a common grid.
- **Save** results as datasets that store the data and the calculation, so the
  tool can tell when the inputs changed and recompute only the affected curves.

Every value is a field `[inputs → outputs]`, like `dmft.E : [U, J, n, T → E]`.
Expressions are translated into trees of a few core operations, and a saved
dataset stores that tree.

It connects existing tools (NumPy, pandas, SciPy, matplotlib, gnuplot) rather
than replacing them.

| Document | What's in it |
|---|---|
| [docs/reference.md](docs/reference.md) | How to use it: files, expressions, commands, settings, Python |
| [docs/spec.md](docs/spec.md) | The specification |
| [docs/core.md](docs/core.md) | The core calculus: fields, operations, saved trees, caching |
| [docs/changes-0.2.md](docs/changes-0.2.md) | What changed in version 0.2, and moving 0.1 files over |

This is version 0.2.

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
    type       [U, J, n, T:ragged → E]
    48 files
    input      U          4 values: 1.0, 2.0, 3.0, 4.0  (path)
    ...
> explain plot dE by U where J=0.1, n=1.0
  dE = rename(mapping={value: dE})  [U, J, n, T:ragged → dE]
    dmft.E - ed.E = map(value = a.E - b.E)  [U, J, n, T:ragged → value]
      dmft.E - ed.E = align(along T, grid overlap(n=200), method pchip)  [U, J, n, T:ragged → a.E, b.E]
      ...
    filters pushed to the sources: J=0.1, n=1.0
    dmft: read 4 of 48 files
    ed: read 4 of 44 files
    result: [U, J, n, T:ragged → dE]
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
> Cs = transform(C, T, t = T / gap.gap)
> plot Cs by U where J=0.1, n=1.0 > figs/collapse.png
```

The last two lines measure T in units of each run's gap, so the specific
heat curves for all U fall on one curve.

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

- `glue/core/`: the core calculus. `types.py` (field types), `nodes.py` (the
  operations), `context.py` (evaluation, pushdown, fingerprints), `tree.py`
  (saving trees), `store.py` (cached values, `invalidate` marks)
- `glue/`: the rest. `sources.py` (files, SQLite, HDF5, caches), `lang.py`
  (parser), `elaborate.py` (expressions to core trees), `dataset.py`
  (datasets, calcs, status, refresh), `plotting.py`, `shell.py`, `cli.py`
- `examples/hubbard/`: a realistic sweep with four sources, an analysis
  script, figures, and saved datasets
- `tests/`: `python -m pytest`
- `docs/`: the documents listed above
