"""llm.py — which brain is thinking, and how to talk to it.

Three providers, auto-detected in this order:

    anthropic   Claude, over the API. Best quality. Needs ANTHROPIC_API_KEY
                and costs money per turn.
    gemini      Google Gemini. Fast, generous free tier, needs only
                GEMINI_API_KEY from aistudio.google.com. Your prompts go to
                Google.
    ollama      A model running on this machine. Free, private, no key, no
                network. Quality depends on the model pulled.
    none        No model reachable. main.py falls back to keyword and
                file-score routing and says so on screen.

Pin one with JARVIS_LLM=anthropic|gemini|ollama|none. The default is auto,
which prefers Claude, then Gemini, then whatever is running locally.

Conversations are held in a neutral shape and converted per provider, so
the agent loop in main.py never learns either wire format:

    {"role": "user",      "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [{id, name, input}]}
    {"role": "tool",      "tool_call_id": "...", "name": "...",
                          "content": "..."}
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_reads": 0,
         "cache_writes": 0, "calls": 0, "provider": None}

ANTHROPIC_MODEL = os.environ.get("JARVIS_MODEL", "claude-opus-5")
EFFORT = os.environ.get("JARVIS_EFFORT", "low")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta"
# Not hardcoded as a single name: model ids go stale, and a wrong one is an
# opaque 404. The list endpoint decides, preferring flash for latency.
GEMINI_PREFER = ("flash-latest", "flash", "pro-latest", "pro")

# Ollama models that actually support tool calling. A model without it can
# still converse, but every tool would be dead, so JARVIS says so instead of
# silently losing half its abilities.
TOOL_CAPABLE = ("llama3.1", "llama3.2", "llama3.3", "qwen2.5", "qwen3",
                "mistral-nemo", "mistral-small", "mistral-large", "firefunction",
                "command-r", "hermes3", "granite3", "devstral", "magistral",
                "gpt-oss", "deepseek-r1", "smollm2")


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    b = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if b and os.path.exists(b):
        ctx.load_verify_locations(b)
    return ctx


def _post(url: str, payload: dict, headers: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return json.loads(r.read())


# ------------------------------------------------------------------ probing

def _anthropic_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_AI_API_KEY") or "").strip()


_gemini_models_cache: list[str] | None = None


def gemini_models(force: bool = False) -> list[str]:
    """Ask Google what this key can actually use, rather than guessing."""
    global _gemini_models_cache
    if _gemini_models_cache is not None and not force:
        return _gemini_models_cache
    key = _gemini_key()
    if not key:
        return []
    try:
        with urllib.request.urlopen(
                f"{GEMINI_HOST}/models?key={urllib.parse.quote(key)}",
                timeout=8, context=_ssl_ctx()) as r:
            raw = json.loads(r.read())
        out = [m["name"].split("/", 1)[-1] for m in raw.get("models", [])
               if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    except Exception:                                       # noqa: BLE001
        out = []
    _gemini_models_cache = out
    return out


def pick_gemini_model() -> str | None:
    pinned = os.environ.get("JARVIS_GEMINI_MODEL", "").strip()
    if pinned:
        return pinned
    available = gemini_models()
    if not available:
        return None
    for want in GEMINI_PREFER:
        for m in available:
            if want in m and "vision" not in m and "embedding" not in m:
                return m
    return available[0]


def ollama_models() -> list[str]:
    """What is actually pulled on this machine."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3) as r:
            return [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except Exception:                                       # noqa: BLE001
        return []


def ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3).close()
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _tool_capable(name: str) -> bool:
    return any(name.lower().startswith(p) for p in TOOL_CAPABLE)


def pick_ollama_model() -> str | None:
    """Prefer what the user pinned, then a tool-capable model, then anything."""
    pinned = os.environ.get("JARVIS_OLLAMA_MODEL", "").strip()
    installed = ollama_models()
    if pinned:
        return pinned
    if not installed:
        return None
    for m in installed:
        if _tool_capable(m):
            return m
    return installed[0]


def probe() -> dict:
    key = bool(_anthropic_key())
    gkey = bool(_gemini_key())
    gmodel = pick_gemini_model() if gkey else None
    installed = ollama_models()
    up = bool(installed) or ollama_reachable()
    chosen = pick_ollama_model()
    return {
        "anthropic": {
            "ok": key, "model": ANTHROPIC_MODEL if key else None,
            "why": "" if key else "ANTHROPIC_API_KEY not set",
        },
        "gemini": {
            "ok": gkey and bool(gmodel), "model": gmodel,
            "available": gemini_models() if gkey else [],
            "why": ("" if (gkey and gmodel) else
                    ("GEMINI_API_KEY not set — get one free at "
                     "aistudio.google.com/apikey" if not gkey else
                     "that key cannot reach any Gemini model")),
        },
        "ollama": {
            "ok": up and bool(chosen), "host": OLLAMA_HOST,
            "model": chosen, "installed": installed,
            "tools": bool(chosen and _tool_capable(chosen)),
            "why": ("" if (up and chosen) else
                    (f"nothing pulled yet — run `ollama pull llama3.1`"
                     if up else
                     f"no Ollama at {OLLAMA_HOST} — install it from ollama.com "
                     "and leave it running")),
        },
    }


def resolve() -> str:
    pinned = os.environ.get("JARVIS_LLM", "auto").strip().lower()
    if pinned and pinned != "auto":
        return pinned
    p = probe()
    for tier in ("anthropic", "gemini", "ollama"):
        if p[tier]["ok"]:
            return tier
    return "none"


# Published Anthropic rates per million tokens, at time of writing. Cached
# input reads at a tenth of the input rate. These drive the on-screen
# estimate only; check the pricing page before trusting them.
RATES = {
    "claude-opus-5":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}


def estimated_cost() -> dict:
    r = RATES.get(ANTHROPIC_MODEL)
    if not r or USAGE["provider"] != "anthropic":
        return {"usd": 0.0, "known": False}
    usd = (USAGE["input_tokens"] * r["in"]
           + USAGE["cache_reads"] * r["in"] * 0.1
           + USAGE["cache_writes"] * r["in"] * 1.25
           + USAGE["output_tokens"] * r["out"]) / 1_000_000
    per = usd / USAGE["calls"] if USAGE["calls"] else 0
    return {"usd": round(usd, 4), "per_turn": round(per, 4), "known": True,
            "calls": USAGE["calls"], "cached_tokens": USAGE["cache_reads"]}


def status() -> dict:
    p = probe()
    who = resolve()
    ok = who in ("anthropic", "ollama") and p.get(who, {}).get("ok", False)
    name = p[who]["model"] if who in p else None

    warn = ""
    if who == "ollama" and ok and not p["ollama"]["tools"]:
        warn = (f"{name} cannot call tools, so JARVIS can talk but not look "
                "anything up. Pull a tool-capable model — `ollama pull "
                "llama3.1` — for the full thing.")

    reason = ""
    if not ok:
        reason = ("No model reachable. Fastest free option: a Gemini key from "
                  "aistudio.google.com/apikey into GEMINI_API_KEY. Or install "
                  "Ollama from ollama.com for one that never leaves this "
                  "machine. Until then I route by keyword and file score, and "
                  "I say so.")
    return {"provider": who, "ok": ok, "name": name, "reason": reason,
            "warn": warn, "free": who in ("ollama", "gemini"),
            "private": who == "ollama",
            "auto": os.environ.get("JARVIS_LLM", "auto").strip().lower() == "auto",
            "tools": p["ollama"]["tools"] if who == "ollama" else True,
            "providers": p, "usage": USAGE, "cost": estimated_cost()}


# ------------------------------------------------------------------ anthropic

def _to_anthropic(history: list[dict]) -> list[dict]:
    """Neutral history -> Anthropic messages. Consecutive tool results must
    be batched into a single user message or the API rejects them."""
    out: list[dict] = []
    pending: list[dict] = []

    def flush():
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for m in history:
        if m["role"] == "tool":
            pending.append({"type": "tool_result",
                            "tool_use_id": m["tool_call_id"],
                            "content": m["content"]})
            continue
        flush()
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        else:
            blocks: list[dict] = []
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"],
                               "name": c["name"], "input": c["input"]})
            out.append({"role": "assistant", "content": blocks or [
                {"type": "text", "text": ""}]})
    flush()
    return out


def _chat_anthropic(history: list[dict], system: str, tool_schema: list[dict]) -> dict:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    t0 = time.time()
    # The tools and the system prompt are identical on every turn and are
    # most of the input, so they are cached. Render order is tools ->
    # system -> messages, so one breakpoint at the end of system covers
    # both. Without this, every question re-pays for ~3k tokens of identity
    # and tool definitions it has already sent.
    tools_cached = [dict(t) for t in tool_schema]
    if tools_cached:
        tools_cached[-1]["cache_control"] = {"type": "ephemeral"}

    out = _post(base + "/v1/messages", {
        "model": ANTHROPIC_MODEL,
        # Generous, because adaptive thinking spends from this budget too.
        # Answers stay short because the prompt says so, not because the
        # response gets cut off mid-sentence.
        "max_tokens": 4096,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "tools": tools_cached,
        "messages": _to_anthropic(history),
    }, {"x-api-key": _anthropic_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, timeout=120)

    wall = int((time.time() - t0) * 1000)
    u = out.get("usage", {})
    USAGE["input_tokens"] += u.get("input_tokens", 0)
    USAGE["output_tokens"] += u.get("output_tokens", 0)
    USAGE["cache_reads"] += u.get("cache_read_input_tokens", 0)
    USAGE["cache_writes"] += u.get("cache_creation_input_tokens", 0)
    USAGE["calls"] += 1
    USAGE["provider"] = "anthropic"

    text = " ".join(b.get("text", "") for b in out.get("content", [])
                    if b.get("type") == "text").strip()
    calls = [{"id": b["id"], "name": b["name"], "input": b.get("input", {})}
             for b in out.get("content", []) if b.get("type") == "tool_use"]
    return {"text": text, "tool_calls": calls, "provider": "anthropic",
            "model": ANTHROPIC_MODEL,
            "ms": {"total": wall,
                   "prompt_tokens": u.get("input_tokens", 0),
                   "output_tokens": u.get("output_tokens", 0),
                   "cached": u.get("cache_read_input_tokens", 0)}}


# ------------------------------------------------------------------ ollama

def _to_ollama_tools(tool_schema: list[dict]) -> list[dict]:
    """Anthropic tool shape -> the OpenAI-style shape Ollama expects."""
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t["description"],
                          "parameters": t["input_schema"]}}
            for t in tool_schema]


def _to_ollama(history: list[dict], system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("text") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["input"]}}
                    for c in m["tool_calls"]]
            out.append(msg)
        else:
            out.append({"role": "tool", "content": m["content"],
                        "tool_name": m.get("name", "")})
    return out


def _chat_ollama(history: list[dict], system: str, tool_schema: list[dict]) -> dict:
    model = pick_ollama_model()
    if not model:
        raise RuntimeError(probe()["ollama"]["why"])
    body = {
        "model": model,
        "messages": _to_ollama(history, system),
        "stream": False,
        # keep_alive stops Ollama evicting the model between turns. Without
        # it every question pays the load cost again, which on a laptop is
        # most of the wait.
        "keep_alive": os.environ.get("JARVIS_OLLAMA_KEEPALIVE", "30m"),
        "options": {
            "temperature": float(os.environ.get("JARVIS_OLLAMA_TEMP", "0.4")),
            "num_ctx": int(os.environ.get("JARVIS_OLLAMA_CTX", "4096")),
            # Spoken answers are one or two sentences. Without a cap a local
            # model will happily generate for a minute.
            "num_predict": int(os.environ.get("JARVIS_OLLAMA_PREDICT", "220")),
        },
    }
    if _tool_capable(model):
        body["tools"] = _to_ollama_tools(tool_schema)

    t0 = time.time()
    out = _post(f"{OLLAMA_HOST}/api/chat", body,
                {"content-type": "application/json"},
                timeout=int(os.environ.get("JARVIS_OLLAMA_TIMEOUT", "300")))
    wall = int((time.time() - t0) * 1000)

    USAGE["input_tokens"] += out.get("prompt_eval_count", 0)
    USAGE["output_tokens"] += out.get("eval_count", 0)
    USAGE["calls"] += 1
    USAGE["provider"] = "ollama"

    msg = out.get("message", {}) or {}
    calls = []
    for i, c in enumerate(msg.get("tool_calls") or []):
        fn = c.get("function", {}) or {}
        args = fn.get("arguments", {})
        if isinstance(args, str):                 # some builds return a string
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        # Ollama does not issue call ids; the loop needs one to pair results.
        calls.append({"id": f"ollama-{USAGE['calls']}-{i}",
                      "name": fn.get("name", ""), "input": args or {}})
    # Ollama reports its own nanosecond timings; they say whether the wait
    # was loading the model, reading the prompt, or generating.
    ns = lambda k: int(out.get(k, 0) / 1e6)
    return {"text": (msg.get("content") or "").strip(), "tool_calls": calls,
            "provider": "ollama", "model": model,
            "ms": {"total": wall, "load": ns("load_duration"),
                   "prompt": ns("prompt_eval_duration"),
                   "generate": ns("eval_duration"),
                   "prompt_tokens": out.get("prompt_eval_count", 0),
                   "output_tokens": out.get("eval_count", 0)}}


# ------------------------------------------------------------------ gemini

def _to_gemini_tools(tool_schema: list[dict]) -> list[dict]:
    """Gemini takes OpenAPI-subset schemas. Strip what it rejects rather
    than letting it 400 on a keyword it does not know."""
    allowed = {"type", "description", "properties", "required", "items",
               "enum", "nullable"}

    def clean(node):
        if not isinstance(node, dict):
            return node
        out = {k: v for k, v in node.items() if k in allowed}
        if "properties" in out:
            out["properties"] = {k: clean(v) for k, v in out["properties"].items()}
        if "items" in out:
            out["items"] = clean(out["items"])
        return out

    return [{"function_declarations": [
        {"name": t["name"], "description": t["description"],
         "parameters": clean(t["input_schema"])}
        for t in tool_schema]}]


def _to_gemini(history: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "parts": [{"text": m["text"]}]})
        elif m["role"] == "assistant":
            parts = []
            if m.get("text"):
                parts.append({"text": m["text"]})
            for c in m.get("tool_calls") or []:
                parts.append({"functionCall": {"name": c["name"],
                                               "args": c["input"]}})
            out.append({"role": "model", "parts": parts or [{"text": ""}]})
        else:
            # Tool results come back as a user turn carrying functionResponse.
            out.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name", "tool"),
                "response": {"result": m["content"]}}}]})
    return out


def _chat_gemini(history: list[dict], system: str, tool_schema: list[dict]) -> dict:
    model = pick_gemini_model()
    if not model:
        raise RuntimeError(probe()["gemini"]["why"])
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": _to_gemini(history),
        "tools": _to_gemini_tools(tool_schema),
        "generationConfig": {
            "temperature": float(os.environ.get("JARVIS_GEMINI_TEMP", "0.4")),
            "maxOutputTokens": int(os.environ.get("JARVIS_GEMINI_TOKENS", "800")),
        },
    }
    t0 = time.time()
    out = _post(f"{GEMINI_HOST}/models/{model}:generateContent"
                f"?key={urllib.parse.quote(_gemini_key())}",
                body, {"content-type": "application/json"}, timeout=90)
    wall = int((time.time() - t0) * 1000)

    u = out.get("usageMetadata", {})
    USAGE["input_tokens"] += u.get("promptTokenCount", 0)
    USAGE["output_tokens"] += u.get("candidatesTokenCount", 0)
    USAGE["calls"] += 1
    USAGE["provider"] = "gemini"

    cands = out.get("candidates") or []
    parts = (cands[0].get("content", {}) if cands else {}).get("parts") or []
    text = " ".join(p["text"] for p in parts if "text" in p).strip()
    calls = []
    for i, p in enumerate(parts):
        fc = p.get("functionCall")
        if fc:
            calls.append({"id": f"gemini-{USAGE['calls']}-{i}",
                          "name": fc.get("name", ""),
                          "input": fc.get("args") or {}})
    if not text and not calls and cands:
        # A safety block or an empty candidate: say so rather than going mute.
        reason = cands[0].get("finishReason") or "no content"
        text = f"Gemini returned nothing, Sir ({reason})."
    return {"text": text, "tool_calls": calls, "provider": "gemini",
            "model": model,
            "ms": {"total": wall,
                   "prompt_tokens": u.get("promptTokenCount", 0),
                   "output_tokens": u.get("candidatesTokenCount", 0)}}


# ------------------------------------------------------------------ api

def wants_compact_prompt() -> bool:
    """A local 3B model reading eight kilobytes of identity before every
    answer spends most of its time on the prompt, not the question."""
    return resolve() in ("ollama", "gemini") and os.environ.get(
        "JARVIS_FULL_PROMPT", "").strip().lower() not in ("1", "true", "yes")


def chat(history: list[dict], system: str, tool_schema: list[dict]) -> dict:
    """One turn. Returns {text, tool_calls, provider, model, ms}."""
    who = resolve()
    if who == "anthropic":
        return _chat_anthropic(history, system, tool_schema)
    if who == "gemini":
        return _chat_gemini(history, system, tool_schema)
    if who == "ollama":
        return _chat_ollama(history, system, tool_schema)
    raise RuntimeError(status()["reason"])


def available() -> bool:
    return resolve() in ("anthropic", "gemini", "ollama")


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
