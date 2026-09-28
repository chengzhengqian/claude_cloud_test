"""glue: describe, combine, and plot scientific data spread across many files."""

__version__ = "0.2.0"

from .errors import GlueError  # noqa: E402
from .plugins import op  # noqa: E402


def open(path=None, trust=False):  # noqa: A001
    """Open a project, table, or dataset file and return a Session."""
    from .session import Session

    s = Session(trust=trust)
    if path is not None:
        s.load(path)
    return s


__all__ = ["open", "op", "GlueError", "__version__"]
