"""voice.py — speech in and speech out.

The API key lives here, on the server, and never reaches the browser.
The page posts text to /api/speak and audio to /api/listen; it receives
mp3 bytes or a transcript back and nothing else.

Providers sit behind one interface so the stack can be swapped with an
env var rather than a rewrite:

    JARVIS_VOICE=openai      whisper-1 in, tts-1 out      (default)
    JARVIS_VOICE=elevenlabs  scribe_v1 in, eleven tts out
    JARVIS_VOICE=local       whisper.cpp in, OS voice out
    JARVIS_VOICE=none        text only, and it says so on screen

Note on the browser's Web Speech API: deliberately not used. It is
Chrome-only, it ships audio to Google, and in Brave it is a stub that
fails silently. MediaRecorder plus server-side transcription works
everywhere and fails loudly.
"""

from __future__ import annotations

import os
import io
import json
import time
import shutil
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------- cost

# Guardrail: never spend without asking. These are the published rates at
# the time of writing; the meter is an estimate, not a bill. When the
# session cap is hit, voice stops and says so rather than quietly running on.
RATES = {
    "whisper-1": 0.006 / 60,        # USD per second of audio
    "tts-1": 15.0 / 1_000_000,      # USD per character
    "scribe_v1": 0.006 / 60,
    "eleven_tts": 0.30 / 1000,
}
SESSION_CAP_USD = float(os.environ.get("JARVIS_VOICE_CAP_USD", "0.50"))

_spend = {"usd": 0.0, "calls": 0, "since": time.time()}


def _charge(usd: float) -> None:
    _spend["usd"] += usd
    _spend["calls"] += 1


def spend() -> dict:
    return {"usd": round(_spend["usd"], 4), "calls": _spend["calls"],
            "cap_usd": SESSION_CAP_USD,
            "remaining_usd": round(max(0.0, SESSION_CAP_USD - _spend["usd"]), 4)}


def _cap_hit() -> bool:
    return _spend["usd"] >= SESSION_CAP_USD


# ---------------------------------------------------------------- http

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and os.path.exists(bundle):
        ctx.load_verify_locations(bundle)
    return ctx


def _post(url: str, data: bytes, headers: dict, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read()


def _multipart(fields: dict, filename: str, filedata: bytes,
               fieldname: str = "file", mime: str = "audio/webm") -> tuple[bytes, str]:
    boundary = "----jarvis" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        out.write(f"{v}\r\n".encode())
    out.write(f"--{boundary}\r\n".encode())
    out.write(f'Content-Disposition: form-data; name="{fieldname}"; '
              f'filename="{filename}"\r\n'.encode())
    out.write(f"Content-Type: {mime}\r\n\r\n".encode())
    out.write(filedata)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- config

def provider() -> str:
    return os.environ.get("JARVIS_VOICE", "openai").strip().lower()


def _key(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def status() -> dict:
    """Degrade loudly. The UI renders this verbatim when something is off."""
    p = provider()
    ok, why = True, ""
    if p == "openai" and not _key("OPENAI_API_KEY"):
        ok, why = False, "OPENAI_API_KEY is not set — voice is off, text still works"
    elif p == "elevenlabs" and not _key("ELEVENLABS_API_KEY"):
        ok, why = False, "ELEVENLABS_API_KEY is not set — voice is off, text still works"
    elif p == "local":
        if not shutil.which(os.environ.get("JARVIS_WHISPER_BIN", "whisper-cli")):
            ok, why = False, "local whisper binary not found on PATH"
    elif p == "none":
        ok, why = False, "voice disabled (JARVIS_VOICE=none)"
    if ok and _cap_hit():
        ok, why = False, (f"voice session cap of ${SESSION_CAP_USD:.2f} reached — "
                          "raise JARVIS_VOICE_CAP_USD to continue")
    return {"provider": p, "ok": ok, "reason": why,
            "tts_voice": os.environ.get("JARVIS_TTS_VOICE", "onyx"),
            "spend": spend()}


# ---------------------------------------------------------------- speech in

def transcribe(audio: bytes, mime: str = "audio/webm") -> dict:
    st = status()
    if not st["ok"]:
        return {"ok": False, "error": st["reason"], "provider": st["provider"]}
    if not audio:
        return {"ok": False, "error": "empty recording — is the mic actually open?"}

    p = st["provider"]
    try:
        if p == "openai":
            fields = {"model": "whisper-1", "response_format": "json"}
            lang = os.environ.get("JARVIS_STT_LANG", "").strip()
            if lang:
                fields["language"] = lang
            body, ctype = _multipart(fields, "turn.webm", audio, "file", mime)
            raw = _post("https://api.openai.com/v1/audio/transcriptions", body,
                        {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}",
                         "Content-Type": ctype})
            _charge(RATES["whisper-1"] * max(1.0, len(audio) / 16000))
            return {"ok": True, "text": json.loads(raw).get("text", "").strip(),
                    "provider": "openai/whisper-1"}

        if p == "elevenlabs":
            body, ctype = _multipart({"model_id": "scribe_v1"}, "turn.webm",
                                     audio, "file", mime)
            raw = _post("https://api.elevenlabs.io/v1/speech-to-text", body,
                        {"xi-api-key": _key("ELEVENLABS_API_KEY"),
                         "Content-Type": ctype})
            _charge(RATES["scribe_v1"] * max(1.0, len(audio) / 16000))
            return {"ok": True, "text": json.loads(raw).get("text", "").strip(),
                    "provider": "elevenlabs/scribe_v1"}

        if p == "local":
            binary = os.environ.get("JARVIS_WHISPER_BIN", "whisper-cli")
            model = os.environ.get("JARVIS_WHISPER_MODEL", "")
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
                f.write(audio)
                tmp = f.name
            try:
                cmd = [binary, "-f", tmp, "-nt"] + (["-m", model] if model else [])
                out = subprocess.run(cmd, capture_output=True, timeout=120)
                return {"ok": True, "text": out.stdout.decode("utf-8", "replace").strip(),
                        "provider": f"local/{os.path.basename(binary)}"}
            finally:
                os.unlink(tmp)

    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return {"ok": False, "error": f"transcriber returned {e.code}: {detail}",
                "provider": p}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": f"transcriber unreachable: {e}", "provider": p}

    return {"ok": False, "error": f"unknown voice provider '{p}'", "provider": p}


# ---------------------------------------------------------------- speech out

def speak(text: str) -> dict:
    st = status()
    if not st["ok"]:
        return {"ok": False, "error": st["reason"], "provider": st["provider"]}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to say"}
    text = text[:2000]

    p = st["provider"]
    try:
        if p == "openai":
            payload = json.dumps({
                "model": os.environ.get("JARVIS_TTS_MODEL", "tts-1"),
                "voice": os.environ.get("JARVIS_TTS_VOICE", "onyx"),
                "input": text,
                "response_format": "mp3",
            }).encode()
            audio = _post("https://api.openai.com/v1/audio/speech", payload,
                          {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}",
                           "Content-Type": "application/json"})
            _charge(RATES["tts-1"] * len(text))
            return {"ok": True, "audio": audio, "mime": "audio/mpeg",
                    "provider": "openai/tts-1"}

        if p == "elevenlabs":
            vid = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
            payload = json.dumps({"text": text,
                                  "model_id": "eleven_turbo_v2_5"}).encode()
            audio = _post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                          payload, {"xi-api-key": _key("ELEVENLABS_API_KEY"),
                                    "Content-Type": "application/json",
                                    "Accept": "audio/mpeg"})
            _charge(RATES["eleven_tts"] * len(text))
            return {"ok": True, "audio": audio, "mime": "audio/mpeg",
                    "provider": "elevenlabs"}

        if p == "local":
            say = shutil.which("say") or shutil.which("espeak-ng") or shutil.which("espeak")
            if not say:
                return {"ok": False, "error": "no local speech binary (say/espeak)"}
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            try:
                if say.endswith("say"):
                    subprocess.run([say, "-o", tmp, "--data-format=LEF32@22050", text],
                                   capture_output=True, timeout=60)
                else:
                    subprocess.run([say, "-w", tmp, text], capture_output=True, timeout=60)
                return {"ok": True, "audio": open(tmp, "rb").read(),
                        "mime": "audio/wav", "provider": f"local/{os.path.basename(say)}"}
            finally:
                os.path.exists(tmp) and os.unlink(tmp)

    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return {"ok": False, "error": f"speech returned {e.code}: {detail}", "provider": p}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": f"speech unreachable: {e}", "provider": p}

    return {"ok": False, "error": f"unknown voice provider '{p}'", "provider": p}
