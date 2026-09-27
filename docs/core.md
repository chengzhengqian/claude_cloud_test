# glue core: fields, elementary operations, calculation trees

Draft for version 0.2. This document defines the core calculus exactly. The
shell language in [spec.md](spec.md) becomes a convenient surface on top of
it: every surface expression is translated into a core tree, and the core
tree is what gets saved.

## 0. Layers

```
surface expression  ──elaborate──▶  core tree  ──save──▶  TOML
 "dmft.E - ed.E"      (uses settings)   (all parameters explicit)
```

- The **surface** language is short to type. It fills in defaults from
  settings: which grid, which method, which input to align along.
- The **core** has no defaults and no implicit steps. Every parameter is
  written out in the tree. Two trees with the same nodes compute the same
  result, whatever the settings are at the time.
- A saved calculation is a core tree. This replaces "expression text plus
  frozen settings" from 0.1.

`explain` shows the core tree for any surface expression.

---

## 1. Fields

### 1.1 Definition

A **field** has the type

```
F : [I → O]
```

- `I` is an ordered list of **inputs**. Each has a name and a value type
  (`float`, `int`, or `str`).
- `O` is a list of **outputs**. Each has a name and a unit. Output values
  are floats, and may be missing (NaN).

The value of a field is:

- a finite set of **points** P. Each point assigns a value to every input.
- for every point p in P, one value for each output.

**The one rule every field obeys: the inputs determine the outputs.** No two
points have the same input values. Every elementary operation keeps this
true, provided its checks pass. The two places where it can fail are
checked explicitly: reading a source (section 2.1) and swapping an input
with an output (`swap`, section 2.4).

All input and output names in one field are different, so a formula can
refer to any of them by name.

Special cases:

| Case | Type | Example |
|---|---|---|
| constant | `[ → c]`, one point, no inputs | `2.5` |
| per-run value | `[U, J, n → gap]` | the gap table |
| curves | `[U, J, n, T → E]` | a DMFT table |
| point set (grid) | `[U, J, n, T → ]`, no outputs | a grid to evaluate on |

### 1.2 Notation used below

- For a subset K of I, `p|K` is point p restricted to the inputs in K.
- `keys_K(F)` is the set `{ p|K : p in P }`.
- For an input x in I, with K = I − {x}, the **curve at key k** is the list
  of (x value, outputs) for all points p with `p|K = k`, sorted by x. The
  rule in 1.1 means the x values in a curve are all different.
- An operation **along x** acts on each curve separately, and never mixes
  curves.

### 1.3 Matching input values

Float inputs are compared after rounding to a fixed number of decimal
places (`digits`, default 10), applied whenever a float input value is
created: by a source, a grid, or a swap. So `0.1` from a file path and
`0.1000000000001` from SQLite are the same value. Two points created by the
same grid node are always exactly equal.

### 1.4 Missing values

- A source drops rows with a missing input value.
- A missing output value is allowed. Operations along x treat each output
  separately and ignore the points where that output is missing.
- Reductions ignore missing values. If every value of a curve is missing,
  the result is missing.

---

## 2. Elementary operations

Twenty operations, in five groups. Every parameter is required in a core
tree. Section 5 lists which ones the surface language fills in.

| Group | Operations |
|---|---|
| leaves | `source`, `dataset`, `const`, `axis` |
| structure | `select`, `rename`, `filter`, `slice`, `join`, `stack`, `points`, `union`, `span` |
| pointwise | `map` |
| along an input | `resample`, `deriv`, `cumint`, `reduce`, `swap` |
| extension | `apply` |

Shared parameters for operations that interpolate:

| Parameter | Values | Meaning |
|---|---|---|
| `method` | `linear`, `cubic`, `pchip`, `akima`, `smooth:S` | Interpolant fitted through each curve (spec 7.6). `smooth:S` is a smoothing spline with factor S. |
| `extrapolate` | `nan`, `error`, `extend`, `clamp` | Outside a curve's x range. |
| `fewpoints` | `drop`, `linear`, `error` | A curve with fewer points than `method` needs. `linear` falls back to linear with 2 or more points, and drops the curve otherwise. |

Dropped curves and unmatched points are never silent. Each run reports how
many there were, and which, at `report = full`.

### 2.1 Leaves

**`source(table, duplicates, digits)`** `: [I → O]`

Reads a table file. I and O come from its `[field]` section (section 6.2).

- `duplicates` ∈ `error`, `mean`, `first`, `last`, `drop`. Rows with the same
  input values break the rule in 1.1. This parameter says how to repair it,
  or to stop.
- `digits`: rounding for float inputs, per input (1.3).

**`dataset(file)`** `: [I → O]` as recorded in the dataset's `[type]`.

The cached value of a saved dataset. Its recipe isn't expanded. It's a leaf,
and the dependency between the two datasets is recorded.

**`const(value, unit)`** `: [ → value]`

One point with no inputs. The output is named `value`.

**`axis(input, values)`** `: [input → ]`

A one-input point set. `values` is one of:

- `list:[v1, v2, ...]`
- `lin:a:b:n`, n evenly spaced points from a to b
- `log:a:b:n`, n log-spaced points from a to b. a and b are the actual end
  values, not exponents.

### 2.2 Structure

**`select(F, outputs)`** `: [I → outputs]`

Keep only the listed outputs. An empty list gives F's point set.

**`rename(F, mapping)`** `: [I' → O']`

Rename inputs or outputs. The new names must not clash.

**`filter(F, predicate)`** `: [I → O]`

Keep the points where the predicate is true. A predicate is a list of
comparisons, all of which must hold. Each comparison is on one input or
output: `=`, `!=`, `<`, `<=`, `>`, `>=`, a list (`U=[0.1, 0.2]`), or a
range (`T=0.1:0.5`). Equality on float inputs uses 1.3.

**`slice(F, input, value)`** `: [I − input → O]`

Keep the points where `input = value`, then remove that input. The rule in
1.1 still holds, because the removed input had one value.

**`join(operands, unmatched)`** `: [I₁ ∪ I₂ ∪ ... → a.O₁ ∪ b.O₂ ∪ ...]`

`operands` is a map from a label to a field, like `{ a = F, b = G }`.

- Points are combined when they agree on every input the operands share.
  This is an exact natural join. Nothing is interpolated.
- An input that only some operands have is carried over. That's how a
  per-run value like `gap` spreads over every T of a curve.
- Outputs are renamed `label.name`, like `a.E` and `b.E`, so they can't
  clash.
- Operands with no shared inputs give every combination of points: a
  product.
- `unmatched` ∈ `drop`, `error`. It covers points of one operand that have
  no partner in another. `drop` leaves them out and reports how many.

The join is the only way two fields combine. Arithmetic between fields is a
join followed by a `map`.

**`stack(operands, tag)`** `: [I ∪ {tag: str} → O]`

`operands` is a map from a label to a field. All operands must have the
same inputs and the same outputs. The result holds all points, with a new
string input `tag` whose value is the label. No values change.

**`points(F)`** `: [I → ]`

F's point set. The same as `select(F, [])`.

**`union(G₁, G₂, ...)`** `: [I → ]`

The union of point sets that have the same inputs.

**`span(L, input, lo, hi, n, scale)`** `: [K ∪ {input} → ]`

Here `L : [K → lo, hi, ...]`, and `lo` and `hi` name two of its outputs.
For each point of L, it makes n points of `input` between that point's `lo`
and `hi` values. `scale` is `lin` or `log`. A key where `lo ≥ hi` or either
value is missing gives no points, and is reported.

This is how grids that depend on the data are built, such as "200 points
over the range both curves cover" (section 3).

### 2.3 Pointwise

**`map(F, outputs, keep, units)`** `: [I → O']`

Computes new outputs from formulas, point by point.

- `outputs` is a map from a new name to a formula, like
  `{ dE = "a.E - b.E" }`.
- A formula can use F's outputs, F's inputs, and numbers. The operators are
  `+ - * / ^` and unary `-`. The functions are `abs sqrt exp log log10 sin
  cos tan sinh cosh tanh`, and `min(a, b)` and `max(a, b)`, which compare
  two values.
- `keep = true` keeps F's other outputs, and `false` drops them.
- `units` is a map from an output name to its unit string.

Numeric errors, like `log` of a negative number, give a missing value.

### 2.4 Along an input

All of these take an input x of F. Write K = I − {x}. Each works on every
curve of F separately (1.2).

**`resample(F, along, grid, method, extrapolate, fewpoints)`** `: [I → O]`

- `grid : [J → ]` is a point set with x in J, and J − {x} a subset of K.
- For each key k of F, and each grid point g that agrees with k on
  J − {x}, the result has the point `k` with `x = g.x`. Its outputs are
  the curve's interpolant evaluated at `g.x`.
- If the grid lacks some inputs of K, it applies to every curve regardless
  of those inputs. If J has an input that F doesn't, that's an error.

**`deriv(F, along, order, method, fewpoints)`** `: [I → O]`

The same points as F. Each output is replaced by its `order`-th derivative
along x (order 1, 2, or 3), at the curve's own points.

- `method` is a spline method: the derivative of the fitted interpolant.
- `method = fd` means finite differences (`numpy.gradient`), with no
  fitting.
- Units are dropped unless given later by a `map`.

**`cumint(F, along, method)`** `: [I → O]`

The same points as F. Each output becomes its integral from the curve's
smallest x up to each point. `method = trapz` uses the trapezoid rule, and a
spline method integrates the fitted interpolant.

**`reduce(F, along, op, method)`** `: [K → O]`

Reduces each curve to one value per output, which removes the input x.

| `op` | Value per curve |
|---|---|
| `max`, `min` | the largest or smallest point value |
| `mean` | the average over the points. Not weighted by spacing in x. |
| `sum`, `count` | over the points |
| `first`, `last` | the value at the smallest or largest x |
| `argmax`, `argmin` | the x at the largest or smallest value, the smallest such x if there's a tie |
| `integral` | from the smallest to the largest x, with `method` (`trapz` or a spline) |

`method` is only used by `integral`.

**`swap(F, along, output, branches, flat_tol)`** `: [K ∪ {output} (∪ {branch: int}) → {along} ∪ (O − output)]`

Trades the input x with one output y. On each curve, the points (xᵢ, yᵢ)
become points with input y and output x. No values are interpolated. Other
outputs stay as outputs, at the same points.

1. On each curve, sorted by x, drop points where the curve is flat:
   points joined only by steps with `|Δy| ≤ flat_tol × (range of y on that
   curve)`. They're reported.
2. What's left must be strictly increasing or strictly decreasing in y, so
   y determines x again.
3. If it isn't, `branches = error` stops and names the curve.
   `branches = split` cuts the curve into its longest monotonic runs, and
   adds an input `branch` numbered 0, 1, 2, ... in order of x.

This is the one elementary operation that changes which quantities are
inputs. Inverses, changes of variables, and Legendre transforms are all
built from it.

### 2.5 Extension

**`apply(F, along, fn, kind, params)`**

Calls a Python function registered with `@glue.op`. `fn` is
`module.function@version`, and `kind` is:

| `kind` | Function | Type |
|---|---|---|
| `pointwise` | `f(outputs, **params) -> outputs` | `[I → O]` |
| `curve` | `f(x, y, **params) -> (x, y)`, once per curve and output | `[I → O]` |
| `reduce` | `f(x, y, **params) -> float`, once per curve and output | `[K → O]` |

`along` is required for `curve` and `reduce`. For `curve`, the returned x
values become the new points. They're rounded as in 1.3, and must be
distinct.

---

## 3. Standard library

These operations are defined by their expansion into elementary operations.
A core tree may contain them as nodes, because each one has exactly one
expansion, fixed by the library version (`lib = "glue-core 1"`). This keeps
saved trees short, and the meaning is still exact.

**`eval(F, along, value, method, extrapolate, fewpoints)`** `: [K → O]`

The value of each curve at one point.

```
slice(resample(F, along, axis(along, list:[value]), method, extrapolate, fewpoints), along, value)
```

**`range(F, along)`** `: [K → lo, hi]`

The x range of each curve.

```
X = map(points(F), { v = "<along>" }, false, {})     # the input as an output
rename(join({ lo = reduce(X, along, min, -),
              hi = reduce(X, along, max, -) }, drop),
       { lo.v = lo, hi.v = hi })
```

**`overlap(A, B, along, n)`** `: [K ∪ {along} → ]` point set

n points over the x range both A and B cover, per key.

```
R = map(join({ a = range(A, along), b = range(B, along) }, drop),
        { lo = "max(a.lo, b.lo)", hi = "min(a.hi, b.hi)" }, false, {})
span(R, along, lo, hi, n, lin)
```

**`merge_points(A, B, along)`** `: [K ∪ {along} → ]` point set

Every x point of A and B that lies inside the overlap. This is 0.1's
`union()` grid.

```
R = the same R as in overlap
select(filter(join({ g = union(points(A), points(B)), r = R }, drop),
              [along >= r.lo, along <= r.hi]), [])
```

**`align(A, B, along, grid, method, extrapolate, fewpoints)`**

Resamples both A and B onto the same grid. It returns a pair, so it only
appears as a step inside a surface expansion, never as a saved node:

```
A' = resample(A, along, grid, method, extrapolate, fewpoints)
B' = resample(B, along, grid, method, extrapolate, fewpoints)
```

**`legendre(F, along, output, method, branches, flat_tol)`**

For F with output y along x, with slope p = ∂y/∂x. The result has inputs
K ∪ {p} and outputs G = p·x − y, plus x.

```
S = rename(deriv(select(F, [y]), x, 1, method, drop), { y = p })
H = map(join({ f = F, s = S }, error), { p = "s.p", G = "s.p * x - f.y" }, false, {})
swap(H, x, p, branches, flat_tol)
```

This needs y convex or concave along x, so that p is monotonic. Otherwise
`swap` reports it, or splits it into branches. A convex-envelope version
(Legendre–Fenchel, the Maxwell construction) is planned as
`legendre_hull`.

---

## 4. Laws

These equalities hold for every field. The engine uses them to read less
data and skip work. They never change a result.

| # | Law | Used for |
|---|---|---|
| L1 | `filter` or `slice` on input u commutes with `map`, `select`, `rename`, and with every along-x operation where x ≠ u. | Reading only the files a result needs. |
| L2 | `filter` on input u passes into each `join` operand that has u, and is dropped for operands without it. | The same, through joins. |
| L3 | `filter` or `slice` on the input x does **not** commute with `resample`, `deriv`, `cumint`, `reduce`, or `swap` along x. | Why x ranges are applied after these. |
| L4 | `map(map(F, f), g) = map(F, g after f)`. | Fusing pointwise steps. |
| L5 | `join` is associative and commutative, up to output labels. | Reordering joins. |
| L6 | `resample(F, x, points(F), m)` equals F on F's points, for every method that passes through the data (all except `smooth`). | Skipping resampling that changes nothing. |
| L7 | `swap(swap(F, x, y), y, x) = F` where no points were dropped or split. | Checking inversions. |
| L8 | An along-x operation on a curve depends only on that curve. | Evaluating one curve at a time. |

L8 is what lets the engine stream: it reads the files for one key,
computes, keeps the result, and moves on.

---

## 5. From surface expressions to core trees

The surface language (spec.md section 7) is translated into core
operations. This section lists every place where a default or an implicit
step is filled in. Everything else is a direct translation.

### 5.1 Names and columns

| Surface | Core |
|---|---|
| `dmft` | `source(dmft, duplicates, digits)` using the current `duplicates` setting |
| `dmft.E` | `select(source(dmft), [E])` |
| `dmft.T`, where T is an input | `map(points(source(dmft)), { value = "T" }, false, {})`. The output can't be named T, since T is an input. |
| `gap.gap` | `select(source(gap), [gap])` |
| `2.5` | `const(2.5, "")` |
| a view or variable | its own translation, with its own settings |
| a dataset | `dataset(file)` |

### 5.2 Default axis

Operations along an input use the table's default axis when none is named:
`max(dmft.E)` means `reduce(..., along = T, op = max)`. The default axis is
`[field].axis` in the table file (6.2). Derived fields keep the default axis
of their input.

### 5.3 Arithmetic between fields: when to interpolate

`A ⊕ B` translates to `map(join({ a = A', b = B' }, unmatched), { value = "a.y ⊕ b.y" })`.

A' and B' are A and B, possibly resampled. For each input u that A and B
share:

- **Exact inputs** are joined as they are. An input is exact when its
  values come from a fixed set: path coordinates, and inputs produced by
  `axis`.
- **Ragged inputs** are aligned first. An input is ragged when its values
  vary from curve to curve: content columns like T in a file, and inputs
  produced by `span` or `swap`. The translation inserts
  `align(A, B, u, grid, method, extrapolate, fewpoints)`, with the grid
  built from the `grid` setting:

| `grid` setting | Grid node |
|---|---|
| `overlap(n=N)` | `overlap(A, B, u, N)` |
| `union()` | `merge_points(A, B, u)` |
| `like(X)` | `points(X)` |
| `linspace(a, b, n)` | `axis(u, lin:a:b:n)` |
| `logspace(a, b, n)` | `axis(u, log:a:b:n)` |
| `points([...])` | `axis(u, list:[...])` |

- `with align=[u, ...]` sets the aligned inputs explicitly, and
  `align=[]` aligns nothing. `strict = true` makes it an error to align
  without `with grid=...`.
- Two operands that come from the same source with the same selection
  share their points exactly, so no alignment is inserted. That's 0.1's
  row alignment.

Whether an input is exact or ragged is worked out from the tree alone, with
no data. The rules: `source` marks path and constant inputs exact, and
content inputs ragged. `resample` gives x the property of the grid's x.
`join` makes an input ragged if either operand has it ragged. `swap` makes
the new input ragged. Everything else keeps the property.

### 5.4 Other surface forms

| Surface | Core |
|---|---|
| `y[U=2.0, T=0.1:0.5]` | `filter(y, [...])` |
| a bare input name in arithmetic, like T in `C / T` | a reference to that input inside the `map` formula: `map(C, { value = "E / T" })` |
| `... where U=2.0` | `filter(root, [...])`, pushed down by L1 and L2 |
| `y @ T=0.1` | `eval(y, T, 0.1, method, extrapolate, fewpoints)` |
| `resample(y, grid=G)` | `resample(y, axis, G's grid node, method, extrapolate, fewpoints)` |
| `d(y, T)` | `deriv(y, T, 1, method, fewpoints)` |
| `int(y, T)` | `cumint(y, T, trapz)` |
| `integral(y, T)` | `reduce(y, T, integral, trapz)` |
| `max(y)`, `argmax(y)`, ... | `reduce(y, axis, op, -)` |
| `mean(y, n)` | `reduce(y, n, mean, -)` |
| `stack(a=y1, b=y2, tag="m")` | `stack({ a = y1, b = y2 }, m)` |
| `swap(y, T)` | `swap(y, T, output, branches, flat_tol)` |
| `using(y, KEY=VALUE)` | the translation of y with those settings. Leaves no node. |
| `name = expr` | the translation of expr, then `rename(..., { value = name })` |

### 5.5 Settings

Settings only affect translation. They fill in `method`, `grid`,
`extrapolate`, `duplicates`, `fewpoints`, `unmatched`, `branches`,
`flat_tol`, and which inputs are aligned. After translation the tree has
every value written in, so a saved tree needs no settings.

---

## 6. Calculation trees in TOML

### 6.1 Nodes

A tree is a set of nodes. Each node is a TOML table:

```toml
[recipe.nodes.7c1e0a9f3b2d]
op = "resample"
of = "a41b93e0c7d2"
along = "T"
grid = "0f3d77c2a9e1"
method = "pchip"
extrapolate = "nan"
fewpoints = "linear"
name = "dmft.E on the dE grid"
```

| Key | Meaning |
|---|---|
| `op` | The operation, from section 2 or 3. |
| `of` | The input node: an id, a list of ids (`union`), or a map from labels to ids (`join`, `stack`). |
| other keys | The operation's parameters, all of them. A parameter that refers to a node, like `grid`, holds its id. |
| `name` | Optional label for people, like the view or variable a node came from. It doesn't affect the result. |

Rules:

1. **Every parameter is written.** A missing parameter is an error. Defaults
   are never applied when a tree is loaded, so its meaning can't change
   between versions of glue.
2. **Node ids are content hashes.** The id is the first 12 hex digits of
   the SHA-256 of the node's canonical form: `op`, its parameters with keys
   sorted, and the ids of its input nodes, without `name`. Saving the same
   calculation twice gives the same ids. Shared subtrees get the same id
   everywhere.
3. **Nodes are listed leaves first**, so a file reads top to bottom.
4. **`root`** names the node whose value the file holds.

### 6.2 The files

| `kind` | Holds | Written by |
|---|---|---|
| `table` | A source: where the files are, and the field's inputs and outputs | you, `guess`, `new table` |
| `calc` | A core tree with no data | `save NAME --recipe-only`, or by hand |
| `dataset` | A core tree, its type, its cached value, and fingerprints | `save`, `refresh` |
| `project` | Names for tables, views, calcs, and datasets, plus settings | you, and `save` adds entries |

**Table files** get a `[field]` section. It replaces `[curves]`, which is
still read as an alias:

```toml
[field]
inputs = ["U", "J", "n", "T"]     # canonical inputs, in order
outputs = ["E"]
axis = "T"                        # default axis for surface expressions
```

Path placeholders must be inputs. Content columns are outputs unless listed
in `inputs`.

**Calc and dataset files:**

```toml
glue = "0.2"
kind = "dataset"
name = "dE"
created = 2026-09-28T10:00:00Z
tool = "glue 0.2.0"
pinned = false
description = ""

[type]
inputs = ["U", "J", "n", "T"]
outputs = ["dE"]
exact = ["U", "J", "n"]          # the others are ragged
axis = "T"

[type.units]
dE = "t"

[recipe]
lib = "glue-core 1"
surface = "dmft.E - ed.E"        # what was typed, for people
root = "5e0c2b7d91aa"

[recipe.inputs]                  # the leaves' files
dmft = "../tables/dmft.toml"
ed = "../tables/ed.toml"

[recipe.nodes.a41b93e0c7d2]
op = "source"
table = "dmft"
duplicates = "error"
digits = 10
name = "dmft"

# ... more nodes ...

[cache]
file = "dE.parquet"
format = "parquet"
sha256 = "..."
points = 8800

[fingerprint]
mode = "stat"
dmft = "sha256:..."
ed = "sha256:..."
```

A `calc` file is the same without `[cache]` and `[fingerprint]`.

**Projects** keep views as surface text, since views are meant to follow
the current settings. A calc file is a frozen view: a translated tree that
no longer depends on settings.

```toml
[views]
dE = "dmft.E - ed.E"

[calcs]
dE_frozen = "calcs/dE_frozen.toml"

[datasets]
dE_fine = "results/dE_fine.toml"
```

### 6.3 How trees connect

- A tree's leaves are `source` nodes (tables), `dataset` nodes (other
  saved results), and `const` and `axis` nodes.
- A `dataset` leaf refers to another dataset file. So saved results form a
  graph across files, and `refresh` follows it in dependency order.
- Because ids are content hashes, an optional cache of intermediate results,
  `.glue/cache/<node-id>-<fingerprint>.parquet`, can be shared by every
  dataset in a project. Two datasets that share a subtree, like the same
  resampled `dmft.E`, compute it once.
- Every file stays readable and diffs well in git. The only lines that
  change on a refresh are `created`, `[cache]`, and `[fingerprint]`.

### 6.4 Loading and checking a tree

1. Read all nodes and check that every `of` and node parameter refers to a
   node in the file.
2. Check for cycles.
3. Recompute each id from its node and compare it with the stored id. A
   mismatch means the node was edited by hand. The loader reports it and
   uses the recomputed id, so a hand edit is allowed, and visible.
4. Type-check from the leaves up with the rules in section 2. A dataset's
   `[type]` must match the root's type.

---

## 7. Worked example

### 7.1 A difference on a common grid

Surface, with `grid = overlap(n=200)` and `method = pchip`:

```
dE = dmft.E - ed.E
```

Core tree (ids shortened):

```
s1  source(dmft, error, 10)                         [U, J, n, T → E]
e1  select(s1, [E])                                 [U, J, n, T → E]
s2  source(ed, error, 10)
e2  select(s2, [E])
g   overlap(e1, e2, T, 200)                         [U, J, n, T → ]
r1  resample(e1, T, g, pchip, nan, linear)
r2  resample(e2, T, g, pchip, nan, linear)
j   join({ a = r1, b = r2 }, drop)                  [U, J, n, T → a.E, b.E]
m   map(j, { dE = "a.E - b.E" }, false, { dE = "t" })  [U, J, n, T → dE]
```

T is ragged in both sources, so it's aligned. U, J, n are exact, so they're
joined as they are. That's 0.1's behavior, now visible as steps.

### 7.2 Peak position over the gap

```
C = d(dmft.E, T)
ratio = argmax(C) / gap.gap
```

```
s1  source(dmft, error, 10)
e1  select(s1, [E])
c   deriv(e1, T, 1, pchip, linear)                  [U, J, n, T → E]
t   reduce(c, T, argmax, -)                         [U, J, n → E]
s3  source(gap, error, 10)
g1  select(s3, [gap])                               [U, J, n → gap]
j   join({ a = t, b = g1 }, drop)                   [U, J, n → a.E, b.gap]
m   map(j, { ratio = "a.E / b.gap" }, false, {})    [U, J, n → ratio]
```

No input is ragged on both sides here, so nothing is aligned.

### 7.3 An inverse: temperature at a given energy

```
TofE = swap(dmft.E, T)
TofE @ E=-0.3
```

```
e1  select(source(dmft, error, 10), [E])            [U, J, n, T → E]
w   swap(e1, T, E, error, 1e-6)                     [U, J, n, E → T]
v   eval(w, E, -0.3, pchip, nan, linear)            [U, J, n → T]
```

### 7.4 A reduction over a path input

Surface:

```
Eg = resample(dmft.E, grid=logspace(0.02, 1, 100))
mean(Eg, n)
```

```
e1  select(source(dmft, error, 10), [E])
ax  axis(T, log:0.02:1:100)                          [T → ]
r   resample(e1, T, ax, pchip, nan, linear)          [U, J, n, T → E], T now exact
m   reduce(r, n, mean, -)                            [U, J, T → E]
```

Averaging over n needs the curves along n, one per (U, J, T). Those only
exist because T was put on a shared grid first. Without that step, every
curve along n has one point, and the surface language says so.

---

## 8. Moving from 0.1

- A 0.1 recipe (expression text plus settings) is translated into a tree
  with section 5, using its recorded settings. `refresh` does this once and
  writes a 0.2 file. The results don't change.
- `[curves]` in table files is read as `[field]`: `by` and `x` become
  `inputs`, `y` becomes `outputs`, and `x` becomes `axis`.
- The engine's current node types map onto the core: `SourceCol` is
  `select(source)`, `Binary` is `align` + `join` + `map`, `Reduce` is
  `reduce`, `Stack` is `stack`, `At` is `eval`, and so on.

## 9. Open questions

1. **Node ids.** Content hashes make saving deterministic and let datasets
   share intermediate results. The cost is that ids are unreadable, which
   the optional `name` only partly fixes. The alternative is sequential ids
   like `n1, n2, ...`, which are readable but change whenever a tree is
   rebuilt.
2. **Exact or ragged.** Is "path inputs exact, content inputs ragged" the
   right default for your data? An input can also be declared in the table
   file, as `exact = [...]`, when a content column is on a shared grid,
   like β values in QMC.
3. **`mean`.** It's defined as the plain average over points. An average
   weighted by spacing in x is `integral / (range)`, and could be a
   separate op, `xmean`.
4. **`apply` and reproducibility.** A plugin's version string is recorded,
   but its code isn't. A content hash of the plugin's source file would
   catch edits that forget to change the version.
