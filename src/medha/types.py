"""Medha's type system.

A type is three things at once:

* a runtime validator (``validate``) used for annotations and model output,
* a JSON Schema generator (``schema``) used to tell a model what to produce,
* a sample generator (``sample``) used by the mock provider for offline runs.
"""
from __future__ import annotations

import json
import re

MAX_SAMPLE_DEPTH = 4


class ValidationError(Exception):
    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__(message if path == "$" else "%s: %s" % (path, message))


def type_name_of(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "record"
    if isinstance(v, Type):
        return "type"
    return getattr(v, "type_name", type(v).__name__)


def brief(v, limit=60):
    try:
        text = json.dumps(v, ensure_ascii=False, default=lambda o: "<%s>" % type_name_of(o))
    except (TypeError, ValueError):
        text = str(v)
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return "%s %s" % (type_name_of(v), text) if not isinstance(v, (dict, list)) or len(text) <= limit else text


class Type:
    doc = None

    def validate(self, v, lenient=False, path="$"):
        raise NotImplementedError

    def check(self, v):
        try:
            self.validate(v)
            return True
        except ValidationError:
            return False

    def schema(self):
        """A self-contained JSON Schema for this type."""
        ctx = {"defs": {}, "stack": [], "recursive": set()}
        out = self._schema(ctx)
        if ctx["defs"]:
            out = dict(out)
            out["$defs"] = ctx["defs"]
        return out

    def _schema(self, ctx):
        raise NotImplementedError

    def describe(self):
        raise NotImplementedError

    def expand(self):
        """Description with one level of alias expansion."""
        return self.describe()

    def sample(self, hint="value", depth=0):
        raise NotImplementedError

    def resolved(self):
        return self

    def accepts_null(self):
        return False

    def __repr__(self):
        return "<type %s>" % self.describe()


class TPrim(Type):
    def __init__(self, kind):
        self.kind = kind

    def validate(self, v, lenient=False, path="$"):
        k = self.kind
        if k == "any":
            return v
        if k == "null":
            if v is None:
                return v
        elif k == "string":
            if isinstance(v, str):
                return v
            if lenient and isinstance(v, (int, float)) and not isinstance(v, bool):
                return str(v)
        elif k == "int":
            if isinstance(v, int) and not isinstance(v, bool):
                return v
            if isinstance(v, float) and v.is_integer() and lenient:
                return int(v)
            if lenient and isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
                return int(v)
        elif k == "float":
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v) if lenient else v
            if lenient and isinstance(v, str):
                try:
                    return float(v)
                except ValueError:
                    pass
        elif k == "bool":
            if isinstance(v, bool):
                return v
            if lenient and isinstance(v, str) and v.strip().lower() in ("true", "false"):
                return v.strip().lower() == "true"
        raise ValidationError(path, "expected %s, got %s" % (k, brief(v)))

    def _schema(self, ctx):
        return {"string": {"type": "string"}, "int": {"type": "integer"}, "float": {"type": "number"},
                "bool": {"type": "boolean"}, "null": {"type": "null"}, "any": {}}[self.kind]

    def describe(self):
        return self.kind

    def sample(self, hint="value", depth=0):
        return {"string": "<%s>" % hint, "int": 0, "float": 0.0, "bool": False, "null": None,
                "any": "<%s>" % hint}[self.kind]

    def accepts_null(self):
        return self.kind in ("null", "any")


T_STRING = TPrim("string")
T_INT = TPrim("int")
T_FLOAT = TPrim("float")
T_BOOL = TPrim("bool")
T_ANY = TPrim("any")
T_NULL = TPrim("null")
PRIMS = {"string": T_STRING, "int": T_INT, "float": T_FLOAT, "number": T_FLOAT, "bool": T_BOOL,
         "any": T_ANY}


class TLit(Type):
    def __init__(self, value):
        self.value = value

    def validate(self, v, lenient=False, path="$"):
        mine = self.value
        if type(v) is type(mine) and v == mine:
            return v
        if isinstance(mine, float) and isinstance(v, int) and not isinstance(v, bool) and v == mine:
            return v
        if lenient and isinstance(mine, str) and isinstance(v, str) and v.strip().strip("\"'").lower() == mine.lower():
            return mine
        if lenient and isinstance(v, str) and not isinstance(mine, str):
            try:
                if json.loads(v) == mine and type(json.loads(v)) is type(mine):
                    return mine
            except ValueError:
                pass
        raise ValidationError(path, "expected %s, got %s" % (self.describe(), brief(v)))

    def _schema(self, ctx):
        return {"const": self.value}

    def describe(self):
        return json.dumps(self.value, ensure_ascii=False)

    def sample(self, hint="value", depth=0):
        return self.value

    def accepts_null(self):
        return self.value is None


class TList(Type):
    def __init__(self, elem):
        self.elem = elem

    def validate(self, v, lenient=False, path="$"):
        if not isinstance(v, list):
            raise ValidationError(path, "expected a list, got %s" % brief(v))
        return [self.elem.validate(x, lenient, "%s[%d]" % (path, i)) for i, x in enumerate(v)]

    def _schema(self, ctx):
        return {"type": "array", "items": self.elem._schema(ctx)}

    def describe(self):
        return "[%s]" % self.elem.describe()

    def sample(self, hint="value", depth=0):
        if depth >= MAX_SAMPLE_DEPTH:
            return []
        return [self.elem.sample(hint, depth + 1)]


class TMap(Type):
    def __init__(self, value):
        self.value = value

    def validate(self, v, lenient=False, path="$"):
        if not isinstance(v, dict):
            raise ValidationError(path, "expected a map, got %s" % brief(v))
        return {k: self.value.validate(x, lenient, "%s.%s" % (path, k)) for k, x in v.items()}

    def _schema(self, ctx):
        return {"type": "object", "additionalProperties": self.value._schema(ctx)}

    def describe(self):
        return "map<%s>" % self.value.describe()

    def sample(self, hint="value", depth=0):
        return {}


class Field:
    __slots__ = ("name", "type", "optional", "default", "doc")

    def __init__(self, name, type, optional=False, default=None, doc=None):
        self.name = name
        self.type = type
        self.optional = optional
        self.default = default  # None, or a zero-argument callable
        self.doc = doc

    def is_required(self):
        return not (self.optional or self.default is not None or self.type.accepts_null())


class TRecord(Type):
    def __init__(self, fields):
        self.fields = fields

    def validate(self, v, lenient=False, path="$"):
        if not isinstance(v, dict):
            raise ValidationError(path, "expected a record, got %s" % brief(v))
        out = {}
        for f in self.fields:
            fpath = "%s.%s" % (path, f.name)
            if f.name in v and not (v[f.name] is None and f.default is not None):
                out[f.name] = f.type.validate(v[f.name], lenient, fpath)
            elif f.default is not None:
                out[f.name] = f.default()
            elif f.is_required():
                raise ValidationError(fpath, "missing required field (%s)" % f.type.describe())
            else:
                out[f.name] = None
        if not lenient:
            for k, x in v.items():
                if k not in out:
                    out[k] = x
        return out

    def _schema(self, ctx):
        props = {}
        required = []
        for f in self.fields:
            s = dict(f.type._schema(ctx))
            if f.doc:
                s["description"] = f.doc if "description" not in s else "%s %s" % (f.doc, s["description"])
            props[f.name] = s
            if f.is_required():
                required.append(f.name)
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    def describe(self):
        if not self.fields:
            return "{}"
        return "{ %s }" % ", ".join(
            "%s%s: %s" % (f.name, "?" if f.optional else "", f.type.describe()) for f in self.fields)

    def sample(self, hint="value", depth=0):
        out = {}
        for f in self.fields:
            if f.default is not None:
                out[f.name] = f.default()
            elif not f.is_required() and depth >= MAX_SAMPLE_DEPTH - 1:
                out[f.name] = None
            else:
                out[f.name] = f.type.sample(f.name, depth + 1)
        return out


class TUnion(Type):
    def __init__(self, options):
        flat = []
        for o in options:
            if isinstance(o, TUnion):
                flat.extend(o.options)
            else:
                flat.append(o)
        self.options = flat

    def validate(self, v, lenient=False, path="$"):
        for o in self.options:
            try:
                return o.validate(v, False, path)
            except ValidationError:
                pass
        first_error = None
        if lenient:
            for o in self.options:
                try:
                    return o.validate(v, True, path)
                except ValidationError as e:
                    if first_error is None or len(e.path) > len(first_error.path):
                        first_error = e
        if first_error is not None and len(first_error.path) > len(path):
            raise first_error
        raise ValidationError(path, "expected %s, got %s" % (self.describe(), brief(v)))

    def _non_null(self):
        return [o for o in self.options if not (isinstance(o, TPrim) and o.kind == "null")
                and not (isinstance(o, TLit) and o.value is None)]

    def _schema(self, ctx):
        opts = self.options
        if all(isinstance(o, TLit) for o in opts):
            values = [o.value for o in opts]
            if all(isinstance(x, str) for x in values):
                return {"type": "string", "enum": values}
            return {"enum": values}
        return {"anyOf": [o._schema(ctx) for o in opts]}

    def describe(self):
        inner = self._non_null()
        if len(inner) == 1 and len(self.options) == 2:
            d = inner[0].describe()
            return ("(%s)?" % d) if " | " in d else d + "?"
        return " | ".join(o.describe() for o in self.options)

    def sample(self, hint="value", depth=0):
        inner = self._non_null()
        if len(inner) < len(self.options) and (depth >= MAX_SAMPLE_DEPTH - 1 or not inner):
            return None
        return (inner or self.options)[0].sample(hint, depth)

    def accepts_null(self):
        return any(o.accepts_null() for o in self.options)


class TWhere(Type):
    """A refinement: ``int where it >= 1``."""

    def __init__(self, base, pred, src):
        self.base = base
        self.pred = pred
        self.src = src

    def validate(self, v, lenient=False, path="$"):
        v = self.base.validate(v, lenient, path)
        if not self.pred(v):
            raise ValidationError(path, "%s does not satisfy `%s`" % (brief(v), self.src))
        return v

    def _schema(self, ctx):
        s = dict(self.base._schema(ctx))
        note = "Must satisfy: %s (where `it` is this value)." % self.src
        s["description"] = note if "description" not in s else "%s %s" % (s["description"], note)
        return s

    def describe(self):
        return "%s where %s" % (self.base.describe(), self.src)

    def sample(self, hint="value", depth=0):
        base = self.base.sample(hint, depth)
        candidates = [base]
        root = self.base.resolved()
        if isinstance(root, TPrim) and root.kind in ("int", "float"):
            candidates += [1, 5, 10, 50, 100, 1000, -1, 0.5]
            if root.kind == "float":
                candidates = [float(c) if not isinstance(c, float) else c for c in candidates]
        elif isinstance(root, TPrim) and root.kind == "string":
            candidates += ["sample", "sample text for " + hint, "a", "abc", "ABC", "2026-01-01",
                           "2026-01-01T00:00:00Z", "user@example.com", "https://example.com", "12345",
                           "A longer sample sentence about %s." % hint]
        elif isinstance(root, TList):
            one = root.elem.sample(hint, depth + 1)
            candidates += [[one], [one, one], [one, one, one], []]
        for c in candidates:
            try:
                if self.check(c):
                    return self.validate(c)
            except Exception:
                continue
        raise ValidationError("$", "cannot invent a sample value satisfying `%s`; mock this call explicitly" % self.src)

    def resolved(self):
        return self.base.resolved()

    def accepts_null(self):
        return self.base.accepts_null()


class TAlias(Type):
    """A named type: the result of a ``type Name = ...`` declaration."""

    def __init__(self, name, target, doc=None):
        self.name = name
        self.target = target
        self.doc = doc

    def validate(self, v, lenient=False, path="$"):
        return self.target.validate(v, lenient, path)

    def _schema(self, ctx):
        if self.name in ctx["stack"]:
            ctx["recursive"].add(self.name)
            return {"$ref": "#/$defs/%s" % self.name}
        ctx["stack"].append(self.name)
        s = dict(self.target._schema(ctx))
        ctx["stack"].pop()
        if self.doc and "description" not in s:
            s["description"] = self.doc
        if self.name in ctx["recursive"]:
            ctx["defs"][self.name] = s
            return {"$ref": "#/$defs/%s" % self.name}
        return s

    def describe(self):
        return self.name

    def expand(self):
        return self.target.describe()

    def sample(self, hint="value", depth=0):
        return self.target.sample(hint, depth)

    def resolved(self):
        return self.target.resolved()

    def accepts_null(self):
        return self.target.accepts_null()


class TRef(Type):
    """A by-name reference, resolved lazily so types can be recursive and order-independent."""

    def __init__(self, name, resolver):
        self.name = name
        self.resolver = resolver
        self._target = None

    def target(self):
        if self._target is None:
            self._target = self.resolver(self.name)
        return self._target

    def validate(self, v, lenient=False, path="$"):
        return self.target().validate(v, lenient, path)

    def _schema(self, ctx):
        return self.target()._schema(ctx)

    def describe(self):
        return self.name

    def expand(self):
        return self.target().expand()

    def sample(self, hint="value", depth=0):
        return self.target().sample(hint, depth)

    def resolved(self):
        return self.target().resolved()

    def accepts_null(self):
        return self.target().accepts_null()


def optional(t):
    return TUnion([t, T_NULL])


def to_type(v):
    """Coerce a runtime value into a type: ``[string]`` and ``{a: int}`` work as inline types."""
    if isinstance(v, Type):
        return v
    if v is None:
        return T_NULL
    if isinstance(v, (str, bool, int, float)):
        return TLit(v)
    if isinstance(v, list) and len(v) == 1:
        return TList(to_type(v[0]))
    if isinstance(v, dict):
        return TRecord([Field(k, to_type(x)) for k, x in v.items()])
    raise ValidationError("$", "%s is not a type" % brief(v))


def is_string_type(t):
    r = t.resolved()
    return isinstance(r, TPrim) and r.kind == "string"


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)


def json_candidates(text):
    """Every plausible JSON value hiding in a model reply, most trustworthy first."""
    out = []
    stripped = text.strip()
    try:
        out.append(json.loads(stripped))
    except ValueError:
        pass
    for m in _FENCE.finditer(text):
        try:
            out.append(json.loads(m.group(1).strip()))
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text, m.start())
            out.append(value)
            break
        except ValueError:
            continue
    return out


def parse_model_output(t, text):
    """Turn raw model text into a value of type ``t`` or raise ValidationError."""
    first_error = None
    for cand in json_candidates(text):
        try:
            return t.validate(cand, lenient=True)
        except ValidationError as e:
            first_error = first_error or e
    try:
        return t.validate(text.strip().strip("`").strip(), lenient=True)
    except ValidationError as e:
        if first_error is None:
            preview = text.strip().replace("\n", " ")
            if len(preview) > 80:
                preview = preview[:79] + "…"
            first_error = ValidationError("$", "no valid JSON found in reply %r (%s)" % (preview, e.message))
    raise first_error
