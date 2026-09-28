# glue core: fields, elementary operations, calculation trees

Version 0.2. This document defines the core calculus exactly. The shell
language in [spec.md](spec.md) is a convenient surface on top of it: every
surface expression is translated into a core tree, and the core tree is what
gets saved. The code is in `glue/core/`, and the translation is in
`glue/elaborate.py`. [changes-0.2.md](changes-0.2.md) lists what changed
from 0.1.

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

**Every node is a field.** Every operation in sections 2 and 3 takes
fields and returns one field, so any step of any calculation can be shown,
saved, loaded, used as a grid, or combined like any other. Plots and
exports consume a field and produce a figure or a file. They come at the
end of a calculation, and aren't part of it.

**All outputs of a field share its points.** Quantities sampled at
different points can only become outputs of one field after they're
resampled onto shared points (`align`), or joined with missing values where
one has none.

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
- The setting `nan_rows = error` is a check on top of these rules:
  evaluation stops at a source that has a missing value. It isn't a
  parameter of any node, since it can only stop a calculation, never change
  its values. So it doesn't change node ids.

---

## 2. Elementary operations

Twenty-one operations, in five groups. Every parameter is required in a core
tree. Section 5 lists which ones the surface language fills in.

| Group | Operations |
|---|---|
| leaves | `source`, `dataset`, `const`, `axis` |
| structure | `select`, `rename`, `transform`, `filter`, `slice`, `join`, `stack`, `points`, `union`, `span` |
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

The cached value of a saved dataset. Its id is the id of that dataset's
root node, so using a saved dataset gives the same ids as writing out its
calculation (section 6.5).

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

Rename inputs or outputs. The new names must not clash. No values change.

**`transform(F, input, to, formula)`** `: [(I − input) ∪ {to} → O]`

Replaces the input `input` with a new input `to`, whose value at each point
is `formula`. Use it when a source stores a quantity in different units or
a different convention, like u = U/D, or to change variables, like t = T/Δ.

- The formula uses the `map` language (2.3). It can refer to the input
  being replaced, other inputs, outputs, and numbers.
- New float values are rounded as in 1.3, so `0.3 / 1.5` becomes `0.2` and
  matches the value 0.2 from another source.
- **Check:** no two points may end up with the same input values. A
  formula that is strictly monotonic in the replaced input, and uses
  nothing else that varies, always passes, like `u * 2.0`. Otherwise it's
  checked on the data, and an error names the points that collide.
- The new input is **exact** if the formula uses only exact inputs and
  numbers. It's **ragged** if it uses a ragged input or an output (5.3).
- `to` may equal `input` to rescale in place.

Rescaling an output needs no special operation. It's a `map`.

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
and `hi` values. `scale` is `lin` or `log`. In TOML, n is stored as
`count`, and a node may give `step` instead: points every `step` from `lo`,
up to `hi`. A key where `lo ≥ hi` or either
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

**`resample(F, along, grid, method, extrapolate, fewpoints)`** `: [I ∪ J → O]`

- `grid : [J → ]` is a point set with x in J.
- For each key k of F, and each grid point g that agrees with k on the
  inputs they share, the result has the point `k ∪ g`. Its outputs are the
  curve's interpolant evaluated at `g.x`.
- If the grid lacks some inputs of K, it applies to every curve regardless
  of those inputs.
- If the grid has inputs that F doesn't, F is broadcast along them: each
  grid point uses the curve of F that matches it on F's own inputs. This
  is what `align` needs when one operand has fewer inputs, as in
  `dmft.E - ref.E` with `ref.E : [U, J, T → E]`. The overlap grid has
  (U, J, n, T), and the reference curve is resampled once for each n.

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

Reduces each curve to one value per output, which removes the input x. In
TOML the `op` parameter is stored as `reduce = "max"`, since `op` already
names the node's operation.

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
`module:qualname@version`, as in `physops:fwhm@1`. The colon separates the
import path from the name inside the module, so functions defined inside
other functions or classes work too. The older form `module.function@version`
is still read. `kind` is:

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

**`overlap(A, B, ..., along, n)`** `: [K ∪ {along} → ]` point set

n points over the x range every operand covers, per key. It takes two or
more operands. The expansion below is for two. More operands add more
`range` terms to the join and to the `max` and `min`.

```
R = map(join({ a = range(A, along), b = range(B, along) }, drop),
        { lo = "max(a.lo, b.lo)", hi = "min(a.hi, b.hi)" }, false, {})
span(R, along, lo, hi, n, lin)
```

**`merge_points(A, B, ..., along)`** `: [K ∪ {along} → ]` point set

Every x point of the operands that lies inside the overlap. Like `overlap`,
it takes two or more operands. This is 0.1's
`union()` grid.

```
R = the same R as in overlap
select(filter(join({ g = union(points(A), points(B)), r = R }, drop),
              [along >= r.lo, along <= r.hi]), [])
```

**`align({ a = A, b = B, ... }, along, grid, method, extrapolate, fewpoints, unmatched)`**
`: [I_A ∪ I_B → a.O_A ∪ b.O_B]`

Resamples the operands onto the same grid and joins them, so the result is
one field with all their outputs on shared points. It takes two or more
operands. An operand without the input `along` is joined as it is, with no
resample. For two:

```
join({ a = resample(A, along, grid, method, extrapolate, fewpoints),
       b = resample(B, along, grid, method, extrapolate, fewpoints) }, unmatched)
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
| L2b | A `filter` on the new input of a `transform` whose formula uses only exact inputs and numbers can be checked against the file index: the formula is evaluated on each file's path values, without reading data. Other filters on it are applied after reading. A table's `[field.transform]` does this when the index is built. A `transform(...)` node does it by asking its operand which values the formula's inputs can take (from the file index, through nodes that keep input values), evaluating the formula on them, and pushing an equality list on those inputs. The filter itself is still applied at the transform, so the result is exact. | Reading less data when inputs are rescaled. |
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

`A ⊕ B` translates to `map(J, { value = "a.y ⊕ b.y" })`, where J is
`join({ a = A, b = B }, unmatched)` if nothing needs aligning, and
`align({ a = A, b = B }, u, ...)` if input u does. For each input u that A
and B share:

- **Exact inputs** are joined as they are. An input is exact when its
  values come from a fixed set: path coordinates, content inputs other than
  the axis (such as U in a SQLite table), and inputs produced by `axis`.
- **Ragged inputs** are aligned first. An input is ragged when its values
  vary from curve to curve: the table's axis, like T in a file, inputs
  produced by `span` or `swap`, and inputs produced by a `transform` whose
  formula uses a ragged input or an output. The translation uses
  `align({ a = A, b = B }, u, grid, method, extrapolate, fewpoints, unmatched)`,
  with the grid built from the `grid` setting:

| `grid` setting | Grid node |
|---|---|
| `overlap(n=N)` | `overlap(A, B, u, N)` |
| `union()` | `merge_points(A, B, u)` |
| `like(X)` | `points(X)` |
| `linspace(a, b, n)` | `axis(u, lin:a:b:n)` |
| `logspace(a, b, n)` | `axis(u, log:a:b:n)` |
| `points([...])` | `axis(u, list:[...])` |

- `with align=[u]` sets the aligned input explicitly, and `align=[]`
  aligns nothing, so the operands are joined on exactly equal values. An
  input named in `align` that no operand has is an error. When more than
  one shared input needs aligning, the statement fails and asks for
  `align=[u]`. `strict = true` makes it an error to align
  without `with grid=...`.
- Two operands that come from the same source with the same selection
  share their points exactly, so no alignment is inserted. That's 0.1's
  row alignment.

Whether an input is exact or ragged is worked out from the tree alone, with
no data. The rules: `source` marks every input exact except its axis, which
is ragged unless the table lists it in `[field].exact`. Path and constant
inputs are always exact. `resample` gives x the property of the grid's x.
`join` makes an input ragged if either operand has it ragged. `swap` makes
the new input ragged. `transform` makes it exact only if its formula uses
exact inputs and numbers alone. Everything else keeps the property.

### 5.4 Other surface forms

| Surface | Core |
|---|---|
| `y[U=2.0, T=0.1:0.5]` | `filter(y, [...])` |
| `rename(y, u=U)` | `rename(y, { u = U })` |
| `transform(y, u, U = u * 2.0)` | `transform(y, u, U, "u * 2.0")` |
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
   sorted, and the ids of its input nodes. Labels are left out: `name`,
   and the `table` label of a `source` node. Saving the same calculation
   twice gives the same ids. Shared subtrees get the same id everywhere. A
   leaf's id comes from where its data is, not from its name (section 6.5).
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
exact = []                        # content inputs to treat as exact (5.3)
```

Path placeholders must be inputs. Content columns are outputs unless listed
in `inputs`. Without `axis`, a table with exactly one content input uses it
as the axis. Every input except the axis is exact. The axis is ragged
unless it's listed in `exact`, for data where every file uses the same
points.

An output's error column (`[columns.E] error = "dE"`) comes with it, so
`outputs = ["E"]` also keeps `dE`.

A table can also fix a source's conventions once, so every expression sees
canonical names and units. These steps are applied right after reading, in
the order written, and are part of the table's definition, so they're part
of its leaf id (6.5):

```toml
[field.transform]                 # inputs, as transform(...)
U = { from = "u", formula = "u * 2.0" }

[field.map]                       # outputs, as map(...)
E = "E_D * 2.0"
```

Renaming needs no extra section: name the placeholder with the canonical
name (`pattern = "u_{U}/..."`), and name the file's columns in `columns`.

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
table = "dmft"                   # label: which [recipe.inputs] entry to open
def = "3f9a0c1e77b2"             # leaf id: what the table reads (section 6.5)
duplicates = "error"
digits = { U = 10, J = 10, n = 10, T = 10 }
name = "dmft"

# ... more nodes ...

[cache]
file = "dE.parquet"
format = "parquet"
sha256 = "..."
points = 8800
curves = 44
dropped = 4

[fingerprint]
mode = "stat"
dmft = "sha256:..."
ed = "sha256:..."
```

A `calc` file is the same without `[cache]` and `[fingerprint]`, and with
`kind = "calc"`. `save NAME --recipe-only` writes one to `calcs/NAME.toml`
and adds it to `[calcs]`.

**Projects** keep views as surface text, since views are meant to follow
the current settings. A calc file is a frozen view: a translated tree that
no longer depends on settings. `save NAME --view` writes a session
variable's surface text to `[views]`.

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

### 6.5 Names and identity

A name is a label you type. It never decides what a node is. Two things
decide that: where a leaf's data lives, and what calculation a node does.

**Names are unique only where you type them.**

- In one project, every table, view, calc, and dataset name must be
  different, as now. A clash is an error when the project loads.
- An included project's names get its prefix: `[[include]] prefix = "old_"`
  turns its `dmft` into `old_dmft`. Loading a second project in the shell
  works the same way: `load ../2025/project.toml as old_`.
- A recipe's `[recipe.inputs]` labels are local to that one file. Two
  dataset files can both call an input `dmft` and mean different tables.

**A source's id comes from what it reads.** It's the hash of the table's
resolved definition:

- the data location, as a path relative to the project folder, with
  `${VAR}` references kept as written
- the pattern or query, and the reader settings
- the `[field]` section: inputs, outputs, and their types

The table's name, and where its TOML file sits, are not part of it. So:

| Situation | Same id? |
|---|---|
| Two `dmft.toml` files in different folders, reading different data folders | No. They're different leaves, even with the same name. |
| One table renamed from `dmft` to `dmft_ctqmc` | Yes. Nothing downstream is recomputed. |
| A table file moved, still reading the same data | Yes |
| The same project copied to another machine | Yes, because paths are relative to the project folder |
| A table's `pattern` or `columns` edited | No. Everything that uses it gets new ids and is recomputed. |

The one case this doesn't unify: two projects that reach the same data
folder by different relative paths, like `data/dmft` and `../p1/data/dmft`,
get different ids. They compute the same thing twice, but never mix
results. Using a shared `${VAR}` root in both gives them the same ids.

**A node's id comes from its calculation:** its op, its parameters, and its
children's ids (6.1). Changing a setting that a view uses changes the tree,
so it gets new ids.

**A saved dataset is transparent.** A `dataset` leaf has the id of that
dataset's root node. So these give exactly the same ids downstream:

```
save C                       # C = d(dmft.E, T), saved as a dataset
Tpeak = argmax(C)            # uses the dataset
Tpeak = argmax(d(dmft.E, T)) # writes the calculation out
```

Saving something never changes the identity of what's built from it. It
only makes its value available without recomputing.

**Data is identified separately, by fingerprint.** An id says *what* is
computed. A fingerprint says *from which data*. A cached value is only used
when both match (6.6). Two leaves can never share a result by accident,
because they either have different ids or different fingerprints.

### 6.6 Caching, tracking, and invalidation

**The store.** Values are cached by `(node id, fingerprint)`:

| Where | What | Lifetime |
|---|---|---|
| memory | any node computed in the session | the session, with a size limit |
| `.glue/cache/<id>-<fp>.parquet` | intermediate nodes worth keeping, like resampled or differentiated curves | until `gc`, with a size limit (`disk_cache_mb`) |
| dataset files | named results | permanent, until you delete them |

A dataset file is a named pointer into this store. It holds a tree, and the
value of its root for one fingerprint. Loading a dataset file puts its
nodes and its value back in the store, so the rest of the project can use
them.

**Fingerprints.** A leaf's fingerprint is computed from its chunks, as in
0.1: size and modification time (`stat`), or a content hash (`hash`), plus
the leaf's **epoch** (below). A node's fingerprint is the combined
fingerprint of the leaves under it. Each cached value also records which
chunks each of its curves used, so a change to one file only invalidates
the curves that read it.

**Tracking is automatic, and happens when a value is used.** Nothing runs
in the background. When a node's value is needed:

1. Compute the current fingerprints of the leaves under it. With `stat`,
   that's one file-system call per file, which is fast even for thousands
   of files.
2. If the store has the value for `(id, fingerprint)`, use it.
3. If it has a value for the same id with an older fingerprint, find the
   changed chunks and recompute only the curves that used them. Keep the
   rest.
4. Otherwise, compute it.

The same check runs for every intermediate node, not only saved datasets.
So after one file changes, a plot of a derived quantity recomputes the one
affected curve all the way up, and reuses everything else.

**Manual invalidation, for what stat can't see.** Some changes don't show
up in size and modification time: files copied with their times preserved
(`cp -p`, `rsync -t`), network file systems with coarse timestamps, or a
fix you know about that glue can't detect. For those:

| Command | Effect |
|---|---|
| `invalidate TABLE` | Increase the table's epoch. Every value that depends on it becomes stale. |
| `invalidate TABLE where U=2.0` | Mark only the matching chunks as changed. Only the curves that read them are recomputed. |
| `invalidate NAME` | For a view, calc, or dataset: drop its cached values, and those of the nodes under it that aren't used elsewhere. |

Epochs are stored in `.glue/epochs.toml`, one number per leaf id. Deleting
the file makes everything stale once, which is safe.

**Related commands:**

| Command | Effect |
|---|---|
| `status [NAME]` | Fresh or stale, for datasets and, with `--all`, for cached intermediate nodes |
| `why NAME` | The tree under NAME, with each node's state, down to the leaves and chunks that changed |
| `refresh NAME \| --all` | Recompute what's stale now, instead of waiting for the next use. Pinned datasets are skipped. |
| `gc` | Delete cached values whose ids no longer appear in any view, calc, or dataset of the project |

**Pinned datasets** keep the value for their recorded fingerprint, even when
their leaves change. Anything built on a pinned dataset uses that old value,
and its own fingerprint includes the pinned value's hash. So it's never
mixed up with results from the new data.

**What is guaranteed.** A value is only reused when its node id and its
fingerprint both match. The id fixes the calculation, including every
parameter. The fingerprint fixes the data. Names, file locations, and the
order in which things were saved can't cause a wrong reuse.

**How 0.2 does it.** The id and fingerprint rules, epochs, `invalidate`
(all three forms), `why`, `gc`, `refresh`, pinning, and `status --all`
work as written. Some details:

- Step 3 above works the same way for saved datasets (`refresh`) and for
  cached values of expensive nodes: resample, deriv, cumint, reduce, swap,
  apply, align, and legendre. Each such value keeps the chunk entries of
  the files under it. On the next use after a change, the changed chunks'
  input values are traced up to the node's inputs, as in `refresh`, only
  those keys are recomputed, and they replace the old rows. When a change
  can't be traced, the value is computed in full. The code is in
  `glue/core/incremental.py`.
- The disk cache is off by default. `set disk_cache true` turns it on for
  the same expensive nodes. Only whole values, with no filter pushed into
  them, are written. Each node keeps only its newest value, which is what
  the next update starts from. `disk_cache_mb` (default 1024) limits the
  folder, and the least recently used values are removed first. `gc`
  removes the values nothing uses.
- `invalidate NAME` for a view or calc drops the cached values of every
  node under it, including ones other names share. They're recomputed on
  next use, so this is only slower, never wrong. For a dataset, it marks
  the dataset invalid, and `refresh` recomputes it in full.

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
- The 0.1 engine's node types map onto the core: `SourceCol` is
  `select(source)`, `Binary` is `align` + `join` + `map`, `Reduce` is
  `reduce`, `Stack` is `stack`, `At` is `eval`, and so on. The 0.1 engine
  and compiler are removed in 0.2.

## 9. Open questions

1. **Node ids.** Content hashes make saving deterministic, and they're what
   makes identity and caching work (6.5, 6.6). The cost is that ids are
   unreadable, which the optional `name` only partly fixes.
2. **Exact or ragged.** 0.2 treats every input except the axis as exact.
   The first draft made every content input ragged, but then the U, J, n
   columns of a SQLite table all needed aligning, and nothing worked
   without `align=[...]`. Is "only the axis is ragged" the right default for
   your data? The axis can be declared exact in the table file, as
   `exact = ["T"]`, when every file uses the same points, like β values in
   QMC.
3. **`mean`.** It's defined as the plain average over points. An average
   weighted by spacing in x is `integral / (range)`, and could be a
   separate op, `xmean`.
4. **`apply` and reproducibility.** A plugin's version string is recorded,
   but its code isn't. A content hash of the plugin's source file would
   catch edits that forget to change the version.
