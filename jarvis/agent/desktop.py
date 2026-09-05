"""desktop.py — controlling the machine, whichever machine it is.

A front door over one backend per platform. Everything above this file —
tools.py, the kill switch, the action log, the origin rule — is unchanged by
which OS is underneath.

    desktop_windows.py   ctypes against user32/gdi32
    desktop_macos.py     Quartz through ctypes, plus osascript and
                         screencapture
    (linux)              not implemented; probe() says so rather than
                         pretending

Adding a platform means adding a backend with the same nine functions and
one line in `_backend()`.
"""

from __future__ import annotations

import sys

import control                                             # noqa: F401
from control import PRINCIPAL


def _backend():
    if sys.platform == "win32":
        import desktop_windows
        return desktop_windows
    if sys.platform == "darwin":
        import desktop_macos
        return desktop_macos
    return None


BACKEND = _backend()


def _unavailable(reason: str) -> dict:
    return {"available": False, "platform": sys.platform, "reason": reason}


NO_BACKEND = ("desktop control is not implemented for "
              f"{sys.platform} — Windows and macOS are supported")


def probe() -> dict:
    return BACKEND.probe() if BACKEND else _unavailable(NO_BACKEND)


def status() -> dict:
    if BACKEND:
        return BACKEND.status()
    return {"key": "desktop", "label": "Desktop control", "domain": "all",
            "backend": None, "connected": False, "mode": "live",
            "readonly": False, "reason": NO_BACKEND, "screen": None,
            "panic_key": None,
            "provides": ["mouse", "keyboard", "windows", "screenshot"]}


def _need():
    if not BACKEND:
        raise RuntimeError(NO_BACKEND)
    return BACKEND


def _gate(origin: str, what: str):
    """The origin rule is checked HERE, at the front door, before anything
    platform-specific can short-circuit it. Each backend checks again — two
    locks on the same door, because this is the one that matters. A machine
    with no backend must still refuse content, not merely fail to act."""
    control.assert_origin(origin, what)
    return _need()


# -- the surface every backend implements ---------------------------

def screen_size() -> dict:
    return BACKEND.screen_size() if BACKEND else {"w": 0, "h": 0}


def cursor() -> dict:
    return _need().cursor()


def screenshot(origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "capture the screen").screenshot(origin=origin)


def windows(origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "list windows").windows(origin=origin)


def focus(title: str, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "focus a window").focus(title, origin=origin)


def move(x: int, y: int, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "move the mouse").move(x, y, origin=origin)


def click(x: int | None = None, y: int | None = None, button: str = "left",
          double: bool = False, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "click").click(x, y, button=button, double=double, origin=origin)


def scroll(amount: int, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "scroll").scroll(amount, origin=origin)


def type_text(text: str, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "type").type_text(text, origin=origin)


def press(combo: str, origin: str = PRINCIPAL) -> dict:
    return _gate(origin, "press keys").press(combo, origin=origin)


def start_panic_key() -> dict:
    if not BACKEND:
        return {"ok": False, "reason": NO_BACKEND}
    return BACKEND.start_panic_key()


if __name__ == "__main__":
    import json
    print(json.dumps(status(), indent=2))
