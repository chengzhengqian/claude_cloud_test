# Example: a spin chain, step by step

Thermodynamics of the spin-1/2 XXZ chain from exact diagonalization, finite-temperature Lanczos,
and the exact XX solution, spread over text files, SQLite, HDF5, and CSV.

**Start with [TUTORIAL.md](TUTORIAL.md).** It walks through every part of glue on this data, with
the real output of every command, the figures they make, and diagrams of what happens inside.

| File | What it is |
|---|---|
| `make_data.py` | Computes the data (about a minute). `--rerun` recomputes two runs, for the update demo |
| `TUTORIAL.src.md` | The tutorial source |
| `build_tutorial.py` | Runs every example in the source and writes `TUTORIAL.md` |
| `diagrams.py` | Draws the diagrams, mostly from a live glue session |
| `tables/`, `project.toml`, `ops/` | Written by the tutorial itself |
| `results/`, `calcs/`, `figs/`, `exports/` | What the tutorial produced |

To rebuild everything from scratch:

```
pip install -e ../..
python build_tutorial.py
```
