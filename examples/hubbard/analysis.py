"""The same engine from Python. Run from this folder: python analysis.py"""

import glue

s = glue.open("project.toml", trust=True)

# An expression string, evaluated lazily. Only the 4 matching files per table are read.
dE = s.eval("dmft.E - ed.E", where="J=0.1, n=1.0")
df = dE.to_pandas()
print(dE.report)
print(df.groupby("U")["value"].agg(["min", "max"]))

# Operator overloading builds the same expression tree.
diff = (s.dmft.E - s.ed.E).to_pandas(where="J=0.1, n=1.0", method="cubic")
print(diff.head())

# Per-key results come back as one row per (U, J, n).
peaks = s.eval("argmax(d(dmft.E, T)) / gap.gap", where="n=1.0").to_pandas()
print(peaks.pivot(index="U", columns="J", values="value").round(3))


# A custom operation, usable in expressions right after it is registered.
@glue.op(kind="reduce")
def low_t_slope(x, y):
    """Slope of y between the two lowest x points."""
    return (y[1] - y[0]) / (x[1] - x[0])


print(s.eval("low_t_slope(dmft.E)", where="U=2.0").to_pandas())
