"""data.py — THE ONLY FILE THAT TOUCHES REAL DATA.

Every path that points at the user's actual life is resolved here and
nowhere else. `JARVIS_DEMO` is read in this file only; the rest of the
codebase asks this module for roots and never checks the variable itself.

Default is DEMO. You have to opt *in* to your real life.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ------------------------------------------------------------------
# The demo/real switch. Read here, once, and nowhere else in the project.
# JARVIS_DEMO=1 (default)  invented fixtures, safe to screen-record
# JARVIS_DEMO=0            your actual folders, listed below
# ------------------------------------------------------------------

def demo_mode() -> bool:
    return os.environ.get("JARVIS_DEMO", "1").strip() not in ("0", "false", "no", "off")


def mode_label() -> str:
    return "demo" if demo_mode() else "live"


# ------------------------------------------------------------------
# YOUR REAL FOLDERS
#
# Absolute paths, read-only, indexed recursively. Put yours here — or set
# JARVIS_ROOTS as an os.pathsep-separated list to override without editing
# code. These are never written to; see memory.py for the only writes
# JARVIS is permitted to make.
# ------------------------------------------------------------------

REAL_ROOTS: list[str] = [
    # "/Users/you/Documents/Clients",
    # "/Users/you/Notes",
]

DEMO_ROOTS: list[str] = [str(ROOT / "data" / "vault")]


def active_roots() -> list[str]:
    if demo_mode():
        return DEMO_ROOTS
    env = os.environ.get("JARVIS_ROOTS", "").strip()
    if env:
        return [p for p in env.split(os.pathsep) if p.strip()]
    return REAL_ROOTS


def roots_status() -> dict:
    """Degrade loudly: the UI shows this so a misconfigured root is visible."""
    roots = active_roots()
    return {
        "mode": mode_label(),
        "roots": roots,
        "missing": [r for r in roots if not os.path.isdir(os.path.expanduser(r))],
        "configured": bool(roots),
    }


# ------------------------------------------------------------------
# Inbox and calendar.
#
# read_inbox and brief_me are READ-ONLY by design and by guardrail. In demo
# mode they read the fixture files below. In live mode they read whatever
# export you point them at — JARVIS never authenticates to a mail provider
# and never holds a token that could send anything.
# ------------------------------------------------------------------

DEMO_INBOX = ROOT / "data" / "inbox.json"
DEMO_CALENDAR = ROOT / "data" / "calendar.json"


def inbox_path() -> Path:
    if demo_mode():
        return DEMO_INBOX
    return Path(os.environ.get("JARVIS_INBOX", str(ROOT / "data" / "inbox.live.json")))


def calendar_path() -> Path:
    if demo_mode():
        return DEMO_CALENDAR
    return Path(os.environ.get("JARVIS_CALENDAR", str(ROOT / "data" / "calendar.live.json")))


MEMORY_DIR = ROOT / "memory"          # the one writable location
UI_DIR = ROOT / "ui"
PROMPT_FILE = HERE / "prompt.md"
IDENTITY_FILE = ROOT / "CLAUDE.md"


def load_env_file(path: Path | None = None) -> None:
    """Minimal .env loader so no dependency is needed. Never logs values."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
