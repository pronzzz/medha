"""Runtime values that are not plain data (functions, models, prompts, chats)."""
from __future__ import annotations

import json

from .types import Type


class Function:
    type_name = "function"

    def __init__(self, name, params, body, env, is_expr, ret, doc=None):
        self.name = name
        self.params = params
        self.body = body
        self.env = env
        self.is_expr = is_expr
        self.ret = ret
        self.doc = doc
        self.types = None  # lazily: ([param types], return type)

    def __repr__(self):
        return "<fn %s>" % (self.name or "anonymous")


class Builtin:
    type_name = "function"

    def __init__(self, name, fn, doc=None):
        self.name = name
        self.fn = fn
        self.doc = doc

    def __repr__(self):
        return "<fn %s>" % self.name


class ModelValue:
    type_name = "model"

    def __init__(self, spec, options=None, name=None):
        self.spec = spec
        self.name = name
        self.options = dict(options or {})
        if ":" in spec:
            self.provider, self.id = spec.split(":", 1)
        else:
            self.provider, self.id = spec, ""

    def __repr__(self):
        return "<model %s>" % self.spec


class PromptValue:
    type_name = "prompt"

    def __init__(self, decl, env):
        self.decl = decl
        self.env = env
        self.name = decl.name
        self.kind = decl.prompt_kind
        self.doc = decl.doc

    def __repr__(self):
        return "<%s %s>" % (self.kind, self.name)


class Chat:
    type_name = "chat"

    def __init__(self, model, system, options):
        self.model = model
        self.system = system
        self.options = options
        self.messages = []

    def __repr__(self):
        return "<chat with %s, %d messages>" % (self.model.spec, len(self.messages))


def _json_default(o):
    if isinstance(o, Type):
        return o.describe()
    return repr(o)


def to_json(v, pretty=False):
    if pretty:
        return json.dumps(v, ensure_ascii=False, indent=2, default=_json_default)
    return json.dumps(v, ensure_ascii=False, default=_json_default)


def show(v):
    """How a value prints at the top level: strings bare, everything else JSON-like."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, dict)):
        return to_json(v)
    if isinstance(v, Type):
        return v.describe()
    return repr(v)


def truthy(v):
    if v is None or v is False:
        return False
    if isinstance(v, (int, float, str, list, dict)):
        return bool(v)
    return True
