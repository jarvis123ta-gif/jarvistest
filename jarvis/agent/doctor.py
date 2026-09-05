#!/usr/bin/env python3
"""doctor.py — one command that says what is actually wrong.

    python3 agent/doctor.py

Checks every moving part and prints a report you can paste anywhere. It
changes nothing: no files written, no browser launched, no input synthesised.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OK, WARN, BAD = "ok  ", "warn", "FAIL"
rows: list[tuple[str, str, str]] = []


def row(state: str, name: str, detail: str = "") -> None:
    rows.append((state, name, detail))


def section(title: str) -> None:
    rows.append(("", "", ""))
    rows.append(("--", title.upper(), ""))


def safe(fn, label: str):
    try:
        return fn()
    except Exception as e:                                  # noqa: BLE001
        row(BAD, label, f"{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- machine

section("machine")
row(OK, "platform", f"{platform.system()} {platform.release()} ({platform.machine()})")
row(OK, "python", sys.version.split()[0] + f"  ({sys.executable})")
# Every module carries `from __future__ import annotations` and none uses
# match statements, so 3.9 genuinely works. 3.10+ is still worth having.
if sys.version_info < (3, 9):
    row(BAD, "python version", "3.9 or newer is required")
elif sys.version_info < (3, 10):
    row(WARN, "python version",
        f"{sys.version_info.major}.{sys.version_info.minor} works, but it is "
        "Apple's stock build and now unsupported upstream. "
        "`brew install python@3.12` when convenient.")

# ---------------------------------------------------------------- config

section("configuration")
import data                                                 # noqa: E402
data.load_env_file()

envfile = data.ROOT / ".env"
row(OK if envfile.exists() else WARN, ".env",
    str(envfile) if envfile.exists() else "not created — copy .env.example")

st = data.roots_status()
row(OK, "mode", f"{st['mode']}  (JARVIS_DEMO={'1' if data.demo_mode() else '0'})")
row(OK, "timezone", st["timezone"])
import tz                                                   # noqa: E402
note = tz.fallback_note()
row(WARN if note else OK, "clock", note or "system timezone database in use")

if data.demo_mode():
    row(WARN, "your folders", "still on demo data — set JARVIS_DEMO=0 and the "
                              "three root variables to index your own files")
else:
    for dom, paths in st["by_domain"].items():
        if not paths:
            row(WARN, f"{dom} folder", "not configured")
        for p in paths:
            good = os.path.isdir(os.path.expanduser(p))
            row(OK if good else BAD, f"{dom} folder",
                p + ("" if good else "  <- does not exist"))

# ---------------------------------------------------------------- index

section("your files")
try:
    from vault import Vault
    v = Vault(data.active_roots(), data.mode_label(), data.domain_of).build()
    row(OK if v.notes else WARN, "indexed",
        f"{len(v.notes)} notes, {len(v.edges)} links"
        + ("" if v.notes else "  <- nothing found; wrong paths, or the files "
                              "are .docx/Google Docs which are not readable"))
    if v.notes:
        row(OK, "by domain", ", ".join(f"{k}: {n}" for k, n in
                                       v.counts_by_domain().items()))
    if v.skipped:
        row(WARN, "skipped", f"{len(v.skipped)} files (too big, or unreadable)")
except Exception as e:                                      # noqa: BLE001
    row(BAD, "indexer", f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- model

section("the brain")
import llm                                                  # noqa: E402
ms = llm.status()
row(OK if ms["ok"] else BAD, "model",
    f"{ms['provider']} / {ms['name']}" if ms["ok"] else ms["reason"][:100])
if ms["warn"]:
    row(WARN, "model note", ms["warn"][:110])
oll = ms["providers"]["ollama"]
row(OK if oll["ok"] else WARN, "ollama",
    f"{oll['host']}  models: {', '.join(oll['installed']) or 'none pulled'}"
    if oll["installed"] or oll["ok"] else oll["why"][:100])
if oll["ok"] and not oll["tools"]:
    row(WARN, "ollama tools", f"{oll['model']} cannot call tools — "
                              "`ollama pull llama3.2:3b`")
row(OK if ms["providers"]["anthropic"]["ok"] else WARN, "anthropic",
    "key set" if ms["providers"]["anthropic"]["ok"] else "no key (optional)")

# ---------------------------------------------------------------- voice

section("voice")
import voice                                                # noqa: E402
vs = voice.status()
row(OK if vs["speak"]["ok"] else BAD, "speaking",
    vs["speak"]["provider"] if vs["speak"]["ok"] else vs["speak"]["reason"][:100])
row(OK if vs["listen"]["ok"] else WARN, "listening",
    vs["listen"]["provider"] if vs["listen"]["ok"] else vs["listen"]["reason"][:110])
w, m = voice.find_whisper(), voice.find_whisper_model()
row(OK if w else WARN, "whisper binary", w or "not found")
row(OK if m else WARN, "whisper model", m or "no ggml-*.bin found")

# ---------------------------------------------------------------- connectors

section("connectors")
import connectors                                           # noqa: E402
for c in connectors.status_all():
    row(OK if c["connected"] else WARN, c["label"].lower(),
        (f"{c['mode']} — {', '.join(c['provides'])}" if c["connected"]
         else c["reason"][:110]))
if any(not c["connected"] for c in connectors.status_all()):
    row(OK, "connect google", "python3 agent/setup_google.py  "
                              "(Gmail, Calendar, Classroom, Drive, YouTube)")

# ---------------------------------------------------------------- browser

section("browser control")
import browser                                              # noqa: E402
binary = browser._chrome_binary()
row(OK if binary else BAD, "browser found", binary or "none")
if sys.platform == "darwin":
    cands = browser._mac_candidates()
    row(OK if cands else WARN, "candidates",
        ", ".join(os.path.basename(os.path.dirname(os.path.dirname(
            os.path.dirname(c)))) for c in cands) or "nothing in /Applications")
    row(OK, "searched", ", ".join(browser.MAC_APP_DIRS))
    if binary:
        row(OK if os.access(binary, os.X_OK) else BAD, "executable",
            "yes" if os.access(binary, os.X_OK) else "binary is not executable")

port_open = False
try:
    s = socket.create_connection(("127.0.0.1", browser.PORT), timeout=1.5)
    s.close()
    port_open = True
except OSError:
    pass
row(OK if port_open else WARN, f"debug port {browser.PORT}",
    "listening" if port_open else "nothing there — run `python3 agent/browser.py launch`")
if port_open:
    tabs = safe(lambda: browser.tabs(quiet=True), "tabs") or []
    row(OK, "tabs", f"{len(tabs)} open")
row(OK, "profile", str(browser.PROFILE)
    + ("" if browser.PROFILE.exists() else "  (not created yet)"))

# ---------------------------------------------------------------- desktop

section("desktop control")
import desktop                                              # noqa: E402
ds = desktop.status()
row(OK if ds["connected"] else WARN, "desktop",
    f"{ds.get('backend')} {ds.get('screen')}" if ds["connected"]
    else ds["reason"][:130])
if sys.platform == "darwin":
    row(OK, "how to grant",
        "System Settings > Privacy & Security > Accessibility AND Screen "
        "Recording, switch ON your terminal app, then restart JARVIS")

# ---------------------------------------------------------------- control

section("safety")
import control                                              # noqa: E402
cs = control.status()
row(OK if cs["armed"] else WARN, "control",
    "armed" if cs["armed"] else f"HALTED — {cs['reason']}")
row(OK, "action log", f"{cs['log']}  ({len(control.recent(500))} entries)")

# ---------------------------------------------------------------- server

section("server")
running = False
try:
    s = socket.create_connection(("127.0.0.1", int(os.environ.get("JARVIS_PORT", "8720"))),
                                 timeout=1.5)
    s.close()
    running = True
except OSError:
    pass
row(OK if running else WARN, "jarvis",
    f"listening on {os.environ.get('JARVIS_PORT', '8720')}" if running
    else "not running — `python3 agent/main.py`")

# ---------------------------------------------------------------- print

print()
print("  JARVIS doctor")
print("  " + "-" * 74)
for state, name, detail in rows:
    if state == "--":
        print(f"\n  {name}")
        continue
    if not state:
        continue
    print(f"  [{state}] {name:<18} {detail}")

bad = sum(1 for s, _, _ in rows if s == BAD)
warn = sum(1 for s, _, _ in rows if s == WARN)
print("\n  " + "-" * 74)
print(f"  {bad} failing, {warn} worth a look."
      + ("  Nothing is broken." if not bad else ""))
print()
