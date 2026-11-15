"""Recursive-descent parser producing a tree of ``Node`` objects."""
from __future__ import annotations

from .errors import MedhaError
from .lexer import lex

DIRECTIVES = {"system", "user", "assistant", "example", "tools", "max_steps"}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/="}
COMPARE_OPS = {"==", "!=", "<", "<=", ">", ">="}


class Node:
    def __init__(self, kind, pos, **fields):
        self.kind = kind
        self.pos = pos
        self.__dict__.update(fields)

    def __repr__(self):
        return "Node(%s)" % self.kind


class Parser:
    def __init__(self, src, file="<input>", line_offset=0):
        self.src = src
        self.file = file
        self.toks = lex(src, file)
        if line_offset:
            for t in self.toks:
                t.line += line_offset
        self.i = 0
        self.prompt_depth = 0

    # ---- token helpers -------------------------------------------------
    @property
    def tok(self):
        return self.toks[self.i]

    def peek(self, k=1):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def advance(self):
        t = self.toks[self.i]
        if t.type != "eof":
            self.i += 1
        return t

    def pos(self, t=None):
        t = t or self.tok
        return (self.file, t.line, t.col)

    def err(self, msg, t=None):
        raise MedhaError(msg, self.pos(t), "SyntaxError")

    @staticmethod
    def describe(t):
        if t.type == "eof":
            return "end of file"
        if t.type == "str":
            return "a string"
        return "'%s'" % (t.value,)

    def is_op(self, v, k=0):
        t = self.peek(k) if k else self.tok
        return t.type == "op" and t.value == v

    def is_kw(self, v, k=0):
        t = self.peek(k) if k else self.tok
        return t.type == "kw" and t.value == v

    def is_name(self, v, k=0):
        t = self.peek(k) if k else self.tok
        return t.type == "ident" and t.value == v

    def eat_op(self, v):
        if self.is_op(v):
            self.i += 1
            return True
        return False

    def need_op(self, v, ctx=""):
        if not self.eat_op(v):
            self.err("expected '%s'%s, found %s" % (v, " " + ctx if ctx else "", self.describe(self.tok)))

    def need_kw(self, v):
        if not self.is_kw(v):
            self.err("expected '%s', found %s" % (v, self.describe(self.tok)))
        self.i += 1

    def ident(self, what="a name"):
        t = self.tok
        if t.type != "ident":
            if t.type == "kw":
                self.err("'%s' is a reserved word and cannot be used as %s" % (t.value, what))
            self.err("expected %s, found %s" % (what, self.describe(t)))
        self.i += 1
        return t.value

    def word(self, what="a name"):
        """An identifier or keyword used as a plain name (field names, record keys)."""
        t = self.tok
        if t.type not in ("ident", "kw"):
            self.err("expected %s, found %s" % (what, self.describe(t)))
        self.i += 1
        return t.value

    def source_since(self, start_index):
        return self.src[self.toks[start_index].start:self.toks[self.i - 1].end]

    def matching_paren(self, k=0):
        """Offset (from current token) of the ')' matching the '(' at offset ``k``, or -1."""
        depth = 0
        j = self.i + k
        while j < len(self.toks):
            t = self.toks[j]
            if t.type == "op":
                if t.value in "([{" and len(t.value) == 1:
                    depth += 1
                elif t.value in ")]}" and len(t.value) == 1:
                    depth -= 1
                    if depth == 0:
                        return j - self.i
            elif t.type == "eof":
                break
            j += 1
        return -1

    # ---- program & statements -----------------------------------------
    def parse_program(self):
        stmts = []
        while self.tok.type != "eof":
            stmts.append(self.statement())
            while self.eat_op(";"):
                pass
        return Node("Program", (self.file, 1, 1), stmts=stmts, file=self.file)

    def block(self, what="block"):
        p = self.pos()
        self.need_op("{", "to start %s" % what)
        stmts = []
        while not self.is_op("}"):
            if self.tok.type == "eof":
                self.err("unclosed '{' — expected '}' before end of file", self.toks[self.i])
            stmts.append(self.statement())
            while self.eat_op(";"):
                pass
        self.i += 1
        return Node("Block", p, stmts=stmts)

    def statement(self):
        t = self.tok
        p = self.pos()
        if t.type == "kw":
            v = t.value
            if v in ("let", "var"):
                return self.let_stmt()
            if v == "fn" and self.peek().type == "ident":
                return self.fn_decl()
            if v == "return":
                self.i += 1
                value = None
                if not (self.tok.nl or self.is_op("}") or self.is_op(";") or self.tok.type == "eof"):
                    value = self.expr()
                return Node("Return", p, value=value)
            if v == "while":
                self.i += 1
                cond = self.expr()
                return Node("While", p, cond=cond, body=self.block("the loop body"))
            if v == "for":
                return self.for_stmt()
            if v == "try":
                return self.try_stmt()
            if v == "throw":
                self.i += 1
                return Node("Throw", p, value=self.expr())
            if v == "break":
                self.i += 1
                return Node("Break", p)
            if v == "continue":
                self.i += 1
                return Node("Continue", p)
            if v == "assert":
                self.i += 1
                start = self.i
                cond = self.expr()
                src = self.source_since(start)
                msg = self.expr() if self.eat_op(",") else None
                return Node("Assert", p, cond=cond, msg=msg, src=src)
            if v == "import":
                self.i += 1
                path = self.plain_string("a module path string")
                alias = None
                if self.is_kw("as"):
                    self.i += 1
                    alias = self.ident("a module alias")
                return Node("Import", p, path=path, alias=alias)
        elif t.type == "ident":
            v = t.value
            n1 = self.peek(1)
            n2 = self.peek(2)
            if v == "type" and n1.type == "ident" and not n1.nl and n2.type == "op" and n2.value in ("=", "{"):
                return self.type_decl()
            if v == "model" and n1.type == "ident" and n2.type == "op" and n2.value == "=":
                return self.model_decl()
            if v in ("prompt", "agent") and n1.type == "ident" and n2.type == "op" and n2.value == "(":
                return self.prompt_decl()
            if v == "test" and n1.type == "str" and not n1.nl:
                self.i += 1
                name = self.plain_string("a test name")
                saved, self.prompt_depth = self.prompt_depth, 0
                body = self.block("the test body")
                self.prompt_depth = saved
                return Node("Test", p, name=name, body=body)
            if v == "mock" and n1.type == "ident" and n2.type == "op" and n2.value == "->":
                self.i += 1
                target = Node("Ident", self.pos(), name=self.ident())
                self.i += 1
                return Node("Mock", p, target=target, value=self.expr())
            if v == "budget" and n1.type == "op" and n1.value == "(" and not n1.nl:
                close = self.matching_paren(1)
                if close > 0 and self.is_op("{", close + 1):
                    return self.budget_stmt()
            if self.prompt_depth and v in DIRECTIVES and not n1.nl and not (
                    n1.type == "op" and n1.value in ("=", ".", "?.", "+=", "-=", "*=", "/=", ",", ")")):
                return self.directive()

        expr = self.expr()
        if self.tok.type == "op" and self.tok.value in ASSIGN_OPS:
            op = self.advance().value
            if expr.kind not in ("Ident", "Field", "Index"):
                self.err("cannot assign to this expression", t)
            return Node("Assign", p, target=expr, op=op, value=self.expr())
        emit = bool(self.prompt_depth) and expr.kind not in ("If", "Match")
        return Node("ExprStmt", p, expr=expr, emit=emit)

    def plain_string(self, what):
        t = self.tok
        if t.type != "str" or len(t.value) != 1 or not isinstance(t.value[0], str):
            self.err("expected %s (without interpolation)" % what)
        self.i += 1
        return t.value[0]

    def let_stmt(self):
        p = self.pos()
        mutable = self.advance().value == "var"
        if self.is_op("{") or self.is_op("["):
            pattern = self.pattern_one()
            self.need_op("=", "after the destructuring pattern")
            return Node("Let", p, name=None, pattern=pattern, type=None, value=self.expr(), mutable=mutable)
        name = self.ident("a variable name")
        ty = None
        if self.eat_op(":"):
            ty = self.type_expr()
        self.need_op("=", "in '%s' (every binding needs an initial value)" % ("var" if mutable else "let"))
        return Node("Let", p, name=name, pattern=None, type=ty, value=self.expr(), mutable=mutable)

    def params(self):
        self.need_op("(", "to start the parameter list")
        out = []
        seen = set()
        while not self.is_op(")"):
            p = self.pos()
            name = self.ident("a parameter name")
            if name in seen:
                self.err("duplicate parameter '%s'" % name, self.toks[self.i - 1])
            seen.add(name)
            ty = self.type_expr() if self.eat_op(":") else None
            default = self.expr() if self.eat_op("=") else None
            out.append(Node("Param", p, name=name, type=ty, default=default))
            if not self.eat_op(","):
                break
        self.need_op(")", "to close the parameter list")
        return out

    def fn_decl(self):
        p = self.pos()
        doc = self.tok.doc
        self.i += 1
        name = self.ident("a function name")
        params = self.params()
        ret = self.type_expr() if self.eat_op("->") else None
        saved, self.prompt_depth = self.prompt_depth, 0
        if self.eat_op("=>"):
            body, is_expr = self.expr(), True
        else:
            body, is_expr = self.block("the function body"), False
        self.prompt_depth = saved
        return Node("FnDecl", p, name=name, params=params, ret=ret, body=body, is_expr=is_expr, doc=doc)

    def for_stmt(self):
        p = self.pos()
        self.i += 1
        first = self.ident("a loop variable")
        second = None
        if self.eat_op(","):
            second = self.ident("a loop variable")
        self.need_kw("in")
        iterable = self.expr()
        body = self.block("the loop body")
        key, value = (first, second) if second else (None, first)
        return Node("For", p, key=key, value=value, iterable=iterable, body=body)

    def try_stmt(self):
        p = self.pos()
        self.i += 1
        body = self.block("the try body")
        name = handler = final = None
        if self.is_kw("catch"):
            self.i += 1
            if self.tok.type == "ident":
                name = self.ident()
            handler = self.block("the catch body")
        if self.is_kw("finally"):
            self.i += 1
            final = self.block("the finally body")
        if handler is None and final is None:
            self.err("'try' needs a 'catch' or 'finally' block")
        return Node("Try", p, body=body, name=name, handler=handler, final=final)

    def type_decl(self):
        p = self.pos()
        doc = self.tok.doc
        self.i += 1
        name = self.ident("a type name")
        if self.eat_op("="):
            ty = self.type_expr()
        else:
            ty = self.type_primary()
            if self.is_name("where"):
                ty = self.where_clause(ty)
        return Node("TypeDecl", p, name=name, type=ty, doc=doc)

    def model_decl(self):
        p = self.pos()
        self.i += 1
        name = self.ident("a model name")
        self.need_op("=")
        spec = self.expr()
        options = None
        if self.is_op("{") and not self.tok.nl:
            options = self.primary()
        return Node("ModelDecl", p, name=name, spec=spec, options=options)

    def prompt_decl(self):
        p = self.pos()
        doc = self.tok.doc
        kind = self.advance().value
        name = self.ident("a %s name" % kind)
        params = self.params()
        ret = self.type_expr() if self.eat_op("->") else None
        using = opts = None
        if self.is_name("using"):
            self.i += 1
            using = self.coalesce()
        if self.is_name("with"):
            self.i += 1
            if not self.is_op("{"):
                self.err("expected an options record like { temperature: 0 } after 'with'")
            opts = self.primary()
        self.prompt_depth += 1
        body = self.block("the %s body" % kind)
        self.prompt_depth -= 1
        return Node("PromptDecl", p, prompt_kind=kind, name=name, params=params, ret=ret,
                    using=using, opts=opts, body=body, doc=doc)

    def budget_stmt(self):
        p = self.pos()
        self.i += 2
        limits = {}
        while not self.is_op(")"):
            key = self.ident("a budget limit name")
            if key not in ("tokens", "calls"):
                self.err("unknown budget limit '%s' (use tokens: or calls:)" % key, self.toks[self.i - 1])
            self.need_op(":")
            limits[key] = self.expr()
            if not self.eat_op(","):
                break
        self.need_op(")")
        return Node("Budget", p, limits=limits, body=self.block("the budget body"))

    def directive(self):
        p = self.pos()
        name = self.advance().value
        a = self.expr()
        b = None
        if name == "example":
            self.need_op("->", "between the example input and its expected output")
            b = self.expr()
        return Node("Directive", p, name=name, a=a, b=b)

    # ---- types ---------------------------------------------------------
    def type_expr(self):
        ty = self.type_union()
        if self.is_name("where"):
            ty = self.where_clause(ty)
        return ty

    def where_clause(self, base):
        p = self.pos()
        self.i += 1
        start = self.i
        expr = self.expr()
        return Node("TWhere", p, base=base, expr=expr, src=self.source_since(start))

    def type_union(self):
        p = self.pos()
        first = self.type_postfix()
        if not self.is_op("|"):
            return first
        options = [first]
        while self.eat_op("|"):
            options.append(self.type_postfix())
        return Node("TUnion", p, options=options)

    def type_postfix(self):
        ty = self.type_primary()
        while self.is_op("?") and not self.tok.nl:
            p = self.pos()
            self.i += 1
            ty = Node("TOpt", p, inner=ty)
        return ty

    def type_primary(self):
        t = self.tok
        p = self.pos()
        if t.type == "str":
            return Node("TLit", p, value=self.plain_string("a literal string type"))
        if t.type in ("int", "float"):
            self.i += 1
            return Node("TLit", p, value=t.value)
        if self.is_op("-") and self.peek().type in ("int", "float"):
            self.i += 1
            return Node("TLit", p, value=-self.advance().value)
        if t.type == "kw" and t.value in ("true", "false", "null"):
            self.i += 1
            return Node("TLit", p, value={"true": True, "false": False, "null": None}[t.value])
        if t.type == "ident":
            self.i += 1
            if t.value == "map" and self.is_op("<"):
                self.i += 1
                inner = self.type_union()
                self.need_op(">", "to close map<...>")
                return Node("TMap", p, value=inner)
            return Node("TName", p, name=t.value)
        if self.eat_op("["):
            inner = self.type_expr()
            self.need_op("]", "to close the list type")
            return Node("TList", p, elem=inner)
        if self.eat_op("("):
            inner = self.type_expr()
            self.need_op(")")
            return inner
        if self.eat_op("{"):
            fields = []
            seen = set()
            while not self.is_op("}"):
                ft = self.tok
                if ft.type == "eof":
                    self.err("unclosed '{' in record type")
                doc = ft.doc
                name = self.plain_string("a field name") if ft.type == "str" else self.word("a field name")
                if name in seen:
                    self.err("duplicate field '%s'" % name, ft)
                seen.add(name)
                optional = self.eat_op("?")
                self.need_op(":", "after field name '%s'" % name)
                fty = self.type_expr()
                default = self.expr() if self.eat_op("=") else None
                fields.append({"name": name, "type": fty, "optional": optional, "default": default,
                               "doc": doc, "pos": (self.file, ft.line, ft.col)})
                self.eat_op(",")
            self.i += 1
            return Node("TRecord", p, fields=fields)
        self.err("expected a type, found %s" % self.describe(t))

    # ---- patterns ------------------------------------------------------
    def pattern(self):
        p = self.pos()
        first = self.pattern_one()
        if not self.is_op("|"):
            return first
        options = [first]
        while self.eat_op("|"):
            options.append(self.pattern_one())
        return Node("PAlt", p, options=options)

    def pattern_one(self):
        t = self.tok
        p = self.pos()
        if t.type in ("int", "float"):
            self.i += 1
            return Node("PLit", p, value=t.value)
        if self.is_op("-") and self.peek().type in ("int", "float"):
            self.i += 1
            return Node("PLit", p, value=-self.advance().value)
        if t.type == "str":
            return Node("PLit", p, value=self.plain_string("a literal string pattern"))
        if t.type == "kw":
            if t.value in ("true", "false", "null"):
                self.i += 1
                return Node("PLit", p, value={"true": True, "false": False, "null": None}[t.value])
            if t.value == "is":
                self.i += 1
                return Node("PType", p, type=self.type_postfix())
        if t.type == "ident":
            self.i += 1
            if t.value == "_":
                return Node("PWild", p)
            return Node("PBind", p, name=t.value)
        if self.eat_op("{"):
            fields = []
            rest = None
            while not self.is_op("}"):
                if self.eat_op("..."):
                    rest = self.ident("a name for the remaining fields")
                else:
                    kt = self.tok
                    key = self.plain_string("a field name") if kt.type == "str" else self.word("a field name")
                    if self.eat_op(":"):
                        sub = self.pattern()
                    else:
                        sub = Node("PBind", (self.file, kt.line, kt.col), name=key)
                    fields.append((key, sub))
                if not self.eat_op(","):
                    break
            self.need_op("}", "to close the record pattern")
            return Node("PRecord", p, fields=fields, rest=rest)
        if self.eat_op("["):
            items = []
            rest = None
            has_rest = False
            while not self.is_op("]"):
                if self.eat_op("..."):
                    has_rest = True
                    rest = self.ident("a name for the remaining items")
                    self.eat_op(",")
                    break
                items.append(self.pattern())
                if not self.eat_op(","):
                    break
            self.need_op("]", "to close the list pattern")
            return Node("PList", p, items=items, rest=rest, has_rest=has_rest)
        self.err("expected a pattern, found %s" % self.describe(t))

    # ---- expressions ---------------------------------------------------
    def expr(self):
        t = self.tok
        if t.type == "ident" and self.is_op("=>", 1):
            p = self.pos()
            self.i += 2
            param = Node("Param", p, name=t.value, type=None, default=None)
            return self.lambda_body(p, [param])
        if self.is_op("("):
            close = self.matching_paren()
            if close > 0 and self.is_op("=>", close + 1):
                p = self.pos()
                params = self.params()
                self.i += 1
                return self.lambda_body(p, params)
        return self.pipe()

    def looks_like_record(self):
        """At a '{': is this a record literal rather than a block of statements?"""
        a, b = self.peek(1), self.peek(2)
        if a.type == "op":
            return a.value in ("}", "...", "[")
        if a.type in ("ident", "kw", "str"):
            return b.type == "op" and b.value in (":", ",", "}") and not (a.type == "kw" and b.value != ":")
        return False

    def lambda_body(self, p, params):
        saved, self.prompt_depth = self.prompt_depth, 0
        if self.is_op("{") and not self.looks_like_record():
            body, is_expr = self.block("the function body"), False
        else:
            body, is_expr = self.expr(), True
        self.prompt_depth = saved
        return Node("Lambda", p, params=params, body=body, is_expr=is_expr, ret=None, name=None)

    def pipe(self):
        left = self.coalesce()
        while self.is_op("|>"):
            p = self.pos()
            self.i += 1
            left = Node("Pipe", p, left=left, right=self.coalesce())
        return left

    def coalesce(self):
        left = self.or_expr()
        while self.is_op("??"):
            p = self.pos()
            self.i += 1
            left = Node("Coalesce", p, left=left, right=self.or_expr())
        return left

    def or_expr(self):
        left = self.and_expr()
        while self.is_kw("or"):
            p = self.pos()
            self.i += 1
            left = Node("Logical", p, op="or", left=left, right=self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.is_kw("and"):
            p = self.pos()
            self.i += 1
            left = Node("Logical", p, op="and", left=left, right=self.not_expr())
        return left

    def not_expr(self):
        if self.is_kw("not"):
            p = self.pos()
            self.i += 1
            return Node("Unary", p, op="not", operand=self.not_expr())
        return self.comparison()

    def comparison(self):
        left = self.range_expr()
        while True:
            t = self.tok
            p = self.pos()
            if t.type == "op" and t.value in COMPARE_OPS:
                self.i += 1
                left = Node("Binary", p, op=t.value, left=left, right=self.range_expr())
            elif t.type == "kw" and t.nl:
                return left
            elif t.type == "kw" and t.value == "in":
                self.i += 1
                left = Node("Binary", p, op="in", left=left, right=self.range_expr())
            elif t.type == "kw" and t.value == "not" and self.is_kw("in", 1):
                self.i += 2
                inner = Node("Binary", p, op="in", left=left, right=self.range_expr())
                left = Node("Unary", p, op="not", operand=inner)
            elif t.type == "kw" and t.value == "is":
                self.i += 1
                negate = False
                if self.is_kw("not"):
                    self.i += 1
                    negate = True
                left = Node("Is", p, value=left, type=self.type_union())
                if negate:
                    left = Node("Unary", p, op="not", operand=left)
            elif t.type == "kw" and t.value == "as":
                self.i += 1
                left = Node("As", p, value=left, type=self.type_union())
            else:
                return left

    def range_expr(self):
        left = self.additive()
        if self.is_op(".."):
            p = self.pos()
            self.i += 1
            return Node("Range", p, lo=left, hi=self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.tok.type == "op" and self.tok.value in ("+", "-"):
            if self.tok.value == "-" and self.tok.nl:
                break
            p = self.pos()
            op = self.advance().value
            left = Node("Binary", p, op=op, left=left, right=self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.tok.type == "op" and self.tok.value in ("*", "/", "%"):
            p = self.pos()
            op = self.advance().value
            left = Node("Binary", p, op=op, left=left, right=self.unary())
        return left

    def unary(self):
        if self.is_op("-"):
            p = self.pos()
            self.i += 1
            return Node("Unary", p, op="-", operand=self.unary())
        return self.power()

    def power(self):
        base = self.postfix()
        if self.is_op("**"):
            p = self.pos()
            self.i += 1
            return Node("Binary", p, op="**", left=base, right=self.unary())
        return base

    def call_args(self):
        args = []
        while not self.is_op(")"):
            if self.eat_op("..."):
                args.append(("spread", None, self.expr()))
            elif self.tok.type == "ident" and self.is_op(":", 1):
                name = self.advance().value
                self.i += 1
                args.append(("named", name, self.expr()))
            else:
                args.append(("pos", None, self.expr()))
            if not self.eat_op(","):
                break
        self.need_op(")", "to close the argument list")
        return args

    def postfix(self):
        e = self.primary()
        while True:
            t = self.tok
            if t.type != "op":
                return e
            p = self.pos()
            if t.value == "(" and not t.nl:
                self.i += 1
                e = Node("Call", p, callee=e, args=self.call_args())
            elif t.value == "[" and not t.nl:
                self.i += 1
                lo = None if self.is_op(":") else self.expr()
                if self.eat_op(":"):
                    hi = None if self.is_op("]") else self.expr()
                    self.need_op("]", "to close the slice")
                    e = Node("Slice", p, obj=e, lo=lo, hi=hi)
                else:
                    self.need_op("]", "to close the index")
                    e = Node("Index", p, obj=e, index=lo)
            elif t.value in (".", "?."):
                self.i += 1
                e = Node("Field", p, obj=e, name=self.word("a field name"), optional=t.value == "?.")
            else:
                return e

    def string_node(self, t):
        p = self.pos(t)
        parts = []
        for part in t.value:
            if isinstance(part, str):
                parts.append(part)
            else:
                _, src, line, _col = part
                sub = Parser(src, self.file, line_offset=line - 1)
                node = sub.expr()
                if sub.tok.type != "eof":
                    sub.err("unexpected %s inside '{...}' interpolation" % sub.describe(sub.tok))
                parts.append(node)
        return Node("Str", p, parts=parts)

    def primary(self):
        t = self.tok
        p = self.pos()
        if t.type in ("int", "float"):
            self.i += 1
            return Node("Num", p, value=t.value)
        if t.type == "str":
            self.i += 1
            return self.string_node(t)
        if t.type == "ident":
            self.i += 1
            return Node("Ident", p, name=t.value)
        if t.type == "kw":
            v = t.value
            if v in ("true", "false"):
                self.i += 1
                return Node("Bool", p, value=v == "true")
            if v == "null":
                self.i += 1
                return Node("Null", p)
            if v == "if":
                return self.if_expr()
            if v == "match":
                return self.match_expr()
            if v == "fn":
                self.i += 1
                params = self.params()
                ret = self.type_expr() if self.eat_op("->") else None
                saved, self.prompt_depth = self.prompt_depth, 0
                if self.eat_op("=>"):
                    body, is_expr = self.expr(), True
                else:
                    body, is_expr = self.block("the function body"), False
                self.prompt_depth = saved
                return Node("Lambda", p, params=params, body=body, is_expr=is_expr, ret=ret, name=None)
            self.err("unexpected '%s'" % v)
        if self.eat_op("("):
            e = self.expr()
            self.need_op(")", "to close the parenthesis")
            return e
        if self.eat_op("["):
            items = []
            while not self.is_op("]"):
                if self.eat_op("..."):
                    items.append(Node("Spread", p, value=self.expr()))
                else:
                    items.append(self.expr())
                if not self.eat_op(","):
                    break
            self.need_op("]", "to close the list")
            return Node("List", p, items=items)
        if self.eat_op("{"):
            entries = []
            while not self.is_op("}"):
                kt = self.tok
                if self.eat_op("..."):
                    entries.append(("spread", None, self.expr()))
                elif self.eat_op("["):
                    key = self.expr()
                    self.need_op("]")
                    self.need_op(":")
                    entries.append(("computed", key, self.expr()))
                else:
                    if kt.type == "str":
                        key = self.plain_string("a record key")
                    elif kt.type in ("ident", "kw"):
                        self.i += 1
                        key = kt.value
                    else:
                        self.err("expected a record key, found %s" % self.describe(kt))
                    if self.eat_op(":"):
                        entries.append(("kv", key, self.expr()))
                    elif kt.type == "ident":
                        entries.append(("kv", key, Node("Ident", (self.file, kt.line, kt.col), name=key)))
                    else:
                        self.err("expected ':' after record key '%s'" % key)
                if not self.eat_op(","):
                    if self.is_op("}") or self.tok.nl:
                        continue
                    self.err("expected ',' or '}' in record, found %s" % self.describe(self.tok))
            self.i += 1
            return Node("Record", p, entries=entries)
        self.err("unexpected %s" % self.describe(t))

    def if_expr(self):
        p = self.pos()
        self.i += 1
        cond = self.expr()
        then = self.block("the 'if' body")
        els = None
        if self.is_kw("else"):
            self.i += 1
            els = self.if_expr() if self.is_kw("if") else self.block("the 'else' body")
        return Node("If", p, cond=cond, then=then, els=els)

    def match_expr(self):
        p = self.pos()
        self.i += 1
        subject = self.expr()
        self.need_op("{", "to start the match arms")
        arms = []
        while not self.is_op("}"):
            if self.tok.type == "eof":
                self.err("unclosed 'match'")
            ap = self.pos()
            pat = self.pattern()
            guard = None
            if self.is_kw("if"):
                self.i += 1
                guard = self.expr()
            self.need_op("->", "after the match pattern")
            body = self.block("the arm body") if self.is_op("{") and not self.looks_like_record() else self.expr()
            arms.append(Node("Arm", ap, pattern=pat, guard=guard, body=body))
            self.eat_op(",")
        self.i += 1
        return Node("Match", p, subject=subject, arms=arms)


def parse(src, file="<input>"):
    return Parser(src, file).parse_program()
