"""Field types: [inputs → outputs] with per-input properties."""

from dataclasses import dataclass, replace

from ..errors import GlueError


@dataclass(frozen=True)
class Var:
    name: str
    dtype: str = "float"  # float, int, str
    exact: bool = True
    unit: str = ""


@dataclass(frozen=True)
class Out:
    name: str
    unit: str = ""
    label: str = ""


@dataclass(frozen=True)
class FType:
    inputs: tuple = ()
    outputs: tuple = ()
    axis: str = None

    def __post_init__(self):
        names = [v.name for v in self.inputs] + [o.name for o in self.outputs]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise GlueError(f"a field can't use the name {sorted(dup)[0]!r} twice "
                            "(inputs and outputs need different names). Use rename.")
        if self.axis is not None and self.axis not in self.input_names:
            object.__setattr__(self, "axis", None)

    @property
    def input_names(self):
        return [v.name for v in self.inputs]

    @property
    def output_names(self):
        return [o.name for o in self.outputs]

    def var(self, name):
        for v in self.inputs:
            if v.name == name:
                return v
        return None

    def out(self, name):
        for o in self.outputs:
            if o.name == name:
                return o
        return None

    def has_input(self, name):
        return self.var(name) is not None

    def need_input(self, name, what):
        v = self.var(name)
        if v is None:
            raise GlueError(f"{what}: no input {name!r} (inputs: {', '.join(self.input_names) or 'none'})")
        return v

    def need_output(self, name, what):
        o = self.out(name)
        if o is None:
            raise GlueError(f"{what}: no output {name!r} (outputs: {', '.join(self.output_names) or 'none'})")
        return o

    def without_input(self, name):
        return FType(tuple(v for v in self.inputs if v.name != name), self.outputs,
                     None if self.axis == name else self.axis)

    def with_outputs(self, outputs):
        return FType(self.inputs, tuple(outputs), self.axis)

    def replace_var(self, name, new):
        return FType(tuple(new if v.name == name else v for v in self.inputs), self.outputs,
                     new.name if self.axis == name else self.axis)

    def __str__(self):
        return self.text()

    def text(self):
        ins = ", ".join(v.name + ("" if v.exact or v.dtype == "str" else ":ragged") for v in self.inputs)
        return f"[{ins} → {', '.join(self.output_names)}]"

    def to_toml(self):
        d = {"inputs": self.input_names, "outputs": self.output_names,
             "exact": [v.name for v in self.inputs if v.exact]}
        if self.axis:
            d["axis"] = self.axis
        dtypes = {v.name: v.dtype for v in self.inputs if v.dtype != "float"}
        units = {v.name: v.unit for v in self.inputs if v.unit}
        units.update({o.name: o.unit for o in self.outputs if o.unit})
        from ..util import Inline

        if dtypes:
            d["dtypes"] = Inline(dtypes)
        if units:
            d["units"] = Inline(units)
        return d

    @staticmethod
    def from_toml(d):
        exact = set(d.get("exact", []))
        dtypes = d.get("dtypes", {})
        units = d.get("units", {})
        ins = tuple(Var(n, dtypes.get(n, "float"), n in exact, units.get(n, "")) for n in d.get("inputs", []))
        outs = tuple(Out(n, units.get(n, "")) for n in d.get("outputs", []))
        return FType(ins, outs, d.get("axis"))


def curve_key(ftype, along=None):
    """Inputs that identify a curve: all inputs except `along` (default: the axis)."""
    along = along or ftype.axis
    return [n for n in ftype.input_names if n != along]


__all__ = ["Var", "Out", "FType", "curve_key", "replace"]
