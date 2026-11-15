"""Error types and diagnostic rendering for Medha."""
from __future__ import annotations


class MedhaError(Exception):
    """Any error a Medha program can raise or catch.

    ``kind`` is the name programs see in ``catch e { e.kind }``.
    ``pos`` is ``(file, line, col)`` once the interpreter knows where it happened.
    """

    def __init__(self, message, pos=None, kind="Error", data=None):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.kind = kind
        self.data = data or {}
        self.stack = None

    def to_record(self):
        rec = {"kind": self.kind, "message": self.message}
        for k, v in self.data.items():
            if k not in rec:
                rec[k] = v
        if self.pos:
            rec.setdefault("line", self.pos[1])
        return rec

    def __str__(self):
        return self.message


class ReturnSignal(Exception):
    def __init__(self, value):
        self.value = value


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ExitSignal(Exception):
    def __init__(self, code):
        self.code = code


def _color(enabled):
    if not enabled:
        return lambda s, _c: s
    codes = {"red": "31;1", "blue": "34;1", "dim": "2", "bold": "1", "yellow": "33;1"}
    return lambda s, c: "\x1b[%sm%s\x1b[0m" % (codes[c], s)


def format_error(err, sources=None, color=False, label="error"):
    """Render an error with a source excerpt and caret, rustc style."""
    paint = _color(color)
    head = "%s%s %s" % (
        paint(label, "red" if label == "error" else "yellow"),
        paint("[%s]:" % err.kind, "bold"),
        err.message,
    )
    lines = [head]
    if err.pos:
        file, line, col = err.pos
        lines.append("  %s %s:%d:%d" % (paint("-->", "blue"), file, line, col))
        text = None
        if sources and file in sources:
            src_lines = sources[file].split("\n")
            if 1 <= line <= len(src_lines):
                text = src_lines[line - 1]
        if text is not None:
            gutter = str(line)
            pad = " " * len(gutter)
            lines.append("  %s %s" % (pad, paint("|", "blue")))
            lines.append("  %s %s %s" % (paint(gutter, "blue"), paint("|", "blue"), text.replace("\t", " ")))
            lines.append("  %s %s %s%s" % (pad, paint("|", "blue"), " " * max(col - 1, 0), paint("^", "red")))
    stack = getattr(err, "stack", None)
    if stack:
        for name, pos in reversed(stack[-8:]):
            where = "%s:%d" % (pos[0], pos[1]) if pos else "?"
            lines.append("  %s in %s (called at %s)" % (paint("=", "blue"), name, where))
    return "\n".join(lines)
