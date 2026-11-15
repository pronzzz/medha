"""Tokenizer for Medha source text."""
from __future__ import annotations

from .errors import MedhaError

KEYWORDS = {
    "let", "var", "fn", "return", "if", "else", "while", "for", "in", "match",
    "import", "as", "is", "and", "or", "not", "true", "false", "null", "try",
    "catch", "finally", "throw", "break", "continue", "assert",
}

OPS = [
    "...", "|>", "->", "=>", "==", "!=", "<=", ">=", "??", "?.", "**", "+=", "-=",
    "*=", "/=", "..", "+", "-", "*", "/", "%", "<", ">", "=", "(", ")", "{", "}",
    "[", "]", ",", ".", ":", ";", "|", "?",
]

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "{": "{", "}": "}"}


class Token:
    __slots__ = ("type", "value", "line", "col", "nl", "doc", "start", "end")

    def __init__(self, type, value, line, col, nl, start, end, doc=None):
        self.type = type
        self.value = value
        self.line = line
        self.col = col
        self.nl = nl
        self.start = start
        self.end = end
        self.doc = doc

    def __repr__(self):
        return "Token(%s, %r, %d:%d)" % (self.type, self.value, self.line, self.col)


def _find_string_end(src, i, quote, raw, file, line, col):
    """Index of the closing quote for a string whose body starts at ``i``."""
    n = len(src)
    triple = len(quote) == 3
    j = i
    while j < n:
        c = src[j]
        if not raw and c == "\\":
            j += 2
            continue
        if not raw and c == "{":
            j = _skip_braces(src, j, file, line, col)
            continue
        if src.startswith(quote, j):
            return j
        if c == "\n" and not triple:
            break
        j += 1
    raise MedhaError("unterminated string", (file, line, col), "SyntaxError")


def _skip_braces(src, j, file, line, col):
    """Given ``src[j] == '{'``, return the index just past the matching ``}``."""
    n = len(src)
    depth = 0
    while j < n:
        c = src[j]
        if c == '"':
            quote = '"""' if src.startswith('"""', j) else '"'
            k = _find_string_end(src, j + len(quote), quote, False, file, line, col)
            j = k + len(quote)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise MedhaError("unterminated '{' in string interpolation (write \\{ for a literal brace)",
                     (file, line, col), "SyntaxError")


def _dedent(body):
    """Strip the leading newline and common indentation of a triple-quoted string."""
    lines = body.split("\n")
    if lines and lines[0].strip() == "":
        lines = lines[1:]
    if lines and lines[-1].strip() == "":
        lines = lines[:-1]
    indent = None
    for ln in lines:
        if ln.strip() == "":
            continue
        lead = len(ln) - len(ln.lstrip(" \t"))
        indent = lead if indent is None else min(indent, lead)
    indent = indent or 0
    return "\n".join(ln[indent:] if ln.strip() else "" for ln in lines)


def _string_parts(body, raw, file, line, col):
    """Split a string body into literal text and ``("expr", source, line, col)`` parts."""
    if raw:
        return [body]
    parts = []
    buf = []
    j = 0
    n = len(body)
    while j < n:
        c = body[j]
        if c == "\\" and j + 1 < n:
            nxt = body[j + 1]
            buf.append(ESCAPES.get(nxt, "\\" + nxt))
            j += 2
            continue
        if c == "{":
            k = _skip_braces(body, j, file, line, col)
            inner = body[j + 1:k - 1]
            if not inner.strip():
                raise MedhaError("empty '{}' in string (write \\{ for a literal brace)",
                                 (file, line, col), "SyntaxError")
            if buf:
                parts.append("".join(buf))
                buf = []
            parts.append(("expr", inner, line + body.count("\n", 0, j), col))
            j = k
            continue
        buf.append(c)
        j += 1
    if buf or not parts:
        parts.append("".join(buf))
    return parts


def lex(src, file="<input>"):
    toks = []
    i = 0
    n = len(src)
    line = 1
    line_start = 0
    nl = True
    doc = []
    if src.startswith("#!"):
        i = src.find("\n")
        if i < 0:
            i = n

    def add(type, value, start, end, tline, tcol):
        nonlocal nl, doc
        toks.append(Token(type, value, tline, tcol, nl, start, end, "\n".join(doc) if doc else None))
        nl = False
        doc = []

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            line_start = i
            nl = True
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("///", i):
            end = src.find("\n", i)
            end = n if end < 0 else end
            doc.append(src[i + 3:end].strip())
            i = end
            continue
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                raise MedhaError("unterminated block comment", (file, line, i - line_start + 1), "SyntaxError")
            line += src.count("\n", i, end)
            last_nl = src.rfind("\n", i, end)
            if last_nl >= 0:
                line_start = last_nl + 1
                nl = True
            i = end + 2
            continue

        col = i - line_start + 1
        start = i

        raw = c == "r" and i + 1 < n and src[i + 1] == '"'
        if c == '"' or raw:
            if raw:
                i += 1
            quote = '"""' if src.startswith('"""', i) else '"'
            body_start = i + len(quote)
            end = _find_string_end(src, body_start, quote, raw, file, line, col)
            body = src[body_start:end]
            if quote == '"""':
                body = _dedent(body)
            parts = _string_parts(body, raw, file, line, col)
            i = end + len(quote)
            add("str", parts, start, i, line, col)
            newlines = src.count("\n", start, i)
            if newlines:
                line += newlines
                line_start = src.rfind("\n", start, i) + 1
            continue

        if c.isdigit():
            j = i
            while j < n and (src[j].isdigit() or src[j] == "_"):
                j += 1
            is_float = False
            if j + 1 < n and src[j] == "." and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k < n and src[k].isdigit():
                    is_float = True
                    j = k
                    while j < n and src[j].isdigit():
                        j += 1
            text = src[i:j].replace("_", "")
            add("float" if is_float else "int", float(text) if is_float else int(text), start, j, line, col)
            i = j
            continue

        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            add("kw" if word in KEYWORDS else "ident", word, start, j, line, col)
            i = j
            continue

        for op in OPS:
            if src.startswith(op, i):
                add("op", op, start, i + len(op), line, col)
                i += len(op)
                break
        else:
            raise MedhaError("unexpected character %r" % c, (file, line, col), "SyntaxError")

    toks.append(Token("eof", None, line, max(i - line_start + 1, 1), True, n, n))
    return toks
