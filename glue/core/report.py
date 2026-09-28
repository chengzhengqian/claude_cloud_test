import re
from collections import OrderedDict


class Report:
    """Counts of things a run did to the data: alignments, dropped keys, repaired rows."""

    def __init__(self):
        self.items = OrderedDict()
        self.details = OrderedDict()

    def count(self, message, n=1, detail=None):
        self.items[message] = self.items.get(message, 0) + n
        if detail is not None:
            self.details.setdefault(message, []).append(detail)

    def note(self, message):
        self.items.setdefault(message, None)

    def lines(self, level="short"):
        if level == "off":
            return []
        out = []
        for msg, n in self.items.items():
            if n is None:
                out.append(msg)
            elif "{n}" in msg:
                text = msg.format(n=n)
                if n == 1:
                    text = re.sub(r"\b1 (curve|key|row|file|point)s\b", r"1 \1", text)
                    text = re.sub(r"\b1 unmatched keys\b", "1 unmatched key", text)
                out.append(text)
            else:
                out.append(f"{msg}: {n}")
            if level == "full":
                for d in self.details.get(msg, []):
                    out.append(f"    {d}")
        return out
