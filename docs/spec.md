# glue: specification, version 0.1 (draft)

`glue` is a working name.

## 1. Scope

`glue` is a small tool for scientific data that is spread across many files.
It does three things:

1. **Describe** existing data files with TOML interface files, without
   converting or moving them.
2. **Compute** across datasets with a small expression language that knows
   the data is a set of curves on different grids, and handles
   interpolation onto common grids.
3. **Keep** derived results as datasets that store both the data and the
   recipe, so they can be checked and recomputed when the inputs change.

The numerical and plotting work is delegated to existing libraries
(NumPy, pandas, SciPy, matplotlib, gnuplot). `glue` decides which calls to
make.

### Non-goals

- Not a database. No query optimizer, indexes, or transactions.
- Not for data larger than memory. Results must fit in memory. Inputs do
  not, because they are read lazily (section 8).
- Not a programming language. No loops, user-defined functions, or
  conditionals. Python is the escape hatch (section 11).
- No file format for raw data. Raw files stay as they are.
- No plotting engine of its own.

### Running example

Used throughout this document:

```
data/
  dmft/
    U_0.1/J_0.1/n_0.5.dat
    U_0.1/J_0.2/n_0.5.dat
    ...
  ed/
    U_0.1/J_0.1/n_0.5.dat
    ...
```

Each `.dat` file is a text file with two columns, T and E. The number of
rows and the T values differ from file to file. T values can repeat within
a file.

---

## 2. Data model

### 2.1 Tables and columns

A **table** is a set of rows with named columns. Every column has a
**role**:

| Role | Meaning | Example |
|---|---|---|
| `coord` | Identifies which curve a row belongs to. Constant along a curve. | U, J, n |
| `x` | The independent variable of a curve. At most one per table. | T |
| `value` | A measured or computed quantity. | E |
| `error` | The uncertainty of one value column. | dE for E |

Column types are `float`, `int`, and `str`. The `x` column must be `float`
or `int`.

### 2.2 Curves

A table with an `x` column is a **curve family**. Rows that share the same
coordinate values form one **curve**. The tuple of coordinate values is the
**curve key**.

Within a curve, rows have no identity beyond their position. The x values
are not a key. They can repeat, and different curves can have different
numbers of points and different x values.

A table without an `x` column is a **keyed table**. It has at most one row
per curve key. Example: one gap value per (U, J, n).

### 2.3 Value kinds in expressions

Every expression evaluates to one of these kinds:

| Kind | Shape | Produced by |
|---|---|---|
| `Scalar` | One number. | Literals like `2.5`. |
| `Keyed` | One number per curve key. | Keyed tables, coordinates, reductions like `max(y)`. |
| `Curves` | One curve y(x) per curve key. | Value columns of curve tables, and most operations. |
| `Table` | Several value columns. | A bare table name like `dmft`. |

A `Table` can be selected, shown, and plotted, but not used in arithmetic.
Pick a column first, as in `dmft.E`.

### 2.4 Coordinate normalization

Float coordinates are rounded to a fixed number of decimal places when they
are loaded. The default is 10, and it can be changed per column with
`digits`. All matching, grouping, and selection uses the rounded values. So
`0.1`, `0.10`, and `0.1000000000001` from different sources all refer to the
same curve.

Float values in `x` and `value` columns are not rounded.

### 2.5 Chunks

A **chunk** is the smallest unit a source can read. For file sources, one
chunk is one file. Each chunk has the coordinates found from its locator,
such as coordinates parsed from its path. The list of chunks is the
**chunk index**. It is built by scanning, which never opens a data file.

---

## 3. File kinds

All interface files are TOML. There are three kinds:

| `kind` | Purpose |
|---|---|
| `table` | Describes raw data from one source. |
| `project` | Collects tables, views, datasets, and settings. |
| `dataset` | A saved result: recipe, cached data, and input fingerprints. |

Every file starts with:

```toml
glue = "0.1"      # spec version
kind = "table"    # or "project" or "dataset"
```

### 3.1 Common rules

- **Relative paths** are relative to the directory of the file that
  contains them. In the shell, `load` and `run` paths are relative to the
  script being run, or to the current directory when typed. Output paths
  (`plot > FILE`, `save ... as`, `export ... to`) are relative to the
  project directory when a project is loaded.
- **Environment variables** in paths are expanded when written as
  `${NAME}`. Example: `root = "${GLUE_DATA}/dmft"`. This lets the same
  interface file work on a cluster and on a laptop.
- **Unknown keys are an error**, so typos are caught. Two exceptions: the
  `[meta]` table holds free-form notes, and any key starting with `x_` is
  ignored.
- **Version.** A file with the same major version and an older or equal
  minor version loads normally. A newer minor version loads with a warning.
  A different major version is an error.
- **Names** of tables, views, datasets, and columns must match
  `[A-Za-z_][A-Za-z0-9_]*` and must not be a reserved word (section 7.9).

---

## 4. Table files

### 4.1 Structure

```toml
glue = "0.1"
kind = "table"
name = "dmft"                   # optional, default is the file name without .toml
description = "DMFT energy vs temperature"

[source]
locator = "glob"
root = "data/dmft"
pattern = "U_{U}/J_{J}/n_{n}.dat"
constants = { method = "dmft" } # optional extra coordinates with fixed values

[source.reader]
format = "text"
columns = ["T", "E"]

[curves]
by = ["U", "J", "n"]            # optional, default is all coord columns
x = "T"

[columns.T]
unit = "K"
label = "temperature"

[columns.E]
unit = "t"
label = "energy"
```

### 4.2 `[source]`: locators

The locator lists chunks and finds their coordinates.

#### `glob`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `root` | path | no | Directory the pattern starts from. Default is the file's directory. |
| `pattern` | string | yes | Path template, see below. |
| `constants` | table | no | Coordinates with the same value for every chunk. |

A **path template** is a relative path with placeholders:

- `{name}` or `{name:type}` matches one coordinate value. The type is
  `float` (default), `int`, or `str`.
- `*` matches any characters within one path segment, and is not captured.
- A placeholder never matches across a `/`.

Matching rules:

| Type | Matches |
|---|---|
| `float` | `[-+]?(\d+\.?\d*\|\.\d+)([eE][-+]?\d+)?` |
| `int` | `[-+]?\d+` |
| `str` | one or more characters other than `/`, as few as possible |

Examples:

```
U_{U}/J_{J}/n_{n}.dat          U_0.1/J_0.2/n_0.5.dat
run_*/beta_{beta}/G_{k:int}.dat   run_03/beta_10/G_4.dat
{model:str}/U{U}.dat           hubbard/U2.5.dat
```

Files that don't match the template are skipped. `scan` reports how many
were skipped (section 7.8).

If several files give the same curve key, which can happen with `*` in the
template, their rows are joined in path order.

If a placeholder name also appears in `[columns]` with a `type`, the two
types must agree.

#### `sqlite`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `file` | path | yes | SQLite database file. |
| `table` | string | one of | Table name to read. |
| `query` | string | one of | A `SELECT` statement to read from. |
| `rename` | table | no | Map from database column names to `glue` names. |

A SQLite source is one chunk. Coordinates come from content columns listed
in `[curves].by`. Filters on coordinates and on x are sent to SQLite as a
`WHERE` clause with bound parameters, wrapped around the table or query.

```toml
[source]
locator = "sqlite"
file = "results.db"
query = "SELECT u AS U, j AS J, n, temp AS T, energy AS E FROM sweep"

[curves]
by = ["U", "J", "n"]
x = "T"
```

#### `hdf5`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `file` | path or template | yes | HDF5 file. May contain placeholders. |
| `pattern` | template | yes | Path of groups or datasets inside the file. |

The pattern is matched against object paths inside the file, using the same
placeholder rules as `glob`. Each match is one chunk.

```toml
[source]
locator = "hdf5"
file = "run.h5"
pattern = "/U_{U}/J_{J}/n_{n}"

[source.reader]
format = "dataset"
dataset = "data"        # dataset inside each matched group
columns = ["T", "E"]
```

### 4.3 `[source.reader]`: readers

The reader turns one chunk into columns. `format` selects the reader.

#### `text`

Whitespace- or delimiter-separated numbers.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `columns` | list of names | required | Names for the file's columns, in order. `"_"` skips a column. |
| `delimiter` | string | whitespace | Column separator. |
| `comment` | string | `"#"` | Lines starting with this are ignored. |
| `skip_rows` | int | 0 | Lines to skip at the start of the file. |
| `max_rows` | int | none | Stop after this many data rows. |
| `transpose` | bool | false | The file stores quantities as rows instead of columns. |
| `na` | list of strings | `["nan", "NaN"]` | Strings read as missing values. |

The number of names in `columns` must equal the number of columns in the
file. A mismatch is an error that names the file.

#### `csv`

Like `text`, plus:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `header` | bool | true | The first line holds column names. |
| `columns` | list | from header | Required when `header = false`. |
| `rename` | table | none | Map from header names to `glue` names. |

#### `npy`

A 2D NumPy array per file. Keys: `columns`, `transpose`.

#### `raw`

Raw binary numbers.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `columns` | list | required | Column names. |
| `dtype` | string | `"float64"` | NumPy dtype name. |
| `header_bytes` | int | 0 | Bytes to skip at the start. |
| `order` | `"rows"` or `"columns"` | `"rows"` | `rows`: values stored row after row. `columns`: each column stored in full, one after another. |
| `endian` | `"little"`, `"big"` | `"little"` | Byte order. |

#### `parquet`

Columns are read by name. Keys: `rename`. Coordinates may come from the path,
from content columns, or both. Filters are passed to `pyarrow`.

#### `dataset` (for the `hdf5` locator)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dataset` | string | none | Dataset name inside a matched group. Omit if the pattern matches datasets. |
| `columns` | list | required for plain 2D datasets | Column names. |
| `fields` | table | none | For compound datasets: map from field names to `glue` names. |
| `transpose` | bool | false | As for `text`. |

### 4.4 `[curves]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `by` | list | all coordinates | Columns that identify a curve. |
| `x` | name | none | The x column. Omit for a keyed table. |
| `y` | list | all other columns | Value columns. Error columns are excluded. |

Coordinates can come from the locator (path) or from content. Content
coordinates must be listed in `by`. Locator coordinates are always
coordinates, whether or not they appear in `by`. Only locator coordinates
can be used to skip chunks without reading them (section 8.2).

**Keyed tables.** If `x` is omitted, each curve key must have exactly one
row. A key with more than one row is an error when that chunk is read. This
is how summary files work, for example one gap value per (U, J, n):

```toml
[source]
locator = "glob"
root = "data/gap"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]
format = "text"
columns = ["gap"]
```

### 4.5 `[columns.NAME]`

Optional metadata for any column.

| Key | Type | Applies to | Meaning |
|---|---|---|---|
| `type` | `float`, `int`, `str` | all | Column type. Default `float`. |
| `unit` | string | all | Unit, used in plot labels. |
| `label` | string | all | Display name, used in plot labels. |
| `description` | string | all | Free text. |
| `digits` | int | float coords | Decimal places for normalization. Default 10. |
| `error` | name | value columns | Name of the column holding this column's error. |

Declaring `error = "dE"` on column `E` gives `dE` the `error` role.

---

## 5. Project files

A project collects tables, views, and datasets under one namespace, with
shared settings.

```toml
glue = "0.1"
kind = "project"
name = "hubbard"

[tables]
dmft = "tables/dmft.toml"
ed   = "tables/ed.toml"
gap  = "tables/gap.toml"

[[include]]
file = "../2025_runs/project.toml"
prefix = "old_"                     # its tables appear as old_dmft, old_ed, ...

[views]
dE  = "dmft.E - ed.E"
rel = { expr = "(dmft.E - ed.E) / ed.E", description = "relative difference" }
C   = { expr = "d(dmft.E, T)", method = "cubic", unit = "1" }

[datasets]
dE_fine = "results/dE_fine.toml"

[settings]
method = "pchip"
grid = "overlap(n=200)"

[plot]
backend = "matplotlib"
cmap = "viridis"

[plugins]
modules = ["myops"]
path = ["ops"]                      # added to the Python import path
```

### 5.1 Sections

| Section | Meaning |
|---|---|
| `[tables]` | Map from name to table file. |
| `[[include]]` | Another project whose tables, views, and datasets are added with a name prefix. Its settings are not used. |
| `[views]` | Named expressions that are not saved as data. Either a string, or a table with `expr` and optional `description`, `unit`, `label`, and settings keys. |
| `[datasets]` | Map from name to dataset file. `save` adds entries here. |
| `[settings]` | Project defaults, see section 9. |
| `[plot]` | Plot defaults, see section 9. |
| `[plugins]` | Python modules that register custom operations, see section 11. |
| `[meta]` | Free-form notes. |

### 5.2 Names

Tables, views, and datasets share one namespace. A duplicate name is an
error when the project loads.

A session variable may have the same name as a view or dataset. It hides
that view or dataset for the rest of the session, and the shell prints a
note. This allows a script that saves a dataset to run again. A variable
can't have the name of a table.

### 5.3 Views and settings

A view is evaluated with the current settings, unless the view sets its own.
So changing `set method cubic` in the shell changes the result of views that
don't set `method`. Datasets are different: their settings are fixed when
they are saved (section 6).

---

## 6. Dataset files

A dataset is a saved result. It has two files: the TOML file, and a cache
file with the data.

```toml
glue = "0.1"
kind = "dataset"
name = "dE_fine"
created = 2026-09-27T14:03:00Z
tool = "glue 0.1.0"
pinned = false
description = ""

[recipe]
expr = "dmft.E - ed.E"
source_expr = "dE"                 # what was typed, before views were expanded
where = ""                         # selection applied when saving, if any

[recipe.inputs]
dmft = "../tables/dmft.toml"
ed   = "../tables/ed.toml"

[recipe.settings]
method = "pchip"
grid = "overlap(n=500)"
extrapolate = "nan"
duplicates = "error"
fewpoints = "linear"
unmatched = "drop"

[curves]
by = ["U", "J", "n"]
x = "T"
y = ["dE_fine"]

[columns.T]
unit = "K"

[columns.dE_fine]
unit = "t"

[cache]
file = "dE_fine.parquet"
format = "parquet"
sha256 = "9f2c..."
rows = 21000
curves = 42
dropped = 3

[fingerprint]
mode = "stat"
dmft = "sha256:a41b..."
ed   = "sha256:77e0..."
```

### 6.1 `[recipe]`

- `expr` must be self-contained. When saving, session variables and views
  are expanded into the expression. Tables and other datasets stay as names,
  resolved through `[recipe.inputs]`.
- A dataset that uses another dataset depends on it. `refresh` uses these
  links (section 6.4).
- `[recipe.settings]` holds every setting that affects the result, with the
  values used. Changing session or project settings later does not change a
  saved dataset.
- `[recipe.plugins]` lists custom operations used, as
  `name = "module.function@version"`.
- A dataset created from Python data without a recipe has no `[recipe]`
  table, and has `reproducible = false` at the top level. It can be loaded,
  but not checked or recomputed.

### 6.2 `[cache]`

| Key | Meaning |
|---|---|
| `file` | Cache file, next to the TOML file. |
| `format` | `parquet` (default) or `npz`. |
| `sha256` | Hash of the cache file, to detect edits. |
| `rows`, `curves`, `dropped` | Counts, for display without reading the file. |

The per-chunk fingerprints and the map from each result curve to the input
chunks it used are stored inside the cache file: in Parquet key-value
metadata under `glue.chunks`, or in an `npz` array named `__glue_chunks__`.
The TOML file stays short even with thousands of inputs.

A saved dataset is read like any other table. Its source is its cache file.

### 6.3 `[fingerprint]`

Each input gets one combined digest: SHA-256 over its sorted list of chunk
entries. A chunk entry is its relative path plus:

- `mode = "stat"` (default): size and modification time in nanoseconds.
- `mode = "hash"`: size and SHA-256 of the content. Slower, but it survives
  copying files, which changes modification times.

For inputs that are themselves datasets, the digest is the input dataset's
`[cache].sha256`. A dataset input is tracked as a whole: when it changes,
every curve that uses it is recomputed.

### 6.4 Status and refresh

When a dataset is loaded or `status` runs, it gets one state:

| State | Condition | Behavior |
|---|---|---|
| `fresh` | Inputs match their fingerprints. | Cache is used. |
| `stale` | Some input chunks changed, were added, or were removed, or an input dataset is stale. | Cache is used, with a warning listing the changes. |
| `orphaned` | Some inputs cannot be found. | Cache is used. Recompute is not possible. |
| `modified` | The cache file's hash doesn't match. | Warning. The data is not trusted until refreshed. |
| `pinned` | `pinned = true`. | Status is still shown, but `refresh` skips it unless given `--force`. |

`refresh NAME`:

1. Refreshes stale datasets that `NAME` depends on, in dependency order.
2. Recomputes only the curves whose input chunks changed, using the stored
   curve-to-chunk map. Added chunks add curves, removed chunks remove curves.
3. Writes a new cache file and updates `[cache]`, `[fingerprint]`, and
   `created`.

`refresh --all` does this for every stale dataset in the project.
`refresh NAME --full` recomputes every curve.

---

## 7. The shell language

The shell reads one **statement** per line. A statement is a command, an
assignment, or an expression. Scripts (`.glue` files) contain the same
statements.

### 7.1 Lexical rules

- Comments start with `#` and run to the end of the line.
- A line ending in `\` continues on the next line.
- Names: `[A-Za-z_][A-Za-z0-9_]*`.
- Numbers: `1`, `0.5`, `.5`, `1e-3`, `2.0E+4`. Numbers are unsigned. A
  leading `-` is unary minus in expressions, and part of the value in
  selectors and options (`signed` in the grammar).
- Strings: `"..."` or `'...'`, used for `str` coordinates, labels, and file
  names.
- File paths after `load`, `run`, `to`, `as`, and `>` may be written without
  quotes if they contain no spaces.

### 7.2 Grammar

The expression language is parsed by its own small parser, not by Python.
The selection syntax (`y[U=0.1]`) and the `@` point syntax are not valid
Python, and a dedicated parser gives clearer error messages.

```ebnf
statement   = command | assignment | expr_stmt ;
assignment  = NAME "=" expr [ with_clause ] ;
expr_stmt   = expr [ where_clause ] [ with_clause ] ;

with_clause  = "with" option { "," option } ;
where_clause = "where" selector { "," selector } ;
option       = NAME [ "=" optvalue ] ;                  (* a bare NAME is a flag, e.g. logx *)
optvalue     = signed | STRING | NAME | call | list | tuple ;

expr        = at_expr ;
at_expr     = additive [ "@" NAME "=" ( signed | list ) ] ;
additive    = multiplicative { ( "+" | "-" ) multiplicative } ;
multiplicative = unary { ( "*" | "/" ) unary } ;
unary       = ( "-" | "+" ) unary | power ;
power       = postfix [ ( "^" | "**" ) unary ] ;        (* right-associative *)
postfix     = primary { "." NAME | "[" selector { "," selector } "]" } ;
primary     = NUMBER | STRING | NAME | call | "(" expr ")" ;
call        = NAME "(" [ arg { "," arg } ] ")" ;
arg         = expr | NAME "=" optvalue ;

selector    = NAME cmp value
            | NAME "=" range ;
cmp         = "=" | "!=" | "<" | "<=" | ">" | ">=" ;
value       = signed | STRING | list ;
range       = [ signed ] ":" [ signed ] ;
list        = "[" value { "," value } "]" ;
tuple       = "(" signed "," signed ")" ;
signed      = [ "-" | "+" ] NUMBER ;
```

Precedence, from tightest to loosest:

1. `.name` and `[selectors]`
2. `^` and `**` (right-associative)
3. unary `-` and `+`
4. `*` and `/`
5. `+` and `-`
6. `@`

So `-T^2` is `-(T^2)`, and `a.E - b.E @ T=0.1` is `(a.E - b.E) @ T=0.1`.

### 7.3 Name resolution

A bare name in an expression is resolved in this order:

1. Session variables.
2. Views.
3. Tables and datasets.
4. Coordinate and x names of the other operands in the same expression.

If a name matches both a table (or view, dataset, variable) and a
coordinate in the same expression, it's an error: "`n` is both a table and
a coordinate of dmft. Use `dmft.n`, or rename the table."

`t.c` looks up column `c` of `t`:

- a value column gives `Curves` (or `Keyed` for a keyed table),
- a coordinate gives `Keyed`,
- the x column gives `Curves` whose y is x itself.

A variable, view, or dataset with exactly one value column can be used
without naming the column. `max(dE)` is the same as `max(dE.dE)`.

### 7.4 Selection

`y[selectors]` keeps part of `y`. Several selectors are combined with AND.

| Selector | Meaning |
|---|---|
| `U=0.1` | Equal, after normalization (section 2.4). |
| `U=[0.1, 0.2]` | Any of these values. |
| `U!=0.1`, `U<0.3`, `U>=0.1` | Comparison. |
| `T=0.1:0.5` | Range, both ends included. |
| `T=:0.5`, `T=0.1:` | Open range. |
| `model="hubbard"` | String coordinate. |

Selectors may name coordinates and the x column. Selecting on value
columns is not supported in version 0.1. A selector on a name the operand
doesn't have is an error.

Selection applies where it is written. `integral(y[T=0.1:0.5], T)`
integrates only over that range. The engine may read less data than this
suggests (section 8), but it never changes the result.

A `where` clause on a statement applies to the result. It is passed down to
every operand that has the named coordinate. Operands without that
coordinate are broadcast and are not affected.

### 7.5 Combining operands

This is the core of the language. These rules apply to `+ - * / ^` and to
every operation with more than one input.

**Scalar with anything.** The scalar is used for every point.

**Keyed with Keyed.** Keys are matched on shared coordinates. Coordinates
that only one side has are broadcast. The result is `Keyed`.

**Keyed with Curves.** The keyed value is used as a constant along each
matching curve. Example: `dmft.E / n`, or `dmft.E - gap.gap`.

**Curves with Curves.**

1. Both sides must have the same x name. Otherwise it's an error.
2. Keys are matched on shared coordinates. The result has the union of both
   coordinate sets. Coordinates that only one side has are broadcast.
3. Keys without a match are handled by the `unmatched` setting. With
   `drop`, they are left out and listed in the report. With `error`, the
   statement fails.
4. **Row-aligned operands** are combined point by point, with no
   interpolation. Two operands are row-aligned when they have the same
   **grid origin**. Columns of one table under the same selection share a
   grid origin. Elementwise operations keep the origin. `resample`,
   alignment, `d`, `int`, and `at` with a list create a new origin, because
   they sort each curve and may remove rows. Row-aligned operands may
   contain repeated x values.
5. **Other operands are aligned.** For each matched pair of curves, both
   are resampled onto a grid from the `grid` setting, with the `method`,
   `extrapolate`, `duplicates`, and `fewpoints` settings. The result has a
   new grid origin.
6. With `strict = true`, step 5 is an error unless the grid comes from a
   `with grid=...` clause or `using(...)`, or the operands are wrapped in
   `resample`.

A name used twice in one expression is the same node, so `dE - dE` is
row-aligned and gives exactly zero.

**Units.** `+` and `-` keep the unit when both sides have the same unit.
Other operations drop the unit. It can be set again with `unit=` on `save`,
or in a view.

**Error columns** can be plotted with `errorbars` (section 7.8). They are
not carried through operations in version 0.1, and are dropped with a note
in the report.

**Result name.** An assignment names its result: in `dE = dmft.E - ed.E`,
the value column is called `dE`. Unnamed results are called `value`.

### 7.6 Grids and methods

Grid specs, used in the `grid` setting and in `resample`:

| Spec | Grid for each matched curve pair |
|---|---|
| `overlap(n=200)` or `overlap(200)` | `n` evenly spaced points over the x range both curves cover. |
| `overlap(step=0.01)` | Points every `step` over the shared range. |
| `union()` | All x points of all operands, inside the shared range. |
| `like(NAME)` | The x points of that operand's matching curve. |
| `linspace(a, b, n)` | `n` evenly spaced points from `a` to `b`. The same for every curve. |
| `logspace(a, b, n)` | `n` log-spaced points from `a` to `b`. The ends are the actual values, not exponents. |
| `points([x1, x2, ...])` | These exact points. |

For a single operand, as in `resample(y, grid=overlap(100))`, "shared range"
means that curve's own range.

Fixed grids (`linspace`, `logspace`, `points`) can extend past the data. The
`extrapolate` setting decides what happens there.

Interpolation methods, used in the `method` setting:

| Method | SciPy class | Minimum points |
|---|---|---|
| `linear` | `make_interp_spline(k=1)` | 2 |
| `cubic` | `CubicSpline` (not-a-knot) | 4 |
| `pchip` | `PchipInterpolator` | 2 |
| `akima` | `Akima1DInterpolator` | 5 |
| `smooth(s=...)` | `UnivariateSpline` with smoothing factor `s` | 4 |

Before fitting, each curve is sorted by x, and rows with a missing x or y
are removed (per the `nan_rows` setting). Then repeated x values are
handled by the `duplicates` setting, and curves with too few points by the
`fewpoints` setting. Every curve these steps change is counted in the
report.

### 7.7 Functions

| Function | Input | Result | Meaning |
|---|---|---|---|
| `abs sqrt exp log log10 sin cos tan sinh cosh tanh` | any | same kind | Elementwise. |
| `resample(y, grid=, method=)` | Curves | Curves | Evaluate each curve on a new grid. |
| `d(y, x, order=1, method=, grid=)` | Curves | Curves | Derivative along x. Default is at the input points, after sorting and the `duplicates` setting. `method=fd` uses finite differences (`numpy.gradient`) with no fitting. |
| `int(y, x, method="trapz")` | Curves | Curves | Cumulative integral from the first point. `method` is `trapz`, or a spline method to integrate the fitted spline. |
| `integral(y, x, method="trapz")` | Curves | Keyed | Integral over the whole curve. |
| `max min mean first last count` | Curves | Keyed | Reduce each curve to one number. |
| `argmax argmin` | Curves | Keyed | The x value at the maximum or minimum data point. |
| `at(y, x=v)` | Curves | Keyed | Each curve evaluated at `v` with the current method. `y @ x=v` is the same. |
| `at(y, x=[v1, v2])` | Curves | Curves | Each curve evaluated at these points. |
| `stack(a=y1, b=y2, tag="source")` | Curves or Keyed | same kind | Stack families with the same coordinates and x name. Adds a `str` coordinate `tag` whose values are the argument names. No resampling. |
| `using(y, KEY=VALUE, ...)` | any | same kind | Evaluate `y` with these settings. Saving a dataset writes views that have their own settings as `using(...)`, so recipes keep them. |

Custom functions registered by plugins are called the same way (section 11).

### 7.8 Commands

Commands start with a reserved word. Arguments in `[ ]` are optional.

#### Loading and describing data

| Command | Meaning |
|---|---|
| `load FILE` | Load a project, table, or dataset file. Loading a second project merges its names. Duplicates are an error. |
| `reload` | Reload all loaded files from disk. |
| `guess DIR` | Look at a directory tree and suggest a table definition: a path template from `name_number` segments, and the column count from the first file. Prints TOML and does not write it. |
| `new table NAME pattern=... columns=... [root=...] [x=...] [format=text] [file=...]` | Write a table file and add it to the current project. Options are separated by spaces or commas. |
| `edit NAME` | Open the file behind `NAME` in `$EDITOR`, then reload it. |
| `scan [NAME]` | Rebuild the chunk index. Reports matched files, skipped files, and coordinate values found. |

#### Looking at data

| Command | Meaning |
|---|---|
| `ls` | List tables, views, datasets, and variables, with their kind and status. |
| `info NAME` | Schema, coordinates with their value counts, number of curves and chunks. Uses the chunk index only. |
| `values NAME.COORD` | Distinct values of a coordinate. Uses the chunk index when possible. |
| `show EXPR [where ...] [limit N]` | Evaluate and print rows. Default `limit 20`. |
| `explain STATEMENT` | Print the alignment and interpolation steps and which chunks and columns would be read. Works on expressions, assignments, `plot ...`, and `save NAME`. Only sources with content coordinates, such as SQLite, need a query to list their keys. |

#### Computing and saving

| Command | Meaning |
|---|---|
| `NAME = EXPR [with ...]` | Define a session variable. Nothing is computed yet. |
| `del NAME` | Remove a session variable. |
| `save NAME [as PATH] [where ...] [grid=...] [format=parquet\|npz] [unit="..."]` | Compute and save `NAME` as a dataset, and add it to `[datasets]`. Default path is `DATASETS_DIR/NAME`. The dataset's name is the file name of `PATH`. If the result has one value column, that column gets the same name. If the dataset's name equals a view or variable, the dataset replaces it. Nothing is lost, since the recipe keeps the expression. |
| `save NAME --recipe-only` | Write `NAME` as a view in the project file. No data is saved. |
| `export EXPR to FILE [where ...]` | Write plain data with no recipe. The format comes from the extension: `.csv`, `.parquet`, `.dat`. `.dat` writes one block per curve separated by blank lines, with the curve key in a comment, which gnuplot reads as `index` blocks. |
| `status [NAME]` | Dataset states (section 6.4). |
| `refresh NAME \| --all [--full] [--force]` | Recompute stale datasets. |
| `pin NAME`, `unpin NAME` | Set or clear `pinned`. |

#### Plotting

```
plot Y { "," Y } [ vs X ] [ by C1 [ "," C2 ] ] [ where ... ] [ with ... ] [ > FILE ]
plot + Y ...            # add to the current figure
```

- `Y` is any `Curves` or `Keyed` expression. Several are drawn on the same
  axes and told apart by line style. `label=expr` sets a legend label, as
  in `plot dmft=dmft.E, ed=ed.E vs T`. The default label is the expression
  text.
- `vs X` is the x column for `Curves` (the default is the curve's x), or a
  coordinate for `Keyed`, as in `plot max(dmft.E) vs U by J where n=0.5`.
- `by C1` colors curves by `C1`. A numeric `C1` uses a colormap, with a
  legend for up to 6 values and a colorbar for more. A string `C1` uses
  separate colors and a legend. `by C1, C2` also uses line style for `C2`.
  Line style can only mean one thing, so `by C1, C2` is only allowed with a
  single `Y`. Without `by`, each `Y` gets its own color.
- **Every coordinate must have a single value in the plotted data, be
  listed in `by`, or be used as `vs X`.** Otherwise curves for different
  values would overlap with the same color and style, so it's an error
  that names the free coordinates. One exception: if exactly one
  coordinate is free and there is no `by`, that coordinate is used as `by`.
  For `Keyed` values without `vs`, a single free coordinate is used as `vs`.
- The title defaults to the coordinates with a single value, like
  `U=2.0, n=1.0`.
- Values with an error column, plotted `with errorbars`, are drawn as
  points with error bars unless a `style` is given.
- `> FILE` saves the figure. The format comes from the extension. With the
  gnuplot backend, `> fig.gp` writes a script plus its data file.

`with` options for `plot`:

| Option | Meaning |
|---|---|
| `style=lines\|points\|linespoints` | Mark style. |
| `logx`, `logy` | Log axes. Written as bare names: `with logx, logy`. |
| `xlim=(a, b)`, `ylim=(a, b)` | Axis limits. |
| `title="..."`, `xlabel="..."`, `ylabel="..."` | Text. Labels default to column labels and units. |
| `cmap=NAME` | Colormap for numeric `by`. |
| `legend=auto\|off\|colorbar` | Legend type. |
| `errorbars` | Draw error columns if the values have them. |
| `size=(w, h)` | Figure size in inches. |
| `backend=matplotlib\|gnuplot` | Plot backend. |

Settings from section 9, like `method` and `grid`, can also go in a plot's
`with` clause.

#### Session and other

| Command | Meaning |
|---|---|
| `set` | Show all settings and where each value came from. |
| `set KEY VALUE` | Change a setting for this session. |
| `unset KEY` | Go back to the project or built-in value. |
| `run FILE.glue` | Run a script. |
| `py` | Open a Python prompt with the session available as `glue_session` (section 11). `exit()` returns to the shell. |
| `help [TOPIC]` | Help on commands, functions, and settings. |
| `quit` | Leave the shell. |

### 7.9 Reserved words

Command names and keywords cannot be used as names of tables, views,
datasets, variables, or columns:

```
load reload guess new edit scan ls info values show explain del save
export status refresh pin unpin plot set unset run py help quit
with where vs by to as
```

Function names can be used as names, since a function call is always
followed by `(`.

### 7.10 Reports

Any statement that aligns, drops, or changes data prints a short report,
unless `report = off`:

```
> dE = dmft.E - ed.E
> show dE limit 5
  aligned dmft.E, ed.E: 42 curves matched on (U, J, n)
    grid overlap(n=200), method pchip
    dropped 3 unmatched keys (only in dmft)
    2 curves had repeated T: error
```

With `report = full`, every affected curve key is listed. `explain` shows
the same information before running anything.

Error messages name the curve key and the file: "curve U=0.1, J=0.2, n=0.5
in data/ed/U_0.1/J_0.2/n_0.5.dat has repeated T=0.25. Set duplicates to
mean, first, last, or drop to continue."

---

## 8. Evaluation

The rules in this section never change results. They only decide what gets
read and when.

### 8.1 Laziness

Assignments and views build an operation tree. Data is read only by `show`,
`plot`, `save`, `export`, `values` (when the index isn't enough), and the
Python API's `to_pandas()` and `to_numpy()`.

### 8.2 Pushdown

Before reading, the engine works backward from the result:

- **Coordinate filters** pass through every operation in version 0.1, since
  every operation works one curve at a time. At a source, they are checked
  against the chunk index, and chunks that don't match are never opened.
- **Coordinates missing from an operand** are dropped at that operand, since
  it is broadcast.
- **x-range filters** pass through elementwise operations. They stop at
  `resample`, alignment, `d`, and `int`, which need points outside the range.
  The whole curve is read and the range is applied after the operation.
- **Columns.** Only columns the result needs are read. For `text`, this is
  `usecols`.
- **Row filters inside a chunk** are sent to backends that support them:
  SQLite (`WHERE`), Parquet (`pyarrow` filters), and HDF5 (slicing, if x is
  sorted). For `text`, `csv`, `npy`, and `raw`, the smallest unit read is a
  whole chunk.

### 8.3 One curve at a time

The engine loops over matched curve keys. For each key it reads the chunks
that curve needs, computes the result for that curve, and keeps it. Memory
holds one curve's inputs plus the results so far.

### 8.4 Caches

- **Chunk index.** Stored in `.glue/index/` in the project directory.
  Reused until `scan`, or until a directory's modification time changes.
- **Read cache.** In memory, keyed by (path, modification time, columns),
  with a size limit (`read_cache_mb`). The oldest entries are dropped
  first.

The `.glue/` directory should be ignored by version control.

---

## 9. Settings

Settings come from four levels. Higher levels win:

1. `with` on one statement
2. `set` in the session
3. `[settings]` in the project
4. Built-in defaults

| Key | Values | Default | Meaning |
|---|---|---|---|
| `method` | `linear`, `cubic`, `pchip`, `akima`, `smooth(s=...)` | `pchip` | Interpolation method. |
| `grid` | grid spec (section 7.6) | `overlap(n=200)` | Grid for alignment. |
| `extrapolate` | `nan`, `error`, `extend`, `clamp` | `nan` | Outside the data: missing value, error, continue the fitted curve, or repeat the edge value. |
| `duplicates` | `error`, `mean`, `first`, `last`, `drop` | `error` | Repeated x values within one curve, before fitting. `drop` removes every row with a repeated x. |
| `fewpoints` | `linear`, `drop`, `error` | `linear` | A curve with fewer points than `method` needs. `linear` falls back to linear if the curve has at least 2 points, and drops it otherwise. |
| `unmatched` | `drop`, `error` | `drop` | Curve keys that exist on only one side. |
| `nan_rows` | `drop`, `error` | `drop` | Rows with a missing x or y, before fitting. |
| `strict` | `true`, `false` | `false` | Require explicit grids for alignment. |
| `report` | `short`, `full`, `off` | `short` | Report detail. |
| `fingerprint` | `stat`, `hash` | `stat` | Fingerprint mode for new datasets. |
| `cache_format` | `parquet`, `npz` | `parquet` | Cache format for new datasets. |
| `datasets_dir` | path | `"results"` | Default folder for `save`. |
| `read_cache_mb` | int | `512` | Read cache size limit. |

`[plot]` keys: `backend` (`matplotlib`), `style` (`lines`), `cmap`
(`viridis`), `size` (`[6, 4]`), `save_script` (`false`). With
`save_script = true`, `plot ... > fig.pdf` also writes `fig.glue`, a script
with the statements that recreate the figure.

The settings that go into `[recipe.settings]` when saving are `method`,
`grid`, `extrapolate`, `duplicates`, `fewpoints`, `unmatched`, and
`nan_rows`.

---

## 10. Command-line interface

```
glue [PROJECT]                 start the shell, optionally loading a project
glue run SCRIPT.glue           run a script and exit
glue status [PROJECT]          print dataset states
glue refresh --all [PROJECT]   recompute stale datasets
glue scan [PROJECT]            rebuild chunk indexes
```

If no project is given, `glue` looks for `project.toml` in the current
directory.

---

## 11. Python

### 11.1 API

The shell is a thin layer over the Python API. Both use the same engine.

```python
import glue

p = glue.open("project.toml")
dE = p.eval("dmft.E - ed.E", where="n=0.5", method="pchip")
df = dE.to_pandas()                     # long format: U, J, n, T, dE
p.plot("dE vs T by J where n=0.5")

# operator overloading builds the same operation tree
dE2 = p.dmft.E - p.ed.E
```

Values from `eval` are lazy until `to_pandas()` or `to_numpy()`.

### 11.2 Custom operations

```python
import glue

@glue.op(kind="curve", version="1")
def kramers_kronig(x, y):
    ...
    return x_new, y_new
```

| `kind` | Signature | Result |
|---|---|---|
| `elementwise` | `f(y) -> y` | same kind |
| `curve` | `f(x, y, **options) -> (x, y)` | Curves |
| `reduce` | `f(x, y, **options) -> float` | Keyed |

Operations work on one curve at a time, so laziness and pushdown still
apply. After registration, `kramers_kronig(dmft.E)` works in the shell,
in views, and in recipes. Recipes record it in `[recipe.plugins]`.

### 11.3 Trust

Plugins are Python imports, so loading a project with `[plugins]` runs
code. The shell asks once per project and remembers the answer in
`.glue/trust`. `glue --trust` skips the question. Expressions and TOML
files never run code on their own.

### 11.4 Python escape

`py` opens a Python prompt. Data built there can be registered:

```python
glue_session.register("smoothed", df, by=["U", "J", "n"], x="T")
```

Registered data works like a table. If it's saved, the dataset has
`reproducible = false`, since the tool can't know how it was made.

---

## 12. Worked example

### Files

`tables/dmft.toml`:

```toml
glue = "0.1"
kind = "table"

[source]
locator = "glob"
root = "../data/dmft"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]
format = "text"
columns = ["T", "E"]

[curves]
x = "T"

[columns.T]
unit = "K"

[columns.E]
unit = "t"
```

`tables/ed.toml` is the same with `root = "../data/ed"`.

`project.toml`:

```toml
glue = "0.1"
kind = "project"
name = "hubbard"

[tables]
dmft = "tables/dmft.toml"
ed   = "tables/ed.toml"

[views]
dE = "dmft.E - ed.E"

[settings]
method = "pchip"
grid = "overlap(n=200)"
```

### Session

```
$ glue
loaded project hubbard: 2 tables, 1 view

> info dmft
  table dmft: 312 curves in 312 files
  coordinates U (4 values), J (6), n (13)
  x T [K], values E [t]

> plot dE vs T by J where n=0.5
  error: coordinate U is not fixed. Curves for different U would overlap.
  Add U to where, or use: by J, U

> plot dmft=dmft.E, ed=ed.E vs T by J where U=0.1, n=0.5
  read 6 of 312 files (dmft), 6 of 298 files (ed)

> explain plot dE vs T by J where U=0.1, n=0.5
  dE = sub(resample(dmft.E), resample(ed.E))
    grid overlap(n=200), method pchip
  dmft: read 6 of 312 files, columns T, E
  ed:   read 6 of 298 files, columns T, E

> plot dE vs T by J where U=0.1, n=0.5
  aligned dmft.E, ed.E: 6 curves matched on (U, J, n)

> C = d(dmft.E, T)
> plot argmax(C) vs U by J where n=0.5 > figs/peak.pdf

> save dE as results/dE_fine grid=overlap(n=500)
  aligned dmft.E, ed.E: 42 curves matched on (U, J, n), 3 dropped
  wrote results/dE_fine.toml and results/dE_fine.parquet

> status
  dE_fine   fresh
```

Later, after two files in `data/ed` are rerun:

```
> status
  dE_fine   stale   ed: 2 chunks changed
> refresh dE_fine
  recomputed 2 of 42 curves
```

---

## 13. Not in version 0.1

Planned, and possible to add without changing the rules above:

- Reductions across curves, such as `mean(y, over=n)`. These need all curves
  at once, so they break the one-curve-at-a-time evaluation.
- Interpolation across coordinates, such as estimating U=0.15 from U=0.1
  and U=0.2.
- Carrying error columns through operations.
- Unit arithmetic beyond `+` and `-`.
- Filters on value columns, such as `dmft.E[E<0]`.
- Complex numbers.
