"""voice.py — speech in and speech out, using whatever this machine has.

The key, if there is one, lives here on the server and never reaches the
browser. The page posts text to /api/speak and audio to /api/listen and gets
back mp3/wav bytes or a transcript.

TIERS, best first. `JARVIS_VOICE=auto` (the default) probes the machine and
picks the highest tier that actually works, then says on screen which one is
live. Set it explicitly to pin one.

    openai      whisper-1 in, tts-1 out            needs OPENAI_API_KEY
    elevenlabs  scribe_v1 in, eleven turbo out     needs ELEVENLABS_API_KEY
    local       whisper.cpp in, OS voice out       free, private, no cloud
    none        text only, and it says so

Audio arrives as 16 kHz mono WAV, encoded in the browser. That is
deliberate: whisper.cpp cannot read WebM/Opus without ffmpeg, and every
cloud API accepts WAV, so one format serves every tier with nothing to
install.

Note on the browser's Web Speech API: still not used. Chrome-only, ships
audio to Google, and a silent stub in Brave.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

IS_WINDOWS = sys.platform == "win32"

# Guardrail: never spend without asking. Published rates at time of writing;
# the meter is an estimate, not a bill. Local tiers cost nothing and are not
# metered at all.
RATES = {"whisper-1": 0.006 / 60, "tts-1": 15.0 / 1_000_000,
         "scribe_v1": 0.006 / 60, "eleven_tts": 0.30 / 1000}
SESSION_CAP_USD = float(os.environ.get("JARVIS_VOICE_CAP_USD", "0.50"))
_spend = {"usd": 0.0, "calls": 0}


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
    b = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if b and os.path.exists(b):
        ctx.load_verify_locations(b)
    return ctx


def _post(url: str, data: bytes, headers: dict, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read()


def _multipart(fields: dict, filename: str, filedata: bytes,
               fieldname: str = "file", mime: str = "audio/wav") -> tuple[bytes, str]:
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


def _key(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ---------------------------------------------------------------- probing

WHISPER_NAMES = ["whisper-cli", "whisper-cli.exe", "main.exe", "whisper.exe",
                 "whisper-cpp", "whisper"]


def find_whisper() -> str | None:
    explicit = os.environ.get("JARVIS_WHISPER_BIN", "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) or shutil.which(explicit) else None
    for n in WHISPER_NAMES:
        found = shutil.which(n)
        if found:
            return found
    return None


def find_whisper_model() -> str | None:
    m = os.environ.get("JARVIS_WHISPER_MODEL", "").strip()
    return m if m and os.path.isfile(m) else None


def find_tts() -> tuple[str, str] | None:
    """(kind, binary) for local speech-out."""
    if IS_WINDOWS:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        return ("sapi", ps) if ps else None
    say = shutil.which("say")
    if say:
        return ("say", say)
    for n in ("piper", "espeak-ng", "espeak"):
        found = shutil.which(n)
        if found:
            return (n.split("-")[0], found)
    return None


def probe() -> dict:
    """Everything this machine can actually do for voice, right now."""
    whisper, model, tts = find_whisper(), find_whisper_model(), find_tts()
    return {
        "openai": {"ok": bool(_key("OPENAI_API_KEY")),
                   "in": "whisper-1", "out": "tts-1",
                   "why": "" if _key("OPENAI_API_KEY") else "OPENAI_API_KEY not set"},
        "elevenlabs": {"ok": bool(_key("ELEVENLABS_API_KEY")),
                       "in": "scribe_v1", "out": "eleven_turbo_v2_5",
                       "why": "" if _key("ELEVENLABS_API_KEY") else "ELEVENLABS_API_KEY not set"},
        "local": {
            "ok": bool(whisper and tts),
            "in": whisper or None, "out": (tts[1] if tts else None),
            "out_kind": (tts[0] if tts else None),
            "model": model,
            "why": ("" if (whisper and tts) else
                    ("no whisper.cpp binary on PATH — install it and either put "
                     "whisper-cli on PATH or set JARVIS_WHISPER_BIN"
                     if not whisper else "no local speech-out found")),
        },
    }


def resolve() -> str:
    """Which tier is actually live."""
    pinned = os.environ.get("JARVIS_VOICE", "auto").strip().lower()
    if pinned and pinned != "auto":
        return pinned
    p = probe()
    for tier in ("openai", "elevenlabs", "local"):
        if p[tier]["ok"]:
            return tier
    return "none"


def provider() -> str:
    return resolve()


def status() -> dict:
    """Degrade loudly. The UI renders this verbatim when something is off."""
    p = probe()
    tier = resolve()
    ok, why = True, ""

    if tier == "none":
        ok = False
        why = ("No voice available. Cheapest fix: install whisper.cpp for "
               "speech-in — speech-out already works on Windows through the "
               "built-in voice. Or set OPENAI_API_KEY.")
    elif tier in ("openai", "elevenlabs", "local"):
        if not p[tier]["ok"]:
            ok, why = False, p[tier]["why"]
    else:
        ok, why = False, f"unknown voice tier {tier!r}"

    # Local speech-out can work even when local speech-in cannot; say so
    # rather than reporting a flat failure.
    half = None
    if not ok and p["local"]["out"]:
        half = "speech-out works; speech-in does not"

    if ok and tier in ("openai", "elevenlabs") and _cap_hit():
        ok, why = False, (f"voice session cap of ${SESSION_CAP_USD:.2f} reached — "
                          "raise JARVIS_VOICE_CAP_USD, or switch to the local "
                          "tier which costs nothing")

    return {"provider": tier, "ok": ok, "reason": why, "partial": half,
            "auto": os.environ.get("JARVIS_VOICE", "auto").strip().lower() == "auto",
            "tiers": p, "free": tier == "local",
            "tts_voice": os.environ.get("JARVIS_TTS_VOICE", "onyx"),
            "spend": spend()}


# ---------------------------------------------------------------- speech in

def transcribe(audio: bytes, mime: str = "audio/wav") -> dict:
    st = status()
    tier = st["provider"]
    if not audio:
        return {"ok": False, "error": "empty recording — is the mic actually open?"}
    if not st["ok"] and tier != "local":
        return {"ok": False, "error": st["reason"], "provider": tier}

    try:
        if tier == "openai":
            fields = {"model": "whisper-1", "response_format": "json"}
            lang = os.environ.get("JARVIS_STT_LANG", "").strip()
            if lang:
                fields["language"] = lang
            body, ctype = _multipart(fields, "turn.wav", audio, "file", mime)
            raw = _post("https://api.openai.com/v1/audio/transcriptions", body,
                        {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}",
                         "Content-Type": ctype})
            _charge(RATES["whisper-1"] * max(1.0, len(audio) / 32000))
            return {"ok": True, "text": json.loads(raw).get("text", "").strip(),
                    "provider": "openai/whisper-1"}

        if tier == "elevenlabs":
            body, ctype = _multipart({"model_id": "scribe_v1"}, "turn.wav",
                                     audio, "file", mime)
            raw = _post("https://api.elevenlabs.io/v1/speech-to-text", body,
                        {"xi-api-key": _key("ELEVENLABS_API_KEY"),
                         "Content-Type": ctype})
            _charge(RATES["scribe_v1"] * max(1.0, len(audio) / 32000))
            return {"ok": True, "text": json.loads(raw).get("text", "").strip(),
                    "provider": "elevenlabs/scribe_v1"}

        if tier == "local":
            binary = find_whisper()
            if not binary:
                return {"ok": False, "provider": "local",
                        "error": probe()["local"]["why"]}
            model = find_whisper_model()
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(audio)
            tmp.close()
            try:
                cmd = [binary, "-f", tmp.name, "-nt", "-np"]
                if model:
                    cmd += ["-m", model]
                out = subprocess.run(cmd, capture_output=True, timeout=180)
                text = out.stdout.decode("utf-8", "replace").strip()
                if not text and out.returncode != 0:
                    return {"ok": False, "provider": "local",
                            "error": "whisper failed: " +
                                     out.stderr.decode("utf-8", "replace")[:200]}
                return {"ok": True, "text": text, "provider": f"local/{os.path.basename(binary)}"}
            finally:
                os.unlink(tmp.name)

    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return {"ok": False, "error": f"transcriber returned {e.code}: {detail}",
                "provider": tier}
    except subprocess.TimeoutExpired:
        return {"ok": False, "provider": tier,
                "error": "local whisper took too long — try a smaller model"}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": f"transcriber unreachable: {e}", "provider": tier}

    return {"ok": False, "error": f"no speech-in available ({tier})", "provider": tier}


# ---------------------------------------------------------------- speech out

_SAPI = r"""
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText('{txt}', [System.Text.Encoding]::UTF8)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
{voice}
$s.Rate = {rate}
$s.SetOutputToWaveFile('{wav}')
$s.Speak($text)
$s.Dispose()
"""


def _sapi_speak(ps: str, text: str) -> bytes:
    """Windows built-in voice. Text goes via a file so quoting cannot break it."""
    txt = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w",
                                      encoding="utf-8")
    txt.write(text)
    txt.close()
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.close()
    name = os.environ.get("JARVIS_SAPI_VOICE", "").strip()
    script = _SAPI.format(
        txt=txt.name.replace("'", "''"), wav=wav.name.replace("'", "''"),
        rate=os.environ.get("JARVIS_SAPI_RATE", "1"),
        voice=(f"$s.SelectVoice('{name}')" if name else ""))
    try:
        subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=90)
        return open(wav.name, "rb").read()
    finally:
        for p in (txt.name, wav.name):
            try:
                os.unlink(p)
            except OSError:
                pass


def speak(text: str) -> dict:
    st = status()
    tier = st["provider"]
    text = (text or "").strip()[:2000]
    if not text:
        return {"ok": False, "error": "nothing to say"}

    # Speech-out may work even when the chosen tier is off overall.
    if not st["ok"] and tier != "local":
        local_out = find_tts()
        if local_out:
            tier = "local"
        else:
            return {"ok": False, "error": st["reason"], "provider": st["provider"]}

    try:
        if tier == "openai":
            payload = json.dumps({
                "model": os.environ.get("JARVIS_TTS_MODEL", "tts-1"),
                "voice": os.environ.get("JARVIS_TTS_VOICE", "onyx"),
                "input": text, "response_format": "mp3"}).encode()
            audio = _post("https://api.openai.com/v1/audio/speech", payload,
                          {"Authorization": f"Bearer {_key('OPENAI_API_KEY')}",
                           "Content-Type": "application/json"})
            _charge(RATES["tts-1"] * len(text))
            return {"ok": True, "audio": audio, "mime": "audio/mpeg",
                    "provider": "openai/tts-1"}

        if tier == "elevenlabs":
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

        if tier == "local":
            found = find_tts()
            if not found:
                return {"ok": False, "error": "no local speech-out on this machine"}
            kind, binary = found
            if kind == "sapi":
                return {"ok": True, "audio": _sapi_speak(binary, text),
                        "mime": "audio/wav", "provider": "windows/sapi"}
            wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            wav.close()
            try:
                if kind == "say":
                    subprocess.run([binary, "-o", wav.name,
                                    "--data-format=LEI16@22050", text],
                                   capture_output=True, timeout=90)
                elif kind == "piper":
                    subprocess.run([binary, "-f", wav.name], input=text.encode(),
                                   capture_output=True, timeout=90)
                else:
                    subprocess.run([binary, "-w", wav.name, text],
                                   capture_output=True, timeout=90)
                return {"ok": True, "audio": open(wav.name, "rb").read(),
                        "mime": "audio/wav", "provider": f"local/{kind}"}
            finally:
                try:
                    os.unlink(wav.name)
                except OSError:
                    pass

    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return {"ok": False, "error": f"speech returned {e.code}: {detail}",
                "provider": tier}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": f"speech unreachable: {e}", "provider": tier}

    return {"ok": False, "error": f"no speech-out available ({tier})", "provider": tier}


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
