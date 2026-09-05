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


# whisper.cpp ships no weights: `brew install whisper-cpp` gives you the
# program and nothing to run. Rather than fail obscurely, look where the
# model usually ends up.
MODEL_DIRS = ["~/.cache/whisper", "~/Library/Application Support/whisper",
              "~/whisper", "~/models", "~/Downloads",
              "/opt/homebrew/share/whisper-cpp/models",
              "/usr/local/share/whisper-cpp/models",
              "/usr/share/whisper.cpp/models"]
MODEL_PREF = ("base.en", "small.en", "medium.en", "base", "small", "tiny.en", "tiny")


def find_whisper_model() -> str | None:
    m = os.environ.get("JARVIS_WHISPER_MODEL", "").strip()
    if m:
        return m if os.path.isfile(os.path.expanduser(m)) else None
    found: list[str] = []
    for d in MODEL_DIRS:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        try:
            found += [os.path.join(d, f) for f in os.listdir(d)
                      if f.startswith("ggml-") and f.endswith(".bin")]
        except OSError:
            continue
    if not found:
        return None
    # Prefer accuracy that still runs fast on a laptop.
    for want in MODEL_PREF:
        for f in found:
            if os.path.basename(f) == f"ggml-{want}.bin":
                return f
    return found[0]


def find_os_stt() -> tuple[str, str] | None:
    """Speech recognition that is already on the machine, no download.

    Windows ships System.Speech.Recognition; it is markedly less accurate
    than Whisper but it is offline, private and needs nothing installed.
    macOS has no scriptable offline recogniser, so there is no equivalent —
    on a Mac, whisper.cpp is one `brew install whisper-cpp` away.
    """
    if IS_WINDOWS:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        return ("winsr", ps) if ps else None
    return None


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


# Listening and speaking are separate capabilities and are resolved
# separately. Every machine can already speak — macOS has `say`, Windows has
# SAPI, Linux usually has espeak — so treating voice as one on/off switch
# silenced output that worked perfectly whenever speech-in was missing.

WHISPER_HINT = {
    "darwin": "brew install whisper-cpp, then download a model",
    "win32": "download a whisper.cpp release and set JARVIS_WHISPER_BIN",
}


def probe() -> dict:
    """Everything this machine can actually do for voice, right now."""
    whisper, model, tts, ossr = (find_whisper(), find_whisper_model(),
                                 find_tts(), find_os_stt())
    hint = WHISPER_HINT.get(sys.platform, "install whisper.cpp and put "
                                          "whisper-cli on PATH")
    return {
        "openai": {"ok": bool(_key("OPENAI_API_KEY")),
                   "listen": bool(_key("OPENAI_API_KEY")),
                   "speak": bool(_key("OPENAI_API_KEY")),
                   "in": "whisper-1", "out": "tts-1",
                   "why": "" if _key("OPENAI_API_KEY") else "OPENAI_API_KEY not set"},
        "elevenlabs": {"ok": bool(_key("ELEVENLABS_API_KEY")),
                       "listen": bool(_key("ELEVENLABS_API_KEY")),
                       "speak": bool(_key("ELEVENLABS_API_KEY")),
                       "in": "scribe_v1", "out": "eleven_turbo_v2_5",
                       "why": "" if _key("ELEVENLABS_API_KEY") else "ELEVENLABS_API_KEY not set"},
        "whispercpp": {
            "ok": bool(whisper and model), "listen": bool(whisper and model),
            "speak": False, "in": whisper or None, "model": model,
            "why": ("" if (whisper and model) else
                    (f"whisper.cpp not found — {hint}" if not whisper else
                     "whisper.cpp is installed but has no model file. It ships "
                     "without one. Download it:\n"
                     "  curl -L -o ~/.cache/whisper/ggml-base.en.bin --create-dirs "
                     "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                     "ggml-base.en.bin"))},
        "os": {"ok": bool(ossr), "listen": bool(ossr), "speak": False,
               "in": (ossr[0] if ossr else None),
               "why": "" if ossr else
                      ("Windows speech recognition unavailable" if IS_WINDOWS
                       else f"{sys.platform} has no built-in recogniser to script")},
        "local": {"ok": bool(tts), "listen": False, "speak": bool(tts),
                  "out": (tts[1] if tts else None),
                  "out_kind": (tts[0] if tts else None),
                  "why": "" if tts else "no local speech-out found"},
    }


LISTEN_ORDER = ("openai", "elevenlabs", "whispercpp", "os")
SPEAK_ORDER = ("openai", "elevenlabs", "local")


def _pinned() -> str:
    return os.environ.get("JARVIS_VOICE", "auto").strip().lower()


def resolve_listen() -> str:
    pin, p = _pinned(), probe()
    if pin not in ("", "auto"):
        return pin if p.get(pin, {}).get("listen") else "none"
    for tier in LISTEN_ORDER:
        if p[tier]["listen"]:
            return tier
    return "none"


def resolve_speak() -> str:
    pin, p = _pinned(), probe()
    if pin not in ("", "auto"):
        return pin if p.get(pin, {}).get("speak") else (
            "local" if p["local"]["speak"] else "none")
    for tier in SPEAK_ORDER:
        if p[tier]["speak"]:
            return tier
    return "none"


def resolve() -> str:
    """Kept for callers that want one word for the whole subsystem."""
    listen = resolve_listen()
    return listen if listen != "none" else resolve_speak()


def provider() -> str:
    return resolve()


def status() -> dict:
    """Degrade loudly, and per capability. The UI renders this verbatim.

    `ok` means *something* works. Speaking and listening are reported
    separately, because a machine that can speak but not listen is still a
    useful voice assistant — it just needs typing instead of talking.
    """
    p = probe()
    listen, speak = resolve_listen(), resolve_speak()
    capped = (listen in ("openai", "elevenlabs") or
              speak in ("openai", "elevenlabs")) and _cap_hit()

    listen_ok = listen != "none" and not (
        capped and listen in ("openai", "elevenlabs"))
    speak_ok = speak != "none" and not (
        capped and speak in ("openai", "elevenlabs"))

    cap_msg = (f"voice session cap of ${SESSION_CAP_USD:.2f} reached — raise "
               "JARVIS_VOICE_CAP_USD, or use a local tier which costs nothing")

    listen_why = ""
    if not listen_ok:
        listen_why = cap_msg if capped else (
            "No microphone tier. " + p["whispercpp"]["why"] +
            ". Or set OPENAI_API_KEY. You can still type.")
    speak_why = ""
    if not speak_ok:
        speak_why = cap_msg if capped else (
            "No speech-out on this machine. " + p["local"]["why"])

    ok = listen_ok or speak_ok
    if not ok:
        reason = "No voice at all. " + listen_why
    elif not listen_ok:
        reason = f"Speaking works ({speak}); listening does not. " + listen_why
    elif not speak_ok:
        reason = f"Listening works ({listen}); speaking does not. " + speak_why
    else:
        reason = ""

    return {
        "provider": listen if listen != "none" else speak,
        "ok": ok, "reason": reason,
        "listen": {"ok": listen_ok, "provider": listen, "reason": listen_why},
        "speak": {"ok": speak_ok, "provider": speak, "reason": speak_why},
        "auto": _pinned() in ("", "auto"),
        "tiers": p,
        "free": listen in ("whispercpp", "os", "none") and speak == "local",
        "tts_voice": os.environ.get("JARVIS_TTS_VOICE", "onyx"),
        "spend": spend(),
    }


# ---------------------------------------------------------------- speech in

_WINSR = r"""
Add-Type -AssemblyName System.Speech
$ErrorActionPreference = 'Stop'
try {{
  $r = New-Object System.Speech.Recognition.SpeechRecognitionEngine `
       (New-Object System.Globalization.CultureInfo("en-US"))
}} catch {{
  $r = New-Object System.Speech.Recognition.SpeechRecognitionEngine
}}
$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$r.SetInputToWaveFile('{wav}')
$sb = New-Object System.Text.StringBuilder
while ($true) {{
  try {{ $res = $r.Recognize() }} catch {{ break }}
  if ($null -eq $res) {{ break }}
  [void]$sb.Append($res.Text); [void]$sb.Append(' ')
}}
$r.Dispose()
[Console]::Out.Write($sb.ToString().Trim())
"""


def _os_transcribe(ps: str, audio: bytes) -> dict:
    """Windows' own recogniser. Offline, free, already installed."""
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.write(audio)
    wav.close()
    try:
        out = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command",
             _WINSR.format(wav=wav.name.replace("'", "''"))],
            capture_output=True, timeout=120)
        text = out.stdout.decode("utf-8", "replace").strip()
        if not text and out.returncode != 0:
            return {"ok": False, "provider": "windows/speech",
                    "error": "Windows speech recognition failed: " +
                             out.stderr.decode("utf-8", "replace")[:200]}
        return {"ok": True, "text": text, "provider": "windows/speech"}
    finally:
        try:
            os.unlink(wav.name)
        except OSError:
            pass


def transcribe(audio: bytes, mime: str = "audio/wav") -> dict:
    st = status()
    tier = st["listen"]["provider"]
    if not audio:
        return {"ok": False, "error": "empty recording — is the mic actually open?"}
    if not st["listen"]["ok"]:
        return {"ok": False, "error": st["listen"]["reason"], "provider": tier}

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

        if tier == "os":
            found = find_os_stt()
            if not found:
                return {"ok": False, "provider": "os",
                        "error": probe()["os"]["why"]}
            return _os_transcribe(found[1], audio)

        if tier in ("whispercpp", "local"):
            binary = find_whisper()
            if not binary:
                return {"ok": False, "provider": "whispercpp",
                        "error": probe()["whispercpp"]["why"]}
            model = find_whisper_model()
            if not model:
                return {"ok": False, "provider": "whispercpp",
                        "error": probe()["whispercpp"]["why"]}
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(audio)
            tmp.close()
            try:
                cmd = [binary, "-f", tmp.name, "-nt", "-np", "-m", model]
                out = subprocess.run(cmd, capture_output=True, timeout=180)
                text = out.stdout.decode("utf-8", "replace").strip()
                if not text and out.returncode != 0:
                    return {"ok": False, "provider": "local",
                            "error": "whisper failed: " +
                                     out.stderr.decode("utf-8", "replace")[:200]}
                return {"ok": True, "text": text,
                        "provider": f"local/{os.path.basename(binary)}"}
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
    tier = st["speak"]["provider"]
    text = (text or "").strip()[:2000]
    if not text:
        return {"ok": False, "error": "nothing to say"}
    if not st["speak"]["ok"]:
        return {"ok": False, "error": st["speak"]["reason"], "provider": tier}

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
