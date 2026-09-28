# glue: specification, version 0.2

`glue` is a working name.

This document specifies the files, the shell language, and the tools. The
exact meaning of every operation is defined in [core.md](core.md): each
expression is translated into a tree of core operations, and that tree is
what a dataset saves. [changes-0.2.md](changes-0.2.md) lists what changed
from 0.1.

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

### 2.1 Fields

Every value in glue is a **field**, written `[inputs → outputs]`. The
inputs determine the outputs: two points with the same input values have
the same output values. `dmft.E` is `[U, J, n, T → E]`, one E for each
(U, J, n, T). [core.md](core.md) section 1 defines fields exactly.

A table is a field whose points are read from files. Its columns play one
of these parts:

| Part | Meaning | Example |
|---|---|---|
| input | Identifies a point. Path placeholders are always inputs. | U, J, n, T |
| axis | One input, the default for operations along an input, like `d` and `max`. | T |
| output | A measured or computed quantity. | E |
| error | The uncertainty of one output. | dE for E |

These were called coordinates, x, values, and errors in 0.1. The words
still fit: a **curve** is the set of points with the same values for every
input except the axis, and the tuple of those values is the **curve key**.

Column types are `float`, `int`, and `str`. The axis must be `float` or
`int`.

### 2.2 Exact and ragged inputs

Each input is **exact** or **ragged**:

- An exact input takes values from a fixed set, shared across curves, like
  U = 1.0, 2.0, 3.0. Two fields are matched on it by value.
- A ragged input takes different values in different curves, like T when
  every file has its own temperatures. To combine two fields along it, glue
  interpolates both onto a common grid first (section 7.5).

In a table, every input except the axis is exact. The axis is ragged,
unless the table lists it in `[field].exact` because every file uses the
same points. Path and constant inputs are always exact. Operations keep or
change the property as defined in [core.md](core.md) section 5.3. `resample`
onto a fixed grid, for example, makes the axis exact.

### 2.3 Shapes

Within a curve, points have no identity beyond their input values. The
axis values can repeat in raw data (section 7.6, `duplicates`), and
different curves can have different numbers of points.

Some common shapes:

| Shape | Example | Type |
|---|---|---|
| curve family | `dmft.E` | `[U, J, n, T → E]` |
| keyed | `gap.gap`, `max(dmft.E)` | `[U, J, n → gap]` |
| scalar | `2.5` | `[ → value]` |
| several outputs | `dmft` (a bare table) | `[U, J, n, T → E, S]` |

A **keyed table** is a table with no axis: exactly one point per key, like
one gap value per (U, J, n). A field with several outputs can be selected,
shown, and plotted. In arithmetic, pick one output first, as in `dmft.E`.
A field with one output can be used bare.

### 2.4 Input normalization

Float inputs are rounded to a fixed number of decimal places when they are
loaded. The default is 10, and it can be changed per column with
`digits`. All matching, grouping, and selection uses the rounded values. So
`0.1`, `0.10`, and `0.1000000000001` from different sources all refer to the
same curve.

The axis is rounded too, and so are the points of every grid glue makes.
So two points made by the same grid always match exactly. Outputs are not
rounded.

### 2.5 Chunks

A **chunk** is the smallest unit a source can read. For file sources, one
chunk is one file. Each chunk has the input values found from its locator,
such as input values parsed from its path. The list of chunks is the
**chunk index**. It is built by scanning, which never opens a data file.

---

## 3. File kinds

All interface files are TOML. There are four kinds:

| `kind` | Purpose |
|---|---|
| `table` | Describes raw data from one source. |
| `project` | Collects tables, views, calcs, datasets, and settings. |
| `calc` | A saved calculation tree with no data. |
| `dataset` | A saved result: calculation tree, cached data, and input fingerprints. |

Every file starts with:

```toml
glue = "0.2"      # spec version
kind = "table"    # or "project", "calc", or "dataset"
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
- **Names** of tables, views, calcs, datasets, and columns must match
  `[A-Za-z_][A-Za-z0-9_]*` and must not be a reserved word (section 7.9).

---

## 4. Table files

### 4.1 Structure

```toml
glue = "0.2"
kind = "table"
name = "dmft"                   # optional, default is the file name without .toml
description = "DMFT energy vs temperature"

[source]
locator = "glob"
root = "data/dmft"
pattern = "U_{U}/J_{J}/n_{n}.dat"
constants = { method = "dmft" } # optional extra inputs with fixed values

[source.reader]
format = "text"
columns = ["T", "E"]

[field]
inputs = ["U", "J", "n", "method", "T"]
outputs = ["E"]                 # optional, default is every other column
axis = "T"

[columns.T]
unit = "K"
label = "temperature"

[columns.E]
unit = "t"
label = "energy"
```

### 4.2 `[source]`: locators

The locator lists chunks and finds their input values.

#### `glob`

| Key | Type | Required | Meaning |
|---|---|---|---|
| `root` | path | no | Directory the pattern starts from. Default is the file's directory. |
| `pattern` | string | yes | Path template, see below. |
| `constants` | table | no | Inputs with the same value for every chunk. |

A **path template** is a relative path with placeholders:

- `{name}` or `{name:type}` matches one input value. The type is
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

A SQLite source is one chunk. Its inputs are content columns listed in
`[field].inputs`. Filters on inputs are sent to SQLite as a `WHERE` clause
with bound parameters, wrapped around the table or query.

```toml
[source]
locator = "sqlite"
file = "results.db"
query = "SELECT u AS U, j AS J, n, temp AS T, energy AS E FROM sweep"

[field]
inputs = ["U", "J", "n", "T"]
axis = "T"
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

Columns are read by name. Keys: `rename`. Inputs may come from the path,
from content columns, or both. Filters are passed to `pyarrow`.

#### `dataset` (for the `hdf5` locator)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dataset` | string | none | Dataset name inside a matched group. Omit if the pattern matches datasets. |
| `columns` | list | required for plain 2D datasets | Column names. |
| `fields` | table | none | For compound datasets: map from field names to `glue` names. |
| `transpose` | bool | false | As for `text`. |

### 4.4 `[field]`

The table's type.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `inputs` | list | required | Inputs, in order. Must list every path placeholder and constant. |
| `outputs` | list | every other column | Outputs. An output's error column comes with it. |
| `axis` | name | see below | The default input for operations along an input. Omit for a keyed table. |
| `exact` | list | `[]` | Content inputs to treat as exact (section 2.2). Only the axis can be ragged, so this only matters for the axis. |

If `axis` is omitted and exactly one input is a content column, that input
is the axis.

Only path and constant inputs can be used to skip chunks without reading
them (section 8.2).

**Keyed tables.** A table with no axis must have exactly one row per key. A
key with more than one row is an error when that chunk is read. This is how
summary files work, for example one gap value per (U, J, n):

```toml
[source]
locator = "glob"
root = "data/gap"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]
format = "text"
columns = ["gap"]

[field]
inputs = ["U", "J", "n"]
```

**Conventions.** A table can rescale or recompute inputs and outputs right
after reading, so every expression sees the same names and units:

```toml
[field.transform]
U = { from = "u", formula = "u * 2.0" }   # the pattern says u_{u}/...

[field.map]
E = "E_D * 2.0"                           # the file has a column E_D
```

`[field.transform]` replaces input `from` with a new input computed by
`formula`. When the formula only uses path and constant inputs and numbers,
it's applied while the chunk index is built, so `where U=2.0` still skips
files without opening them. `[field.map]` adds outputs computed from
columns. Columns that a map uses are not outputs themselves. Both are part
of the table's definition, and so of its identity ([core.md](core.md)
section 6.5).

**`[curves]`** is the 0.1 form of this section, and is still read: `by`
and the path placeholders become the inputs, `x` becomes the axis and the
last input, and `y` becomes the outputs. A file can't have both.

### 4.5 `[columns.NAME]`

Optional metadata for any column.

| Key | Type | Applies to | Meaning |
|---|---|---|---|
| `type` | `float`, `int`, `str` | all | Column type. Default `float`. |
| `unit` | string | all | Unit, used in plot labels. |
| `label` | string | all | Display name, used in plot labels. |
| `description` | string | all | Free text. |
| `digits` | int | float inputs | Decimal places for normalization. Default 10. |
| `error` | name | outputs | Name of the column holding this column's error. |

Declaring `error = "dE"` on column `E` makes `dE` the error of `E`.

---

## 5. Project files

A project collects tables, views, calcs, and datasets under one namespace,
with shared settings.

```toml
glue = "0.2"
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

[calcs]
dE_frozen = "calcs/dE_frozen.toml"

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
| `[[include]]` | Another project whose tables, views, calcs, and datasets are added with a name prefix. Its settings are not used. |
| `[views]` | Named expressions that are not saved as data. Either a string, or a table with `expr` and optional `description`, `unit`, `label`, and settings keys. `save NAME --view` adds entries here. |
| `[calcs]` | Map from name to calc file. `save NAME --recipe-only` adds entries here. |
| `[datasets]` | Map from name to dataset file. `save` adds entries here. |
| `[settings]` | Project defaults, see section 9. |
| `[plot]` | Plot defaults, see section 9. |
| `[plugins]` | Python modules that register custom operations, see section 11. |
| `[meta]` | Free-form notes. |

### 5.2 Names

Tables, views, calcs, and datasets share one namespace. A duplicate name is
an error when the project loads.

Names are only labels. What a table reads decides its identity, so two
projects that name the same files differently still share cached values,
and two tables called `dmft` in different projects are never mixed up
([core.md](core.md) section 6.5).

A session variable may have the same name as a view or dataset. It hides
that view or dataset for the rest of the session, and the shell prints a
note. This allows a script that saves a dataset to run again. A variable
can't have the name of a table.

### 5.3 Views and settings

A view is evaluated with the current settings, unless the view sets its own.
So changing `set method cubic` in the shell changes the result of views that
don't set `method`. Calcs and datasets are different: their settings are
written into their tree when they are saved (section 6).

---

## 6. Dataset and calc files

A dataset is a saved result. It has two files: the TOML file, and a cache
file with the data. The TOML file holds the calculation as a tree of core
operations. [core.md](core.md) section 6 defines the tree format and the
rules for node ids.

```toml
glue = "0.2"
kind = "dataset"
name = "Tpeak"
created = 2026-09-28T00:17:19Z
tool = "glue 0.2.0"
pinned = false
description = ""

[type]
inputs = ["U", "J", "n"]
outputs = ["Tpeak"]
exact = ["U", "J", "n"]

[recipe]
lib = "glue-core 1"
surface = "argmax(C)"              # what was typed, for people
root = "8939286b0804"

[recipe.inputs]
dmft = "../tables/dmft.toml"

[recipe.nodes.fafeac3f54c2]
op = "source"
def = "8bb4786c839b"               # what the table reads: its leaf id
duplicates = "mean"
digits = { U = 10, J = 10, n = 10, T = 10 }
table = "dmft"                     # which [recipe.inputs] entry to open
name = "dmft"

[recipe.nodes.804fda7cc31d]
op = "select"
of = "fafeac3f54c2"
outputs = ["E"]
name = "dmft.E"

# ... deriv, rename, map, reduce, rename ...

[recipe.nodes.8939286b0804]
op = "rename"
of = "b3f782a865e7"
mapping = { value = "Tpeak" }
name = "Tpeak"

[cache]
file = "Tpeak.parquet"
format = "parquet"
sha256 = "ebe2bef5..."
points = 48
curves = 48
dropped = 0

[fingerprint]
mode = "hash"
dmft = "sha256:4ec601f4fe72a8c6"
```

A **calc** file has the same `[type]` and `[recipe]`, with `kind = "calc"`
and no `[cache]` or `[fingerprint]`. It's a frozen view: a calculation that
no longer follows the current settings. `save NAME --recipe-only` writes
one.

### 6.1 `[type]` and `[recipe]`

- `[type]` is the result's field type: `inputs`, `outputs`, `exact`, and
  `axis` if it has one, plus `dtypes` and `units` when they're not the
  defaults.
- `[recipe.nodes]` holds every node of the tree, leaves first, keyed by
  node id. Every parameter is written, including the settings that were in
  effect, so the tree means the same thing in any later version of glue.
  `lib` names the version of the operation set.
- `root` is the node whose value the dataset holds.
- `[recipe.inputs]` maps each leaf's label to its file: a table file, or
  another dataset. A dataset that uses another dataset depends on it, and
  `refresh` uses these links (section 6.4). The leaf node records the
  table's definition id (`def`), so a table file that now reads something
  else is detected.
- Plugins are recorded in their `apply` node as `fn =
  "module:qualname@version"`.
- `surface` is the expression as typed. It's for people. Loading uses the
  tree.
- A dataset created from Python data has no `[recipe]` table, and has
  `reproducible = false` at the top level. It can be loaded, but not
  checked or recomputed.

A 0.1 dataset has `[recipe].expr` and `[recipe.settings]` instead of
nodes. It loads as before. `refresh` translates its expression with its
recorded settings, and writes the file back as 0.2.

### 6.2 `[cache]`

| Key | Meaning |
|---|---|
| `file` | Cache file, next to the TOML file. |
| `format` | `parquet` (default) or `npz`. |
| `sha256` | Hash of the cache file, to detect edits. |
| `points`, `curves`, `dropped` | Counts, for display without reading the file. |

The per-chunk fingerprints are stored inside the cache file: in Parquet
key-value metadata under `glue.chunks`, or in an `npz` array named
`__glue_chunks__`. The TOML file stays short even with thousands of inputs.

A saved dataset is read like any other table. Its source is its cache
file. When another calculation uses it, it's a `dataset` leaf whose id is
its root node's id, so a calculation built on the dataset and one that
writes the same steps out get the same ids ([core.md](core.md) section
6.5).

### 6.3 `[fingerprint]`

Each input gets one combined digest: SHA-256 over its sorted list of chunk
entries. A chunk entry is its relative path plus:

- `mode = "stat"` (default): size and modification time in nanoseconds.
- `mode = "hash"`: size and SHA-256 of the content. Slower, but it survives
  copying files and git checkouts, which change modification times.

A chunk marked by `invalidate` also carries its epoch (section 7.8), so it
counts as changed.

For inputs that are themselves datasets, the digest is the input dataset's
`[cache].sha256`. A dataset input is tracked as a whole: when it changes,
every point that uses it is recomputed.

### 6.4 Status and refresh

When a dataset is loaded or `status` runs, it gets one state:

| State | Condition | Behavior |
|---|---|---|
| `fresh` | Inputs match their fingerprints. | Cache is used. |
| `stale` | Some input chunks changed, were added, or were removed, a table's definition changed, or an input dataset is stale. | Cache is used, with a warning listing the changes. |
| `orphaned` | Some inputs cannot be found. | Cache is used. Recompute is not possible. |
| `modified` | The cache file's hash doesn't match. | Warning. The data is not trusted until refreshed. |
| `invalidated` | `invalidate NAME` was used on the dataset. | Recomputed in full by the next `refresh`. |
| `pinned` | `pinned = true`. | Status is still shown, but `refresh` skips it unless given `--force`. |

`refresh NAME`:

1. Refreshes stale datasets that `NAME` depends on, in dependency order.
2. Loads the tree. If a table's definition changed, the tree is rebuilt
   with the new definition, and the node ids that changed are reported.
3. Recomputes only the curves whose input chunks changed. For each changed
   chunk, its input values (like U=2.0, J=0.1, n=1.0) are traced from the
   leaf up to the root's inputs. The root is evaluated with those filters,
   and the new rows replace the old ones for those keys. Added chunks add
   curves, and removed chunks remove them. When the changed values can't be
   traced to the root, such as after a `mean` over n, everything is
   recomputed.
4. Writes a new cache file and updates `[cache]`, `[fingerprint]`, and
   `created`.

`refresh --all` does this for every stale dataset in the project.
`refresh NAME --full` recomputes everything.

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
- Strings: `"..."` or `'...'`, used for `str` inputs, labels, and file
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
3. Tables, calcs, and datasets.
4. Input names of the other operands in the same expression.

If a name matches both a table (or view, calc, dataset, variable) and an
input in the same expression, it's an error: "`n` is both a table and an
input of dmft. Use `dmft.n`, or rename the table."

`t.c` looks up column `c` of `t`:

- an output gives the field with only that output, like `dmft.E : [U, J, n, T → E]`,
- an input gives that input's values as an output named `value`, on the
  same points, like `dmft.U : [U, J, n, T → value]`.

A bare input name, like `T` in `C / T`, takes its values from the field it
meets. `T * 2` alone is an error, since there's no field for T to come from.

Anything with exactly one output can be used without naming it.
`max(dE)` is the same as `max(dE.dE)`.

### 7.4 Selection

`y[selectors]` keeps part of `y`. Several selectors are combined with AND.

| Selector | Meaning |
|---|---|
| `U=0.1` | Equal, after normalization (section 2.4). |
| `U=[0.1, 0.2]` | Any of these values. |
| `U!=0.1`, `U<0.3`, `U>=0.1` | Comparison. |
| `T=0.1:0.5` | Range, both ends included. |
| `T=:0.5`, `T=0.1:` | Open range. |
| `model="hubbard"` | String input. |
| `E<0` | A condition on an output. |

Selectors may name inputs and outputs. A selector on a name the operand
doesn't have is an error.

Selection applies where it is written. `integral(y[T=0.1:0.5], T)`
integrates only over that range. glue may read less data than this
suggests (section 8), but it never changes the result.

A `where` clause on a statement applies to the result. It may only name
the result's inputs. It is passed down to every operand that has the named
input. Operands without that input are broadcast and are not affected.

### 7.5 Combining operands

This is the core of the language. These rules apply to `+ - * / ^` and to
every operation with more than one operand. [core.md](core.md) section 5.3
gives the exact translation.

1. **Inputs are matched by name.** The result has the union of the
   operands' inputs. An operand without some input is broadcast along it:
   `dmft.E - gap.gap` uses each run's gap as a constant along its curve,
   and a number is used for every point.
2. **Exact inputs are joined by value.** Points are matched where every
   shared exact input has the same value. Keys that only one side has are
   handled by the `unmatched` setting. With `drop`, they are left out and
   listed in the report. With `error`, the statement fails.
3. **A shared ragged input is aligned.** When a shared input is ragged in
   at least one operand, and the operands don't share their points, each
   operand is interpolated onto a grid from the `grid` setting, with the
   `method`, `extrapolate`, `duplicates`, and `fewpoints` settings. Then
   they're joined as in rule 2. Only one input can be aligned per
   combination. If more than one needs it, the statement fails and asks
   for `with align=[...]`.
4. **Operands that share their points are not aligned.** Two operands
   share their points when they come from the same source with the same
   selection, through steps that keep points, like arithmetic and
   `select`. They're combined point by point, and may contain repeated x
   values. `dE - dE` gives exactly zero.
5. **The `align` setting overrides rule 3.** `with align=[T]` aligns along
   T. `with align=[]` never aligns, so fields are joined on exactly equal
   values. That's useful when every file uses the same temperatures.
6. With `strict = true`, alignment is an error unless the grid comes from a
   `with grid=...` clause or `using(...)`, or the operands are wrapped in
   `resample`.

**Units.** `+` and `-` keep the unit when both sides have the same unit.
Other operations drop the unit. It can be set again with `unit=` on `save`,
or in a view.

**Error columns** can be plotted with `errorbars` (section 7.8). They are
not carried through operations in version 0.2.

**Result name.** An assignment names its result: in `dE = dmft.E - ed.E`,
the output is called `dE`. Unnamed results are called `value`.

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
means that curve's own range. `overlap` and `union` take any number of
operands.

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
are removed. Then repeated x values are
handled by the `duplicates` setting, and curves with too few points by the
`fewpoints` setting. Every curve these steps change is counted in the
report.

### 7.7 Functions

`along` is an input name. Where it's optional, the default is the axis.
[core.md](core.md) sections 2 and 3 define each one exactly.

| Function | Type | Meaning |
|---|---|---|
| `abs sqrt exp log log10 sin cos tan sinh cosh tanh` | `[I → O]` to `[I → O]` | Pointwise. |
| `resample(y, grid=, method=, along=)` | `[I → O]` to `[I → O]` | Evaluate each curve on a new grid. |
| `d(y, along, order=1, method=, grid=)` | `[I → O]` to `[I → O]` | Derivative. Default is at the input points, after sorting and the `duplicates` setting. `method=fd` uses finite differences (`numpy.gradient`) with no fitting. |
| `int(y, along, method="trapz")` | `[I → O]` to `[I → O]` | Cumulative integral from the first point. `method` is `trapz`, or a spline method to integrate the fitted spline. |
| `integral(y, along, method="trapz")` | `[K ∪ {along} → O]` to `[K → O]` | Integral over the whole curve. |
| `max min mean sum first last count` | `[K ∪ {along} → O]` to `[K → O]` | Reduce each curve to one number. `mean(y, n)` averages over n. Points are grouped by exactly equal values of the other inputs, so resample onto one grid first when they're ragged. |
| `argmax argmin` | `[K ∪ {along} → O]` to `[K → O]` | The `along` value at the largest or smallest point. |
| `at(y, T=v)`, `y @ T=v` | `[K ∪ {T} → O]` to `[K → O]` | Each curve evaluated at `v` with the current method. On an exact input, it selects that value. |
| `at(y, T=[v1, v2])` | `[I → O]` to `[I → O]` | Each curve evaluated at these points. |
| `stack(a=y1, b=y2, tag="source")` | to `[I ∪ {tag} → O]` | Stack fields with the same inputs. Adds a `str` input `tag` whose values are the argument names. No resampling. |
| `using(y, KEY=VALUE, ...)` | same as `y` | Evaluate `y` with these settings. |
| `rename(y, old=new, ...)` | same, renamed | Rename inputs or outputs. |
| `transform(y, u, v = formula)` | `[I → O]` to `[I - {u} ∪ {v} → O]` | Replace input u with v. The formula can use u, other inputs, outputs, and other fields, as in `transform(C, T, t = T / gap.gap)`. v is exact only if the formula uses only exact inputs and numbers. |
| `swap(y, along, branches=, flat_tol=)` | `[K ∪ {x} → y]` to `[K ∪ {y} → x]` | Swap an input and the only output. Each curve must be strictly monotonic. `branches=split` splits a curve into monotonic pieces, with a new `branch` input. |
| `legendre(y, along, slope=p, result=G)` | `[K ∪ {x} → y]` to `[K ∪ {p} → G]` | Legendre transform: `p = dy/dx`, `G = p x - y`, as a function of p. Needs y convex or concave. |

Custom functions registered by plugins are called the same way (section 11).

### 7.8 Commands

Commands start with a reserved word. Arguments in `[ ]` are optional.

#### Loading and describing data

| Command | Meaning |
|---|---|
| `load FILE [as NAME]` | Load a project, table, calc, or dataset file. Loading a second project merges its names. Duplicates are an error. `as NAME` gives a single file another name. For a project, NAME is a prefix: `load old/project.toml as old` gives `old_dmft`. |
| `reload` | Reload all loaded files from disk. |
| `guess DIR` | Look at a directory tree and suggest a table definition: a path template from `name_number` segments, and the column count from the first file. Prints TOML and does not write it. |
| `new table NAME pattern=... columns=... [root=...] [x=...] [format=text] [file=...]` | Write a table file and add it to the current project. Options are separated by spaces or commas. |
| `edit NAME` | Open the file behind `NAME` in `$EDITOR`, then reload it. |
| `scan [NAME]` | Rebuild the chunk index. Reports matched files, skipped files, and input values found. |

#### Looking at data

| Command | Meaning |
|---|---|
| `ls` | List tables, views, calcs, datasets, and variables, with their kind and status. |
| `info NAME` | Type, inputs with their value counts, number of curves and chunks. Uses the chunk index only. |
| `values NAME.INPUT` | Distinct values of an input. Uses the chunk index when possible. |
| `show EXPR [where ...] [limit N]` | Evaluate and print rows. Default `limit 20`. |
| `explain STATEMENT` | Print the core tree with each node's type, the filters pushed to the sources, and how many chunks each source would read. Works on expressions, assignments, `plot ...`, and `save NAME`. Nothing is read, except that sources with content inputs, such as SQLite, need a query to list their keys. |

#### Computing and saving

| Command | Meaning |
|---|---|
| `NAME = EXPR [with ...]` | Define a session variable. Nothing is computed yet. |
| `del NAME` | Remove a session variable. |
| `save NAME [as PATH] [where ...] [grid=...] [format=parquet\|npz] [unit="..."]` | Compute and save `NAME` as a dataset, and add it to `[datasets]`. Default path is `DATASETS_DIR/NAME`. The dataset's name is the file name of `PATH`. If the result has one output, that output gets the same name. If the dataset's name equals a view or variable, the dataset replaces it. Nothing is lost, since the recipe keeps the calculation. |
| `save NAME --recipe-only` | Write the calculation tree to `calcs/NAME.toml` with no data, and add it to `[calcs]`. |
| `save NAME --view` | Write a variable's expression text to `[views]` in the project file. |
| `export EXPR to FILE [where ...]` | Write plain data with no recipe. The format comes from the extension: `.csv`, `.parquet`, `.dat`. `.dat` writes one block per curve separated by blank lines, with the curve key in a comment, which gnuplot reads as `index` blocks. |
| `status [NAME] [--all]` | Dataset states (section 6.4). `--all` also lists the cached values under each view, calc, and dataset, and which are stale. |
| `refresh NAME \| --all [--full] [--force]` | Recompute stale datasets. |
| `pin NAME`, `unpin NAME` | Set or clear `pinned`. |
| `invalidate TABLE [where ...]` | Mark a table's files as changed, for changes that size and modification time don't show. With `where`, only the matching files. Values that depend on them are recomputed when next used, and saved datasets become stale. Recorded in `.glue/epochs.toml`. |
| `invalidate NAME` | For a view or calc, drop its cached values. For a dataset, mark it `invalidated`, so `refresh` recomputes it in full. |
| `why NAME` | Why a dataset is fresh or stale: its state, what changed, the tree under it, and each leaf with its file count and fingerprint. For a view or calc, the tree and leaves. |
| `gc` | Delete cached values that no view, calc, or dataset of the project uses. |

#### Plotting

```
plot Y { "," Y } [ vs X ] [ by C1 [ "," C2 ] ] [ where ... ] [ with ... ] [ > FILE ]
plot + Y ...            # add to the current figure
```

- `Y` is any field with one output. Several are drawn on the same
  axes and told apart by line style. `label=expr` sets a legend label, as
  in `plot dmft=dmft.E, ed=ed.E vs T`. The default label is the expression
  text.
- `vs X` is the input on the horizontal axis. The default is the field's
  axis. For a field with no axis, it's any input, as in
  `plot max(dmft.E) vs U by J where n=0.5`. When X is exact, points are
  drawn with markers and joined in order of X.
- `by C1` colors curves by `C1`. A numeric `C1` uses a colormap, with a
  legend for up to 6 values and a colorbar for more. A string `C1` uses
  separate colors and a legend. `by C1, C2` also uses line style for `C2`.
  Line style can only mean one thing, so `by C1, C2` is only allowed with a
  single `Y`. Without `by`, each `Y` gets its own color.
- **Every input must have a single value in the plotted data, be listed
  in `by`, or be used as `vs X`.** Otherwise curves for different values
  would overlap with the same color and style, so it's an error that names
  the free inputs. One exception: if exactly one input is free and there
  is no `by`, that input is used as `by`. For a field with no axis and no
  `vs`, a single free input is used as `vs`.
- The title defaults to the inputs with a single value, like
  `U=2.0, n=1.0`.
- Outputs with an error column, plotted `with errorbars`, are drawn as
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
calcs, datasets, variables, or columns:

```
load reload guess new edit scan ls info values show explain del save
export status refresh pin unpin invalidate why gc plot set unset run py
help quit with where vs by to as
```

Function names can be used as names, since a function call is always
followed by `(`.

### 7.10 Reports

Any statement that aligns, drops, or changes data prints a short report,
unless `report = off`:

```
> set duplicates mean
> dE = dmft.E - ed.E
> show dE limit 5
  curves with repeated T (mean): 1
  dropped 4 unmatched keys (only in dmft.E)
  aligned dmft.E, ed.E: 44 curves matched on (U, J, n), grid overlap(n=200), method pchip
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

Assignments and views build a core tree. Data is read only by `show`,
`plot`, `save`, `export`, `values` (when the index isn't enough), and the
Python API's `to_pandas()` and `to_numpy()`.

### 8.2 Pushdown

Before reading, glue works backward from the result:

- **Filters on inputs** pass through every operation that keeps that input
  point by point: pointwise operations, `select`, `rename`, joins, and
  every operation along a different input ([core.md](core.md) section 4,
  laws L1 and L2). At a source, they are checked against the chunk index,
  and chunks that don't match are never opened.
- **Inputs missing from an operand** are dropped at that operand, since it
  is broadcast.
- **Filters on the input an operation works along** stop there. `resample`,
  alignment, `d`, `int`, reductions, and `swap` need points outside the
  range, so the whole curve is read and the filter is applied after the
  operation.
- **Filters on a transformed input** skip files when the transform's
  formula uses only exact inputs and numbers. In a table's
  `[field.transform]`, the formula is applied while the index is built. For
  `transform(...)` in an expression, glue works out which values of the
  formula's inputs can pass the filter, from the file index, and pushes
  those down. The filter itself is still applied after the transform.
- **Filters on outputs** are applied where they are written.
- **Columns.** Only columns the result needs are read. For `text`, this is
  `usecols`.
- **Row filters inside a chunk** are sent to backends that support them:
  SQLite (`WHERE`), Parquet (`pyarrow` filters), and HDF5 (slicing, if x is
  sorted). For `text`, `csv`, `npy`, and `raw`, the smallest unit read is a
  whole chunk.

### 8.3 Evaluation order

The tree is evaluated from the leaves up. Each node gets the points of its
inputs that survive the filters pushed to it, and returns its own points.
Operations along an input work one curve at a time within a node. Memory
holds the values of the nodes that are still needed.

### 8.4 Caches

- **Chunk index.** Stored in `.glue/index/` in the project directory.
  Reused until `scan`, or until a directory's modification time changes.
- **Read cache.** In memory, keyed by (path, modification time, columns),
  with a size limit (`read_cache_mb`). The oldest entries are dropped
  first.
- **Value store.** Every node's value is kept in memory for the session,
  keyed by its node id and the fingerprints of the leaves under it. A
  value is only reused when both match, so a changed file or an
  `invalidate` is never missed. With `disk_cache = true`, the values of
  expensive nodes (resample, deriv, cumint, reduce, swap, apply, align,
  legendre) are also written to `.glue/cache/`, and reused by later
  sessions, up to `disk_cache_mb`. `gc` removes the ones nothing uses.
- **Updates.** When a file under a cached expensive value changes, the next
  use recomputes only the curves that file affects, and keeps the rest,
  the same way `refresh` updates a dataset. The report says so:
  `updated cached dmft.E - ed.E: recomputed 1 of 44 curves`. `status --all`
  lists the cached values under each name, and which are stale.
  [core.md](core.md) section 6.6 has the details.
- **Epochs.** `.glue/epochs.toml` records `invalidate` marks, one number
  per table and per marked file.

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
| `nan_rows` | `drop`, `error` | `drop` | Rows with a missing value. `drop` leaves them out of fits and reductions. `error` stops at the source that has them. It's a check, not part of the saved tree. |
| `align` | `auto`, `[]`, `[NAME]` | `auto` | Which input to align along when combining fields (section 7.5). |
| `branches` | `error`, `split` | `error` | What `swap` and `legendre` do with a curve that isn't monotonic. |
| `flat_tol` | number | `1e-9` | In `swap`, steps smaller than this count as flat. |
| `disk_cache` | `true`, `false` | `false` | Keep expensive intermediate values in `.glue/cache/` (section 8.4). |
| `disk_cache_mb` | int | `1024` | Size limit for `.glue/cache/`. The least recently used values are removed first. |
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

When a calc or dataset is saved, the settings that affect the result are
written into the parameters of the nodes that use them. Changing settings
later doesn't change a saved tree.

---

## 10. Command-line interface

```
glue [PROJECT]                 start the shell, optionally loading a project
glue run SCRIPT.glue           run a script and exit
glue status [PROJECT] [--all]  print dataset states
glue refresh NAME|--all [--full] [--force] [--project P]
                               recompute stale datasets
glue scan [PROJECT]            rebuild chunk indexes
```

If no project is given, `glue` looks for `project.toml` in the current
directory.

---

## 11. Python

### 11.1 API

The shell is a thin layer over the Python API. Both use the same core.

```python
import glue

p = glue.open("project.toml")
dE = p.eval("dmft.E - ed.E", where="n=0.5", method="pchip")
df = dE.to_pandas()                     # long format: U, J, n, T, value
dE.type                                 # [U, J, n, T:ragged → value]
print(dE.tree())                        # the core tree
p.plot("dE vs T by J where n=0.5")
p.why("dE_fine")                        # also invalidate(), gc(), refresh()

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
| `elementwise` or `pointwise` | `f(y) -> y` | `[I → O]` |
| `curve` | `f(x, y, **options) -> (x, y)` | `[I → O]` |
| `reduce` | `f(x, y, **options) -> float` | `[K → O]` |

`curve` and `reduce` work along the axis, or along `along=NAME`. They work
on one curve at a time, so laziness and pushdown still apply. After
registration, `kramers_kronig(dmft.E)` works in the shell, in views, and in
saved trees. A saved tree records it in an `apply` node as
`fn = "module:qualname@version"`.

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

The full version of this example, with data, is in `examples/hubbard/`.

### Files

`tables/dmft.toml`:

```toml
glue = "0.2"
kind = "table"

[source]
locator = "glob"
root = "../data/dmft"
pattern = "U_{U}/J_{J}/n_{n}.dat"

[source.reader]
format = "text"
columns = ["T", "E"]

[field]
inputs = ["U", "J", "n", "T"]
outputs = ["E"]
axis = "T"

[columns.T]
unit = "t"

[columns.E]
unit = "t"
```

`tables/ed.toml` is the same with `root = "../data/ed"`.

`project.toml`:

```toml
glue = "0.2"
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
  table dmft: data/dmft/U_{U}/J_{J}/n_{n}.dat
    type       [U, J, n, T:ragged → E]
    48 files
    48 curves
    input      U          4 values: 1.0, 2.0, 3.0, 4.0  (path)
    input      J          4 values: 0.0, 0.1, 0.2, 0.3  (path)
    input      n          3 values: 0.8, 0.9, 1.0  (path)
    axis       T [t]
    output     E [t]

> plot dE by J where n=1.0
  error: input U not fixed. Curves for different values would overlap.
  Fix U in where, or use: by J, U

> explain plot dE by U where J=0.1, n=1.0
  dE = rename(mapping={value: dE})  [U, J, n, T:ragged → dE]
    dmft.E - ed.E = map(value = a.E - b.E)  [U, J, n, T:ragged → value]
      dmft.E - ed.E = align(along T, grid overlap(n=200), method pchip)  [U, J, n, T:ragged → a.E, b.E]
        dmft.E = select(outputs=[E])  [U, J, n, T:ragged → E]
          source(dmft, duplicates=error)  [U, J, n, T:ragged → E]
        ed.E = select(outputs=[E])  [U, J, n, T:ragged → E]
          source(ed, duplicates=error)  [U, J, n, T:ragged → E]
        overlap(n=200) = overlap(along=T, count=200, step=0, unmatched=drop)  [U, J, n, T:ragged → ]
          ...
    filters pushed to the sources: J=0.1, n=1.0
    dmft: read 4 of 48 files
    ed: read 4 of 44 files
    result: [U, J, n, T:ragged → dE]

> plot dE by U where J=0.1, n=1.0
  aligned dmft.E, ed.E: 4 curves matched on (U, J, n), grid overlap(n=200), method pchip

> C = d(dmft.E, T)
> Tpeak = argmax(C)
> plot Tpeak vs U by J where n=1.0 > figs/peak.pdf

> save Tpeak
  wrote results/Tpeak.toml and results/Tpeak.parquet (48 curves, 48 rows)

> status
  Tpeak          fresh
```

Later, after one file in `data/dmft` is rerun:

```
> status
  Tpeak          stale      dmft: 1 changed (U_3.0/J_0.2/n_1.0.dat)
> why Tpeak
  dataset Tpeak: stale
    dmft: 1 changed (U_3.0/J_0.2/n_1.0.dat)
    Tpeak = rename(mapping={value: Tpeak})  [U, J, n → Tpeak]
      ...
    leaf dmft: 48 files, fingerprint sha256:...
> refresh Tpeak
  Tpeak: recomputed 1 of 48 keys, 47 unchanged
```

---

## 13. Not in version 0.2

Planned, and possible to add without changing the rules above:

- Interpolation across exact inputs, such as estimating U=0.15 from U=0.1
  and U=0.2. `resample(y, along=U, grid=...)` does this when asked.
- Carrying error columns through operations.
- Unit arithmetic beyond `+` and `-`.
- Complex numbers.
- `legendre_hull`, the convex-envelope version of `legendre`.
