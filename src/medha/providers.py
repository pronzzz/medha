"""Model providers.

Medha speaks two wire formats over plain HTTPS (standard library only, so the
interpreter has no dependencies):

* the Anthropic Messages API, for ``anthropic:<model>``
* the OpenAI-compatible chat completions API, for ``openai:``, ``ollama:``,
  ``openrouter:``, ``groq:``, ``gemini:`` and any server reachable via ``base_url``

Both are normalised to one reply shape::

    {"text": str, "tool_calls": [{"id", "name", "args"}], "usage": {"input", "output"},
     "stop": str, "raw": provider-specific content to echo back verbatim}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from .errors import MedhaError

ANTHROPIC_VERSION = "2026-10-21"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 600

OPENAI_COMPATIBLE = {
    # name: (base URL env var, default base URL, API key env var)
    "openai": ("OPENAI_BASE_URL", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "ollama": ("OLLAMA_BASE_URL", "http://localhost:11434/v1", None),
    "openrouter": ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": ("GROQ_BASE_URL", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": ("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
}

PROVIDERS = ["anthropic", "mock"] + list(OPENAI_COMPATIBLE)

_warned = set()


def warn_once(key, message):
    if key not in _warned:
        _warned.add(key)
        sys.stderr.write("medha: warning: %s\n" % message)


def model_error(message, **data):
    return MedhaError(message, kind="ModelError", data=data)


def http_json(url, body, headers, timeout=DEFAULT_TIMEOUT, retries=2, who="model API"):
    payload = json.dumps(body).encode("utf-8")
    headers = dict(headers)
    headers.setdefault("content-type", "application/json")
    headers.setdefault("user-agent", "medha-lang")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            detail = text
            try:
                err = json.loads(text).get("error")
                if isinstance(err, dict):
                    detail = err.get("message") or text
                elif isinstance(err, str):
                    detail = err
            except (ValueError, AttributeError):
                pass
            last = model_error("%s returned HTTP %d: %s" % (who, e.code, detail.strip()[:500]), status=e.code)
            if (e.code in (408, 409, 429) or e.code >= 500) and attempt < retries:
                delay = e.headers.get("retry-after") if e.headers else None
                try:
                    delay = min(float(delay), 30.0) if delay else 2.0 ** attempt
                except ValueError:
                    delay = 2.0 ** attempt
                time.sleep(delay)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            last = model_error("could not reach %s at %s: %s" % (who, url, reason))
            if attempt < retries:
                time.sleep(2.0 ** attempt)
                continue
            raise last
        except ValueError:
            raise model_error("%s returned a response that is not JSON" % who)
    raise last


# ---- Anthropic ---------------------------------------------------------

def _anthropic_messages(messages):
    out = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if m.get("is_error"):
                block["is_error"] = True
            # All results for one assistant turn must travel in a single user message.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][-1].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            raw = m.get("raw")
            if isinstance(raw, dict) and raw.get("anthropic"):
                # Echo the original content blocks (thinking included) unchanged.
                out.append({"role": "assistant", "content": raw["anthropic"]})
                continue
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


def anthropic_chat(model_id, system, messages, tools, opts):
    base = (opts.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    key_env = opts.get("api_key_env") or "ANTHROPIC_API_KEY"
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    betas = []
    if os.environ.get(key_env):
        headers["x-api-key"] = os.environ[key_env]
    elif os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        headers["authorization"] = "Bearer " + os.environ["ANTHROPIC_AUTH_TOKEN"]
        betas.append("oauth-2025-04-20")
    else:
        raise model_error("no Anthropic credentials: set %s (or run with --dry-run to use the mock provider)" % key_env)

    body = {"model": model_id, "max_tokens": int(opts.get("max_tokens") or DEFAULT_MAX_TOKENS),
            "messages": _anthropic_messages(messages)}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                         for t in tools]
    if opts.get("effort"):
        body["output_config"] = {"effort": opts["effort"]}
    if opts.get("stop"):
        stop = opts["stop"]
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if opts.get("fallbacks"):
        body["fallbacks"] = opts["fallbacks"]
        betas.append("server-side-fallback-2026-07-01")
    for knob in ("temperature", "top_p", "top_k"):
        if opts.get(knob) is not None:
            warn_once("anthropic-" + knob,
                      "the Anthropic API no longer accepts '%s'; ignoring it (use effort: \"low\"…\"max\" instead)" % knob)
    if betas:
        headers["anthropic-beta"] = ",".join(betas)

    data = http_json(base + "/v1/messages", body, headers, timeout=opts.get("timeout") or DEFAULT_TIMEOUT,
                     who="Anthropic API")
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
             for b in content if b.get("type") == "tool_use"]
    usage = data.get("usage") or {}
    stop = data.get("stop_reason") or "end_turn"
    if stop == "refusal":
        details = data.get("stop_details") or {}
        raise MedhaError("the model declined this request%s" % (
            " (%s)" % details["category"] if details.get("category") else ""),
            kind="ModelRefusal", data={"category": details.get("category")})
    return {"text": text, "tool_calls": calls, "stop": stop, "raw": {"anthropic": content},
            "usage": {"input": int(usage.get("input_tokens") or 0), "output": int(usage.get("output_tokens") or 0)}}


# ---- OpenAI-compatible -------------------------------------------------

def _compat_endpoint(provider, opts):
    base_env, base_default, key_env = OPENAI_COMPATIBLE[provider]
    base = (opts.get("base_url") or os.environ.get(base_env) or base_default).rstrip("/")
    key_env = opts.get("api_key_env") or key_env
    headers = {}
    if key_env:
        key = os.environ.get(key_env)
        if key:
            headers["authorization"] = "Bearer " + key
        elif not opts.get("base_url"):
            raise model_error("no %s credentials: set %s (or run with --dry-run to use the mock provider)"
                              % (provider, key_env))
    return base, headers


def _compat_messages(system, messages):
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif role == "assistant":
            msg = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                      "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                                     for tc in m["tool_calls"]]
            out.append(msg)
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


def compat_chat(provider, model_id, system, messages, tools, opts):
    base, headers = _compat_endpoint(provider, opts)
    body = {"model": model_id, "messages": _compat_messages(system, messages)}
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
    for knob in ("temperature", "top_p", "stop", "seed"):
        if opts.get(knob) is not None:
            body[knob] = opts[knob]
    if opts.get("max_tokens"):
        body["max_completion_tokens" if provider == "openai" else "max_tokens"] = int(opts["max_tokens"])
    if opts.get("effort"):
        body["reasoning_effort"] = opts["effort"]

    data = http_json(base + "/chat/completions", body, headers, timeout=opts.get("timeout") or DEFAULT_TIMEOUT,
                     who="%s API" % provider)
    choices = data.get("choices") or []
    if not choices:
        raise model_error("%s API returned no choices" % provider)
    msg = choices[0].get("message") or {}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {"_raw": args}
        calls.append({"id": tc.get("id") or "call_%d" % i, "name": fn.get("name", ""), "args": args})
    usage = data.get("usage") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return {"text": content, "tool_calls": calls, "stop": choices[0].get("finish_reason") or "stop", "raw": None,
            "usage": {"input": int(usage.get("prompt_tokens") or 0), "output": int(usage.get("completion_tokens") or 0)}}


def compat_embed(provider, model_id, texts, opts):
    base, headers = _compat_endpoint(provider, opts)
    data = http_json(base + "/embeddings", {"model": model_id, "input": texts}, headers,
                     timeout=opts.get("timeout") or DEFAULT_TIMEOUT, who="%s API" % provider)
    rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
    return [r["embedding"] for r in rows]


def chat(provider, model_id, system, messages, tools, opts):
    if provider == "anthropic":
        return anthropic_chat(model_id, system, messages, tools, opts)
    if provider in OPENAI_COMPATIBLE:
        return compat_chat(provider, model_id, system, messages, tools, opts)
    raise model_error("unknown model provider '%s' (known: %s)" % (provider, ", ".join(PROVIDERS)))
