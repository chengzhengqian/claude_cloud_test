# glue 0.2: what changed

Version 0.2 rebuilds glue on a small core calculus, defined in
[core.md](core.md). The shell language from 0.1 still works, with a few
changes listed below. Saved datasets now store a calculation tree instead of
expression text.

This document says what changed, why, and how to move 0.1 files over.

## 1. Why

In 0.1, a saved recipe was expression text plus settings. That had a few
problems:

- The meaning of a recipe depended on the version of the compiler that read
  it. A change to how `-` aligns curves would silently change old results.
- Two tables with the same name in different projects could be mixed up,
  and two names for the same table were treated as different data.
- Nothing below a saved dataset was cached. After one file changed, every
  plot recomputed everything from the files.
- Only a fixed set of value kinds existed (Scalar, Keyed, Curves, Table), so
  operations like "swap T and E" or "average over n" had no place to go.

0.2 fixes these with one idea: every value is a **field** `[inputs → outputs]`,
where the inputs determine the outputs. `dmft.E` is `[U, J, n, T → E]`.
`argmax(C)` is `[U, J, n → C]`. A small set of elementary operations maps
fields to fields, and every calculation is a tree of them. Trees are saved
node by node with content-hashed ids, so the same calculation always gets
the same ids, whatever it's called and wherever its files are.

## 2. What's new

### 2.1 In expressions

| Form | Meaning |
|---|---|
| `transform(C, T, t = T / gap.gap)` | Replace input T with t, computed by a formula. The formula can use other fields, like `gap.gap`, which then has one value per curve. |
| `rename(ed.E, u=U)` | Rename inputs or outputs. |
| `swap(dmft.E, T)` | Swap an input and an output, so T becomes a function of E. Each curve must be monotonic. `branches=split` splits it into monotonic pieces instead of stopping. |
| `legendre(F, T, slope=p, result=G)` | Legendre transform along an input. |
| `mean(y, n)`, `max(y, n)`, ... | A reducer with a second argument reduces along that input instead of the axis. Points are grouped by exactly equal values of the other inputs, so put the curves on one grid first, as in `mean(resample(y, grid=linspace(...)), n)`. |
| `resample(y, along=n, grid=...)` | Resample along any input, not only the axis. |
| `y[E < 0]` | Filters on outputs. 0.1 only allowed filters on coordinates and x. |
| `T * a.U` | A bare input name next to a field takes its values from that field. `T * 2` alone is an error, since T has no field to come from. |

### 2.2 Settings

| Key | Values | Default | Meaning |
|---|---|---|---|
| `align` | `auto`, `[]`, `[T]` | `auto` | Which input to interpolate along when combining fields. `auto` aligns a shared ragged input whose points differ. `[]` never aligns, so fields are joined on exactly equal values. |
| `branches` | `error`, `split` | `error` | What `swap` and `legendre` do with a curve that isn't monotonic. |
| `flat_tol` | number | `1e-9` | Steps smaller than this count as flat in `swap`. |
| `disk_cache` | `true`, `false` | `false` | Keep expensive intermediate values in `.glue/cache/` between sessions. |

### 2.3 Commands

| Command | Meaning |
|---|---|
| `invalidate TABLE [where ...]` | Mark a table's files as changed, for changes that file times don't show. With `where`, only the matching files. Recorded in `.glue/epochs.toml`. |
| `invalidate NAME` | For a view or calc, drop its cached values. For a dataset, mark it for a full recompute. |
| `why NAME` | The calculation tree under NAME, its state, and the leaves it reads, with changed files. |
| `gc` | Delete cached values that no view, calc, or dataset uses. |
| `save NAME --recipe-only` | Write the tree to `calcs/NAME.toml` with no data, and add it to `[calcs]`. |
| `save NAME --view` | Write the variable's expression text to `[views]`. This is what `--recipe-only` did in 0.1. |
| `load FILE as NAME` | Load a table, dataset, or calc under another name. For a project, NAME is a prefix: `load old/project.toml as old` gives `old_dmft`. |
| `explain` | Now prints the core tree, the filters pushed to each source, and how many files each source reads. |

### 2.4 Table files

A `[field]` section gives a table's type directly:

```toml
[field]
inputs = ["U", "J", "n", "T"]
outputs = ["E"]
axis = "T"
exact = []                        # content inputs to treat as exact
```

It can also fix a source's conventions once, so every expression sees the
same names and units:

```toml
[field.transform]
U = { from = "u", formula = "u * 2.0" }

[field.map]
E = "E_D * 2.0"
```

A transform that only uses path values is applied when the file index is
built, so `where U=2.0` still skips files without opening them.

`[curves]` is still read, as an alias: `by` and `x` become inputs, `x`
becomes the axis, and `y` becomes the outputs.

### 2.5 Projects

A new `[calcs]` section maps names to calc files. A calc is a frozen view:
a saved tree that doesn't follow the current settings.

### 2.6 Dataset files

Dataset files have version `"0.2"`, a `[type]` section, and a tree in
`[recipe.nodes]`:

```toml
[type]
inputs = ["U", "J", "n"]
outputs = ["Tpeak"]
exact = ["U", "J", "n"]

[recipe]
lib = "glue-core 1"
surface = "argmax(C)"
root = "8939286b0804"

[recipe.inputs]
dmft = "../tables/dmft.toml"

[recipe.nodes.fafeac3f54c2]
op = "source"
def = "8bb4786c839b"
...
```

`[recipe.settings]` and `[recipe].expr` are gone. Every parameter is
written in the node that uses it, so the tree means the same thing in any
later version of glue. `[cache]` gains `points`, the number of rows.

### 2.7 Python

- `Result.type` gives the field type, and `Result.tree()` prints the tree.
- `Session.invalidate`, `Session.why`, and `Session.gc` match the commands.
- `Session.load(path, name=...)` matches `load FILE as NAME`.
- Plugins are recorded as `module:qualname@version`, as in
  `physops:fwhm@1`. The colon allows functions defined inside other
  functions. The 0.1 form `module.function@version` is still read.

## 3. What behaves differently

Most 0.1 scripts run unchanged. These are the differences you might notice:

- **Which inputs get interpolated.** Every input except the table's axis is
  exact, so it's matched by value. Only the axis (usually x) is ragged and
  gets aligned. In 0.1, content coordinates from SQLite were also matched
  by value, so results don't change. The new part is that the rule is
  written down, and `with align=[...]` can override it.
- **Result names.** An unnamed result is still called `value`. A table with
  one output can now be used bare: `a - b.E` works when `a` has one output.
- **Error messages** say "no input J" instead of "no coordinate J", since
  coordinates and x are both inputs now.
- **`show`** evaluates the whole statement, then prints the first rows. 0.1
  stopped reading early.
- **Reports** use the same words, but alignment reports list only the
  operands that were interpolated, and resamples inside an alignment don't
  print their own line.
- **Refresh messages** say "keys" for results with no axis, and "recomputed
  all N" when the whole dataset was recomputed.

## 4. Moving 0.1 files over

Nothing has to be done by hand.

- **Table and project files** with `glue = "0.1"` load as before.
  `[curves]` is read as `[field]`. Change the version to `"0.2"` when you
  first use a 0.2 feature, like `[field]` or `[calcs]`.
- **0.1 datasets** load, and their status works as before, since the
  fingerprints are computed the same way. On the first `refresh`, the 0.1
  expression is translated into a tree with its recorded settings, and the
  file is rewritten as 0.2. The numbers don't change.
- **`save NAME --recipe-only`** now writes a calc file. Use `--view` for the
  0.1 behavior.
- **Plugins** keep working. Datasets that recorded the old dotted form
  still find the function.

## 5. Code layout

| Path | What it holds |
|---|---|
| `glue/core/types.py` | `FType`, `Var`, `Out`: field types |
| `glue/core/formula.py` | Formulas for `map` and `transform`, and filter predicates |
| `glue/core/nodes.py` | The elementary operations and the standard library, one class each |
| `glue/core/context.py` | Evaluation: fingerprints, the value store, filter pushdown, and `explain` plans |
| `glue/core/tree.py` | Writing and reading trees in TOML, and printing them |
| `glue/core/store.py` | The value store, epochs, and fingerprint helpers |
| `glue/elaborate.py` | Translating surface expressions into core trees |
| `glue/dataset.py` | Dataset and calc files, status, incremental refresh |
| `glue/session.py`, `glue/shell.py` | The session and the shell, on top of the core |

`glue/engine.py` and `glue/compiler.py` from 0.1 are removed.

## 6. Not done yet

- A cached intermediate value is recomputed in full when one of its files
  changes. Only saved datasets are refreshed curve by curve.
- The disk cache has no size limit.
- `status --all` for intermediate values.
- A `transform(...)` in an expression doesn't skip files. A filter on its
  new input is applied after reading. A transform in the table's
  `[field.transform]` does skip files.
- The `nan_rows` setting is accepted, but rows with missing values are
  always dropped.
- Error columns can be plotted with `errorbars`, but they aren't carried
  through arithmetic.
- `legendre_hull`, the convex-envelope version of `legendre`.
