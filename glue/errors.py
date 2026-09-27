class GlueError(Exception):
    pass


class ParseError(GlueError):
    def __init__(self, message, text=None, pos=None):
        self.text = text
        self.pos = pos
        if text is not None and pos is not None:
            message = f"{message}\n    {text}\n    {' ' * pos}^"
        super().__init__(message)


class DropCurve(Exception):
    """Raised while evaluating one curve to leave it out of the result."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
