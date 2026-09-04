"""llm.py — which brain is thinking, and how to talk to it.

Three providers, auto-detected in this order:

    anthropic   Claude, over the API. Best quality. Needs ANTHROPIC_API_KEY
                and costs money per turn.
    ollama      A model running on this machine. Free, private, no key, no
                network. Quality depends on the model pulled.
    none        No model reachable. main.py falls back to keyword and
                file-score routing and says so on screen.

Pin one with JARVIS_LLM=anthropic|ollama|none. The default is auto.

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
import urllib.error
import urllib.request

USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": None}

ANTHROPIC_MODEL = os.environ.get("JARVIS_MODEL", "claude-opus-5")
EFFORT = os.environ.get("JARVIS_EFFORT", "low")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

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
    installed = ollama_models()
    up = bool(installed) or ollama_reachable()
    chosen = pick_ollama_model()
    return {
        "anthropic": {
            "ok": key, "model": ANTHROPIC_MODEL if key else None,
            "why": "" if key else "ANTHROPIC_API_KEY not set",
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
    if p["anthropic"]["ok"]:
        return "anthropic"
    if p["ollama"]["ok"]:
        return "ollama"
    return "none"


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
        reason = ("No model reachable. Either set ANTHROPIC_API_KEY, or "
                  "install Ollama from ollama.com and run `ollama pull "
                  "llama3.1` — that one is free and runs on your machine. "
                  "Until then I route by keyword and file score, and I say so.")
    return {"provider": who, "ok": ok, "name": name, "reason": reason,
            "warn": warn, "free": who == "ollama",
            "auto": os.environ.get("JARVIS_LLM", "auto").strip().lower() == "auto",
            "tools": p["ollama"]["tools"] if who == "ollama" else True,
            "providers": p, "usage": USAGE}


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
    out = _post(base + "/v1/messages", {
        "model": ANTHROPIC_MODEL,
        # Generous, because adaptive thinking spends from this budget too.
        # Answers stay short because the prompt says so, not because the
        # response gets cut off mid-sentence.
        "max_tokens": 4096,
        "system": system,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "tools": tool_schema,
        "messages": _to_anthropic(history),
    }, {"x-api-key": _anthropic_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, timeout=120)

    u = out.get("usage", {})
    USAGE["input_tokens"] += u.get("input_tokens", 0)
    USAGE["output_tokens"] += u.get("output_tokens", 0)
    USAGE["calls"] += 1
    USAGE["provider"] = "anthropic"

    text = " ".join(b.get("text", "") for b in out.get("content", [])
                    if b.get("type") == "text").strip()
    calls = [{"id": b["id"], "name": b["name"], "input": b.get("input", {})}
             for b in out.get("content", []) if b.get("type") == "tool_use"]
    return {"text": text, "tool_calls": calls, "provider": "anthropic",
            "model": ANTHROPIC_MODEL}


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
        "options": {"temperature": float(os.environ.get("JARVIS_OLLAMA_TEMP", "0.4")),
                    "num_ctx": int(os.environ.get("JARVIS_OLLAMA_CTX", "8192"))},
    }
    if _tool_capable(model):
        body["tools"] = _to_ollama_tools(tool_schema)

    out = _post(f"{OLLAMA_HOST}/api/chat", body,
                {"content-type": "application/json"},
                timeout=int(os.environ.get("JARVIS_OLLAMA_TIMEOUT", "300")))

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
    return {"text": (msg.get("content") or "").strip(), "tool_calls": calls,
            "provider": "ollama", "model": model}


# ------------------------------------------------------------------ api

def chat(history: list[dict], system: str, tool_schema: list[dict]) -> dict:
    """One turn. Returns {text, tool_calls, provider, model}."""
    who = resolve()
    if who == "anthropic":
        return _chat_anthropic(history, system, tool_schema)
    if who == "ollama":
        return _chat_ollama(history, system, tool_schema)
    raise RuntimeError(status()["reason"])


def available() -> bool:
    return resolve() in ("anthropic", "ollama")


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
