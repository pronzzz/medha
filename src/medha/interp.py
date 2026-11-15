"""Tree-walking evaluator for Medha."""
from __future__ import annotations

import difflib
import os
import re
import threading

from . import ai
from .ai import Runtime
from .errors import BreakSignal, ContinueSignal, ExitSignal, MedhaError, ReturnSignal
from .parser import parse
from .types import (PRIMS, T_NULL, Field, TAlias, TList, TLit, TMap, TRecord, TRef, TUnion, TWhere, Type,
                    ValidationError, optional, to_type, type_name_of)
from .values import Builtin, Chat, Function, ModelValue, PromptValue, show, to_json, truthy

MISSING = object()
MAX_DEPTH = 900


class Env:
    __slots__ = ("vars", "parent", "consts")

    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
        self.consts = None

    def lookup(self, name, default=MISSING):
        e = self
        while e is not None:
            if name in e.vars:
                return e.vars[name]
            e = e.parent
        return default

    def define(self, name, value, const=False):
        self.vars[name] = value
        if const:
            if self.consts is None:
                self.consts = set()
            self.consts.add(name)
        elif self.consts and name in self.consts:
            self.consts.discard(name)

    def assign(self, name, value):
        e = self
        while e is not None:
            if name in e.vars:
                if e.consts and name in e.consts:
                    raise MedhaError("cannot assign to '%s': it was declared with 'let' (use 'var' for a mutable binding)"
                                     % name, kind="TypeError")
                e.vars[name] = value
                return
            e = e.parent
        raise MedhaError("cannot assign to undefined variable '%s' (declare it with 'var')" % name, kind="NameError")

    def names(self):
        out = set()
        e = self
        while e is not None:
            out.update(e.vars)
            e = e.parent
        return out


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def equal(a, b):
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    try:
        return a == b
    except Exception:
        return a is b


class Interp:
    def __init__(self, rt=None, argv=None, out=None):
        from . import builtins as _b
        self.rt = rt or Runtime()
        self.argv = list(argv or [])
        self.out = out
        self.globals = Env()
        self.default_model = None
        self.tests = []
        self.modules = {}
        self.sources = {}
        self.import_depth = 0
        self.local = threading.local()
        self.methods = _b
        _b.install(self)
        self.dispatch = {name[2:]: getattr(self, name) for name in dir(self) if name.startswith("x_")}

    # ---- plumbing ------------------------------------------------------
    def new_env(self, parent):
        return Env(parent)

    def type_name(self, v):
        return type_name_of(v)

    def prompt_stack(self):
        st = getattr(self.local, "prompts", None)
        if st is None:
            st = self.local.prompts = []
        return st

    def call_stack(self):
        st = getattr(self.local, "calls", None)
        if st is None:
            st = self.local.calls = []
        return st

    def write(self, text):
        import sys
        (self.out or sys.stdout).write(text)

    def eval(self, n, env):
        try:
            return self.dispatch[n.kind](n, env)
        except MedhaError as e:
            if e.pos is None:
                e.pos = n.pos
            raise
        except ValidationError as e:
            raise MedhaError(str(e), n.pos, "ValidationError")
        except RecursionError:
            raise MedhaError("stack overflow: recursion is too deep", n.pos, "StackOverflow")

    def exec_block(self, stmts, env):
        self.hoist(stmts, env)
        last = None
        for s in stmts:
            last = self.eval(s, env)
        return last

    def hoist(self, stmts, env):
        for s in stmts:
            k = s.kind
            if k == "FnDecl":
                env.define(s.name, Function(s.name, s.params, s.body, env, s.is_expr, s.ret, s.doc), True)
            elif k == "PromptDecl":
                env.define(s.name, PromptValue(s, env), True)
            elif k == "TypeDecl":
                env.define(s.name, TAlias(s.name, self.mk_type(s.type, env), s.doc), True)

    # ---- running files -------------------------------------------------
    def run_source(self, src, file="<input>", env=None):
        self.sources[file] = src
        tree = parse(src, file)
        env = env or Env(self.globals)
        self.exec_block(tree.stmts, env)
        return env

    def run_file(self, path):
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        return self.run_source(src, path)

    def run_main(self, env):
        main = env.vars.get("main")
        if isinstance(main, Function):
            return self.call(main, [self.argv] if main.params else [])
        return None

    def load_module(self, path, pos):
        path = os.path.abspath(path)
        if path in self.modules:
            if self.modules[path] is None:
                raise MedhaError("circular import of %s" % path, pos, "ImportError")
            return self.modules[path]
        if not os.path.isfile(path):
            raise MedhaError("cannot import %s: file not found" % path, pos, "ImportError")
        self.modules[path] = None
        self.import_depth += 1
        try:
            env = self.run_file(path)
        finally:
            self.import_depth -= 1
            if self.modules.get(path) is None:
                self.modules.pop(path, None)
        self.modules[path] = env
        return env

    def run_tests(self, only=None):
        """Run registered ``test`` blocks. Returns a list of ``(name, error or None, seconds)``."""
        import time
        results = []
        saved_default = self.default_model
        for name, node, env in self.tests:
            if only and only not in name:
                continue
            self.rt.reset_for_test()
            self.default_model = saved_default
            start = time.time()
            try:
                self.exec_block(node.body.stmts, Env(env))
                results.append((name, None, time.time() - start))
            except MedhaError as e:
                results.append((name, e, time.time() - start))
            except (ReturnSignal, BreakSignal, ContinueSignal):
                results.append((name, MedhaError("'return', 'break' or 'continue' escaped the test body", node.pos),
                                time.time() - start))
        self.rt.reset_for_test()
        return results

    # ---- types ---------------------------------------------------------
    def mk_type(self, n, env):
        k = n.kind
        if k == "TName":
            if n.name in PRIMS:
                return PRIMS[n.name]

            def resolve(name, env=env, pos=n.pos):
                v = env.lookup(name)
                if isinstance(v, Type):
                    return v
                if v is MISSING:
                    raise MedhaError("unknown type '%s'" % name, pos, "NameError")
                raise MedhaError("'%s' is a %s, not a type" % (name, type_name_of(v)), pos, "TypeError")
            return TRef(n.name, resolve)
        if k == "TLit":
            return T_NULL if n.value is None else TLit(n.value)
        if k == "TList":
            return TList(self.mk_type(n.elem, env))
        if k == "TMap":
            return TMap(self.mk_type(n.value, env))
        if k == "TOpt":
            return optional(self.mk_type(n.inner, env))
        if k == "TUnion":
            return TUnion([self.mk_type(o, env) for o in n.options])
        if k == "TRecord":
            fields = []
            for f in n.fields:
                t = self.mk_type(f["type"], env)
                if f["optional"]:
                    t = optional(t)
                default = None
                if f["default"] is not None:
                    default = (lambda node=f["default"]: self.eval(node, env))
                fields.append(Field(f["name"], t, f["optional"], default, f["doc"]))
            return TRecord(fields)
        if k == "TWhere":
            def pred(v, node=n.expr):
                scope = Env(env)
                scope.define("it", v)
                return truthy(self.eval(node, scope))
            return TWhere(self.mk_type(n.base, env), pred, n.src)
        raise MedhaError("not a type", n.pos)

    def fn_types(self, f):
        if f.types is None:
            f.types = ([self.mk_type(p.type, f.env) if p.type is not None else None for p in f.params],
                       self.mk_type(f.ret, f.env) if f.ret is not None else None)
        return f.types

    # ---- calling -------------------------------------------------------
    def bind_params(self, params, types, args, kwargs, env, name):
        if len(args) > len(params):
            raise MedhaError("%s() takes %d argument(s) but got %d" % (name or "function", len(params), len(args)),
                             kind="TypeError")
        kwargs = dict(kwargs) if kwargs else {}
        for i, p in enumerate(params):
            if i < len(args):
                if p.name in kwargs:
                    raise MedhaError("%s() got '%s' twice" % (name or "function", p.name), kind="TypeError")
                v = args[i]
            elif p.name in kwargs:
                v = kwargs.pop(p.name)
            elif p.default is not None:
                v = self.eval(p.default, env)
            else:
                raise MedhaError("%s() is missing argument '%s'" % (name or "function", p.name), kind="TypeError")
            t = types[i]
            if t is not None:
                try:
                    v = t.validate(v, False, p.name)
                except ValidationError as e:
                    raise MedhaError("%s(): argument %s" % (name or "function", e), kind="TypeError")
            env.vars[p.name] = v
        if kwargs:
            raise MedhaError("%s() has no parameter named '%s'" % (name or "function", next(iter(kwargs))),
                             kind="TypeError")

    def call_function(self, f, args, kwargs, pos):
        stack = self.call_stack()
        if len(stack) >= MAX_DEPTH:
            raise MedhaError("stack overflow: more than %d nested calls" % MAX_DEPTH, pos, "StackOverflow")
        types, ret = self.fn_types(f)
        env = Env(f.env)
        self.bind_params(f.params, types, args, kwargs, env, f.name)
        stack.append((f.name or "<lambda>", pos))
        try:
            if f.is_expr:
                result = self.eval(f.body, env)
            else:
                result = self.exec_block(f.body.stmts, env)
        except ReturnSignal as s:
            result = s.value
        except MedhaError as e:
            if e.stack is None:
                e.stack = list(stack)
            raise
        finally:
            stack.pop()
        if ret is not None:
            try:
                result = ret.validate(result, False, "return value")
            except ValidationError as e:
                raise MedhaError("%s(): %s" % (f.name or "function", e), pos, "TypeError")
        return result

    def call(self, f, args, kwargs=None, pos=None):
        kwargs = kwargs or {}
        if isinstance(f, Function):
            return self.call_function(f, args, kwargs, pos)
        if isinstance(f, Builtin):
            try:
                return f.fn(self, *args, **kwargs)
            except (MedhaError, ReturnSignal, BreakSignal, ContinueSignal, ExitSignal):
                raise
            except ValidationError as e:
                raise MedhaError(str(e), pos, "ValidationError")
            except RecursionError:
                raise MedhaError("stack overflow: recursion is too deep", pos, "StackOverflow")
            except OSError as e:
                raise MedhaError("%s(): %s" % (f.name, e.strerror or e), pos, "IOError")
            except ZeroDivisionError:
                raise MedhaError("division by zero", pos, "MathError")
            except (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError) as e:
                raise MedhaError("%s(): %s" % (f.name, _clean_py_error(f.name, e)), pos, "TypeError")
        if isinstance(f, PromptValue):
            return ai.run_prompt(self, f, args, kwargs)
        if isinstance(f, ModelValue):
            return ai.call_model(self, f, args, kwargs)
        if isinstance(f, Type):
            if len(args) != 1 or kwargs:
                raise MedhaError("a type converts exactly one value: %s(value)" % f.describe(), pos, "TypeError")
            try:
                return f.validate(args[0], lenient=True)
            except ValidationError as e:
                raise MedhaError("cannot convert to %s: %s" % (f.describe(), e), pos, "ValidationError")
        raise MedhaError("%s is not callable" % type_name_of(f), pos, "TypeError")

    def member(self, obj, name):
        """Field or method ``name`` of ``obj``, or MISSING."""
        m = self.methods
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
            fn = m.RECORD_METHODS.get(name)
        elif isinstance(obj, str):
            fn = m.STRING_METHODS.get(name)
        elif isinstance(obj, list):
            fn = m.LIST_METHODS.get(name)
        elif isinstance(obj, Type):
            if name == "name":
                return obj.describe()
            if name == "doc":
                return getattr(obj, "doc", None)
            fn = m.TYPE_METHODS.get(name)
        elif isinstance(obj, ModelValue):
            if name in ("name", "spec", "provider", "id", "options"):
                return getattr(obj, name)
            return MISSING
        elif isinstance(obj, PromptValue):
            if name in ("name", "kind", "doc"):
                return getattr(obj, name)
            fn = m.PROMPT_METHODS.get(name)
        elif isinstance(obj, Chat):
            if name == "history":
                return [{"role": x["role"], "content": x.get("content") or ""} for x in obj.messages]
            if name == "model":
                return obj.model
            fn = m.CHAT_METHODS.get(name)
        elif isinstance(obj, (Function, Builtin)):
            if name in ("name", "doc"):
                return getattr(obj, name)
            return MISSING
        else:
            return MISSING
        if fn is None:
            return MISSING
        return Builtin(name, lambda interp, *a, **k: fn(interp, obj, *a, **k))

    def no_member(self, obj, name, pos):
        tn = type_name_of(obj)
        if obj is None:
            return MedhaError("cannot read '%s' of null (use ?. to allow null)" % name, pos, "TypeError")
        if isinstance(obj, dict):
            keys = list(obj)
            close = difflib.get_close_matches(name, keys, 1)
            hint = " — did you mean '%s'?" % close[0] if close else (
                " (fields: %s)" % ", ".join(keys[:8]) if keys else " (the record is empty)")
            return MedhaError("record has no field '%s'%s" % (name, hint), pos, "FieldError")
        return MedhaError("%s has no field or method '%s'" % (tn, name), pos, "FieldError")

    # ---- literals & names ----------------------------------------------
    def x_Num(self, n, env):
        return n.value

    def x_Bool(self, n, env):
        return n.value

    def x_Null(self, n, env):
        return None

    def x_Str(self, n, env):
        parts = n.parts
        if len(parts) == 1 and isinstance(parts[0], str):
            return parts[0]
        return "".join(p if isinstance(p, str) else show(self.eval(p, env)) for p in parts)

    def x_Ident(self, n, env):
        v = env.lookup(n.name)
        if v is MISSING:
            if n.name in self.methods.ALL_METHODS:
                return self.methods.dispatcher(n.name)
            close = difflib.get_close_matches(n.name, list(env.names()) + list(self.methods.ALL_METHODS), 1)
            hint = " — did you mean '%s'?" % close[0] if close else ""
            raise MedhaError("'%s' is not defined%s" % (n.name, hint), n.pos, "NameError")
        return v

    def x_List(self, n, env):
        out = []
        for item in n.items:
            if item.kind == "Spread":
                v = self.eval(item.value, env)
                if not isinstance(v, list):
                    raise MedhaError("can only spread a list into a list, got %s" % type_name_of(v), item.pos, "TypeError")
                out.extend(v)
            else:
                out.append(self.eval(item, env))
        return out

    def x_Record(self, n, env):
        out = {}
        for kind, key, value in n.entries:
            if kind == "kv":
                out[key] = self.eval(value, env)
            elif kind == "computed":
                k = self.eval(key, env)
                if not isinstance(k, str):
                    k = show(k)
                out[k] = self.eval(value, env)
            else:
                v = self.eval(value, env)
                if not isinstance(v, dict):
                    raise MedhaError("can only spread a record into a record, got %s" % type_name_of(v),
                                     value.pos, "TypeError")
                out.update(v)
        return out

    def x_Lambda(self, n, env):
        return Function(n.name, n.params, n.body, env, n.is_expr, n.ret)

    # ---- operators -----------------------------------------------------
    def x_Unary(self, n, env):
        v = self.eval(n.operand, env)
        if n.op == "not":
            return not truthy(v)
        if not is_num(v):
            raise MedhaError("cannot negate %s" % type_name_of(v), n.pos, "TypeError")
        return -v

    def x_Logical(self, n, env):
        left = self.eval(n.left, env)
        if n.op == "and":
            return self.eval(n.right, env) if truthy(left) else left
        return left if truthy(left) else self.eval(n.right, env)

    def x_Coalesce(self, n, env):
        left = self.eval(n.left, env)
        return self.eval(n.right, env) if left is None else left

    def x_Binary(self, n, env):
        return self.binop(n.op, self.eval(n.left, env), self.eval(n.right, env), n.pos)

    def binop(self, op, a, b, pos):
        if op == "==":
            return equal(a, b)
        if op == "!=":
            return not equal(a, b)
        if op == "in":
            if isinstance(b, list):
                return any(equal(a, x) for x in b)
            if isinstance(b, dict):
                return a in b if isinstance(a, str) else False
            if isinstance(b, str) and isinstance(a, str):
                return a in b
            raise MedhaError("'in' needs a list, record or string on the right, got %s" % type_name_of(b), pos, "TypeError")
        if op == "+":
            if is_num(a) and is_num(b):
                return a + b
            if isinstance(a, str) and isinstance(b, str):
                return a + b
            if isinstance(a, list) and isinstance(b, list):
                return a + b
            if isinstance(a, str) or isinstance(b, str):
                raise MedhaError('cannot add %s and %s — use interpolation ("{a}{b}") or str() to build text'
                                 % (type_name_of(a), type_name_of(b)), pos, "TypeError")
        elif op in ("<", "<=", ">", ">="):
            if (is_num(a) and is_num(b)) or (isinstance(a, str) and isinstance(b, str)) \
                    or (isinstance(a, list) and isinstance(b, list)):
                try:
                    if op == "<":
                        return a < b
                    if op == "<=":
                        return a <= b
                    if op == ">":
                        return a > b
                    return a >= b
                except TypeError:
                    pass
            raise MedhaError("cannot compare %s with %s" % (type_name_of(a), type_name_of(b)), pos, "TypeError")
        elif is_num(a) and is_num(b):
            try:
                if op == "-":
                    return a - b
                if op == "*":
                    return a * b
                if op == "/":
                    return a / b
                if op == "%":
                    return a % b
                if op == "**":
                    return a ** b
            except ZeroDivisionError:
                raise MedhaError("division by zero", pos, "MathError")
            except OverflowError:
                raise MedhaError("number too large", pos, "MathError")
        elif op == "*" and isinstance(a, str) and isinstance(b, int) and not isinstance(b, bool):
            return a * b
        raise MedhaError("operator '%s' does not work on %s and %s" % (op, type_name_of(a), type_name_of(b)),
                         pos, "TypeError")

    def x_Range(self, n, env):
        lo, hi = self.eval(n.lo, env), self.eval(n.hi, env)
        if not (isinstance(lo, int) and isinstance(hi, int)) or isinstance(lo, bool) or isinstance(hi, bool):
            raise MedhaError("range bounds must be integers", n.pos, "TypeError")
        return list(range(lo, hi))

    def x_Is(self, n, env):
        return self.mk_type(n.type, env).check(self.eval(n.value, env))

    def x_As(self, n, env):
        t = self.mk_type(n.type, env)
        v = self.eval(n.value, env)
        try:
            return t.validate(v, lenient=True)
        except ValidationError as e:
            raise MedhaError("value is not a valid %s: %s" % (t.describe(), e), n.pos, "ValidationError")

    # ---- access --------------------------------------------------------
    def x_Field(self, n, env):
        obj = self.eval(n.obj, env)
        v = self.member(obj, n.name)
        if v is MISSING:
            if n.optional:
                return None
            raise self.no_member(obj, n.name, n.pos)
        return v

    def x_Index(self, n, env):
        obj = self.eval(n.obj, env)
        idx = self.eval(n.index, env)
        if isinstance(obj, (list, str)):
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise MedhaError("%s index must be an int, got %s" % (type_name_of(obj), type_name_of(idx)), n.pos, "TypeError")
            if not -len(obj) <= idx < len(obj):
                raise MedhaError("index %d is out of range for a %s of length %d" % (idx, type_name_of(obj), len(obj)),
                                 n.pos, "IndexError")
            return obj[idx]
        if isinstance(obj, dict):
            if not isinstance(idx, str):
                raise MedhaError("record keys are strings, got %s" % type_name_of(idx), n.pos, "TypeError")
            return obj.get(idx)
        raise MedhaError("cannot index into %s" % type_name_of(obj), n.pos, "TypeError")

    def x_Slice(self, n, env):
        obj = self.eval(n.obj, env)
        lo = self.eval(n.lo, env) if n.lo is not None else None
        hi = self.eval(n.hi, env) if n.hi is not None else None
        if not isinstance(obj, (list, str)):
            raise MedhaError("cannot slice %s" % type_name_of(obj), n.pos, "TypeError")
        for b in (lo, hi):
            if b is not None and (not isinstance(b, int) or isinstance(b, bool)):
                raise MedhaError("slice bounds must be ints", n.pos, "TypeError")
        return obj[lo:hi]

    def eval_args(self, arg_nodes, env):
        args = []
        kwargs = {}
        for kind, name, node in arg_nodes:
            if kind == "pos":
                args.append(self.eval(node, env))
            elif kind == "named":
                kwargs[name] = self.eval(node, env)
            else:
                v = self.eval(node, env)
                if isinstance(v, list):
                    args.extend(v)
                elif isinstance(v, dict):
                    kwargs.update(v)
                else:
                    raise MedhaError("can only spread a list or record into arguments", node.pos, "TypeError")
        return args, kwargs

    def x_Call(self, n, env, piped=MISSING):
        callee = n.callee
        if callee.kind == "Field":
            obj = self.eval(callee.obj, env)
            f = self.member(obj, callee.name)
            if f is MISSING:
                if obj is None and callee.optional:
                    return None
                g = env.lookup(callee.name)
                if g is MISSING or not isinstance(g, (Function, Builtin, PromptValue)):
                    raise self.no_member(obj, callee.name, callee.pos)
                args, kwargs = self.eval_args(n.args, env)
                lead = [obj] if piped is MISSING else [piped, obj]
                return self.call(g, lead + args, kwargs, n.pos)
        else:
            f = self.eval(callee, env)
        args, kwargs = self.eval_args(n.args, env)
        if piped is not MISSING:
            args.insert(0, piped)
        return self.call(f, args, kwargs, n.pos)

    def x_Pipe(self, n, env):
        left = self.eval(n.left, env)
        right = n.right
        if right.kind == "Call":
            try:
                return self.x_Call(right, env, piped=left)
            except MedhaError as e:
                if e.pos is None:
                    e.pos = right.pos
                raise
        return self.call(self.eval(right, env), [left], None, n.pos)

    # ---- control flow --------------------------------------------------
    def x_Block(self, n, env):
        return self.exec_block(n.stmts, Env(env))

    def x_If(self, n, env):
        if truthy(self.eval(n.cond, env)):
            return self.exec_block(n.then.stmts, Env(env))
        if n.els is None:
            return None
        if n.els.kind == "If":
            return self.eval(n.els, env)
        return self.exec_block(n.els.stmts, Env(env))

    def x_Match(self, n, env):
        subject = self.eval(n.subject, env)
        for arm in n.arms:
            scope = Env(env)
            if not self.bind_pattern(arm.pattern, subject, scope):
                continue
            if arm.guard is not None and not truthy(self.eval(arm.guard, scope)):
                continue
            if arm.body.kind == "Block":
                return self.exec_block(arm.body.stmts, scope)
            return self.eval(arm.body, scope)
        raise MedhaError("no match arm matched %s (add a `_ -> ...` arm)" % show(subject)[:80], n.pos, "MatchError")

    def bind_pattern(self, p, v, env):
        k = p.kind
        if k == "PWild":
            return True
        if k == "PBind":
            env.define(p.name, v)
            return True
        if k == "PLit":
            return type(p.value) is type(v) and p.value == v or (is_num(p.value) and is_num(v) and p.value == v)
        if k == "PType":
            return self.mk_type(p.type, env).check(v)
        if k == "PAlt":
            return any(self.bind_pattern(o, v, env) for o in p.options)
        if k == "PRecord":
            if not isinstance(v, dict):
                return False
            for key, sub in p.fields:
                if key not in v or not self.bind_pattern(sub, v[key], env):
                    return False
            if p.rest:
                taken = {key for key, _ in p.fields}
                env.define(p.rest, {a: b for a, b in v.items() if a not in taken})
            return True
        if k == "PList":
            if not isinstance(v, list):
                return False
            if len(v) < len(p.items) or (not p.has_rest and len(v) != len(p.items)):
                return False
            for sub, item in zip(p.items, v):
                if not self.bind_pattern(sub, item, env):
                    return False
            if p.rest:
                env.define(p.rest, v[len(p.items):])
            return True
        return False

    def x_While(self, n, env):
        while truthy(self.eval(n.cond, env)):
            try:
                self.exec_block(n.body.stmts, Env(env))
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return None

    def x_For(self, n, env):
        it = self.eval(n.iterable, env)
        if isinstance(it, list):
            pairs = list(enumerate(it))
        elif isinstance(it, dict):
            pairs = [(k, v) for k, v in it.items()] if n.key else [(None, k) for k in it]
        elif isinstance(it, str):
            pairs = list(enumerate(it))
        else:
            raise MedhaError("cannot loop over %s" % type_name_of(it), n.iterable.pos, "TypeError")
        for key, value in pairs:
            scope = Env(env)
            if n.key:
                scope.define(n.key, key)
            scope.define(n.value, value)
            try:
                self.exec_block(n.body.stmts, scope)
            except BreakSignal:
                break
            except ContinueSignal:
                continue
        return None

    def x_Return(self, n, env):
        raise ReturnSignal(self.eval(n.value, env) if n.value is not None else None)

    def x_Break(self, n, env):
        raise BreakSignal()

    def x_Continue(self, n, env):
        raise ContinueSignal()

    def x_Try(self, n, env):
        try:
            try:
                return self.exec_block(n.body.stmts, Env(env))
            except MedhaError as e:
                if n.handler is None:
                    raise
                scope = Env(env)
                if n.name:
                    scope.define(n.name, e.to_record())
                return self.exec_block(n.handler.stmts, scope)
        finally:
            if n.final is not None:
                self.exec_block(n.final.stmts, Env(env))

    def x_Throw(self, n, env):
        v = self.eval(n.value, env)
        if isinstance(v, dict) and isinstance(v.get("message"), str):
            data = {k: x for k, x in v.items() if k not in ("kind", "message")}
            raise MedhaError(v["message"], n.pos, str(v.get("kind") or "Error"), data)
        if isinstance(v, str):
            raise MedhaError(v, n.pos, "Error")
        raise MedhaError(show(v), n.pos, "Error", {"value": v})

    def x_Assert(self, n, env):
        c = n.cond
        detail = ""
        if c.kind == "Binary" and c.op in ("==", "!=", "<", "<=", ">", ">=", "in"):
            left, right = self.eval(c.left, env), self.eval(c.right, env)
            ok = truthy(self.binop(c.op, left, right, c.pos))
            detail = "\n    left:  %s\n    right: %s" % (to_json(left), to_json(right))
        else:
            ok = truthy(self.eval(c, env))
        if not ok:
            msg = show(self.eval(n.msg, env)) if n.msg is not None else "assertion failed: %s" % n.src
            raise MedhaError(msg + detail, n.pos, "AssertionError")
        return None

    # ---- statements ----------------------------------------------------
    def x_ExprStmt(self, n, env):
        v = self.eval(n.expr, env)
        if n.emit and isinstance(v, str):
            stack = self.prompt_stack()
            if stack:
                stack[-1].add_user(v)
        return v

    def x_Let(self, n, env):
        v = self.eval(n.value, env)
        if n.pattern is not None:
            if not self.bind_pattern(n.pattern, v, env):
                raise MedhaError("value %s does not match the destructuring pattern" % show(v)[:60], n.pos, "MatchError")
            return None
        if n.type is not None:
            t = self.mk_type(n.type, env)
            try:
                v = t.validate(v, False, n.name)
            except ValidationError as e:
                raise MedhaError("%s (declared as %s)" % (e, t.describe()), n.pos, "TypeError")
        env.define(n.name, v, const=not n.mutable)
        return None

    def x_Assign(self, n, env):
        value = self.eval(n.value, env)
        t = n.target
        if n.op != "=":
            value = self.binop(n.op[0], self.eval(t, env), value, n.pos)
        if t.kind == "Ident":
            env.assign(t.name, value)
        elif t.kind == "Field":
            obj = self.eval(t.obj, env)
            if not isinstance(obj, dict):
                raise MedhaError("cannot set field '%s' on %s" % (t.name, type_name_of(obj)), n.pos, "TypeError")
            obj[t.name] = value
        else:
            obj = self.eval(t.obj, env)
            idx = self.eval(t.index, env)
            if isinstance(obj, list):
                if not isinstance(idx, int) or isinstance(idx, bool) or not -len(obj) <= idx < len(obj):
                    raise MedhaError("list index %s is out of range" % show(idx), n.pos, "IndexError")
                obj[idx] = value
            elif isinstance(obj, dict):
                if not isinstance(idx, str):
                    raise MedhaError("record keys are strings, got %s" % type_name_of(idx), n.pos, "TypeError")
                obj[idx] = value
            else:
                raise MedhaError("cannot assign into %s" % type_name_of(obj), n.pos, "TypeError")
        return None

    def x_FnDecl(self, n, env):
        if not isinstance(env.vars.get(n.name), Function) or env.vars[n.name].body is not n.body:
            env.define(n.name, Function(n.name, n.params, n.body, env, n.is_expr, n.ret, n.doc), True)
        return None

    def x_PromptDecl(self, n, env):
        cur = env.vars.get(n.name)
        if not isinstance(cur, PromptValue) or cur.decl is not n:
            env.define(n.name, PromptValue(n, env), True)
        return None

    def x_TypeDecl(self, n, env):
        if not isinstance(env.vars.get(n.name), TAlias):
            env.define(n.name, TAlias(n.name, self.mk_type(n.type, env), n.doc), True)
        return None

    def x_ModelDecl(self, n, env):
        from .providers import PROVIDERS
        spec = self.eval(n.spec, env)
        if isinstance(spec, ModelValue):
            spec = spec.spec
        if not isinstance(spec, str):
            raise MedhaError('a model is declared from a "provider:model-id" string, got %s' % type_name_of(spec),
                             n.pos, "TypeError")
        options = self.eval(n.options, env) if n.options is not None else {}
        mv = ModelValue(spec, options, n.name)
        if mv.provider not in PROVIDERS:
            raise MedhaError("unknown model provider '%s' in \"%s\" (known: %s)"
                             % (mv.provider, spec, ", ".join(PROVIDERS)), n.pos, "ModelError")
        ai.merged_options(mv, None, "model '%s'" % n.name)
        env.define(n.name, mv, True)
        if self.default_model is None:
            self.default_model = mv
        return None

    def x_Test(self, n, env):
        if self.import_depth == 0:
            self.tests.append((n.name, n, env))
        return None

    def x_Mock(self, n, env):
        target = self.eval(n.target, env)
        if not isinstance(target, (ModelValue, PromptValue)):
            raise MedhaError("only models, prompts and agents can be mocked; '%s' is a %s"
                             % (n.target.name, type_name_of(target)), n.pos, "TypeError")
        self.rt.add_mock(target, self.eval(n.value, env))
        return None

    def x_Budget(self, n, env):
        limits = {"tokens": None, "calls": None, "used_tokens": 0, "used_calls": 0}
        for key, node in n.limits.items():
            v = self.eval(node, env)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise MedhaError("budget %s must be a non-negative int" % key, node.pos, "TypeError")
            limits[key] = v
        with self.rt.lock:
            self.rt.budgets.append(limits)
        try:
            return self.exec_block(n.body.stmts, Env(env))
        finally:
            with self.rt.lock:
                if limits in self.rt.budgets:
                    self.rt.budgets.remove(limits)

    def x_Directive(self, n, env):
        stack = self.prompt_stack()
        if not stack:
            raise MedhaError("'%s' only works inside a prompt or agent body" % n.name, n.pos, "PromptError")
        ctx = stack[-1]
        v = self.eval(n.a, env)
        name = n.name
        if name == "system":
            ctx.system.append(ai.as_text(v))
        elif name == "user":
            ctx.add_user(ai.as_text(v))
        elif name == "assistant":
            ctx.messages.append({"role": "assistant", "content": ai.as_text(v)})
        elif name == "example":
            ctx.add_example(ai.as_text(v), ai.as_text(self.eval(n.b, env)))
        elif name == "tools":
            ctx.tools.extend(v if isinstance(v, list) else [v])
        elif name == "max_steps":
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise MedhaError("max_steps must be a positive int", n.pos, "TypeError")
            ctx.max_steps = v
        return None

    def x_Import(self, n, env):
        base = os.path.dirname(os.path.abspath(n.pos[0])) if os.path.isfile(n.pos[0]) else os.getcwd()
        path = n.path if os.path.isabs(n.path) else os.path.join(base, n.path)
        if not os.path.isfile(path) and os.path.isfile(path + ".mdh"):
            path += ".mdh"
        mod = self.load_module(path, n.pos)
        if n.alias:
            env.define(n.alias, dict(mod.vars), True)
        else:
            for k, v in mod.vars.items():
                env.define(k, v, bool(mod.consts and k in mod.consts))
        return None


def _clean_py_error(name, e):
    text = str(e)
    if isinstance(e, KeyError):
        return "missing key %s" % text
    text = re.sub(r"^\S*\(\) ", "", text)
    return text.replace("positional argument", "argument")
