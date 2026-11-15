"""Static checks that run before a program does.

The checker is deliberately conservative: it reports only things that are
certainly wrong (undefined names, unknown types, assignment to ``let``
bindings, wrong argument counts, misplaced ``return``/``break``), so a clean
check never blocks a valid program.
"""
from __future__ import annotations

import difflib
import os

from .builtins import ALL_METHODS, GLOBALS
from .errors import MedhaError
from .parser import Node, parse
from .types import PRIMS

BASE_NAMES = set(GLOBALS) | set(PRIMS) | set(ALL_METHODS) | {"null_type"}


class Scope:
    __slots__ = ("names", "open")

    def __init__(self):
        self.names = {}
        self.open = False


class Checker:
    def __init__(self, file, known=()):
        self.file = file
        self.errors = []
        self.scopes = [Scope()]
        for name in BASE_NAMES | set(known):
            self.scopes[0].names[name] = {"const": True}
        self.fn_depth = 0
        self.loop_depth = 0
        self.prompt_kind = None
        self._importing = set()

    # ---- helpers -------------------------------------------------------
    def error(self, message, pos, kind="CheckError"):
        self.errors.append(MedhaError(message, pos, kind))

    def push(self):
        self.scopes.append(Scope())

    def pop(self):
        self.scopes.pop()

    def declare(self, name, **info):
        info.setdefault("const", False)
        self.scopes[-1].names[name] = info

    def resolve(self, name):
        for s in reversed(self.scopes):
            if name in s.names:
                return s.names[name]
        return None

    def is_open(self):
        return any(s.open for s in self.scopes)

    def all_names(self):
        out = set()
        for s in self.scopes:
            out.update(s.names)
        return out

    def undefined(self, name, pos, what="'%s' is not defined"):
        if self.is_open():
            return
        close = difflib.get_close_matches(name, self.all_names(), 1)
        hint = " — did you mean '%s'?" % close[0] if close else ""
        self.error((what % name) + hint, pos, "NameError")

    def pattern_names(self, p, out):
        k = p.kind
        if k == "PBind":
            out.append(p.name)
        elif k == "PAlt":
            for o in p.options:
                self.pattern_names(o, out)
        elif k == "PRecord":
            for _key, sub in p.fields:
                self.pattern_names(sub, out)
            if p.rest:
                out.append(p.rest)
        elif k == "PList":
            for sub in p.items:
                self.pattern_names(sub, out)
            if p.rest:
                out.append(p.rest)
        return out

    def module_names(self, node):
        base = os.path.dirname(os.path.abspath(self.file)) if os.path.isfile(self.file) else os.getcwd()
        path = node.path if os.path.isabs(node.path) else os.path.join(base, node.path)
        if not os.path.isfile(path) and os.path.isfile(path + ".mdh"):
            path += ".mdh"
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self.error("cannot import \"%s\": file not found" % node.path, node.pos, "ImportError")
            return None
        if path in self._importing:
            return None
        self._importing.add(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = parse(f.read(), path)
        except (MedhaError, OSError):
            return None
        finally:
            self._importing.discard(path)
        names = []
        opened = False
        for s in tree.stmts:
            if s.kind == "Import" and not s.alias:
                opened = True
            names.extend(self.bound_names(s))
        return None if opened else names

    def bound_names(self, s):
        k = s.kind
        if k == "Let":
            return [s.name] if s.name else self.pattern_names(s.pattern, [])
        if k in ("FnDecl", "PromptDecl", "TypeDecl", "ModelDecl"):
            return [s.name]
        if k == "Import" and s.alias:
            return [s.alias]
        return []

    def predeclare(self, stmts):
        for s in stmts:
            k = s.kind
            if k == "Let":
                for name in self.bound_names(s):
                    self.declare(name, const=not s.mutable)
            elif k in ("FnDecl", "PromptDecl"):
                self.declare(s.name, const=True, params=s.params, what=k)
            elif k in ("TypeDecl", "ModelDecl"):
                self.declare(s.name, const=True)
            elif k == "Import":
                if s.alias:
                    self.declare(s.alias, const=True)
                else:
                    names = self.module_names(s)
                    if names is None:
                        self.scopes[-1].open = True
                    else:
                        for name in names:
                            self.declare(name, const=True)

    def block(self, stmts, names=()):
        self.push()
        for name in names:
            self.declare(name)
        self.predeclare(stmts)
        for s in stmts:
            self.visit(s)
        self.pop()

    # ---- traversal -----------------------------------------------------
    def check(self, tree):
        self.predeclare(tree.stmts)
        for s in tree.stmts:
            self.visit(s)
        return self.errors

    def visit(self, n):
        if isinstance(n, Node):
            handler = getattr(self, "c_" + n.kind, None)
            if handler is not None:
                handler(n)
            else:
                self.children(n)
        elif isinstance(n, (list, tuple)):
            for x in n:
                self.visit(x)
        elif isinstance(n, dict):
            for x in n.values():
                self.visit(x)

    def children(self, n):
        for key, value in n.__dict__.items():
            if key in ("kind", "pos"):
                continue
            self.visit(value)

    def c_Ident(self, n):
        if self.resolve(n.name) is None:
            self.undefined(n.name, n.pos)

    def c_Field(self, n):
        self.visit(n.obj)

    def c_TName(self, n):
        if n.name not in PRIMS and self.resolve(n.name) is None:
            self.undefined(n.name, n.pos, "unknown type '%s'")

    def c_TWhere(self, n):
        self.visit(n.base)
        self.push()
        self.declare("it")
        self.visit(n.expr)
        self.pop()

    def c_Block(self, n):
        self.block(n.stmts)

    def c_Let(self, n):
        self.visit(n.type)
        self.visit(n.value)

    def c_Assign(self, n):
        self.visit(n.value)
        t = n.target
        if t.kind == "Ident":
            info = self.resolve(t.name)
            if info is None:
                self.undefined(t.name, t.pos, "cannot assign to undefined variable '%s'")
            elif info.get("const"):
                self.error("cannot assign to '%s': it is not declared with 'var'" % t.name, t.pos, "TypeError")
        else:
            self.visit(t)

    def function(self, n):
        for p in n.params:
            self.visit(p.type)
        self.visit(getattr(n, "ret", None))
        self.push()
        for p in n.params:
            self.visit(p.default)
            self.declare(p.name)
        saved = (self.fn_depth, self.loop_depth, self.prompt_kind)
        self.fn_depth, self.loop_depth, self.prompt_kind = self.fn_depth + 1, 0, None
        if n.is_expr:
            self.visit(n.body)
        else:
            self.block(n.body.stmts)
        self.fn_depth, self.loop_depth, self.prompt_kind = saved
        self.pop()

    c_FnDecl = function
    c_Lambda = function

    def c_PromptDecl(self, n):
        for p in n.params:
            self.visit(p.type)
        self.visit(n.ret)
        self.visit(n.using)
        self.visit(n.opts)
        self.push()
        for p in n.params:
            self.visit(p.default)
            self.declare(p.name)
        saved = (self.fn_depth, self.loop_depth, self.prompt_kind)
        self.fn_depth, self.loop_depth, self.prompt_kind = 0, 0, n.prompt_kind
        self.block(n.body.stmts)
        self.fn_depth, self.loop_depth, self.prompt_kind = saved
        self.pop()

    def c_Directive(self, n):
        if n.name in ("tools", "max_steps") and self.prompt_kind == "prompt":
            self.error("'%s' is only allowed in an agent — change 'prompt' to 'agent'" % n.name, n.pos)
        self.visit(n.a)
        self.visit(n.b)

    def c_Return(self, n):
        if self.fn_depth == 0:
            self.error("'return' outside of a function", n.pos)
        self.visit(n.value)

    def c_Break(self, n):
        if self.loop_depth == 0:
            self.error("'break' outside of a loop", n.pos)

    def c_Continue(self, n):
        if self.loop_depth == 0:
            self.error("'continue' outside of a loop", n.pos)

    def c_While(self, n):
        self.visit(n.cond)
        self.loop_depth += 1
        self.block(n.body.stmts)
        self.loop_depth -= 1

    def c_For(self, n):
        self.visit(n.iterable)
        self.loop_depth += 1
        self.block(n.body.stmts, [x for x in (n.key, n.value) if x])
        self.loop_depth -= 1

    def c_If(self, n):
        self.visit(n.cond)
        self.block(n.then.stmts)
        if n.els is not None:
            self.visit(n.els)

    def c_Match(self, n):
        self.visit(n.subject)
        for arm in n.arms:
            self.push()
            self.visit_pattern_types(arm.pattern)
            for name in self.pattern_names(arm.pattern, []):
                self.declare(name)
            self.visit(arm.guard)
            if arm.body.kind == "Block":
                self.block(arm.body.stmts)
            else:
                self.visit(arm.body)
            self.pop()

    def visit_pattern_types(self, p):
        if p.kind == "PType":
            self.visit(p.type)
        elif p.kind == "PAlt":
            for o in p.options:
                self.visit_pattern_types(o)
        elif p.kind == "PRecord":
            for _key, sub in p.fields:
                self.visit_pattern_types(sub)
        elif p.kind == "PList":
            for sub in p.items:
                self.visit_pattern_types(sub)

    def c_Try(self, n):
        self.block(n.body.stmts)
        if n.handler is not None:
            self.block(n.handler.stmts, [n.name] if n.name else [])
        if n.final is not None:
            self.block(n.final.stmts)

    def c_Test(self, n):
        saved = (self.fn_depth, self.loop_depth, self.prompt_kind)
        self.fn_depth, self.loop_depth, self.prompt_kind = 0, 0, None
        self.block(n.body.stmts)
        self.fn_depth, self.loop_depth, self.prompt_kind = saved

    def c_Budget(self, n):
        self.visit(n.limits)
        self.block(n.body.stmts)

    def c_Import(self, n):
        pass

    def c_Pipe(self, n):
        self.visit(n.left)
        if n.right.kind == "Call":
            self.c_Call(n.right, extra=1)
        else:
            self.visit(n.right)

    def c_Call(self, n, extra=0):
        self.visit(n.callee)
        self.visit(n.args)
        if n.callee.kind != "Ident":
            return
        info = self.resolve(n.callee.name)
        if not info or "params" not in info:
            return
        params = info["params"]
        kinds = [a[0] for a in n.args]
        if "spread" in kinds:
            return
        npos = kinds.count("pos") + extra
        named = [a[1] for a in n.args if a[0] == "named"]
        name = n.callee.name
        if npos > len(params):
            self.error("%s() takes %d argument(s) but %d were given" % (name, len(params), npos), n.pos, "TypeError")
            return
        known = [p.name for p in params]
        for nm in named:
            if nm not in known:
                self.error("%s() has no parameter named '%s'" % (name, nm), n.pos, "TypeError")
                return
        for i, p in enumerate(params):
            if i >= npos and p.default is None and p.name not in named:
                self.error("%s() is missing argument '%s'" % (name, p.name), n.pos, "TypeError")
                return


def check(tree, file, known=()):
    return Checker(file, known).check(tree)
