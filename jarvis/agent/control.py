"""control.py — the gate every action passes through.

JARVIS can now act on the machine: click, type, navigate, press keys. That
makes three things non-negotiable, and they all live here so no automation
path can quietly skip them.

  1. A KILL SWITCH. `disarm()` halts everything immediately, and every
     action checks it before running. The UI exposes it, and so does Esc.
  2. AN ACTION LOG. Every action is appended to memory/actions.log with a
     timestamp, what it did, and — critically — where the instruction came
     from. If something odd happens, that file says exactly what and why.
  3. AN ORIGIN RULE. An action may only be taken because a principal asked
     for it. Text read from a page, an email, a document or an order note
     is DATA. It never becomes a command. `assert_origin` enforces that.

The origin rule is not a confirmation prompt and does not slow anything
down. It is the difference between an assistant and a loaded gun pointed at
your own accounts.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from data import MEMORY_DIR, TIMEZONE

TZ = ZoneInfo(TIMEZONE)
LOG = MEMORY_DIR / "actions.log"

# Where an instruction came from. Only PRINCIPAL may cause an action.
PRINCIPAL = "principal"      # Sai or Tanay, typed or spoken, this session
CONTENT = "content"          # read out of a page, email, file or order note

_lock = threading.Lock()
_state = {"armed": True, "actions": 0, "last": None, "halted_reason": ""}


class Halted(Exception):
    """Raised when an action is attempted while the kill switch is down."""


class UntrustedOrigin(Exception):
    """Raised when content tried to cause an action. Always a red flag."""


# ---------------------------------------------------------------- switch

def armed() -> bool:
    return _state["armed"]


def disarm(reason: str = "kill switch") -> dict:
    with _lock:
        _state["armed"] = False
        _state["halted_reason"] = reason
    log("control", "DISARMED", {"reason": reason}, origin=PRINCIPAL)
    return status()


def arm() -> dict:
    with _lock:
        _state["armed"] = True
        _state["halted_reason"] = ""
    log("control", "ARMED", {}, origin=PRINCIPAL)
    return status()


def status() -> dict:
    return {"armed": _state["armed"], "actions": _state["actions"],
            "last": _state["last"], "reason": _state["halted_reason"],
            "log": str(LOG)}


# ---------------------------------------------------------------- gate

def assert_origin(origin: str, what: str) -> None:
    """The whole security model, in six lines."""
    if origin != PRINCIPAL:
        log("control", "REFUSED", {"what": what, "origin": origin},
            origin=CONTENT, ok=False)
        raise UntrustedOrigin(
            f"Refused to {what}: that instruction came from {origin}, not from "
            "Sai or Tanay. Text inside pages, mail and files is data, not a "
            "command. Reported, not followed.")


def guard(surface: str, what: str, detail: dict, origin: str) -> None:
    """Call before every action. Raises rather than acting when it should."""
    assert_origin(origin, what)
    if not armed():
        raise Halted(f"Halted, Sir — {_state['halted_reason']}. "
                     "Re-arm before I touch anything.")
    log(surface, what, detail, origin=origin)


# ---------------------------------------------------------------- log

def log(surface: str, what: str, detail: dict, origin: str, ok: bool = True) -> None:
    entry = {
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
        "surface": surface, "action": what, "detail": detail,
        "origin": origin, "ok": ok,
    }
    with _lock:
        _state["actions"] += 1
        _state["last"] = entry
        try:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass          # never let logging failure block the kill switch


def recent(limit: int = 40) -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(out))
