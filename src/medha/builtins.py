"""Medha's standard library: global functions and the methods on strings, lists and records."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import ai
from .errors import ExitSignal, MedhaError
from .types import PRIMS, T_NULL, Type, ValidationError, parse_model_output, to_type, type_name_of
from .values import Builtin, Chat, ModelValue, show, to_json, truthy


def _need(cond, message, kind="TypeError"):
    if not cond:
        raise MedhaError(message, kind=kind)


def _int(v, what):
    _need(isinstance(v, int) and not isinstance(v, bool), "%s must be an int, got %s" % (what, type_name_of(v)))
    return v


def _num(v, what):
    _need(isinstance(v, (int, float)) and not isinstance(v, bool), "%s must be a number, got %s" % (what, type_name_of(v)))
    return v


def _str(v, what):
    _need(isinstance(v, str), "%s must be a string, got %s" % (what, type_name_of(v)))
    return v


def _list(v, what):
    _need(isinstance(v, list), "%s must be a list, got %s" % (what, type_name_of(v)))
    return v


def _equal(a, b):
    return isinstance(a, bool) == isinstance(b, bool) and a == b


# ---- string methods ----------------------------------------------------

def s_split(interp, s, sep=None):
    return s.split() if sep is None else (list(s) if sep == "" else s.split(_str(sep, "separator")))


def s_slice(interp, s, start=0, end=None):
    return s[start:end]


def s_truncate(interp, s, n, suffix="…"):
    _int(n, "length")
    return s if len(s) <= n else s[:max(n - len(suffix), 0)] + suffix


def s_chunk(interp, s, size, overlap=0):
    """Split text into pieces of about ``size`` characters, breaking on whitespace where possible."""
    _int(size, "chunk size")
    _int(overlap, "overlap")
    _need(size > 0 and 0 <= overlap < size, "chunk size must be positive and overlap smaller than size", "ValueError")
    out = []
    i = 0
    n = len(s)
    while i < n:
        end = min(i + size, n)
        if end < n:
            cut = max(s.rfind(" ", i, end), s.rfind("\n", i, end))
            if cut > i + size // 2:
                end = cut
        piece = s[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return out


STRING_METHODS = {
    "len": lambda interp, s: len(s),
    "upper": lambda interp, s: s.upper(),
    "lower": lambda interp, s: s.lower(),
    "trim": lambda interp, s: s.strip(),
    "split": s_split,
    "lines": lambda interp, s: s.splitlines(),
    "words": lambda interp, s: s.split(),
    "chars": lambda interp, s: list(s),
    "contains": lambda interp, s, sub: _str(sub, "contains() argument") in s,
    "starts_with": lambda interp, s, p: s.startswith(_str(p, "prefix")),
    "ends_with": lambda interp, s, p: s.endswith(_str(p, "suffix")),
    "replace": lambda interp, s, a, b: s.replace(_str(a, "pattern"), _str(b, "replacement")),
    "find": lambda interp, s, sub: s.find(_str(sub, "find() argument")),
    "slice": s_slice,
    "repeat": lambda interp, s, n: s * _int(n, "count"),
    "is_empty": lambda interp, s: len(s) == 0,
    "truncate": s_truncate,
    "matches": lambda interp, s, pattern: re.search(_str(pattern, "pattern"), s) is not None,
    "find_all": lambda interp, s, pattern: [m.group(0) for m in re.finditer(_str(pattern, "pattern"), s)],
    "chunk": s_chunk,
    "tokens": lambda interp, s: ai.estimate_tokens(s),
    "pad_left": lambda interp, s, n, ch=" ": s.rjust(_int(n, "width"), _str(ch, "pad")[:1] or " "),
    "pad_right": lambda interp, s, n, ch=" ": s.ljust(_int(n, "width"), _str(ch, "pad")[:1] or " "),
}


# ---- list methods ------------------------------------------------------

def l_reduce(interp, xs, f, init=None):
    acc = init
    items = xs
    if init is None and xs:
        acc, items = xs[0], xs[1:]
    for x in items:
        acc = interp.call(f, [acc, x])
    return acc


def l_each(interp, xs, f):
    for x in xs:
        interp.call(f, [x])
    return xs


def l_push(interp, xs, *items):
    xs.extend(items)
    return xs


def l_pop(interp, xs):
    _need(xs, "pop() on an empty list", "IndexError")
    return xs.pop()


def _sort_key(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, v)
    if isinstance(v, str):
        return (1, v)
    if isinstance(v, list):
        return (2, [_sort_key(x) for x in v])
    return (3, to_json(v))


def l_sort(interp, xs, key=None, desc=False):
    keyed = (lambda x: _sort_key(interp.call(key, [x]))) if key is not None else _sort_key
    return sorted(xs, key=keyed, reverse=truthy(desc))


def l_min(interp, xs, key=None):
    _need(xs, "min() of an empty list", "ValueError")
    return min(xs, key=(lambda x: _sort_key(interp.call(key, [x]))) if key is not None else _sort_key)


def l_max(interp, xs, key=None):
    _need(xs, "max() of an empty list", "ValueError")
    return max(xs, key=(lambda x: _sort_key(interp.call(key, [x]))) if key is not None else _sort_key)


def l_find(interp, xs, f):
    for x in xs:
        if truthy(interp.call(f, [x])):
            return x
    return None


def l_flat(interp, xs):
    out = []
    for x in xs:
        if isinstance(x, list):
            out.extend(x)
        else:
            out.append(x)
    return out


def l_unique(interp, xs):
    out = []
    for x in xs:
        if not any(_equal(x, y) for y in out):
            out.append(x)
    return out


def l_group_by(interp, xs, f):
    out = {}
    for x in xs:
        out.setdefault(show(interp.call(f, [x])), []).append(x)
    return out


def l_chunk(interp, xs, n):
    _need(_int(n, "chunk size") > 0, "chunk size must be positive", "ValueError")
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def l_index_of(interp, xs, item):
    for i, x in enumerate(xs):
        if _equal(x, item):
            return i
    return -1


def l_count(interp, xs, f=None):
    return len(xs) if f is None else sum(1 for x in xs if truthy(interp.call(f, [x])))


def l_join(interp, xs, sep=""):
    return _str(sep, "separator").join(show(x) for x in xs)


def l_sum(interp, xs):
    total = 0
    for x in xs:
        total += _num(x, "sum() item")
    return total


LIST_METHODS = {
    "len": lambda interp, xs: len(xs),
    "map": lambda interp, xs, f: [interp.call(f, [x]) for x in xs],
    "filter": lambda interp, xs, f: [x for x in xs if truthy(interp.call(f, [x]))],
    "reduce": l_reduce,
    "each": l_each,
    "join": l_join,
    "push": l_push,
    "pop": l_pop,
    "contains": lambda interp, xs, item: any(_equal(item, x) for x in xs),
    "first": lambda interp, xs: xs[0] if xs else None,
    "last": lambda interp, xs: xs[-1] if xs else None,
    "slice": lambda interp, xs, start=0, end=None: xs[start:end],
    "take": lambda interp, xs, n: xs[:max(_int(n, "count"), 0)],
    "drop": lambda interp, xs, n: xs[max(_int(n, "count"), 0):],
    "sort": l_sort,
    "reverse": lambda interp, xs: xs[::-1],
    "sum": l_sum,
    "min": l_min,
    "max": l_max,
    "find": l_find,
    "any": lambda interp, xs, f=None: any(truthy(interp.call(f, [x]) if f is not None else x) for x in xs),
    "all": lambda interp, xs, f=None: all(truthy(interp.call(f, [x]) if f is not None else x) for x in xs),
    "flat": l_flat,
    "flat_map": lambda interp, xs, f: l_flat(interp, [interp.call(f, [x]) for x in xs]),
    "zip": lambda interp, xs, ys: [[a, b] for a, b in zip(xs, _list(ys, "zip() argument"))],
    "enumerate": lambda interp, xs: [[i, x] for i, x in enumerate(xs)],
    "unique": l_unique,
    "chunk": l_chunk,
    "group_by": l_group_by,
    "count": l_count,
    "index_of": l_index_of,
    "concat": lambda interp, xs, ys: xs + _list(ys, "concat() argument"),
    "is_empty": lambda interp, xs: len(xs) == 0,
}


# ---- record methods ----------------------------------------------------

RECORD_METHODS = {
    "len": lambda interp, r: len(r),
    "keys": lambda interp, r: list(r.keys()),
    "values": lambda interp, r: list(r.values()),
    "entries": lambda interp, r: [[k, v] for k, v in r.items()],
    "has": lambda interp, r, k: k in r,
    "get": lambda interp, r, k, default=None: r.get(k, default),
    "merge": lambda interp, r, other: {**r, **other},
    "remove": lambda interp, r, *keys: {k: v for k, v in r.items() if k not in keys},
    "pick": lambda interp, r, *keys: {k: r[k] for k in keys if k in r},
    "map_values": lambda interp, r, f: {k: interp.call(f, [v]) for k, v in r.items()},
    "is_empty": lambda interp, r: len(r) == 0,
}


# ---- type / prompt / chat methods --------------------------------------

def t_parse(interp, t, text):
    try:
        return parse_model_output(t, _str(text, "parse() argument"))
    except ValidationError as e:
        raise MedhaError("cannot parse as %s: %s" % (t.describe(), e), kind="ValidationError")


def t_validate(interp, t, v):
    try:
        return t.validate(v, lenient=False)
    except ValidationError as e:
        raise MedhaError("not a valid %s: %s" % (t.describe(), e), kind="ValidationError")


def t_errors(interp, t, v):
    try:
        t.validate(v, lenient=False)
        return None
    except ValidationError as e:
        return str(e)


TYPE_METHODS = {
    "schema": lambda interp, t: t.schema(),
    "parse": t_parse,
    "check": lambda interp, t, v: t.check(v),
    "validate": t_validate,
    "errors": t_errors,
    "sample": lambda interp, t: t.sample(),
    "describe": lambda interp, t: t.expand(),
}

PROMPT_METHODS = {
    "render": lambda interp, p, *args, **kwargs: ai.render_prompt(interp, p, list(args), kwargs),
    "schema": lambda interp, p: (interp.mk_type(p.decl.ret, p.env).schema() if p.decl.ret is not None
                                 else {"type": "string"}),
}


def c_reset(interp, chat):
    chat.messages = []
    return chat


CHAT_METHODS = {
    "say": lambda interp, chat, text, ret=None, tools=None: ai.chat_say(interp, chat, _str(text, "message"), ret, tools),
    "reset": c_reset,
}

ALL_METHODS = set(STRING_METHODS) | set(LIST_METHODS) | set(RECORD_METHODS)
_dispatchers = {}


def dispatcher(name):
    """``map(xs, f)`` as a free function: forwards to the method on its first argument."""
    d = _dispatchers.get(name)
    if d is None:
        def call(interp, *args, **kwargs):
            _need(args, "%s() needs a value to work on" % name)
            m = interp.member(args[0], name)
            from .interp import MISSING
            if m is MISSING or isinstance(args[0], dict) and name in args[0]:
                fn = RECORD_METHODS.get(name) if isinstance(args[0], dict) else None
                _need(fn is not None, "%s() does not work on %s" % (name, type_name_of(args[0])))
                return fn(interp, *args, **kwargs)
            return interp.call(m, list(args[1:]), kwargs)
        d = _dispatchers[name] = Builtin(name, call)
    return d


# ---- global functions --------------------------------------------------

def b_print(interp, *args):
    interp.write(" ".join(show(a) for a in args) + "\n")


def b_eprint(interp, *args):
    sys.stderr.write(" ".join(show(a) for a in args) + "\n")


def b_range(interp, a, b=None, step=1):
    _int(a, "range start")
    if b is None:
        a, b = 0, a
    _need(_int(step, "range step") != 0, "range step cannot be 0", "ValueError")
    return list(range(a, _int(b, "range end"), step))


def b_from_json(interp, text):
    try:
        return json.loads(_str(text, "from_json() argument"))
    except ValueError as e:
        raise MedhaError("invalid JSON: %s" % e, kind="ValueError")


def b_read_file(interp, path):
    with open(_str(path, "path"), "r", encoding="utf-8") as f:
        return f.read()


def b_write_file(interp, path, text):
    with open(_str(path, "path"), "w", encoding="utf-8") as f:
        f.write(show(text))
    return None


def b_append_file(interp, path, text):
    with open(_str(path, "path"), "a", encoding="utf-8") as f:
        f.write(show(text))
    return None


def b_input(interp, prompt=""):
    if prompt:
        interp.write(show(prompt))
        (interp.out or sys.stdout).flush()
    line = sys.stdin.readline()
    return None if line == "" else line.rstrip("\n")


def b_fetch(interp, url, method="GET", headers=None, body=None, timeout=30):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = to_json(body).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        else:
            data = show(body).encode("utf-8")
    headers.setdefault("user-agent", "medha-lang")
    req = urllib.request.Request(_str(url, "url"), data=data, headers=headers, method=_str(method, "method").upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "body": resp.read().decode("utf-8", errors="replace"),
                    "headers": {k.lower(): v for k, v in resp.headers.items()}}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", errors="replace"),
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()}}
    except (urllib.error.URLError, OSError) as e:
        raise MedhaError("fetch failed for %s: %s" % (url, getattr(e, "reason", e)), kind="IOError")


def b_round(interp, x, digits=0):
    _num(x, "round() argument")
    return round(x) if digits == 0 else round(x, _int(digits, "digits"))


def b_min(interp, *args, **kwargs):
    if len(args) == 1 and isinstance(args[0], list):
        return l_min(interp, args[0], **kwargs)
    return l_min(interp, list(args), **kwargs)


def b_max(interp, *args, **kwargs):
    if len(args) == 1 and isinstance(args[0], list):
        return l_max(interp, args[0], **kwargs)
    return l_max(interp, list(args), **kwargs)


def b_error(interp, kind, message="", **extra):
    if not message:
        kind, message = "Error", kind
    return {"kind": _str(kind, "kind"), "message": show(message), **extra}


def b_usage(interp):
    with interp.rt.lock:
        u = dict(interp.rt.usage)
    u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    return u


def b_pmap(interp, xs, f, workers=8):
    """Map ``f`` over ``xs`` concurrently — the natural shape for fan-out model calls."""
    _list(xs, "pmap() first argument")
    _need(_int(workers, "workers") > 0, "workers must be positive", "ValueError")
    if len(xs) <= 1 or workers == 1:
        return [interp.call(f, [x]) for x in xs]
    with ThreadPoolExecutor(max_workers=min(workers, len(xs))) as pool:
        return list(pool.map(lambda x: interp.call(f, [x]), xs))


def b_cosine(interp, a, b):
    _list(a, "cosine() argument")
    _list(b, "cosine() argument")
    _need(len(a) == len(b), "cosine() needs vectors of equal length", "ValueError")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def b_chat(interp, model=None, system="", **options):
    m = ai.resolve_model(interp, model, "chat()")
    return Chat(m, _str(system, "system"), ai.merged_options(m, options, "chat()"))


def b_model(interp, spec, **options):
    from .providers import PROVIDERS
    m = ModelValue(_str(spec, "model spec"), options)
    _need(m.provider in PROVIDERS, "unknown model provider '%s' (known: %s)" % (m.provider, ", ".join(PROVIDERS)),
          "ModelError")
    ai.merged_options(m, None, "model()")
    return m


def b_assert_eq(interp, actual, expected, message=None):
    if not _equal(actual, expected):
        raise MedhaError("%s\n    actual:   %s\n    expected: %s" % (
            show(message) if message is not None else "values are not equal", to_json(actual), to_json(expected)),
            kind="AssertionError")


def b_assert_throws(interp, f, kind=None):
    try:
        interp.call(f, [])
    except MedhaError as e:
        if kind is not None and e.kind != kind:
            raise MedhaError("expected a %s error but got %s: %s" % (kind, e.kind, e.message), kind="AssertionError")
        return e.to_record()
    raise MedhaError("expected an error but none was thrown", kind="AssertionError")


def b_exit(interp, code=0):
    raise ExitSignal(_int(code, "exit code"))


def b_type_of(interp, v):
    return type_name_of(v)


def b_schema(interp, t):
    return to_type(t).schema()


def b_sleep(interp, seconds):
    time.sleep(max(_num(seconds, "seconds"), 0))


def b_env(interp, name, default=None):
    return os.environ.get(_str(name, "name"), default)


GLOBALS = {
    "print": b_print,
    "eprint": b_eprint,
    "str": lambda interp, v: show(v),
    "range": b_range,
    "type_of": b_type_of,
    "to_json": lambda interp, v, pretty=False: to_json(v, truthy(pretty)),
    "from_json": b_from_json,
    "read_file": b_read_file,
    "write_file": b_write_file,
    "append_file": b_append_file,
    "file_exists": lambda interp, path: os.path.exists(_str(path, "path")),
    "list_dir": lambda interp, path=".": sorted(os.listdir(_str(path, "path"))),
    "env": b_env,
    "args": lambda interp: list(interp.argv),
    "now": lambda interp: time.time(),
    "timestamp": lambda interp: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "sleep": b_sleep,
    "input": b_input,
    "fetch": b_fetch,
    "abs": lambda interp, x: abs(_num(x, "abs() argument")),
    "floor": lambda interp, x: math.floor(_num(x, "floor() argument")),
    "ceil": lambda interp, x: math.ceil(_num(x, "ceil() argument")),
    "round": b_round,
    "sqrt": lambda interp, x: math.sqrt(_num(x, "sqrt() argument")),
    "min": b_min,
    "max": b_max,
    "random": lambda interp: random.random(),
    "random_int": lambda interp, a, b: random.randint(_int(a, "low"), _int(b, "high")),
    "random_choice": lambda interp, xs: random.choice(_list(xs, "random_choice() argument")),
    "seed": lambda interp, n: random.seed(n),
    "uuid": lambda interp: str(_uuid.uuid4()),
    "hash": lambda interp, s: hashlib.sha256(show(s).encode("utf-8")).hexdigest(),
    "error": b_error,
    "exit": b_exit,
    "assert_eq": b_assert_eq,
    "assert_throws": b_assert_throws,
    # AI
    "usage": b_usage,
    "pmap": b_pmap,
    "embed": lambda interp, texts, model=None: ai.embed(interp, model, texts),
    "cosine": b_cosine,
    "tokens": lambda interp, text: ai.estimate_tokens(show(text)),
    "chat": b_chat,
    "model": b_model,
    "schema": b_schema,
}


def install(interp):
    g = interp.globals
    for name, fn in GLOBALS.items():
        g.define(name, Builtin(name, fn), True)
    for name, t in PRIMS.items():
        g.define(name, t, True)
    g.define("null_type", T_NULL, True)


__all__ = ["install", "GLOBALS", "STRING_METHODS", "LIST_METHODS", "RECORD_METHODS", "TYPE_METHODS",
           "PROMPT_METHODS", "CHAT_METHODS", "ALL_METHODS", "dispatcher", "Type"]
