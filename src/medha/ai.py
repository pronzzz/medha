"""The AI runtime: model calls, typed output with repair, tool loops, budgets, mocks, tapes."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from collections import deque

from . import providers
from .errors import MedhaError
from .types import (T_STRING, Type, ValidationError, is_string_type, parse_model_output, to_type)
from .values import Builtin, Chat, Function, ModelValue, PromptValue, to_json

NOT_MOCKED = object()
PROVIDER_OPTIONS = ("temperature", "top_p", "top_k", "max_tokens", "effort", "stop", "seed", "timeout",
                    "base_url", "api_key_env", "fallbacks")
KNOWN_OPTIONS = PROVIDER_OPTIONS + ("retries", "cache", "max_steps")


def estimate_tokens(text):
    """A rough, provider-independent token estimate (about four characters per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0


class Runtime:
    """Everything that outlives a single prompt call."""

    def __init__(self, dry_run=False, trace=False, model_override=None, record=None, replay=None):
        self.dry_run = dry_run
        self.trace = trace
        self.model_override = model_override
        self.lock = threading.RLock()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.budgets = []
        self.mocks = {}
        self.cache = {}
        self.record = [] if record else None
        self.record_path = record
        self.replay = None
        self.replay_pos = 0
        if replay:
            with open(replay, "r", encoding="utf-8") as f:
                self.replay = json.load(f)["calls"]

    # -- mocks ----------------------------------------------------------
    def add_mock(self, target, value):
        entry = self.mocks.setdefault(id(target), {"target": target, "queue": deque()})
        entry["queue"].append(value)

    def take_mock(self, target):
        entry = self.mocks.get(id(target))
        if not entry:
            return NOT_MOCKED
        q = entry["queue"]
        if entry.get("stale") and len(q) > 1:
            q.popleft()  # the reply that was standing in until a newer one was scripted
        entry["stale"] = len(q) == 1
        return q[0] if len(q) == 1 else q.popleft()

    def reset_for_test(self):
        self.mocks = {}
        self.cache = {}
        self.budgets = []
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    # -- budgets --------------------------------------------------------
    def _budget_before(self):
        for b in self.budgets:
            if b["calls"] is not None and b["used_calls"] >= b["calls"]:
                raise MedhaError("budget exceeded: this block allows %d model call(s)" % b["calls"],
                                 kind="BudgetExceeded", data={"limit": "calls"})

    def _budget_after(self, tokens):
        for b in self.budgets:
            b["used_calls"] += 1
            b["used_tokens"] += tokens
        for b in self.budgets:
            if b["tokens"] is not None and b["used_tokens"] > b["tokens"]:
                raise MedhaError("budget exceeded: used %d tokens of %d" % (b["used_tokens"], b["tokens"]),
                                 kind="BudgetExceeded", data={"limit": "tokens", "used": b["used_tokens"]})

    # -- tapes ----------------------------------------------------------
    def save_tape(self):
        if self.record is not None:
            with open(self.record_path, "w", encoding="utf-8") as f:
                json.dump({"medha_tape": 1, "calls": self.record}, f, ensure_ascii=False, indent=2)

    # -- the one place a model is actually called -----------------------
    def complete(self, interp, model, system, messages, tools, opts, label, ret_type=None):
        with self.lock:
            self._budget_before()
        spec = self.model_override or model.spec
        provider, _, model_id = spec.partition(":")
        started = time.time()
        how = ""
        if self.replay is not None:
            with self.lock:
                if self.replay_pos >= len(self.replay):
                    raise MedhaError("replay tape is exhausted: the program made more model calls than were recorded",
                                     kind="ReplayError")
                entry = self.replay[self.replay_pos]
                self.replay_pos += 1
            reply = entry["reply"]
            how = " (replay)"
        else:
            scripted = self.take_mock(model)
            if scripted is not NOT_MOCKED:
                reply = self._scripted_reply(interp, scripted, model, system, messages, tools)
                how = " (mock)"
            elif self.dry_run or provider == "mock":
                reply = self._default_mock_reply(system, messages, ret_type, label)
                how = " (mock)"
            else:
                call_opts = {k: v for k, v in opts.items() if k in PROVIDER_OPTIONS}
                reply = providers.chat(provider, model_id, system, messages, tools, call_opts)
            if self.record is not None:
                with self.lock:
                    self.record.append({"label": label, "model": spec, "reply": reply})
        used = reply["usage"]
        with self.lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += used["input"]
            self.usage["output_tokens"] += used["output"]
            if self.trace:
                sys.stderr.write("[medha] %s -> %s%s  in=%d out=%d  %.2fs\n" % (
                    label, spec, how, used["input"], used["output"], time.time() - started))
            self._budget_after(used["input"] + used["output"])
        return reply

    @staticmethod
    def _usage_for(system, messages, text):
        prompt = (system or "") + "".join(m.get("content") or "" for m in messages)
        return {"input": estimate_tokens(prompt), "output": estimate_tokens(text)}

    def _scripted_reply(self, interp, item, model, system, messages, tools):
        if isinstance(item, (Function, Builtin)):
            request = {"model": model.spec, "system": system or "",
                       "messages": [{"role": m["role"], "content": m.get("content") or ""} for m in messages],
                       "tools": [t["name"] for t in tools]}
            item = interp.call(item, [request])
        calls = []
        text = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and isinstance(item.get("tool"), str):
            calls = [item]
        elif isinstance(item, list) and item and all(isinstance(x, dict) and isinstance(x.get("tool"), str) for x in item):
            calls = item
        else:
            text = to_json(item)
        tool_calls = [{"id": "mock_%d_%d" % (len(messages), i), "name": c["tool"], "args": c.get("args") or {}}
                      for i, c in enumerate(calls)]
        return {"text": text, "tool_calls": tool_calls, "stop": "tool_use" if calls else "end_turn", "raw": None,
                "usage": self._usage_for(system, messages, text or to_json(calls))}

    def _default_mock_reply(self, system, messages, ret_type, label):
        if ret_type is not None and not is_string_type(ret_type):
            try:
                text = to_json(ret_type.sample(label))
            except ValidationError as e:
                raise MedhaError("%s: %s" % (label, e.message), kind="MockError")
        else:
            last = ""
            for m in reversed(messages):
                if m["role"] == "user":
                    last = " ".join((m.get("content") or "").split())
                    break
            if len(last) > 72:
                last = last[:71] + "…"
            text = "[mock reply to: %s]" % last
        return {"text": text, "tool_calls": [], "stop": "end_turn", "raw": None,
                "usage": self._usage_for(system, messages, text)}


# ---- prompt context ----------------------------------------------------

class PromptCtx:
    def __init__(self):
        self.system = []
        self.messages = []
        self.tools = []
        self.max_steps = None

    def add_user(self, text):
        if self.messages and self.messages[-1]["role"] == "user" and not self.messages[-1].get("closed"):
            self.messages[-1]["content"] += "\n" + text
        else:
            self.messages.append({"role": "user", "content": text})

    def add_example(self, given, expected):
        self.messages.append({"role": "user", "content": given, "closed": True})
        self.messages.append({"role": "assistant", "content": expected})

    def final_messages(self):
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]


def output_instruction(t):
    return ("Respond with a single JSON value and nothing else: no prose, no explanation, no code fences.\n"
            "The value must conform to this JSON Schema:\n" + json.dumps(t.schema(), ensure_ascii=False))


def as_text(v):
    return v if isinstance(v, str) else to_json(v)


def resolve_model(interp, value, what):
    if isinstance(value, ModelValue):
        return value
    if isinstance(value, str):
        return ModelValue(value)
    if value is None:
        if interp.default_model is not None:
            return interp.default_model
        env_spec = os.environ.get("MEDHA_MODEL")
        if env_spec:
            return ModelValue(env_spec, name="MEDHA_MODEL")
        if interp.rt.dry_run or interp.rt.model_override:
            return ModelValue("mock", name="mock")
        raise MedhaError('%s has no model: declare one, e.g.  model m = "anthropic:claude-opus-5-5"' % what,
                         kind="ModelError")
    raise MedhaError("%s: expected a model after 'using', got %s" % (what, interp.type_name(value)), kind="TypeError")


def merged_options(model, extra, what):
    opts = dict(model.options)
    opts.update(extra or {})
    for k in opts:
        if k not in KNOWN_OPTIONS:
            raise MedhaError("%s: unknown option '%s' (known: %s)" % (what, k, ", ".join(KNOWN_OPTIONS)),
                             kind="TypeError")
    return opts


# ---- tools -------------------------------------------------------------

def tool_spec(interp, f):
    if isinstance(f, Function):
        params, (types, _ret) = f.params, interp.fn_types(f)
    elif isinstance(f, PromptValue):
        params = f.decl.params
        types = [interp.mk_type(p.type, f.env) if p.type else None for p in params]
    else:
        raise MedhaError("tools must be functions, prompts or agents, got %s" % interp.type_name(f), kind="TypeError")
    if not f.name:
        raise MedhaError("an anonymous function cannot be a tool: the model needs a name to call", kind="TypeError")
    props = {}
    required = []
    for p, t in zip(params, types):
        props[p.name] = t.schema() if t is not None else {}
        if p.default is None:
            required.append(p.name)
    return {"name": f.name, "description": f.doc or "",
            "parameters": {"type": "object", "properties": props, "required": required}, "fn": f, "types": types}


def run_tool(interp, spec, args):
    """Run one tool call. Failures are reported back to the model rather than crashing the agent."""
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object", True
    f = spec["fn"]
    params = f.params if isinstance(f, Function) else f.decl.params
    known = {p.name: t for p, t in zip(params, spec["types"])}
    kwargs = {}
    try:
        for k, v in args.items():
            if k not in known:
                return "Error: unknown argument '%s' (expected: %s)" % (k, ", ".join(known) or "none"), True
            kwargs[k] = known[k].validate(v, lenient=True, path=k) if known[k] is not None else v
        return as_text(interp.call(f, [], kwargs)), False
    except ValidationError as e:
        return "Error: invalid arguments: %s" % e, True
    except MedhaError as e:
        if e.kind in ("BudgetExceeded", "ReplayError"):
            raise
        return "Error: [%s] %s" % (e.kind, e.message), True


# ---- generation --------------------------------------------------------

def generate(interp, model, system, messages, ret, tools, opts, label, max_steps=None):
    """Call ``model`` until it produces a value of type ``ret``.

    Runs the tool loop, validates the final answer, and on a mismatch feeds the
    validation error back to the model (``retries`` times) before giving up.
    Returns ``(value, full message list including the final assistant turn)``.
    """
    rt = interp.rt
    typed = not is_string_type(ret)
    sys_text = system or ""
    if typed:
        sys_text = (sys_text + "\n\n" if sys_text else "") + output_instruction(ret)
    specs = [tool_spec(interp, t) for t in tools]
    by_name = {s["name"]: s for s in specs}
    wire_tools = [{"name": s["name"], "description": s["description"], "parameters": s["parameters"]} for s in specs]
    retries = int(opts.get("retries", 2))
    if max_steps is None:
        max_steps = int(opts.get("max_steps", 10))

    cache_key = None
    if opts.get("cache"):
        blob = json.dumps([rt.model_override or model.spec, sys_text, messages, wire_tools,
                           {k: opts[k] for k in sorted(opts) if k in PROVIDER_OPTIONS}],
                          sort_keys=True, default=str)
        cache_key = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        with rt.lock:
            hit = rt.cache.get(cache_key)
        if hit is not None:
            if rt.trace:
                sys.stderr.write("[medha] %s -> %s (cache hit)\n" % (label, model.spec))
            return copy.deepcopy(hit[0]), copy.deepcopy(hit[1])

    msgs = [dict(m) for m in messages]
    steps = 0
    failures = 0
    while True:
        reply = rt.complete(interp, model, sys_text, msgs, wire_tools, opts, label, ret)
        assistant = {"role": "assistant", "content": reply["text"], "raw": reply.get("raw")}
        if reply["tool_calls"]:
            steps += 1
            if steps > max_steps:
                raise MedhaError("%s: stopped after %d tool steps without a final answer (raise max_steps?)"
                                 % (label, max_steps), kind="StepLimit")
            assistant["tool_calls"] = reply["tool_calls"]
            msgs.append(assistant)
            for tc in reply["tool_calls"]:
                spec = by_name.get(tc["name"])
                if spec is None:
                    result, failed = "Error: unknown tool '%s'" % tc["name"], True
                else:
                    result, failed = run_tool(interp, spec, tc["args"])
                if rt.trace:
                    preview = " ".join(result.split())
                    sys.stderr.write("[medha]   tool %s(%s) -> %s\n" % (
                        tc["name"], to_json(tc["args"])[1:-1], preview[:100] + ("…" if len(preview) > 100 else "")))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                             "content": result, "is_error": failed})
            continue
        msgs.append(assistant)
        if not typed:
            value = reply["text"]
            break
        try:
            value = parse_model_output(ret, reply["text"])
            break
        except ValidationError as e:
            failures += 1
            if failures > retries:
                hint = " (the reply was cut off at max_tokens)" if reply.get("stop") in ("max_tokens", "length") else ""
                raise MedhaError("%s: model output did not match %s after %d attempt(s): %s%s"
                                 % (label, ret.describe(), failures, e, hint),
                                 kind="ModelOutputError", data={"raw": reply["text"]})
            msgs.append({"role": "user", "content":
                         "That reply is not valid: %s\nReply again with only the corrected JSON value." % e})
    if cache_key is not None:
        with rt.lock:
            rt.cache[cache_key] = (copy.deepcopy(value), copy.deepcopy(msgs))
    return value, msgs


def build_context(interp, pv, env):
    ctx = PromptCtx()
    interp.prompt_stack().append(ctx)
    try:
        interp.exec_block(pv.decl.body.stmts, env)
    finally:
        interp.prompt_stack().pop()
    return ctx


def prepare_prompt(interp, pv, args, kwargs):
    decl = pv.decl
    env = interp.new_env(pv.env)
    types = [interp.mk_type(p.type, pv.env) if p.type else None for p in decl.params]
    interp.bind_params(decl.params, types, args, kwargs, env, decl.name)
    ret = interp.mk_type(decl.ret, pv.env) if decl.ret is not None else T_STRING
    return env, ret


def run_prompt(interp, pv, args, kwargs):
    decl = pv.decl
    env, ret = prepare_prompt(interp, pv, args, kwargs)
    rt = interp.rt
    mocked = rt.take_mock(pv)
    if mocked is not NOT_MOCKED:
        if isinstance(mocked, (Function, Builtin)):
            mocked = interp.call(mocked, [env.vars[p.name] for p in decl.params])
        try:
            return ret.validate(mocked, lenient=True)
        except ValidationError as e:
            raise MedhaError("mock for %s does not match %s: %s" % (decl.name, ret.describe(), e),
                             kind="ValidationError")
    ctx = build_context(interp, pv, env)
    if not ctx.messages:
        raise MedhaError("%s '%s' produced no message: its body needs at least one string" % (pv.kind, decl.name),
                         kind="PromptError")
    model = resolve_model(interp, interp.eval(decl.using, pv.env) if decl.using is not None else None,
                          "%s '%s'" % (pv.kind, decl.name))
    extra = interp.eval(decl.opts, pv.env) if decl.opts is not None else None
    opts = merged_options(model, extra, "%s '%s'" % (pv.kind, decl.name))
    value, _ = generate(interp, model, "\n\n".join(ctx.system), ctx.final_messages(), ret, ctx.tools, opts,
                        decl.name, ctx.max_steps)
    return value


def render_prompt(interp, pv, args, kwargs):
    """What would be sent, without sending it."""
    decl = pv.decl
    env, ret = prepare_prompt(interp, pv, args, kwargs)
    ctx = build_context(interp, pv, env)
    system = "\n\n".join(ctx.system)
    if not is_string_type(ret):
        system = (system + "\n\n" if system else "") + output_instruction(ret)
    out = {"system": system, "messages": ctx.final_messages(),
           "tools": [tool_spec(interp, t)["name"] for t in ctx.tools]}
    if decl.using is not None or interp.default_model is not None:
        out["model"] = resolve_model(interp, interp.eval(decl.using, pv.env) if decl.using is not None else None,
                                     decl.name).spec
    return out


def call_model(interp, model, args, kwargs):
    """``m("question")`` or ``m("question", Type, system: "...", tools: [...])``."""
    if not args or not isinstance(args[0], str):
        raise MedhaError("a model is called with a prompt string: %s(\"...\")" % (model.name or "model"),
                         kind="TypeError")
    kwargs = dict(kwargs)
    ret = to_type(args[1]) if len(args) > 1 else T_STRING
    system = kwargs.pop("system", "") or ""
    tools = kwargs.pop("tools", []) or []
    opts = merged_options(model, kwargs, "model call")
    value, _ = generate(interp, model, system, [{"role": "user", "content": args[0]}], ret, tools, opts,
                        model.name or model.spec)
    return value


def chat_say(interp, chat, text, ret=None, tools=None):
    ret = to_type(ret) if ret is not None else T_STRING
    msgs = chat.messages + [{"role": "user", "content": text}]
    value, msgs = generate(interp, chat.model, chat.system, msgs, ret, tools or [], chat.options,
                           "chat")
    chat.messages = msgs
    return value


# ---- embeddings --------------------------------------------------------

def mock_embedding(text, dims=256):
    """A deterministic bag-of-words embedding so similarity code runs offline."""
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed(interp, model, texts):
    rt = interp.rt
    model = resolve_model(interp, model, "embed()")
    single = isinstance(texts, str)
    items = [texts] if single else list(texts)
    if not all(isinstance(t, str) for t in items):
        raise MedhaError("embed() takes a string or a list of strings", kind="TypeError")
    spec = rt.model_override or model.spec
    provider, _, model_id = spec.partition(":")
    with rt.lock:
        rt._budget_before()
    if rt.dry_run or provider == "mock":
        vectors = [mock_embedding(t) for t in items]
    elif provider in providers.OPENAI_COMPATIBLE:
        vectors = providers.compat_embed(provider, model_id, items, model.options)
    else:
        raise MedhaError("provider '%s' has no embeddings endpoint; use an OpenAI-compatible embedding model"
                         % provider, kind="ModelError")
    tokens = sum(estimate_tokens(t) for t in items)
    with rt.lock:
        rt.usage["calls"] += 1
        rt.usage["input_tokens"] += tokens
        rt._budget_after(tokens)
    return vectors[0] if single else vectors


__all__ = ["Runtime", "PromptCtx", "run_prompt", "render_prompt", "call_model", "chat_say", "embed", "Chat",
           "estimate_tokens", "Type"]
