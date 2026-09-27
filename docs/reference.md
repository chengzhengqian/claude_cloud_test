# glue usage reference (version 0.1)

This is the complete reference for using glue: the files you write, the
expression language, every operation, every shell command, settings, and the
Python API. For the design reasons behind these rules, see
[spec.md](spec.md).

Contents

1. [Quick start](#1-quick-start)
2. [How the pieces fit](#2-how-the-pieces-fit)
3. [Table files](#3-table-files)
4. [Project files](#4-project-files)
5. [Dataset files](#5-dataset-files)
6. [Creating and editing interface files](#6-creating-and-editing-interface-files)
7. [The expression language](#7-the-expression-language)
8. [Operations reference](#8-operations-reference)
9. [Grids and interpolation methods](#9-grids-and-interpolation-methods)
10. [Settings](#10-settings)
11. [Shell commands](#11-shell-commands)
12. [Plotting](#12-plotting)
13. [Saving, status, and refresh](#13-saving-status-and-refresh)
14. [Command line](#14-command-line)
15. [Python API](#15-python-api)
16. [Files glue creates](#16-files-glue-creates)
17. [Error messages and fixes](#17-error-messages-and-fixes)
18. [Limits of version 0.1](#18-limits-of-version-01)

---

## 1. Quick start

Say your data looks like this, with two columns (T and E) in each file:

```
data/dmft/U_1.0/J_0.1/n_0.5.dat
data/dmft/U_1.0/J_0.2/n_0.5.dat
...
```

**Step 1.** Let glue suggest a table file:

```
$ glue
> guess data/dmft name=dmft
  48 files matched, coordinates: U, J, n
  2 columns in U_1.0/J_0.0/n_0.8.dat (names from its header comment)

  glue = "0.1"
  kind = "table"
  name = "dmft"
  ...
```

**Step 2.** Write it, or create it directly:

```
> new table dmft pattern="U_{U}/J_{J}/n_{n}.dat" columns=["T", "E"] root="data/dmft"
  wrote dmft.toml
```

**Step 3.** Look, compute, plot, save:

```
> info dmft
> dmft.E where U=1.0, J=0.1 limit 5
> C = d(dmft.E, T)
> plot C by J where U=2.0, n=1.0 > figs/C.png
> save C
> status
```

**Step 4.** Put it in a project file (section 4) so next time `glue` loads
everything at once.

A complete worked example is in `examples/hubbard/`.

---

## 2. How the pieces fit

| Thing | What it is | Where it lives |
|---|---|---|
| **Table** | A description of raw data: where the files are, what the columns mean. | `kind = "table"` TOML file |
| **Project** | A collection of tables, views, and datasets, with shared settings. | `kind = "project"` TOML file |
| **View** | A named expression in the project file. It's computed each time it's used. | `[views]` in the project file |
| **Variable** | A named expression in the current session only. | typed in the shell |
| **Dataset** | A saved result: the data, plus the recipe that made it. | `kind = "dataset"` TOML file + cache file |

Every table, view, variable, and dataset is used the same way in
expressions.

Data is organized as **curves**. In `U_{U}/J_{J}/n_{n}.dat`, the values U, J,
n are **coordinates**. Each combination of coordinates is one curve, and
that combination is its **curve key**. Inside a curve, T is the **x column**
and E is a **value column**.

---

## 3. Table files

A table file describes one source of raw data.

```toml
glue = "0.1"                      # required: spec version
kind = "table"                    # required
name = "dmft"                     # optional, default is the file name
description = "DMFT energy"       # optional

[source]                          # required: where the data is
locator = "glob"
root = "data/dmft"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]                   # how to read one file
format = "text"
columns = ["T", "E"]

[curves]                          # how rows form curves
x = "T"

[columns.T]                       # optional metadata per column
unit = "t"
label = "temperature"

[meta]                            # optional free-form notes
owner = "me"
```

Allowed top-level keys: `glue`, `kind`, `name`, `description`, `source`,
`curves`, `columns`, `meta`. Any other key is an error, so typos are caught.
Keys starting with `x_` are ignored everywhere, so you can use them for your
own notes.

### 3.1 `[source]` with `locator = "glob"` (files in folders)

| Key | Required | Meaning |
|---|---|---|
| `locator` | no | `"glob"` (the default). |
| `root` | no | Folder the pattern starts from, relative to this TOML file. Default: the TOML file's folder. `${VAR}` and `~` are expanded. |
| `pattern` | yes | Path template, see below. |
| `constants` | no | Coordinates with the same value for every file, like `{ method = "dmft" }`. |
| `reader` | yes | The `[source.reader]` table, see 3.4. |

**Path templates.**

| Piece | Matches | Example |
|---|---|---|
| `{name}` or `{name:float}` | a number like `0.1`, `-2`, `1e-3` | `U_{U}` matches `U_0.25` |
| `{name:int}` | a whole number | `k{k:int}` matches `k4` |
| `{name:str}` | any text within one folder name | `{model:str}` matches `hubbard` |
| `*` | any text within one folder name, not captured | `run_*` matches `run_03` |
| other text | itself | `.dat` |

A placeholder never matches across `/`. Files that don't match are skipped,
and `scan` reports how many. If two files give the same curve key (possible
with `*`), their rows are joined in path order.

### 3.2 `[source]` with `locator = "sqlite"`

| Key | Required | Meaning |
|---|---|---|
| `locator` | yes | `"sqlite"` |
| `file` | yes | The database file. |
| `table` | one of these | Read this table. |
| `query` | one of these | Read from this `SELECT` statement. Use it to rename or compute columns. |
| `rename` | no | `{ db_name = "glue_name" }`. |
| `constants` | no | As for glob. |

A SQLite table has no path, so its coordinates must be listed in
`[curves].by`. Filters on coordinates and x become a `WHERE` clause, so only
matching rows are fetched.

```toml
[source]
locator = "sqlite"
file = "../data/qmc.db"
query = "SELECT u AS U, j AS J, n, 1.0 / beta AS T, energy AS E, energy_err AS dE FROM runs"

[curves]
by = ["U", "J", "n"]
x = "T"

[columns.E]
error = "dE"
```

### 3.3 `[source]` with `locator = "hdf5"`

| Key | Required | Meaning |
|---|---|---|
| `locator` | yes | `"hdf5"` |
| `file` | yes | The HDF5 file. May itself be a template like `runs/U_{U}.h5`. |
| `pattern` | yes | Template for object paths inside the file, like `U_{U}/J_{J}`. |
| `constants` | no | As for glob. |

The reader format defaults to `dataset`. Needs `pip install h5py`.

```toml
[source]
locator = "hdf5"
file = "run.h5"
pattern = "U_{U}/J_{J}"

[source.reader]
dataset = "data"        # dataset inside each matched group
columns = ["T", "E"]
```

### 3.4 `[source.reader]`: file formats

| `format` | Reads | Keys |
|---|---|---|
| `text` (default) | numbers separated by spaces or a delimiter | `columns` (required), `delimiter`, `comment` (default `"#"`), `skip_rows`, `max_rows`, `transpose`, `na` |
| `csv` | CSV, with or without a header | `header` (default true), `columns` (required if `header = false`), `rename`, `delimiter`, `comment`, `skip_rows`, `max_rows`, `na` |
| `npy` | a 2D NumPy array | `columns` (required), `transpose` |
| `raw` | raw binary numbers | `columns` (required), `dtype` (default `"float64"`), `header_bytes`, `order` (`"rows"` or `"columns"`), `endian` (`"little"` or `"big"`) |
| `parquet` | Parquet files, columns by name | `rename` |
| `dataset` | HDF5 datasets | `dataset`, `columns`, `fields` (for compound datasets), `transpose` |

Details:

- `columns` names the file's columns in order. Use `"_"` to skip one. The
  count must match the file exactly, or you get an error naming the file.
- `transpose = true` is for files that store each quantity as a row.
- `na` lists strings read as missing values. Default `["nan", "NaN"]`.
- `order = "rows"` means values are stored row after row. `"columns"` means
  each column is stored in full, one after another.

### 3.5 `[curves]`

| Key | Default | Meaning |
|---|---|---|
| `by` | all path coordinates | Columns that identify a curve. List content columns here if a coordinate is stored inside the file (always needed for SQLite). |
| `x` | none | The x column. Leave it out for a **keyed table**. |
| `y` | all other columns | The value columns. Error columns are left out automatically. |

**Keyed tables.** A table without `x` has exactly one row per curve key. Use
it for summary files, like one gap value per run:

```toml
[source]
root = "data/gap"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]
columns = ["gap"]
```

In expressions its values act as a constant along each matching curve:
`dmft.E / gap.gap`.

### 3.6 `[columns.NAME]`

| Key | Applies to | Meaning |
|---|---|---|
| `type` | any | `"float"` (default), `"int"`, or `"str"`. For path coordinates the placeholder decides, and the two must agree. |
| `unit` | any | Shown in plot labels and `info`. |
| `label` | any | Display name for plot axes. |
| `description` | any | Free text. |
| `digits` | float coordinates | Decimal places used to match coordinate values. Default 10. |
| `error` | value columns | Name of the column that holds this column's error bars. |

Coordinates are rounded to `digits` decimal places when read. So `0.1` from a
path and `0.1000000000001` from SQLite are the same curve.

---

## 4. Project files

```toml
glue = "0.1"
kind = "project"
name = "hubbard"
description = "..."

[tables]
dmft = "tables/dmft.toml"
ed   = { file = "tables/ed.toml" }

[[include]]
file = "../2025_runs/project.toml"
prefix = "old_"

[views]
dE  = "dmft.E - ed.E"
C   = { expr = "d(dmft.E, T)", unit = "1", label = "C", description = "specific heat" }
dEc = { expr = "dmft.E - ed.E", method = "cubic", grid = "overlap(n=500)" }

[datasets]
dE_fine = "results/dE_fine.toml"

[settings]
method = "pchip"
grid = "overlap(n=200)"
duplicates = "mean"

[plot]
backend = "matplotlib"
style = "lines"
cmap = "viridis"
size = [6, 4]
save_script = false

[plugins]
modules = ["physops"]
path = ["ops"]

[meta]
notes = "..."
```

| Section | Meaning |
|---|---|
| `[tables]` | Name → table file (a string, or `{ file = "..." }`). The name here wins over the table file's own `name`. |
| `[[include]]` | Load another project's tables, views, and datasets with a name prefix. Its settings aren't used. Names inside its views are prefixed too. |
| `[views]` | Name → expression, or a table with `expr` and optional `unit`, `label`, `description`, and any setting from section 10. |
| `[datasets]` | Name → dataset file. `save` adds entries here for you. |
| `[settings]` | Project defaults for any setting in section 10. |
| `[plot]` | Plot defaults: `backend`, `style`, `cmap`, `size`, `save_script`. |
| `[plugins]` | Python modules that register custom operations. `path` is added to Python's import path, relative to the project. |
| `[meta]` | Free-form notes. |

Names of tables, views, and datasets must be unique within a project, use
letters, digits, and `_`, and not be a reserved word (section 7.2).

A view evaluates with the current settings, except for settings the view
sets itself. So `set method cubic` in the shell changes a plain view's
result. A dataset's settings are fixed when it's saved.

**Plugins run Python code.** The first time you load a project with
`[plugins]`, the shell asks whether to trust it and remembers your answer in
`.glue/trust`. In scripts and non-interactive use, pass `--trust`.

---

## 5. Dataset files

`save` writes these. You normally don't write them by hand.

```toml
glue = "0.1"
kind = "dataset"
name = "dE_fine"
created = 2026-09-27T14:03:00Z
tool = "glue 0.1.0"
pinned = false
description = ""

[recipe]
expr = "dmft.E - ed.E"          # self-contained: views and variables are expanded
source_expr = "dE"              # what was typed
where = ""                      # selection used when saving

[recipe.inputs]
dmft = "../tables/dmft.toml"
ed = "../tables/ed.toml"

[recipe.settings]
method = "pchip"
grid = "overlap(n=400)"
extrapolate = "nan"
duplicates = "mean"
fewpoints = "linear"
unmatched = "drop"
nan_rows = "drop"

[curves]
by = ["U", "J", "n"]
x = "T"
y = ["dE_fine"]

[columns.dE_fine]
unit = "t"

[cache]
file = "dE_fine.parquet"
format = "parquet"
sha256 = "..."
rows = 17600
curves = 44
dropped = 0

[fingerprint]
mode = "stat"
dmft = "sha256:..."
ed = "sha256:..."
```

**What you can edit by hand:**

| Field | Safe to edit? |
|---|---|
| `pinned`, `description`, `[meta]` | Yes. `refresh` keeps them. |
| `[columns.*].unit`, `label` | Yes, it only affects display. |
| `[recipe]` (expression, inputs, settings) | Yes, but then run `refresh NAME --full`. Status only compares inputs, so it won't notice a recipe edit. |
| `[cache]`, `[fingerprint]` | No. glue manages them. |

`refresh` rewrites the whole dataset file, so comments in it are lost. Keep
notes in `description` or `[meta]`.

A dataset saved from data registered in Python has `reproducible = false`
and no `[recipe]`. It can be loaded and used, but not checked or refreshed.

---

## 6. Creating and editing interface files

### 6.1 Ways to create and change files

| Task | How |
|---|---|
| Suggest a table file for a folder | `guess DIR [name=NAME]`. It prints TOML and writes nothing. |
| Create a table file | `new table NAME pattern="..." columns=["T", "E"] [root="..."] [x=T] [format=text] [file=PATH]`. It writes the file, loads it, and adds it to the project's `[tables]`. |
| Edit any file in your editor | `edit NAME` opens the table or dataset file behind NAME (for a view, the project file) in `$EDITOR`, then reloads everything. |
| Edit by hand, outside glue | Edit the TOML, then `reload` in the shell. |
| Turn a variable into a view | `save NAME --recipe-only` writes it to the project's `[views]`. |
| Save a result as a dataset | `save NAME [as PATH] ...` writes the dataset files and adds it to `[datasets]`. |
| Pin or unpin a dataset | `pin NAME`, `unpin NAME` set `pinned` in the dataset file. |
| Change settings for good | Put them in the project's `[settings]`. `set KEY VALUE` only lasts for the session. |
| Pick up new data files | Nothing to do. The index notices new files. `scan NAME` forces a rescan. |

### 6.2 What glue writes into your project file

glue only changes single lines in your project file. Everything else,
including comments, stays as it is.

| Command | Change to `project.toml` |
|---|---|
| `save NAME` | Adds `NAME = "results/NAME.toml"` under `[datasets]`. If a view has the same name, removes it from `[views]`, since the dataset's recipe keeps the expression. |
| `save NAME --recipe-only` | Adds `NAME = "expr"` under `[views]`, or an inline table if the variable has settings. |
| `new table NAME ...` | Adds `NAME = "NAME.toml"` under `[tables]`. |

If a section doesn't exist yet, it's added at the end of the file.

### 6.3 Checking files

Files are checked when they're loaded:

- Unknown keys are errors, with the list of allowed keys.
- `glue = "0.1"` must be present. A newer minor version loads with a warning.
  A different major version is an error.
- Missing required keys (`pattern`, `columns` for text files, `file` for
  SQLite, and so on) are errors that name the file.
- Column count mismatches are reported when a file is first read, with the
  file name.

A quick check after editing a table: `reload`, then `info NAME`, then
`NAME.COLUMN limit 5`.

---

## 7. The expression language

### 7.1 Kinds of values

Every expression has one of these kinds:

| Kind | Shape | Examples |
|---|---|---|
| **Scalar** | one number | `2.5` |
| **Keyed** | one number per curve key | `gap.gap`, `max(dmft.E)`, `dmft.U` |
| **Curves** | one curve y(x) per curve key | `dmft.E`, `d(dmft.E, T)` |
| **Table** | several columns | `dmft` |

A Table can be shown but not used in arithmetic. Pick a column, as in
`dmft.E`.

### 7.2 Lexical rules

- Names: letters, digits, `_`, not starting with a digit.
- Numbers: `1`, `0.5`, `.5`, `1e-3`, `2.0E+4`.
- Strings: `"..."` or `'...'`. Used for string coordinates, labels, and file
  names.
- Comments: `#` to the end of the line.
- A line ending in `\` continues on the next line.
- Reserved words, which can't be used as names:
  `load reload guess new edit scan ls info values show explain del save
  export status refresh pin unpin plot set unset run py help quit with
  where vs by to as`.

### 7.3 Operators

From tightest to loosest binding:

| Operator | Meaning | Example |
|---|---|---|
| `t.c` | column `c` of `t` | `dmft.E` |
| `y[...]` | select curves or an x range | `dmft.E[U=2.0, T=0.1:0.5]` |
| `^`, `**` | power (right-associative) | `T^2` |
| `-y`, `+y` | negation | `-dmft.E` |
| `*`, `/` | multiply, divide | `dmft.E / n` |
| `+`, `-` | add, subtract | `dmft.E - ed.E` |
| `y @ x=v` | value at a point | `dmft.E @ T=0.1` |

So `-T^2` is `-(T^2)`, and `a.E - b.E @ T=0.1` is `(a.E - b.E) @ T=0.1`.
Use parentheses when in doubt.

### 7.4 Names

A bare name is looked up in this order:

1. session variables
2. views
3. tables and datasets
4. coordinate and x names of the tables in the same expression

So in `dmft.E / n`, `n` is the coordinate n of each dmft curve. In
`C / T`, `T` is the x value of C at each point.

If a name is both a table and a coordinate in the same expression, that's an
error. Write `dmft.n` instead.

Column access `t.c`:

| `c` is | Result |
|---|---|
| a value or error column | Curves (Keyed for a keyed table) |
| a coordinate | Keyed: the coordinate value per key |
| the x column | Curves whose values are x itself |

A variable, view, or dataset with one value column can be used bare:
`max(dE)` is the same as `max(dE.dE)`.

### 7.5 Selection

`y[...]` and `where ...` take the same selectors, combined with AND:

| Selector | Meaning |
|---|---|
| `U=0.1` | equal |
| `U=[0.1, 0.2]` | any of these values |
| `U!=0.1`, `U!=[0.1, 0.2]` | not equal |
| `U<0.3`, `U<=0.3`, `U>0.1`, `U>=0.1` | comparison |
| `T=0.1:0.5` | range, both ends included |
| `T=:0.5`, `T=0.1:` | open range |
| `model="hubbard"` | string coordinate |

Selectors can name coordinates and the x column. Filtering on value
columns, like `E<0`, isn't supported yet.

- `y[...]` applies where it's written. `integral(y[T=0.1:0.5], T)` integrates
  only over that range.
- `where ...` at the end of a statement applies to the result, and is passed
  to every input that has that coordinate. It never changes the result, but
  it decides which files are read.

### 7.6 `with` settings

`with KEY=VALUE, ...` at the end of an assignment or statement changes
settings for that statement only:

```
dE = dmft.E - ed.E with grid=overlap(n=500), method=cubic
plot dE by U where n=1.0 with method=akima, logx
```

For an assignment, the settings stay attached to the variable. Inside an
expression, `using(expr, KEY=VALUE)` does the same for one part.

### 7.7 How values combine

This table covers `+ - * / ^` and every operation with more than one input:

| | Scalar | Keyed | Curves |
|---|---|---|---|
| **Scalar** | Scalar | Keyed | Curves |
| **Keyed** | Keyed | Keyed: keys matched | Curves: the keyed value is constant along each curve |
| **Curves** | Curves | Curves: as on the left | Curves: row-aligned or aligned, see below |

Table in arithmetic is always an error.

**Matching keys.** Two inputs are matched on the coordinates they share.
The result has all coordinates of both. A coordinate that only one side has
is broadcast. For example, a reference `ref.E` with only (U, J) compared to
`dmft.E` with (U, J, n) uses the same reference curve for every n.

**Unmatched keys.** Keys that exist on only one side are dropped and
reported, or with `unmatched=error` the statement stops.

**Curves with curves.**

1. Both must have the same x name, or it's an error.
2. **Row-aligned** inputs are combined point by point, with no
   interpolation. That's columns of the same table under the same
   selection, like `dmft.E * dmft.T`, and anything built from them with
   elementwise operations. The same name used twice (`dE - dE`) is also
   row-aligned. Repeated x values are fine here.
3. **Everything else is aligned.** For each matched pair of curves, both
   are sorted by x, cleaned (`nan_rows`, `duplicates`), fitted (`method`,
   `fewpoints`), and evaluated on a grid (`grid`, `extrapolate`). The
   report says how many curves were aligned, and with which grid and method.
4. With `strict=true`, step 3 is an error unless the grid comes from a
   `with grid=...`, `using(...)`, or `resample`.

**A bare x name** like `T` in `C / T` takes its points from the curve it's
combined with. `T` alone, or `T` combined only with numbers, is an error.

**Units.** `+` and `-` keep the unit if both sides have the same unit, or if
one side is a plain number. Other operations drop it. `save ... unit="..."`
or a view's `unit` sets it again.

**Result names.** An assignment names the value column: `dE = ...` gives a
column `dE`. A bare column keeps its name. Other unnamed results are called
`value`.

**Error columns** can be plotted with `errorbars`. They aren't carried
through operations.

---

## 8. Operations reference

### 8.1 Summary

| Operation | Input | Result | Settings it uses |
|---|---|---|---|
| `+ - * / ^` | any (not Table) | see 7.7 | alignment settings, when aligning |
| `-y` | any | same | none |
| `abs sqrt exp log log10 sin cos tan sinh cosh tanh` | any | same | none |
| `resample(y, grid=, method=)` | Curves | Curves | `grid`, `method`, `extrapolate`, `duplicates`, `nan_rows`, `fewpoints` |
| `d(y, x, order=, method=, grid=)` | Curves | Curves | `method`, `extrapolate`, `duplicates`, `nan_rows`, `fewpoints` |
| `int(y, x, method=)` | Curves | Curves | `duplicates`, `nan_rows` (plus `fewpoints` with a spline method) |
| `integral(y, x, method=)` | Curves | Keyed | same as `int` |
| `max min mean first last count argmax argmin` | Curves | Keyed | none |
| `at(y, x=v)`, `y @ x=v` | Curves | Keyed | `method`, `extrapolate`, `duplicates`, `nan_rows` |
| `at(y, x=[v1, ...])`, `y @ x=[...]` | Curves | Curves | same |
| `stack(a=y1, b=y2, tag=)` | Curves or Keyed | same | none |
| `using(y, KEY=VALUE, ...)` | any | same | the ones you set |
| custom `@glue.op` | see 8.9 | | |

### 8.2 Elementwise functions

`abs(y)`, `sqrt(y)`, `exp(y)`, `log(y)` (natural log), `log10(y)`, `sin`,
`cos`, `tan`, `sinh`, `cosh`, `tanh`.

- Work on Scalar, Keyed, and Curves, and keep row alignment.
- `abs` and `-` keep the unit and column name. The others drop both.
- Invalid values (like `log` of a negative number) give NaN.

### 8.3 `resample(y, grid=GRID, method=METHOD)`

Evaluates each curve on a new grid. Without `grid=` it uses the `grid`
setting. For one input, `overlap(...)` means the curve's own range.

```
resample(dmft.E, grid=logspace(0.01, 1, 300))
resample(qmc.E, method=smooth(s=0.001), grid=overlap(n=100))
```

Keeps the unit and column name.

A resampled curve is on a new grid, so combining it with another curve aligns
them again. To put one side on the other side's points, set the grid on the
combination instead:

```
dmft.E - ed.E with grid=like(dmft.E)     # evaluated at dmft's T points
```

Points of `dmft.E` outside the range of `ed.E` then give NaN, because of
`extrapolate = nan`.

### 8.4 `d(y, x, order=1, method=METHOD, grid=GRID)`

Derivative along x. The second argument must be the x name.

| Option | Default | Meaning |
|---|---|---|
| `order` | 1 | 1, 2, or 3 |
| `method` | the `method` setting | A spline method (the derivative of the fitted spline), or `fd` for finite differences with `numpy.gradient` and no fitting |
| `grid` | the input points | Where to evaluate the derivative |

```
C = d(dmft.E, T)
d(dmft.E, T, order=2, method=cubic)
d(qmc.E, T, method=fd)
```

The result has no unit.

### 8.5 `int(y, x, method=trapz)` and `integral(y, x, method=trapz)`

- `int` gives the cumulative integral, starting at 0 at the first point.
  The result is Curves.
- `integral` gives the integral over each whole curve. The result is Keyed.
- `method=trapz` (the default) uses the trapezoid rule on the data points.
  A spline method name integrates the fitted spline instead.

```
S = int(C / T, T)                          # entropy from specific heat
integral(dmft.E[T=0.1:0.5], T)             # over a range
```

### 8.6 Reductions: one number per curve

| Function | Result |
|---|---|
| `max(y)`, `min(y)`, `mean(y)` | over the curve's points |
| `first(y)`, `last(y)` | at the smallest or largest x |
| `count(y)` | number of points |
| `argmax(y)`, `argmin(y)` | the x value at the largest or smallest point (the first one if there's a tie) |

Missing values are ignored. A curve with no points is dropped. `max`, `min`,
`mean`, `first`, and `last` keep the unit.

```
Tpeak = argmax(d(dmft.E, T))
plot Tpeak vs U by J where n=1.0
```

### 8.7 `at(y, x=v)` and `y @ x=v`

Evaluates each curve at a point, using the current `method` and
`extrapolate`. A single point gives Keyed. A list gives Curves on those
points.

```
dmft.E @ T=0.1
at(dmft.E, T=[0.05, 0.1, 0.2])
```

### 8.8 `stack(name1=y1, name2=y2, ..., tag="source")`

Puts families with the same coordinates and x on top of each other, adding a
string coordinate (`tag`) whose values are the argument names. Nothing is
interpolated.

```
all = stack(dmft=dmft.E, ed=ed.E, tag="method")
plot all by method where U=2.0, J=0.1, n=1.0
all where method="ed"
```

### 8.9 `using(y, KEY=VALUE, ...)`

Evaluates `y` with these settings. It's the in-expression version of
`with`.

```
dmft.E - using(ed.E - ref.E, method=linear)
```

### 8.10 Custom operations

Register a Python function with `@glue.op`. It's then usable in the shell,
in views, and in saved recipes.

```python
import glue

@glue.op(kind="reduce", version="1")
def fwhm(x, y):
    ...
    return width
```

| `kind` | Function signature | Input | Result |
|---|---|---|---|
| `elementwise` | `f(y, **options) -> y` | any | same |
| `curve` | `f(x, y, **options) -> (x, y)` | Curves | Curves |
| `reduce` | `f(x, y, **options) -> float` | Curves | Keyed |

Options are passed to the function as keyword arguments, as numbers or
strings. For `def peak_width(x, y, level=0.5)`, write
`peak_width(C, level=0.25)`. Put the
module in the project's `[plugins]`, or register it in Python before use. A
saved recipe records `module.function@version`, and refresh imports that
module again.

---

## 9. Grids and interpolation methods

### 9.1 Grids

| Grid | Points for each matched set of curves |
|---|---|
| `overlap(n=200)` or `overlap(200)` | `n` evenly spaced points over the x range all inputs cover |
| `overlap(step=0.01)` | points every `step` over that range |
| `union()` | every x point of every input, within that range |
| `like(EXPR)` | the x points of `EXPR`'s matching curve |
| `linspace(a, b, n)` | `n` evenly spaced points from `a` to `b`, the same for every curve |
| `logspace(a, b, n)` | `n` log-spaced points from `a` to `b`. `a` and `b` are the actual end values, not exponents. |
| `points([x1, x2, ...])` | exactly these points |

`overlap` and `union` never extrapolate. If two curves don't overlap at all,
that key is dropped and reported. The fixed grids (`linspace`, `logspace`,
`points`) and `like` can go past the data. The `extrapolate` setting decides
what happens there.

### 9.2 Methods

| Method | SciPy | Minimum points | Notes |
|---|---|---|---|
| `linear` | `make_interp_spline(k=1)` | 2 | Straight lines between points. |
| `cubic` | `CubicSpline` | 4 | Smooth, but can overshoot near sharp features. |
| `pchip` (default) | `PchipInterpolator` | 2 | Smooth and never overshoots. A safe default. |
| `akima` | `Akima1DInterpolator` | 5 | Less wiggle than cubic around outliers. |
| `smooth(s=...)` | `UnivariateSpline(k=3, s=...)` | 4 | Doesn't pass through the points. Use it for noisy data. A larger `s` means more smoothing. |
| `fd` | `numpy.gradient` | 2 | Only for `d(...)`. |

Before fitting, every curve is:

1. sorted by x,
2. cleaned of rows with a missing x or y (`nan_rows`),
3. cleaned of repeated x values (`duplicates`),
4. checked for enough points (`fewpoints`).

The report counts each curve these steps change.

---

## 10. Settings

### 10.1 Where settings come from

Higher levels win:

1. `with ...` on one statement, or `using(...)` inside an expression
2. `set KEY VALUE` in the session
3. `[settings]` in the project file
4. the built-in defaults

`set` with no arguments shows every setting and which level it comes from.

### 10.2 All settings

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `method` | `linear`, `cubic`, `pchip`, `akima`, `smooth(s=...)` | `pchip` | Interpolation method |
| `grid` | any grid from 9.1 | `overlap(n=200)` | Grid for aligning curves |
| `extrapolate` | `nan`, `error`, `extend`, `clamp` | `nan` | Outside the data: missing value, stop, continue the fitted curve, or repeat the end value |
| `duplicates` | `error`, `mean`, `first`, `last`, `drop` | `error` | Repeated x within one curve: stop, average, keep the first or last, or drop all copies |
| `fewpoints` | `linear`, `drop`, `error` | `linear` | Too few points for the method: fall back to linear (needs 2 points, else the curve is dropped), drop the curve, or stop |
| `unmatched` | `drop`, `error` | `drop` | Curve keys on only one side of an operation |
| `nan_rows` | `drop`, `error` | `drop` | Rows with a missing x or y |
| `strict` | `true`, `false` | `false` | Require an explicit grid for alignment |
| `report` | `short`, `full`, `off` | `short` | `full` also lists every affected curve key |
| `fingerprint` | `stat`, `hash` | `stat` | How saved datasets detect changed inputs (13.3) |
| `cache_format` | `parquet`, `npz` | `parquet` | Cache format for new datasets. `npz` doesn't need pyarrow. |
| `datasets_dir` | a path | `"results"` | Default folder for `save`, relative to the project |
| `read_cache_mb` | a whole number | `512` | Memory limit for keeping recently read files |
| `plot.backend` | `matplotlib`, `gnuplot` | `matplotlib` | |
| `plot.style` | `lines`, `points`, `linespoints` | `lines` | |
| `plot.cmap` | a matplotlib colormap name | `viridis` | |
| `plot.size` | `(width, height)` in inches | `(6, 4)` | |
| `plot.save_script` | `true`, `false` | `false` | Also write `FIG.glue` next to each saved figure, to recreate it |

`save` records the settings that affect results (`method`, `grid`,
`extrapolate`, `duplicates`, `fewpoints`, `unmatched`, `nan_rows`) in the
dataset's recipe.

---

## 11. Shell commands

Start the shell with `glue` in a folder that has `project.toml`, or with
`glue path/to/project.toml`. Anything that isn't a command is treated as an
assignment or an expression to show.

Paths after `load` and `run` are relative to the script being run, or to the
current folder when typed. Output paths (`plot > FILE`, `save ... as`,
`export ... to`) are relative to the project folder.

### 11.1 Statements

| Syntax | Meaning |
|---|---|
| `NAME = EXPR [with ...]` | Define a variable. Nothing is computed yet. |
| `EXPR [where ...] [with ...] [limit N]` | Compute and show the first rows. Same as `show`. |

### 11.2 Loading and describing data

| Command | Meaning | Example |
|---|---|---|
| `load FILE` | Load a project, table, or dataset file. | `load project.toml` |
| `reload` | Reload every loaded file from disk. Keeps variables and `set` settings. | `reload` |
| `guess DIR [name=NAME]` | Suggest a table file for a folder tree. Prints TOML and writes nothing. | `guess data/ed name=ed` |
| `new table NAME pattern="..." columns=[...] [root="..."] [x=NAME] [format=text] [file=PATH]` | Write a table file, load it, and add it to the project. `columns` may also be `"T,E"`. `x` defaults to the first column. | `new table ed pattern="U_{U}/n_{n}.dat" columns=["T","E"] root="data/ed"` |
| `edit NAME` | Open the file behind NAME in `$EDITOR`, then reload. | `edit dmft` |
| `scan [NAME]` | Rebuild the file index and report files, skipped files, and coordinate values. | `scan dmft` |

### 11.3 Looking at data

| Command | Meaning | Example |
|---|---|---|
| `ls` | List tables, views, datasets (with status), and variables. | `ls` |
| `info NAME` | Source, coordinates and their values, x, value columns, units. For datasets, also recipe, settings, and status. | `info qmc` |
| `values NAME.COORD` | Distinct values of a coordinate. | `values dmft.U` |
| `show EXPR [where ...] [with ...] [limit N]` | Compute and print rows. Default `limit 20`. Stops reading once enough rows are found. | `show max(dmft.E) where n=1.0` |
| `explain STATEMENT` | Show the alignment and interpolation steps and which files and columns would be read, without reading data. Works on expressions, assignments, `plot ...`, and `save NAME`. | `explain plot dE by U where J=0.1, n=1.0` |

### 11.4 Computing and saving

| Command | Meaning | Example |
|---|---|---|
| `del NAME` | Remove a variable. | `del tmp` |
| `save NAME [as PATH] [where ...] [grid=GRID] [format=parquet\|npz] [unit="..."]` | Compute NAME and save it as a dataset (13.1). | `save dE as results/dE_fine grid=overlap(n=400)` |
| `save NAME --recipe-only` | Write the variable NAME as a view in the project file. No data is saved. | `save rel --recipe-only` |
| `export EXPR to FILE [where ...] [with ...]` | Write plain data with no recipe. `.csv`, `.parquet`, or `.dat`/`.txt` (one block per curve for gnuplot). | `export dE to out/dE.dat where U=2.0` |
| `status [NAME]` | Dataset states (13.2). | `status` |
| `refresh NAME \| --all [--full] [--force]` | Recompute stale datasets (13.3). | `refresh --all` |
| `pin NAME`, `unpin NAME` | Stop or allow automatic refresh of a dataset. | `pin figure3_data` |

### 11.5 Plotting

| Command | Meaning |
|---|---|
| `plot Y [, Y ...] [vs X] [by C1 [, C2]] [where ...] [with ...] [> FILE]` | Plot. See section 12. |
| `plot + Y ...` | Add to the current figure. |

### 11.6 Session

| Command | Meaning | Example |
|---|---|---|
| `set` | Show all settings and where each comes from. | `set` |
| `set KEY VALUE` | Change a setting for this session. `set KEY=VALUE` also works. | `set grid overlap(n=500)`, `set plot.backend gnuplot` |
| `unset KEY` | Go back to the project or default value. | `unset grid` |
| `run FILE.glue` | Run a script. It stops at the first error and reports the line. | `run analysis.glue` |
| `py` | Open a Python prompt with `glue_session` (the session) and `glue`. `exit()` returns. | `py` |
| `help [TOPIC]` | Topics: `expressions`, `functions`, `settings`, `plot`, or any command name. | `help save` |
| `quit` | Leave. Ctrl-D also works. | |

Tab completes commands, functions, names, and `table.` columns. History is
kept in `~/.glue_history`.

### 11.7 Scripts

A `.glue` script holds shell statements, one per line, with `#` comments and
`\` line continuation:

```
load project.toml
set duplicates mean
plot C by J where U=2.0, n=1.0 \
     with logx > figs/C.png
save C
```

Run it with `glue run script.glue` (add `--trust` for plugins, `--quiet` to
not print each statement) or with `run script.glue` in the shell.

---

## 12. Plotting

```
plot Y [, Y ...] [vs X] [by C1 [, C2]] [where SELECTORS] [with OPTIONS] [> FILE]
```

| Part | Meaning |
|---|---|
| `Y` | Any Curves or Keyed expression. `label=Y` sets its legend label: `plot dmft=dmft.E, ed=ed.E`. |
| `vs X` | For Curves: the x column (the default, and the only choice in 0.1). For Keyed: the coordinate on the horizontal axis. |
| `by C1` | Color by coordinate C1. |
| `by C1, C2` | Color by C1 and line style by C2. Only with a single `Y`. |
| `where ...` | Which curves to plot, and x ranges. |
| `with ...` | Plot options (below) and any setting from section 10. |
| `> FILE` | Save the figure. The format comes from the extension: `.png`, `.pdf`, `.svg`, ... |

**The free-coordinate rule.** Every coordinate must have one value in the
plotted data, be listed in `by`, or be the `vs` axis. Otherwise curves for
different values would overlap with the same color and look like one
dataset, so it's an error that tells you what to add. Shortcuts: if exactly
one coordinate is left and there's no `by`, it's used as `by`. For Keyed
values without `vs`, a single free coordinate is used as `vs`.

**Colors and legends.**

- Numeric `by`: colors from the colormap, with a legend for up to 6 values
  and a colorbar for more.
- String `by`: separate colors and a legend.
- Several `Y`: each gets a line style and a legend entry. Without `by`, each
  also gets its own color.
- The title defaults to the coordinates with one value, like
  `U=2.0, n=1.0`.
- Axis labels come from column labels and units.

**Options.**

| Option | Meaning |
|---|---|
| `style=lines\|points\|linespoints` | How points are drawn. Keyed values always get markers. |
| `logx`, `logy` | Log axes. |
| `xlim=(a, b)`, `ylim=(a, b)` | Axis limits. |
| `title="..."`, `xlabel="..."`, `ylabel="..."` | Text. |
| `cmap=NAME` | Colormap for numeric `by`. |
| `legend=auto\|off\|colorbar` | Legend type. |
| `errorbars` | Draw error bars for values whose table declares an `error` column. Drawn as points unless `style` is given. |
| `size=(w, h)` | Figure size in inches. |
| `backend=matplotlib\|gnuplot` | See below. |

Examples:

```
plot dmft.E by J where U=2.0, n=1.0 with logx
plot dmft=dmft.E, ed=ed.E by U where J=0.1, n=1.0 > figs/compare.png
plot argmax(C) vs U by J where n=1.0
plot qmc=qmc.E, dmft=dmft.E where U=4.0, J=0.1, n=1.0, T=:0.5 with errorbars
plot dE by J, n where U=2.0
plot + ed.E where U=2.0, J=0.1, n=1.0
```

**Without `> FILE`:** with a display, a window opens. Without one, as over
SSH or in the cloud, the figure is saved to `.glue/last_plot.png` and the
path is printed.

**gnuplot backend.** `with backend=gnuplot > fig.gp` writes `fig.gp` and a
data file per plotted value, with one block per curve. With `> fig.png`,
`.pdf`, or `.svg`, it also runs gnuplot if it's installed.

---

## 13. Saving, status, and refresh

### 13.1 `save`

```
save NAME [as PATH] [where ...] [grid=GRID] [format=parquet|npz] [unit="..."]
```

1. NAME must be a variable or view.
2. Views and variables inside it are expanded, so the recipe only names
   tables and datasets. Views with their own settings become `using(...)`.
3. The current settings, plus NAME's own `with` settings and `grid=`, are
   recorded in the recipe.
4. The result is computed from the input files directly, the same way
   `refresh` does it.
5. glue writes `PATH.toml` and `PATH.parquet` (or `.npz`). The default path
   is `results/NAME`. The dataset's name is the last part of PATH, and its
   value column gets the same name.
6. The dataset is added to the project's `[datasets]`. If a view or variable
   had the same name, the dataset replaces it.

`where` saves only part of the data, and is recorded in the recipe.

### 13.2 States

| State | Meaning | What happens |
|---|---|---|
| `fresh` | Inputs match the recorded fingerprints. | The cache is used. |
| `stale` | Input files changed, were added, or were removed, or an input dataset is stale or was recomputed. | The cache is still used, with a warning. `refresh` updates it. |
| `orphaned` | An input file can't be found. | The cache still works. It can't be recomputed. |
| `modified` | The cache file was changed after saving. | Warning. `refresh` recomputes everything. |
| `no recipe` | Saved from Python data. | Can be used, not checked or refreshed. |

`(pinned)` after a state means `refresh` skips it without `--force`.

When a project loads, every dataset that isn't fresh gets a one-line warning.

### 13.3 Fingerprints and refresh

Each input file's fingerprint is its size plus either its modification time
(`fingerprint = stat`, the default and fast) or a SHA-256 hash of its
content (`fingerprint = hash`, which survives copying files).

`refresh NAME`:

1. Refreshes stale datasets that NAME depends on first.
2. Recomputes only the curves whose input files changed. New files add
   curves, and removed files remove them.
3. Rewrites the cache file and the dataset TOML.

| Flag | Meaning |
|---|---|
| `--all` | Every dataset in the project. |
| `--full` | Recompute every curve. Needed after editing a recipe by hand. |
| `--force` | Include pinned datasets. |

A dataset built from another dataset tracks that input as a whole. When it
changes, all curves are recomputed.

---

## 14. Command line

```
glue [--trust] [PROJECT]                     start the shell
glue run SCRIPT [--project P] [--quiet] [--trust]
glue status [PROJECT] [--trust]
glue refresh NAME | --all [--full] [--force] [--project P] [--trust]
glue scan [PROJECT] [--trust]
```

Without PROJECT, `glue` loads `project.toml` from the current folder if one
exists. `python -m glue` works the same as `glue`. The exit code is 0 on
success and non-zero on an error.

---

## 15. Python API

```python
import glue

s = glue.open("project.toml", trust=True)       # also takes a table or dataset file
```

### 15.1 Evaluating

```python
r = s.eval("dmft.E - ed.E", where="J=0.1, n=1.0", method="cubic")
df = r.to_pandas()          # long format: U, J, n, T, value
arr = r.to_numpy()
r.report                    # report lines, after to_pandas()
```

`eval(expr, where=None, **settings)` returns a lazy result. Nothing is read
until `to_pandas()` or `to_numpy()`. Settings are passed as keyword
arguments, with the same values as in the shell, as strings or numbers.

**Operator overloading.** Names in the session are attributes, and
arithmetic builds the same expressions:

```python
diff = s.dmft.E - s.ed.E
df = diff.to_pandas(where="U=2.0", method="cubic", duplicates="mean")
scaled = (2 * s.dmft.E + 1) / s.gap.gap
```

Handles support `+ - * / **`, unary `-`, and numbers on either side. For
functions, use `s.eval("d(dmft.E, T)")`.

### 15.2 Session methods

| Method | Meaning |
|---|---|
| `s.load(path)`, `s.reload()`, `s.rescan(name=None)` | As the shell commands. |
| `s.define(name, expr, **settings)` | Define a variable, as `name = expr with ...`. |
| `s.save(name, path=None, where="", grid=None, fmt=None, unit=None, recipe_only=False)` | As `save`. `where` and `grid` are text. Returns report lines. |
| `s.status_lines(name=None)` | As `status`. |
| `s.refresh(name=None, full=False, force=False)` | As `refresh`. `name=None` means all. Returns report lines. |
| `s.pin(name, value=True)` | As `pin` / `unpin`. |
| `s.plot(text)` | As `plot`, like `s.plot("dE by U where J=0.1, n=1.0 > fig.png")`. Returns the saved path. |
| `s.set_setting(key, value)`, `s.unset_setting(key)` | As `set` / `unset`. The value is text, like `"overlap(n=500)"`. |
| `s.explain(*s.compile(expr))` | As `explain`. Returns lines. |
| `s.info_lines(name)`, `s.ls_lines()`, `s.settings_lines()` | As `info`, `ls`, `set`. |
| `s.register(name, frame, by, x=None, y=None)` | Use a pandas DataFrame as a table. |

Errors raise `glue.GlueError`, with the same messages as the shell.

### 15.3 Data from Python

```python
s.register("smoothed", df, by=["U", "J", "n"], x="T")
s.eval("smoothed.E - dmft.E").to_pandas()
```

Registered data works like a table. Saving something that uses it gives a
dataset with `reproducible = false`, since glue can't know how it was made.

### 15.4 Custom operations

See 8.10. Functions registered with `@glue.op` in a script can be used by
`s.eval` right away.

---

## 16. Files glue creates

| Path | What | Safe to delete? |
|---|---|---|
| `results/NAME.toml`, `results/NAME.parquet` | Saved datasets | Deleting the cache file makes the dataset `modified`, and `refresh` rebuilds it. |
| `.glue/index/*.json` | File index per table, reused while folders are unchanged | Yes. It's rebuilt on demand. |
| `.glue/trust` | Projects you allowed to load plugins | Yes. You'll be asked again. |
| `.glue/last_plot.png` | The last plot when there's no display | Yes |
| `FIG.glue` | Script to recreate a figure, with `plot.save_script = true` | Yes |

Add `.glue/` to `.gitignore`.

---

## 17. Error messages and fixes

| Message (shortened) | Cause | Fix |
|---|---|---|
| `curve U=2.0, ... in FILE has repeated T = 0.2959` | Two rows with the same x in one curve, often from a restarted job. | `set duplicates mean` (or `first`, `last`, `drop`), or fix the file. |
| `dropped 4 unmatched keys (only in dmft.E)` | Some runs exist in one source only. | Nothing, if that's expected. `unmatched=error` makes it stop instead. |
| `coordinate n not fixed. Curves for different values would overlap.` | A plot has curves for several n in one color. | Add `n=...` to `where`, or add n to `by`. |
| `FILE: file has 3 columns, but the table lists 2` | `columns` doesn't match the file. | Fix `columns` in the table file, using `"_"` for columns to skip. |
| `unknown key 'patern'` | A typo in a TOML file. | The message lists the allowed keys. |
| `unknown name 'dmtf'. Did you mean dmft?` | A typo in an expression. | |
| `dmft has no column 'X'. Columns: ...` | A wrong column name. | Use one of the listed columns. |
| `can't do arithmetic on the table dmft. Pick a column` | A table name used without a column. | `dmft.E` |
| `A has x T but B has x beta` | Curves with different x columns. | Convert one of them in its table, for example in a SQLite `query`. |
| `strict mode: ... are on different grids` | `strict = true` and no explicit grid. | Add `with grid=...`, or use `resample(...)`. |
| `point 1.2 is outside the data range` | `extrapolate = error` and a grid past the data. | Use `overlap(...)`, or `extrapolate nan`. |
| `curves with too few points for cubic (used linear)` | Some curves have fewer points than the method needs. | Nothing, or `fewpoints drop`. |
| `... loads Python plugins ... Run with --trust` | A project with `[plugins]` used non-interactively. | `--trust`, or open it once in the shell and approve. |
| `dataset X is stale: ed: 2 changed (...)` | Input files changed since saving. | `refresh X`. |

For an unexpected internal error, set `GLUE_DEBUG=1` to print the full
traceback in the shell.

---

## 18. Limits of version 0.1

- No reductions across curves, like `mean(y, over=n)`. For now, use Python.
- No interpolation across coordinates, like estimating U=0.15 from U=0.1 and
  U=0.2.
- Error columns aren't carried through operations.
- Units are only kept by `+`, `-`, and a few functions. No unit arithmetic.
- No filters on value columns, like `E<0`.
- `vs` for curves can only be the x column.
- No complex numbers.
- A dataset used as an input to another dataset is tracked as a whole.
