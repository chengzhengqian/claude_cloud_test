<!-- Built by build_tutorial.py from TUTORIAL.src.md. Edit the source, not this file. -->

# glue, step by step: thermodynamics of a spin chain

This tutorial walks through every part of glue on a realistic dataset. You describe data spread
over many files in four formats, combine it, do calculus on it, change variables, extend glue with
Python, save results, and watch them update when the data changes. At the end there's a tour of
how the code does it.

Every command below was run, and the output under it is what glue printed. The figures are the
ones those commands made. `build_tutorial.py` rebuilds this file from `TUTORIAL.src.md`.

**Contents**

1. [The physics and the data](#1-the-physics-and-the-data)
2. [Describing the files](#2-describing-the-files)
3. [A project and a first look](#3-a-project-and-a-first-look)
4. [Fields: what every value is](#4-fields-what-every-value-is)
5. [Plotting](#5-plotting)
6. [Messy files: a restarted job](#6-messy-files-a-restarted-job)
7. [Combining sources](#7-combining-sources)
8. [Calculus on curves](#8-calculus-on-curves)
9. [Changing variables](#9-changing-variables)
10. [Your own operations in Python](#10-your-own-operations-in-python)
11. [Settings](#11-settings)
12. [Saving results](#12-saving-results)
13. [When the data changes](#13-when-the-data-changes)
14. [Exporting](#14-exporting)
15. [The Python API](#15-the-python-api)
16. [The command line](#16-the-command-line)
17. [How it works inside](#17-how-it-works-inside)

---

## 1. The physics and the data

The model is the spin-1/2 XXZ chain with periodic boundaries:

    H = J Σ_i [ Sx_i Sx_i+1 + Sy_i Sy_i+1 + Δ Sz_i Sz_i+1 ] − h Σ_i Sz_i ,   J = 1

Δ is the anisotropy and h a magnetic field. Δ = 0 is the XX chain (free fermions), Δ = 1 the
Heisenberg chain. We want the energy E, specific heat C, magnetization M, and susceptibility χ per
site as functions of temperature T, and we want to know how they depend on the chain length L.

`make_data.py` computes all of it with real numerics. There's nothing made up here:

| Source | Method | Format | What's odd about it |
|---|---|---|---|
| `data/ed/L_*/Delta_*/h_*.dat` | full exact diagonalization, L = 8 to 14 | one text file per run | every run picked its own temperatures; one job was restarted and wrote some rows twice; one job never finished |
| `data/ftlm.db` | finite-temperature Lanczos, L = 16 and 18, h = 0 | SQLite | another code: it calls Δ `jz`, stores β = 1/T, writes energies for the whole chain, and has statistical error bars |
| `data/gap.h5` | Lanczos ground states, L = 8 to 18 | HDF5, one group per run | one number per run, no temperature |
| `data/exact/xx_h*.csv` | the exact infinite XX chain (Δ = 0) | CSV | long column names, and no L at all |

```
$ python make_data.py
ED L=8 done (0 s)
ED L=10 done (0 s)
ED L=12 done (1 s)
ED L=14 done (18 s)
FTLM L=16 done (27 s)
FTLM L=18 done (67 s)
done in 67 s
```

Here's what one ED run looks like. Note the two header lines, and that the temperatures are
this run's own:

```
# ed-xxz 2.3  L=12 Delta=1.0 h=0.0 periodic
# T E C M chi
0.034451 -0.4489463361 0.0008717353007 4.884077891e-19 0.0001580295935
0.03854 -0.4489405435 0.002084836394 1.316467693e-18 0.000422559033
0.043115 -0.4489260604 0.004439927695 3.11916011e-18 0.001005708745
0.048233 -0.4488935608 0.00852248777 6.7094489e-18 0.002156080104
...
```

The FTLM database, and the HDF5 file:

```python
import sqlite3, h5py
con = sqlite3.connect("data/ftlm.db")
print(con.execute("SELECT sql FROM sqlite_master").fetchone()[0])
for row in con.execute("SELECT L, jz, beta, e_tot, e_err FROM ftlm WHERE L = 16 AND jz = 1.0 LIMIT 3"):
    print(row)
with h5py.File("data/gap.h5") as f:
    print(sorted(f.keys()), sorted(f["L_12"].keys()), f["L_12/Delta_1.0/data"][()])
```

```
CREATE TABLE ftlm (L INTEGER, jz REAL, beta REAL, e_tot REAL, e_err REAL, cv_tot REAL, cv_err REAL, nvec INTEGER, lanczos_steps INTEGER)
(16, 1.0, 0.2, -0.619839645796469, 0.003928350143572641)
(16, 1.0, 0.218792, -0.6809848118837017, 0.0041536284194305705)
(16, 1.0, 0.239349, -0.7482457785655939, 0.004408155493294729)
['L_10', 'L_12', 'L_14', 'L_16', 'L_18', 'L_8'] ['Delta_0.0', 'Delta_0.5', 'Delta_1.0', 'Delta_1.5'] [[ 0.35584751 -0.44894924]]
```

The rest of this tutorial turns these four very different sources into one consistent picture:

![How the pieces fit](figs/diagram_pipeline.png)

## 2. Describing the files

glue never converts or moves your data. Instead, each source gets a small **table file** in TOML
that says where the files are and what's in them.

### Let glue guess

For a folder of files, `guess` looks at the folder names and the first file and suggests a table
file. It writes nothing:

```
> guess data/ed name=ed
  31 files matched, path inputs: L, Delta, h
  5 columns in L_10/Delta_0.0/h_0.0.dat (names from its header comment)

  glue = "0.2"
  kind = "table"
  name = "ed"
  
  [source]
  locator = "glob"
  root = "data/ed"
  pattern = "L_{L}/Delta_{Delta}/h_{h}.dat"
  
  [source.reader]
  format = "text"
  columns = ["T", "E", "C", "M", "chi"]
  
  [field]
  inputs = ["L", "Delta", "h", "T"]
  outputs = ["E", "C", "M", "chi"]
  axis = "T"
```

It found L, Delta, and h in the folder names, and the column names in the header comment. It
guessed that the first column, T, is the axis. We'll write the real file by hand, with two
changes: `L` is an integer, and the columns get labels and units for the plots.

### Exact diagonalization: text files in folders

```toml
glue = "0.2"
kind = "table"
description = "Full exact diagonalization of periodic XXZ chains, L = 8 to 14. One file per run."

[source]
locator = "glob"
root = "../data/ed"
pattern = "L_{L:int}/Delta_{Delta}/h_{h}.dat"

[source.reader]
format = "text"
columns = ["T", "E", "C", "M", "chi"]

[field]
inputs = ["L", "Delta", "h", "T"]
outputs = ["E", "C", "M", "chi"]
axis = "T"

[columns.L]
label = "chain length"

[columns.Delta]
label = "anisotropy"

[columns.h]
unit = "J"
label = "field"

[columns.T]
unit = "J"
label = "temperature"

[columns.E]
unit = "J"
label = "energy per site"

[columns.C]
label = "specific heat per site"

[columns.M]
label = "magnetization per site"

[columns.chi]
unit = "1/J"
label = "susceptibility per site"
```

- `[source]` says where the files are. `{L:int}`, `{Delta}`, and `{h}` capture values from the
  path. They're called **path inputs**.
- `[source.reader]` says how to read one file.
- `[field]` is the table's **type**: the inputs identify a point, the outputs are measured there,
  and the **axis** is the input that operations like `d` and `max` work along by default.
- `[columns.*]` adds units and labels, which the plots use.

### FTLM: a database with other conventions

This code differs in three ways. It calls Δ `jz`, it stores β instead of T, and its energies are
for the whole chain. Fix that once, in the table file, and every expression afterwards sees the
same names and units as the ED table:

```toml
glue = "0.2"
kind = "table"
description = """Finite-temperature Lanczos for L = 16 and 18 at h = 0. This code has its own
conventions: it calls Delta jz, stores beta = 1/T, and writes energies for the whole chain.
The [field] sections below turn that into the same names and units as the ED table."""

[source]
locator = "sqlite"
file = "../data/ftlm.db"
query = "SELECT L, jz AS Delta, beta, e_tot, e_err, cv_tot, cv_err FROM ftlm"
constants = { h = 0.0 }

[field]
inputs = ["L", "Delta", "h", "T"]
outputs = ["E", "C"]
axis = "T"

[field.transform]
T = { from = "beta", formula = "1 / beta" }

[field.map]
E = "e_tot / L"
dE = "e_err / L"
C = "cv_tot / L"
dC = "cv_err / L"

[columns.L]
type = "int"
label = "chain length"

[columns.T]
unit = "J"
label = "temperature"

[columns.E]
unit = "J"
label = "energy per site"
error = "dE"

[columns.C]
label = "specific heat per site"
error = "dC"
```

- The SQL `query` renames `jz` to `Delta`. Any SELECT works.
- `constants` adds an input with the same value everywhere: this code only ran at h = 0.
- `[field.transform]` replaces the input β with T = 1/β.
- `[field.map]` computes outputs from columns: energies per site, and their errors.
- `error = "dE"` marks `dE` as the error bar of `E`. It comes along with `E`, and
  `plot ... with errorbars` draws it.

### Gaps: an HDF5 file

```toml
glue = "0.2"
kind = "table"
description = "Spin gap E0(Sz=1) - E0(Sz=0) and ground-state energy per site, from Lanczos. One HDF5 group per run."

[source]
locator = "hdf5"
file = "../data/gap.h5"
pattern = "L_{L:int}/Delta_{Delta}"

[source.reader]
format = "dataset"
dataset = "data"
columns = ["gap", "e0"]

[field]
inputs = ["L", "Delta"]
outputs = ["gap", "e0"]

[columns.gap]
unit = "J"
label = "spin gap"

[columns.e0]
unit = "J"
label = "ground-state energy per site"
```

This table has no axis: one value per (L, Delta). That's a **keyed table**.

### The exact XX chain: CSV

```toml
glue = "0.2"
kind = "table"
description = "The exact infinite XX chain (Delta = 0), from free fermions. CSV with long column names."

[source]
locator = "glob"
root = "../data/exact"
pattern = "xx_h{h}.csv"
constants = { Delta = 0.0 }

[source.reader]
format = "csv"
rename = { temperature = "T", energy = "E", heat_capacity = "C", magnetization = "M", susceptibility = "chi" }

[field]
inputs = ["Delta", "h", "T"]
outputs = ["E", "C", "M", "chi"]
axis = "T"

[columns.T]
unit = "J"
label = "temperature"

[columns.E]
unit = "J"
label = "energy per site"
```

The CSV header gives the column names, and `rename` maps them to the short ones. The file is
for an infinite chain, so this table has no L. We'll see in section 7 that this is exactly
right: comparing it with ED uses the same exact curve for every L.

## 3. A project and a first look

A **project** file names the tables and holds shared settings. Views, calcs, and datasets get
added to it as you work.

```toml
glue = "0.2"
kind = "project"
name = "spinchain"
description = "Thermodynamics of the spin-1/2 XXZ chain from exact diagonalization, FTLM, and the exact XX solution"

[tables]
ed    = "tables/ed.toml"
ftlm  = "tables/ftlm.toml"
gap   = "tables/gap.toml"
exact = "tables/exact.toml"

[settings]
method = "pchip"
grid = "overlap(n=200)"
fingerprint = "hash"

[plot]
cmap = "viridis"
```

`fingerprint = "hash"` makes glue compare file contents rather than modification times, so the
saved results stay valid after a git clone. Now load it and look around. `info` works from the
file index, so it never opens a data file:

```
> load project.toml
  loaded project spinchain: 4 tables, 0 views, 0 datasets
> ls
  table    ed             data/ed/L_{L:int}/Delta_{Delta}/h_{h}.dat
  table    ftlm           sqlite data/ftlm.db
  table    gap            hdf5 data/gap.h5:L_{L:int}/Delta_{Delta}
  table    exact          data/exact/xx_h{h}.csv
> info ed
  table ed: data/ed/L_{L:int}/Delta_{Delta}/h_{h}.dat
    Full exact diagonalization of periodic XXZ chains, L = 8 to 14. One file per run.
    type       [L, Delta, h, T:ragged → E, C, M, chi]
    31 files
    31 curves
    input      L          4 values: 8, 10, 12, 14  (path)
    input      Delta      4 values: 0.0, 0.5, 1.0, 1.5  (path)
    input      h          2 values: 0.0, 0.5  (path)
    axis       T [J]
    output     E [J]
    output     C
    output     M
    output     chi [1/J]
> info ftlm
  table ftlm: sqlite data/ftlm.db
    Finite-temperature Lanczos for L = 16 and 18 at h = 0. This code has its own
    conventions: it calls Delta jz, stores beta = 1/T, and writes energies for the whole chain.
    The [field] sections below turn that into the same names and units as the ED table.
    type       [L, Delta, h, T:ragged → E, C, dE, dC]
    1 file
    6 curves
    input      L          2 values: 16, 18  (content)
    input      Delta      3 values: 0.5, 1.0, 1.5  (content)
    input      h          1 value : 0.0  (constant)
    axis       T [J]
    output     E [J], error dE
    output     C, error dC
> info gap
  table gap: hdf5 data/gap.h5:L_{L:int}/Delta_{Delta}
    Spin gap E0(Sz=1) - E0(Sz=0) and ground-state energy per site, from Lanczos. One HDF5 group per run.
    type       [L, Delta → gap, e0]
    24 files
    24 keys
    input      L          6 values: 8, 10, 12, 14, 16, 18  (path)
    input      Delta      4 values: 0.0, 0.5, 1.0, 1.5  (path)
    output     gap [J]
    output     e0 [J]
> values ed.Delta
  0.0 0.5 1.0 1.5
```

Note that `info ed` counts 31 files, not 32: the L = 14, Δ = 1.5, h = 0.5 job never finished. glue
doesn't need a complete grid.

Anything that isn't a command is an expression to show:

```
> ed.E where L=8, Delta=1.0, h=0.0 limit 5
   L  Delta   h        T         E
   8    1.0 0.0 0.032121 -0.456387
   8    1.0 0.0 0.034236 -0.456387
   8    1.0 0.0 0.036491 -0.456387
   8    1.0 0.0 0.038893 -0.456386
   8    1.0 0.0 0.041454 -0.456386
  ... more rows (use limit N, or export to a file)
> gap.gap where Delta=1.0
   L  Delta      gap
   8    1.0 0.522674
  10    1.0 0.423239
  12    1.0 0.355848
  14    1.0 0.307106
  16    1.0 0.270190
  18    1.0 0.241249
> ftlm.E where L=16, Delta=1.0 limit 3
   L  Delta   h        T         E
  16    1.0 0.0 0.025000 -0.446393
  16    1.0 0.0 0.027349 -0.446393
  16    1.0 0.0 0.029919 -0.446392
  ... more rows (use limit N, or export to a file)
```

The FTLM values arrive per site and by T, with their error column, because of the table file.

### Reading only what's needed

Data is read lazily. `explain` shows what a statement would read, without reading it:

```
> explain ed.C where L=12, Delta=1.0
  ed.C = select(outputs=[C])  [L, Delta, h, T:ragged → C]
    source(ed, duplicates=error)  [L, Delta, h, T:ragged → E, C, M, chi]
    filters pushed to the sources: L=12, Delta=1.0
    ed: read 2 of 31 files
    result: [L, Delta, h, T:ragged → C]
```

The filter went all the way down to the source, which only opens the 2 matching files:

![Files read](figs/diagram_read_one.png)

## 4. Fields: what every value is

Every value in glue is a **field**, written `[inputs → outputs]`. The inputs determine the
outputs. `info` showed `ed : [L, Delta, h, T:ragged → E, C, M, chi]`.

Each input is either **exact** or **ragged**:

- **Exact** inputs take values from a fixed set, like L = 8, 10, 12, 14. Two fields are matched on
  them by value.
- **Ragged** inputs have different values in every curve. T is ragged here, because each run chose
  its own temperatures. To combine two fields along T, glue interpolates onto a common grid.

In a table, every input except the axis is exact. The axis is ragged, unless you declare it exact
in `[field].exact` because every file uses the same points.

A **curve** is the set of points with the same values of every input except the axis. `ed` has 31
curves, one per file. `gap` has none: it's keyed, one value per (L, Delta).

Operations change the type, and glue works it out before reading any data:

```
> explain max(ed.C)
  max(ed.C) = rename(mapping={C: value})  [L, Delta, h → value]
    reduce(along=T, reduce=max, method=-)  [L, Delta, h → C]
      ed.C = select(outputs=[C])  [L, Delta, h, T:ragged → C]
        source(ed, duplicates=error)  [L, Delta, h, T:ragged → E, C, M, chi]
    ed: read 31 of 31 files
    result: [L, Delta, h → value]
> explain ed.E @ T=0.5
  ed.E @ T=0.5 = eval(along=T, value=0.5, method=pchip, extrapolate=nan, fewpoints=linear)  [L, Delta, h → E]
    ed.E = select(outputs=[E])  [L, Delta, h, T:ragged → E]
      source(ed, duplicates=error)  [L, Delta, h, T:ragged → E, C, M, chi]
    ed: read 31 of 31 files
    result: [L, Delta, h → E]
```

`max` along T removes T: one number per curve. `@ T=0.5` evaluates each curve at one temperature
and also removes T.

## 5. Plotting

Plot the specific heat of the Heisenberg chain for each chain length:

```
> plot ed.C by L where Delta=1.0, h=0.0 with logx > figs/05_C_by_L.png
  saved figs/05_C_by_L.png
```

![C by L](figs/05_C_by_L.png)

`by L` colors the curves by L, `where` fixes the other inputs, and `with logx` is a plot option.
The axis labels come from the table's `[columns]`.

**Every input must be fixed, colored, or on the x axis.** Otherwise curves for different values
would overlap in the same color. If you forget one, glue says so:

```
> plot ed.C where Delta=0.5
  error: inputs L, h not fixed. Curves for different values would overlap. Fix L, h in where, or use: by L, h
```

If exactly one input is free, glue uses it for the colors. Two by-inputs use color and line style:

```
> plot ed.chi by L, h where Delta=0.5 with logx > figs/05_chi_by_L_h.png
  saved figs/05_chi_by_L_h.png
```

![chi by L and h](figs/05_chi_by_L_h.png)

The field (dashed) polarizes the chain and suppresses the susceptibility at low temperature.

Several expressions go on the same axes, told apart by line style. `name=expr` sets the legend
name, and `errorbars` draws the FTLM error bars:

```
> plot ed=ed.C, ftlm=ftlm.C by L where Delta=1.0, h=0.0 with logx, errorbars > figs/05_ed_ftlm.png
  saved figs/05_ed_ftlm.png
```

![ED and FTLM](figs/05_ed_ftlm.png)

The Lanczos data for L = 16 and 18 agrees with ED above T ≈ 0.3. Below that its statistical errors
grow and a spurious bump appears, a known artifact of FTLM with few random vectors. That's the kind
of thing you want to see before trusting a number.

A keyed field is plotted against one of its inputs with `vs`:

```
> plot gap.gap vs L by Delta > figs/05_gap_vs_L.png
  saved figs/05_gap_vs_L.png
```

![gap vs L](figs/05_gap_vs_L.png)

`plot + ...` adds to the current figure:

```
> plot ed.E where L=14, Delta=0.0, h=0.0 with logx > figs/05_plus.png
  saved figs/05_plus.png
> plot + exact.E where h=0.0 > figs/05_plus.png
  saved figs/05_plus.png
```

![plot +](figs/05_plus.png)

## 6. Messy files: a restarted job

One job, L = 12, Δ = 1.0, h = 0.5, crashed and was restarted. It wrote some temperatures twice.
Reading it stops with an error that names the file:

```
> ed.E where L=12, Delta=1.0, h=0.5
  error: curve L=12, Delta=1.0, h=0.5 in data/ed/L_12/Delta_1.0/h_0.5.dat has repeated T = 0.588273. Set duplicates to mean, first, last, or drop to continue.
```

Here the repeated rows are identical, so keeping the first copy is right. `set` changes a
setting for the rest of the session:

```
> set duplicates first
> ed.E where L=12, Delta=1.0, h=0.5 limit 3
  curves with repeated T (first): 1
   L  Delta   h        T         E
  12    1.0 0.5 0.035505 -0.460758
  12    1.0 0.5 0.039725 -0.460650
  12    1.0 0.5 0.044446 -0.460507
  ... more rows (use limit N, or export to a file)
```

glue reports what it changed whenever it touches your data: repaired curves, dropped keys,
interpolation. The report is short by default. `set report full` lists every affected curve.

## 7. Combining sources

Compare ED with the exact infinite chain at Δ = 0:

```
> dX = ed.E - exact.E
> plot dX by L where Delta=0.0, h=0.0 with logx > figs/07_finite_size.png
  aligned ed.E, exact.E: 4 curves matched on (L, Delta, h), grid overlap(n=200), method pchip
  saved figs/07_finite_size.png
```

![finite-size error](figs/07_finite_size.png)

That one line did a lot, and the report says what:

- **Inputs are matched by name.** `exact.E` has no L, so the same exact curve is compared with
  every L. That's called broadcasting.
- **Exact inputs are joined by value.** Delta and h are matched.
- **Ragged inputs are aligned.** T is ragged, and the two sources have different temperatures. So
  for each curve, both are interpolated with `pchip` onto `overlap(n=200)`: 200 points spread over
  the T range both cover.

![alignment](figs/diagram_alignment.png)

The finite-size error falls quickly with L, and it's largest at low T, where the finite chain's
gap matters. `explain` shows the calculation that glue built:

```
> explain plot dX by L where Delta=0.0, h=0.0
  dX = rename(mapping={value: dX})  [L, Delta, h, T:ragged → dX]
    ed.E - exact.E = map(value = a.E - b.E)  [L, Delta, h, T:ragged → value]
      ed.E - exact.E = align(along T, grid overlap(n=200), method pchip)  [L, Delta, h, T:ragged → a.E, b.E]
        ed.E = select(outputs=[E])  [L, Delta, h, T:ragged → E]
          source(ed, duplicates=first)  [L, Delta, h, T:ragged → E, C, M, chi]
        exact.E = select(outputs=[E])  [Delta, h, T:ragged → E]
          source(exact, duplicates=first)  [Delta, h, T:ragged → E, C, M, chi]
        overlap(n=200) = overlap(along=T, count=200, step=0, unmatched=drop)  [L, Delta, h, T:ragged → ]
          ed.E = select(outputs=[E])  [L, Delta, h, T:ragged → E]  (same as above: 87e1e1c077f4)
          exact.E = select(outputs=[E])  [Delta, h, T:ragged → E]  (same as above: 0d78ea4caaeb)
    filters pushed to the sources: Delta=0.0, h=0.0
    ed: read 4 of 31 files
    exact: read 1 of 2 files
    result: [L, Delta, h, T:ragged → dX]
```

![tree of dX](figs/diagram_tree_dX.png)

Each box is one core operation with its type and its id. `align` is a standard-library operation
made of resamples and a join. The overlap grid reads the same two selects: they're the same
nodes, so they're computed once.

### Operands that share their points

Fields from the same table under the same filter have exactly the same points, so they combine
point by point with no interpolation. The report says nothing about aligning. χT should approach
the Curie constant 1/4 at high temperature:

```
> ed.T * ed.chi where L=8, Delta=1.0, h=0.0, T=3.0: limit 3
   L  Delta   h        T    value
   8    1.0 0.0 3.167500 0.210892
   8    1.0 0.0 3.376058 0.213267
   8    1.0 0.0 3.598349 0.215502
  ... more rows (use limit N, or export to a file)
```

### A dimensionless keyed field

A keyed field acts as a constant along each curve. Dividing by the gap measures T in units of
each chain's own gap:

```
> ed.T / gap.gap where L=8, Delta=1.5, h=0.0 limit 3
   L  Delta   h        T    value
   8    1.5 0.0 0.042140 0.055399
   8    1.5 0.0 0.044833 0.058940
   8    1.5 0.0 0.047699 0.062707
  ... more rows (use limit N, or export to a file)
```

Keys that only one side has are dropped and reported. The specific-heat peak in units of the gap
needs both tables, and `gap` has L = 16 and 18, which ED doesn't:

```
> argmax(ed.C) / gap.gap where Delta=1.5, h=0.0
  dropped 2 unmatched keys (only in gap.gap)
   L  Delta   h    value
   8    1.5 0.0 0.702504
  10    1.5 0.0 0.829328
  12    1.5 0.0 1.040007
  14    1.5 0.0 1.129344
```

The ratio grows with L. The peak barely moves, while the finite-size gap shrinks: at Δ = 1.5 the
peak is set by the exchange J, not by the small gap.

## 8. Calculus on curves

### Derivatives: C = dE/dT

The ED files contain C, computed exactly from the spectrum. So we can check a numerical derivative
of E against it:

```
> derr = d(ed.E, T) - ed.C
> plot derr by L where Delta=1.0, h=0.0 with logx > figs/08_derivative_check.png
  saved figs/08_derivative_check.png
```

![derivative check](figs/08_derivative_check.png)

The error is below 2·10⁻³ on a C of about 0.35. `d` keeps the curve's points, so `d(ed.E, T)` and
`ed.C` share their points and nothing is interpolated. `d(y, T, method=fd)` uses finite
differences instead of the fitted spline.

### Integrals: the entropy

The entropy is S(T) = ∫ C/T dT. At high temperature it should approach ln 2 = 0.693 per site:

```
> S = int(ed.C / T, T)
> plot S by Delta where L=14, h=0.0 with logx > figs/08_entropy.png
  saved figs/08_entropy.png
> S @ T=4.0 where L=14, h=0.0
   L  Delta   h        S
  14    0.0 0.0 0.654688
  14    0.5 0.0 0.687434
  14    1.0 0.0      NaN
  14    1.5 0.0 0.674765
```

![entropy](figs/08_entropy.png)

One value is NaN: that run stopped below T = 4, and outside the data glue gives NaN rather than
guess. The `extrapolate` setting can change that. `int` is the running integral from the first
point. `integral` integrates over the whole curve and
gives one number per curve. Since ∫ C dT = E(T_max) − E(T_min), this is a check too:

```
> integral(ed.C, T) - (last(ed.E) - first(ed.E)) where Delta=1.0, h=0.0
   L  Delta   h    value
   8    1.0 0.0 0.000200
  10    1.0 0.0 0.000124
  12    1.0 0.0 0.000674
  14    1.0 0.0 0.000229
```

### Reductions: one number per curve

`max`, `min`, `mean`, `sum`, `first`, `last`, `count`, `argmax`, and `argmin` reduce each curve to a
number. The temperature of the specific-heat peak:

```
> Tpeak = argmax(ed.C)
> plot Tpeak vs L by Delta where h=0.0 > figs/08_Tpeak.png
  saved figs/08_Tpeak.png
```

![Tpeak](figs/08_Tpeak.png)

`argmax` gives a temperature, so its unit is the unit of T.

### Values at a point

`@` and `at` evaluate each curve at a point, or at a list of points:

```
> ed.E @ T=0.5 where Delta=1.0, h=0.0
   L  Delta   h         E
   8    1.0 0.0 -0.343210
  10    1.0 0.0 -0.341774
  12    1.0 0.0 -0.341474
  14    1.0 0.0 -0.341426
> at(ed.E, T=[0.1, 1.0]) where L=14, Delta=1.0, h=0.0
  resampled ed.E: 1 curve, grid points([0.1, 1.0]), method pchip
   L  Delta   h   T         E
  14    1.0 0.0 0.1 -0.444299
  14    1.0 0.0 1.0 -0.204654
```

### Resampling

`resample` puts curves on a grid you choose. On a fixed grid like `logspace`, T becomes exact, so
curves on the same grid match by value, with no further interpolation:

```
> Eg = resample(ed.E, grid=logspace(0.1, 3.0, 60))
> explain Eg
  Eg = rename(mapping={E: Eg})  [L, Delta, h, T → Eg]
    resample(ed.E, grid=logspace(0.1, 3.0, 60)) = resample(along T, grid logspace(0.1, 3.0, 60), method pchip)  [L, Delta, h, T → E]
      ed.E = select(outputs=[E])  [L, Delta, h, T:ragged → E]
        source(ed, duplicates=first)  [L, Delta, h, T:ragged → E, C, M, chi]
      logspace(0.1, 3.0, 60) = axis(T, log:0.1:3.0:60)  [T → ]
    ed: read 31 of 31 files
    result: [L, Delta, h, T → Eg]
```

## 9. Changing variables

### transform: a new input from a formula

`transform(y, u, v = formula)` replaces input u with v. The gap should close like 1/L for a
gapless chain, so plot it against x = 1/L:

```
> plot transform(gap.gap, L, x = 1 / L) vs x by Delta > figs/09_gap_vs_invL.png
  saved figs/09_gap_vs_invL.png
```

![gap vs 1/L](figs/09_gap_vs_invL.png)

For Δ ≤ 1 the lines head to zero, and for Δ = 1.5 they don't: the Ising-like chain has a real gap.
Section 10 turns this into numbers.

A filter on the new input still skips files. glue works out from the file index that only L = 10
gives x = 0.1, and opens only those 4 groups:

```
> explain transform(gap.gap, L, x = 1 / L) where x=0.1
  transform(gap.gap, L, x=1 / L) = transform(input=L, to=x, formula='1 / L')  [x, Delta → gap]
    gap.gap = select(outputs=[gap])  [L, Delta → gap]
      source(gap, duplicates=first)  [L, Delta → gap, e0]
    filters pushed to the sources: x=0.1
    gap: read 4 of 24 files
    result: [x, Delta → gap]
```

### swap: trade an input for an output

E rises with T, so T is a function of E too. `swap` turns `[.., T → E]` into `[.., E → T]`:

```
> TE = swap(ed.E[T=0.05:], T)
> TE @ E=-0.3 where L=14, h=0.0
   L  Delta   h       TE
  14    0.0 0.0 0.182465
  14    0.5 0.0 0.390542
  14    1.0 0.0 0.622588
  14    1.5 0.0 0.918142
```

That's the temperature at which each chain has energy −0.3 per site. `swap` checks that every curve
is monotonic, and says which one isn't if it fails.

### legendre: the free energy

The entropy S(E) is concave, and its Legendre transform along E has slope β = 1/T and value
G = βE − S = F/T. Make S a function of E with `transform`, whose formula may use another field,
then apply `legendre`:

```
> SE = transform(S, T, E = ed.E)
> bF = legendre(SE, E, slope=beta, result=G)
> bF where L=14, Delta=1.0, h=0.0 limit 3
   L  Delta   h     beta         E         G
  14    1.0 0.0 0.257627 -0.050751 -0.697804
  14    1.0 0.0 0.275914 -0.054551 -0.698767
  14    1.0 0.0 0.295648 -0.058640 -0.699883
  ... more rows (use limit N, or export to a file)
```

At high temperature βF → −ln 2, as it should. Check it against F/T = E/T − S computed directly:

```
> direct = transform(ed.E / T - S, T, beta = 1 / T)
> max(abs(bF.G - direct)) where Delta=1.0, h=0.0
  aligned legendre(SE, E, slope=beta, result=G).G, direct: 4 curves matched on (L, Delta, h), grid overlap(n=200), method pchip
   L  Delta   h        value
   8    1.0 0.0 7.763965e-07
  10    1.0 0.0 2.671768e-07
  12    1.0 0.0 6.688226e-06
  14    1.0 0.0 1.146366e-06
```

### Reducing along another input

A reduction can run along any input. Resample onto one grid first, then take the spread over
chain lengths at each temperature:

```
> spread = max(Eg, L) - min(Eg, L)
> plot spread by Delta where h=0.0 with logx, logy > figs/09_spread.png
  resampled ed.E: 16 curves, grid logspace(0.1, 3.0, 60), method pchip
  saved figs/09_spread.png
```

![finite-size spread](figs/09_spread.png)

### stack and rename

`stack` puts fields with the same inputs together, with a new string input that says which one:

```
> all_C = stack(ed=ed.C, ftlm=ftlm.C, tag="method")
> max(all_C) where Delta=1.0, h=0.0
   L  Delta   h method    value
   8    1.0 0.0     ed 0.368722
  10    1.0 0.0     ed 0.354975
  12    1.0 0.0     ed 0.350269
  14    1.0 0.0     ed 0.349786
  16    1.0 0.0   ftlm 0.367212
  18    1.0 0.0   ftlm 0.335152
```

`rename(y, old=new)` renames inputs or outputs, for sources that disagree on names:

```
> rename(gap.gap, L=N) where Delta=1.0
   N  Delta      gap
   8    1.0 0.522674
  10    1.0 0.423239
  12    1.0 0.355848
  14    1.0 0.307106
  16    1.0 0.270190
  18    1.0 0.241249
```

## 10. Your own operations in Python

For anything the built-in operations don't do, write a Python function. This one fits a
polynomial and returns its value at x = 0, which is how you extrapolate a finite-size result:

```python
"""Custom operations for the spin chain tutorial. The project loads this module through [plugins]."""

import numpy as np

import glue


@glue.op(kind="reduce", version="1")
def intercept(x, y, deg=1):
    """Fit a polynomial of degree `deg` to y(x) and return its value at x = 0.

    With x = 1/L^2 this extrapolates a finite-size result to the infinite chain.
    """
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() <= deg:
        return np.nan
    return float(np.polyval(np.polyfit(x[ok], y[ok], int(deg)), 0.0))
```

Tell the project to load it. Plugins run code, so glue asks before loading them, or you pass
`--trust`:

```python
with open("project.toml", "a") as f:
    f.write('\n[plugins]\nmodules = ["fits"]\npath = ["ops"]\n')
```

```
> reload
  loaded project spinchain: 4 tables, 0 views, 0 datasets
> gap_inf = intercept(transform(gap.gap, L, x = 1 / L), x, deg=2)
> gap_inf
   Delta  gap_inf
     0.0 0.000868
     0.5 0.000375
     1.0 0.004170
     1.5 0.065235
```

The extrapolated gap is zero, within the accuracy of this fit, for Δ ≤ 1. For Δ = 1.5 it isn't:
the chain is gapped there.

The same idea extrapolates the energy at each temperature, using x = 1/L²:

```
> Einf = intercept(transform(Eg, L, x = 1 / L^2), x)
> plot Einf - exact.E where Delta=0.0, h=0.0 with logx > figs/10_extrapolation.png
  resampled ed.E: 4 curves, grid logspace(0.1, 3.0, 60), method pchip
  aligned Einf, exact.E: 1 curve matched on (Delta, h), grid overlap(n=200), method pchip
  saved figs/10_extrapolation.png
```

![extrapolation error](figs/10_extrapolation.png)

Against the exact XX result the extrapolation is good to 3·10⁻³, and exact above T ≈ 1. At low T the
1/L² form is too simple, since finite-size effects there are exponential.

![tree of Einf](figs/diagram_tree_Einf.png)

The plugin is an `apply` node. A saved result records it as `fits:intercept@1`.

## 11. Settings

`set` with no arguments shows every setting and where its value comes from:

```
> set
  method         pchip                    (project)
  grid           overlap(n=200)           (project)
  extrapolate    nan                      (default)
  duplicates     first                    (session)
  fewpoints      linear                   (default)
  unmatched      drop                     (default)
  nan_rows       drop                     (default)
  align          auto                     (default)
  branches       error                    (default)
  flat_tol       1e-09                    (default)
  disk_cache     false                    (default)
  disk_cache_mb  1024                     (default)
  strict         false                    (default)
  report         short                    (default)
  fingerprint    hash                     (project)
  cache_format   parquet                  (default)
  datasets_dir   results                  (default)
  read_cache_mb  512                      (default)
  plot.backend   matplotlib              
  plot.style     lines                   
  plot.cmap      viridis                 
  plot.size      (6.0, 4.0)              
  plot.save_script False                   
```

There are four levels, and the closer one wins: `with` on one statement, `using(...)` inside an
expression, `set` for the session, `[settings]` in the project, and the defaults.

```
> ed.E - exact.E where L=14, Delta=0.0, h=0.0 with method=linear, grid=logspace(0.05, 5, 100) limit 2
  aligned ed.E, exact.E: 1 curve matched on (L, Delta, h), grid logspace(0.05, 5.0, 100), method linear
   L  Delta   h        T     value
  14    0.0 0.0 0.050000 -0.002586
  14    0.0 0.0 0.052381 -0.002581
  ... more rows (use limit N, or export to a file)
> max(abs(using(ed.E - exact.E, method=cubic) - dX)) where Delta=0.0, h=0.0
  aligned ed.E, exact.E: 4 curves matched on (L, Delta, h), grid overlap(n=200), method cubic
  aligned ed.E, exact.E: 4 curves matched on (L, Delta, h), grid overlap(n=200), method pchip
  aligned ed.E - exact.E, dX: 4 curves matched on (L, Delta, h), grid overlap(n=200), method pchip
   L  Delta   h    value
   8    0.0 0.0 0.000003
  10    0.0 0.0 0.000002
  12    0.0 0.0 0.000011
  14    0.0 0.0 0.000003
```

A few settings change how fields combine:

- `align=[]` never interpolates, so fields are joined on exactly equal values. The two sources
  share no temperatures, so nothing is left:

```
> ed.E - exact.E where L=14, Delta=0.0, h=0.0 with align=[]
  dropped 67 points of ed.E with no matching T
  dropped 300 points of exact.E with no matching T
  (no data)
```

- `strict=true` makes interpolation an error unless you asked for a grid:

```
> ed.E - exact.E where Delta=0.0, h=0.0 with strict=true
  error: strict mode: ed.E - exact.E needs aligning along T. Add `with grid=...` or use resample().
```

- `extrapolate`, `fewpoints`, `unmatched`, `nan_rows`, and `branches` decide what happens at the
  edges of the data. The reference lists them all.

`unset` goes back to the project or default value.

## 12. Saving results

There are four kinds of names:

| Kind | Lives in | Follows current settings | Holds data |
|---|---|---|---|
| variable | this session | yes | no |
| view | `[views]` in the project | yes | no |
| calc | a `calcs/*.toml` file | no, fixed when saved | no |
| dataset | a `results/*.toml` file plus a cache | no, fixed when saved | yes |

`save` turns a variable into a dataset:

```
> save Tpeak
  curves with repeated T (first): 1
  wrote results/Tpeak.toml and results/Tpeak.parquet (31 curves, 31 rows)
  dataset Tpeak replaces the variable of the same name
> save Einf
  resampled ed.E: 31 curves, grid logspace(0.1, 3.0, 60), method pchip
  wrote results/Einf.toml and results/Einf.parquet (8 curves, 480 rows)
  dataset Einf replaces the variable of the same name
> save dX --view
  wrote view dX to project.toml
> save gap_inf --recipe-only
  wrote calc calcs/gap_inf.toml: [Delta → gap_inf]
  calc gap_inf replaces the variable of the same name
> ls
  table    ed             data/ed/L_{L:int}/Delta_{Delta}/h_{h}.dat
  table    ftlm           sqlite data/ftlm.db
  table    gap            hdf5 data/gap.h5:L_{L:int}/Delta_{Delta}
  table    exact          data/exact/xx_h{h}.csv
  view     dX             ed.E - exact.E
  calc     gap_inf        [Delta → gap_inf]
  dataset  Tpeak          fresh, 31 curves
  dataset  Einf           fresh, 8 curves
  variable derr           d(ed.E, T) - ed.C
  variable S              int(ed.C / T, T)
  variable Eg             resample(ed.E, grid=logspace(0.1, 3.0, 60))
  variable TE             swap(ed.E[T=0.05:], T)
  variable SE             transform(S, T, E=ed.E)
  variable bF             legendre(SE, E, slope=beta, result=G)
  variable direct         transform(ed.E / T - S, T, beta=1 / T)
  variable spread         max(Eg, L) - min(Eg, L)
  variable all_C          stack(ed=ed.C, ftlm=ftlm.C, tag="method")
```

A dataset file holds the calculation as a tree of core operations, one TOML table per node,
followed by its type, its cache, and the fingerprints of the files it read:

```toml
glue = "0.2"
kind = "dataset"
name = "Tpeak"
created = 2026-09-28T02:18:31Z
tool = "glue 0.2.0"
pinned = false
description = ""

[type]
inputs = ["L", "Delta", "h"]
outputs = ["Tpeak"]
exact = ["L", "Delta", "h"]
dtypes = { L = "int" }
units = { h = "J", Tpeak = "J" }

[recipe]
lib = "glue-core 1"
surface = "argmax(ed.C)"
root = "f3cc3e5714b6"

[recipe.inputs]
ed = "../tables/ed.toml"

[recipe.nodes.fae5faf81f75]
op = "source"
def = "13c91b25e926"
duplicates = "first"
digits = { Delta = 10, h = 10, T = 10 }
table = "ed"
name = "ed"

[recipe.nodes.2d2ec33a9829]
op = "select"
of = "fae5faf81f75"
outputs = ["C"]
name = "ed.C"

[recipe.nodes.d90027ea4f5d]
op = "reduce"
of = "2d2ec33a9829"
along = "T"
reduce = "argmax"
method = "-"

[recipe.nodes.0017c18b0c17]
...
```

- Every parameter is written into the node that uses it, including the settings in effect, like
  `duplicates = "first"`. So the dataset means the same thing whatever your settings are later.
- A node's id is a hash of its operation, its parameters, and its inputs' ids. The same
  calculation always gets the same ids.
- A leaf's `def` id comes from what the table reads, not its name or where its file is. Renaming
  the table can't break the dataset, and two projects reading the same files share results.

A saved dataset is a table like any other, read from its cache:

```
> info Tpeak
  dataset Tpeak: dataset cache results/Tpeak.parquet
    type       [L, Delta, h → Tpeak]
    31 keys
    input      L          4 values: 8, 10, 12, 14
    input      Delta      4 values: 0.0, 0.5, 1.0, 1.5
    input      h          2 values: 0.0, 0.5
    output     Tpeak [J]
    recipe     argmax(ed.C)  (5 nodes)
    status     fresh
> plot Tpeak vs L by Delta where h=0.0 > figs/12_saved_Tpeak.png
  saved figs/12_saved_Tpeak.png
```

![saved Tpeak](figs/12_saved_Tpeak.png)

## 13. When the data changes

Two ED jobs are rerun with a finer temperature grid:

```
$ python make_data.py --rerun
rewrote data/ed/L_12/Delta_1.5/h_0.0.dat with 228 temperatures
rewrote data/ed/L_10/Delta_0.5/h_0.5.dat with 180 temperatures
```

```
> status
  Tpeak          stale      ed: 2 changed (L_10/Delta_0.5/h_0.5.dat, ...)
  Einf           stale      ed: 2 changed (L_10/Delta_0.5/h_0.5.dat, ...)
> why Tpeak
  dataset Tpeak: stale
    ed: 2 changed (L_10/Delta_0.5/h_0.5.dat, ...)
    Tpeak = rename(mapping={value: Tpeak})  [L, Delta, h → Tpeak]
      argmax(ed.C) = rename(mapping={C: value})  [L, Delta, h → value]
        reduce(along=T, reduce=argmax, method=-)  [L, Delta, h → C]
          ed.C = select(outputs=[C])  [L, Delta, h, T:ragged → C]
            source(ed, duplicates=first)  [L, Delta, h, T:ragged → E, C, M, chi]
    leaf ed: 31 files, fingerprint sha256:0541fd3868fdcf0e
```

`status` compares each file's content hash with the one recorded when the dataset was saved.
`refresh` recomputes only the curves that read a changed file:

```
> refresh --all
  Tpeak: recomputed 2 of 31 keys, 29 unchanged
  Einf: recomputed 2 of 8 curves, 6 unchanged
    resampled ed.E: 8 curves, grid logspace(0.1, 3.0, 60), method pchip
> status
  Tpeak          fresh      
  Einf           fresh      
```

![changed files](figs/diagram_changed_files.png)

![recomputed keys](figs/diagram_refresh_keys.png)

For `Tpeak`, one number per run, that's 2 of 31. For `Einf`, which combines all L at each
(Δ, h), a changed file changes its whole (Δ, h) curve, so it's 2 of 8. glue works this out by
tracing each changed file's path values up the tree: Δ and h reach the result, and L is reduced
away.

### Values that aren't saved update too

Nothing has to be saved for this to work. glue keeps every value it computes, keyed by its node id
and the fingerprints of the files under it. After a change, the next use recomputes only what the
changed files affect:

```
> plot dX by L where Delta=0.0, h=0.0 with logx > figs/13_before.png
  saved figs/13_before.png
> invalidate ed where L=14, Delta=0.0
  marked 2 of 31 files of ed as changed
  values that depend on them are recomputed when next used, and saved datasets are stale
> plot dX by L where Delta=0.0, h=0.0 with logx > figs/13_after.png
  aligned ed.E, exact.E: 1 curve matched on (L, Delta, h), grid overlap(n=200), method pchip
  updated cached ed.E - exact.E: recomputed 1 of 4 curves
  saved figs/13_after.png
```

`invalidate` marks files as changed by hand, for changes that file contents don't show, like a bug
fix you know about. `status --all` also lists the cached values under each name:

```
> status --all
  Tpeak          stale      ed: 2 changed (L_14/Delta_0.0/h_0.0.dat, ...)
  Einf           stale      ed: 2 changed (L_14/Delta_0.0/h_0.0.dat, ...)
  cached values:
    Eg (variable): 1 cached value, 0 fresh, 1 stale
      stale: resample 51fca9007ee2 (resample(ed.E, grid=logspace(0.1, 3.0, 60))), reads ed
    spread (variable): 1 cached value, 0 fresh, 1 stale
      stale: resample 51fca9007ee2 (resample(ed.E, grid=logspace(0.1, 3.0, 60))), reads ed
    gap_inf (calc): 1 cached value, 1 fresh
    Tpeak (dataset): 1 cached value, 0 fresh, 1 stale
      stale: reduce d90027ea4f5d, reads ed
    Einf (dataset): 2 cached values, 0 fresh, 2 stale
      stale: apply 535f5aaa639c, reads ed
      stale: resample 51fca9007ee2 (resample(ed.E, grid=logspace(0.1, 3.0, 60))), reads ed
```

With `set disk_cache true`, expensive values are also kept in `.glue/cache/`, so the next session
starts from them. `gc` deletes cached values that nothing uses anymore. `pin NAME` keeps a
dataset as it is, even when its files change:

```
> pin Tpeak
  Tpeak pinned
> refresh --all
  Tpeak: pinned, skipped (use --force)
  Einf: recomputed 2 of 8 curves, 6 unchanged
    resampled ed.E: 8 curves, grid logspace(0.1, 3.0, 60), method pchip
> unpin Tpeak
  Tpeak unpinned
> gc
  removed 0 cached values that nothing uses
  kept values for 66 nodes
```

## 14. Exporting

`export` writes plain data with no recipe. The format comes from the extension. `.dat` writes one
block per curve, which gnuplot reads with `index`:

```
> export ed.C to exports/C_heisenberg.csv where Delta=1.0, h=0.0
  wrote exports/C_heisenberg.csv (4 curves, 276 rows)
> export ed.C to exports/C_heisenberg.dat where Delta=1.0, h=0.0
  wrote exports/C_heisenberg.dat (4 curves, 276 rows)
```

```
# T C
# L=8, Delta=1.0, h=0.0
0.032121 8.512495392e-06
0.034236 2.047573349e-05
0.036491 4.629785135e-05
...
```

`plot ... with backend=gnuplot > fig.gp` writes a gnuplot script and its data files instead of a
matplotlib figure:

```
> plot ed.C by L where Delta=1.0, h=0.0 with backend=gnuplot, logx > exports/C.gp
  wrote exports/C.gp
```

## 15. The Python API

Everything above works from Python too. The shell is a thin layer over the same session:

```python
import glue

s = glue.open("project.toml", trust=True)
s.set_setting("duplicates", "first")

r = s.eval("ed.E - exact.E", where="Delta=0.0, h=0.0")
print(r.type)
df = r.to_pandas()
print(df.groupby("L")["value"].agg(["min", "max"]))
print(r.report)
```

```
loaded project spinchain: 4 tables, 1 views, 1 calcs, 2 datasets
warning: dataset Tpeak is stale: ed: 2 changed (L_14/Delta_0.0/h_0.0.dat, ...)
[L, Delta, h, T:ragged → value]
         min       max
L                     
8  -0.009234  0.000002
10 -0.005874  0.000002
12 -0.004039  0.000009
14 -0.002922  0.000003
['aligned ed.E, exact.E: 4 curves matched on (L, Delta, h), grid overlap(n=200), method pchip']
```

`r.tree()` prints the core tree, and names in the session are attributes, so arithmetic builds
the same expressions:

```python
diff = (s.ed.E - s.exact.E).to_pandas(where="L=14, Delta=0.0, h=0.0", method="cubic")
print(len(diff), diff["value"].abs().max())
```

```
200 0.0029218368442319176
```

Data you made in Python can join in. `register` makes a DataFrame a table:

```python
import numpy as np, pandas as pd

# the leading term of the high-temperature expansion: C = (2 + Delta^2) / (16 T^2)
T = np.geomspace(0.05, 10, 80)
hte = pd.concat([pd.DataFrame({"Delta": d, "h": 0.0, "T": T, "C": (2 + d**2) / (16 * T**2)})
                 for d in (0.0, 0.5, 1.0, 1.5)])
s.register("hte", hte, by=["Delta", "h"], x="T")
ratio = s.eval("ed.C / hte.C @ T=3.0", where="L=14, h=0.0").to_pandas()
print(ratio.round(3).to_string(index=False))
```

```
 L  Delta   h  value
14    0.0 0.0  0.979
14    0.5 0.0  1.081
14    1.0 0.0  1.121
14    1.5 0.0  1.114
```

At T = 3 every ratio is close to 1. The rest is the next order of the expansion, which
depends on Δ. Custom operations can be registered inline too:

```python
@glue.op(kind="reduce")
def half_width(x, y):
    """Width of the region where y is above half its maximum."""
    above = x[y >= y.max() / 2]
    return float(above.max() - above.min())

print(s.eval("half_width(ed.C)", where="Delta=1.0, h=0.0").to_pandas())
```

```
    L  Delta    h     value
0   8    1.0  0.0  0.825475
1  10    1.0  0.0  0.817828
2  12    1.0  0.0  0.811698
3  14    1.0  0.0  0.849833
```

## 16. The command line

```
$ glue status --trust
loaded project spinchain: 4 tables, 1 views, 1 calcs, 2 datasets
Tpeak          stale      ed: 2 changed (L_14/Delta_0.0/h_0.0.dat, ...)
Einf           fresh
$ glue status --all --trust | head -8
loaded project spinchain: 4 tables, 1 views, 1 calcs, 2 datasets
Tpeak          stale      ed: 2 changed (L_14/Delta_0.0/h_0.0.dat, ...)
Einf           fresh      
no cached values
```

`Tpeak` is still stale: it was pinned during the last `refresh`. A new process has no cached
values in memory, and this project doesn't use the disk cache, so `--all` finds none.

`glue run SCRIPT.glue` runs a script of shell statements, `glue refresh --all` updates stale
datasets, and `glue` alone starts the interactive shell. There, `help` lists the commands, `help
NAME` explains one, `edit NAME` opens a file in your editor, and `py` drops into Python with the
session loaded.

```
> help transform
  transform(y, u, U = u * 2.0)                          replace input u with a formula
> help invalidate
  invalidate TABLE [where ...]   mark files as changed, so what depends on them recomputes
  invalidate NAME               drop cached values of a view or calc, or mark a dataset invalid
> help intercept
  intercept   custom reduce operation, fits:intercept@1
  Fit a polynomial of degree `deg` to y(x) and return its value at x = 0.
  
  With x = 1/L^2 this extrapolates a finite-size result to the infinite chain.
```

## 17. How it works inside

### The life of a statement

Take `plot dX by L where Delta=0.0, h=0.0`:

1. **Parse.** `glue/lang.py` turns the text into an expression tree, with the `where` and `with`
   clauses. The language has its own small parser, so `y[U=0.1]` and `y @ T=0.1` work.
2. **Elaborate.** `glue/elaborate.py` translates the expression into a tree of **core
   operations**. This is where the rules of section 7 live: it looks at the operands' types, sees
   that T is ragged and differs, and inserts `align` with the grid from the settings. Every setting
   in effect is written into the nodes as a parameter.
3. **Type.** Each node works out its own type from its inputs, in `glue/core/nodes.py`, before any
   data is read. Errors like "no input J" come from here.
4. **Push filters down.** `where Delta=0.0, h=0.0` is pushed through every node that keeps those
   inputs point by point, down to the sources, which skip the files that don't match
   (`glue/core/context.py`). Filters on T would stop at operations along T, since those need the
   whole curve.
5. **Evaluate.** Nodes are computed from the leaves up. Sources read their files
   (`glue/sources.py`). Each value is stored by (node id, filters, fingerprint), so shared nodes
   are computed once, and a changed file is never missed.
6. **Report and plot.** Operations count what they did to the data, and `glue/plotting.py` draws
   the result.

### Code map

| Module | What it does |
|---|---|
| `glue/lang.py` | The parser for statements, expressions, selectors, and options |
| `glue/elaborate.py` | Surface expressions to core trees: names, arithmetic, alignment, all the functions |
| `glue/core/types.py` | Field types `[inputs → outputs]`, with exact or ragged inputs |
| `glue/core/nodes.py` | The 21 core operations and the standard library, one class each |
| `glue/core/formula.py` | Formulas for `map` and `transform`, and filter predicates |
| `glue/core/context.py` | Evaluation: fingerprints, filter pushdown, the value store, `explain` plans |
| `glue/core/incremental.py` | Tracing changed files to the curves they affect |
| `glue/core/store.py` | The value store in memory and on disk, and `invalidate` marks |
| `glue/core/tree.py` | Writing trees to TOML and reading them back, with id checks |
| `glue/sources.py` | Table files, locators (glob, SQLite, HDF5), readers, the file index |
| `glue/numerics.py` | Interpolation, derivatives, and integrals, on SciPy |
| `glue/dataset.py` | Dataset and calc files, status, refresh |
| `glue/session.py` | Names, projects, settings, and the Python API |
| `glue/shell.py`, `glue/cli.py` | The shell commands and the command line |
| `glue/plotting.py` | Plots with matplotlib or gnuplot |

### Identity

Two rules make saving and caching safe:

- **A node's id comes from its calculation**: its operation, its parameters, and the ids of its
  inputs. A leaf's id comes from what it reads. Names are labels only.
- **A value is reused only when its id and its fingerprint both match.** The fingerprint is a hash
  of the files under the node, plus any `invalidate` marks.

So a calculation gets the same ids whatever its tables are called. Load the ED table a second time
under another name, and compare:

```
> load tables/ed.toml as ed_copy
  loaded table ed_copy
```

```python
n1 = session.compile("d(ed.E, T)")[0]
print("same expression again:  ", n1.id == session.compile("d(ed.E, T)")[0].id)
print("same files, other name: ", n1.id == session.compile("d(ed_copy.E, T)")[0].id)
print("a different method:     ", n1.id == session.compile("d(ed.E, T, method=akima)")[0].id)
```

```
same expression again:   True
same files, other name:  True
a different method:      False
```

The copy reads the same files, so its values are the same values, and glue knows it. A different
method is a different calculation.

### Where to read more

- [docs/reference.md](../../docs/reference.md): every command, function, and setting
- [docs/spec.md](../../docs/spec.md): the file formats and the language, exactly
- [docs/core.md](../../docs/core.md): the core operations and their laws, saved trees, identity,
  and caching
