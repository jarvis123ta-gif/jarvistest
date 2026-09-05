"""desktop_macos.py — macOS mouse, keyboard, windows and screen.

Quartz through ctypes, so there is still nothing to install. macOS will not
let any program synthesise input without an explicit grant, which is a
feature, not an obstacle: `probe()` reports whether the grant exists and
`status()` says exactly which checkbox to tick.

    System Settings -> Privacy & Security -> Accessibility     (input)
    System Settings -> Privacy & Security -> Screen Recording  (screenshots)

Add the terminal you launch JARVIS from — Terminal, iTerm, VS Code — not
Python itself; the grant follows the host application.

    !! Written for macOS and NOT executable on the Linux build machine.
    !! The keycode table and event shapes are checked; the Quartz calls are
    !! unverified until this runs on the Mac.

Every action routes through control.guard() — kill switch, action log,
origin rule — exactly as the Windows backend does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import control
from control import PRINCIPAL

IS_MACOS = sys.platform == "darwin"

if IS_MACOS:
    import ctypes
    import ctypes.util
    from ctypes import c_bool, c_double, c_int32, c_uint16, c_uint32, c_void_p

    _AS_PATH = ("/System/Library/Frameworks/ApplicationServices.framework"
                "/ApplicationServices")
    _CF_PATH = ("/System/Library/Frameworks/CoreFoundation.framework"
                "/CoreFoundation")
    try:
        AS = ctypes.cdll.LoadLibrary(_AS_PATH)
        CF = ctypes.cdll.LoadLibrary(_CF_PATH)
    except OSError:                      # pragma: no cover - broken install
        AS = CF = None

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", c_double), ("y", c_double)]

    if AS:
        AS.CGEventCreateMouseEvent.restype = c_void_p
        AS.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
        AS.CGEventCreateKeyboardEvent.restype = c_void_p
        AS.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
        AS.CGEventCreateScrollWheelEvent.restype = c_void_p
        AS.CGEventCreateScrollWheelEvent.argtypes = [c_void_p, c_uint32, c_uint32, c_int32]
        AS.CGEventPost.argtypes = [c_uint32, c_void_p]
        AS.CGEventSetFlags.argtypes = [c_void_p, ctypes.c_uint64]
        AS.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, c_uint32,
                                                       ctypes.POINTER(c_uint16)]
        AS.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        AS.CGMainDisplayID.restype = c_uint32
        AS.CGDisplayPixelsWide.restype = ctypes.c_size_t
        AS.CGDisplayPixelsWide.argtypes = [c_uint32]
        AS.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        AS.CGDisplayPixelsHigh.argtypes = [c_uint32]
        AS.AXIsProcessTrusted.restype = c_bool
        CF.CFRelease.argtypes = [c_void_p]
else:
    ctypes = None
    AS = CF = None
    CGPoint = None

# Quartz constants
HID_TAP = 0
EV_MOUSE_MOVED = 5
EV_LEFT_DOWN, EV_LEFT_UP = 1, 2
EV_RIGHT_DOWN, EV_RIGHT_UP = 3, 4
BTN_LEFT, BTN_RIGHT = 0, 1
SCROLL_LINE = 1

FLAG = {"shift": 0x00020000, "control": 0x00040000, "ctrl": 0x00040000,
        "alt": 0x00080000, "option": 0x00080000, "cmd": 0x00100000,
        "command": 0x00100000, "win": 0x00100000}

# US-layout virtual keycodes. Modifiers are applied as flags, not keycodes.
VK = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39,
    "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "backspace": 51, "delete": 51,
    "esc": 53, "escape": 53,
    "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103,
    "f10": 109, "f12": 111, "f4": 118, "f2": 120, "f1": 122,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119,
    "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
}


def probe() -> dict:
    """What is actually available, decided at runtime."""
    if not IS_MACOS:
        return {"available": False, "platform": sys.platform,
                "reason": f"this backend is macOS-only; running on {sys.platform}"}
    if AS is None:
        return {"available": False, "platform": "darwin",
                "reason": "could not load ApplicationServices"}
    try:
        trusted = bool(AS.AXIsProcessTrusted())
        did = AS.CGMainDisplayID()
        screen = {"w": int(AS.CGDisplayPixelsWide(did)),
                  "h": int(AS.CGDisplayPixelsHigh(did))}
    except Exception as e:                                  # noqa: BLE001
        return {"available": False, "platform": "darwin",
                "reason": f"Quartz unreachable: {e}"}
    if not trusted:
        return {
            "available": False, "platform": "darwin", "screen": screen,
            "trusted": False,
            "reason": ("macOS has not granted permission to control the "
                       "computer. Open System Settings -> Privacy & Security "
                       "-> Accessibility and switch ON the app you run JARVIS "
                       "from (Terminal, iTerm or VS Code — not Python). Then "
                       "restart JARVIS. Reading the screen also needs Screen "
                       "Recording in the same panel."),
        }
    return {"available": True, "platform": "darwin", "trusted": True,
            "screen": screen, "reason": ""}


def _require() -> None:
    p = probe()
    if not p["available"]:
        raise RuntimeError(p["reason"])


def _post(event) -> None:
    if not event:
        return
    AS.CGEventPost(HID_TAP, event)
    CF.CFRelease(event)


# ---------------------------------------------------------------- screen

def screen_size() -> dict:
    return probe().get("screen") or {"w": 0, "h": 0}


def cursor() -> dict:
    """Read back from a zero-distance move; Quartz has no simple getter that
    does not require an event source."""
    _require()
    ev = AS.CGEventCreateMouseEvent(None, EV_MOUSE_MOVED, CGPoint(0, 0), BTN_LEFT)
    if not ev:
        return {"x": 0, "y": 0}
    AS.CGEventGetLocation.restype = CGPoint
    AS.CGEventGetLocation.argtypes = [c_void_p]
    pt = AS.CGEventGetLocation(ev)
    CF.CFRelease(ev)
    return {"x": int(pt.x), "y": int(pt.y)}


def screenshot(origin: str = PRINCIPAL) -> dict:
    """`screencapture` is already on every Mac and produces a real PNG, so
    there is nothing to encode by hand here."""
    _require()
    control.log("desktop", "screenshot", {}, origin=origin)
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    f.close()
    try:
        out = subprocess.run(["screencapture", "-x", "-t", "png", f.name],
                             capture_output=True, timeout=30)
        png = open(f.name, "rb").read()
        if not png:
            return {"ok": False, "reason":
                    "screencapture produced nothing — grant Screen Recording "
                    "in System Settings -> Privacy & Security. "
                    + out.stderr.decode("utf-8", "replace")[:150]}
        s = screen_size()
        return {"ok": True, "width": s["w"], "height": s["h"], "png": png}
    finally:
        try:
            os.unlink(f.name)
        except OSError:
            pass


# ---------------------------------------------------------------- apps

def _osa(script: str, timeout: int = 20) -> str:
    out = subprocess.run(["osascript", "-e", script],
                         capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:200])
    return out.stdout.decode("utf-8", "replace").strip()


def windows(origin: str = PRINCIPAL) -> dict:
    """Visible applications and, best effort, their window titles."""
    _require()
    control.log("desktop", "list windows", {}, origin=origin)
    try:
        apps = [a.strip() for a in _osa(
            'tell application "System Events" to get name of every process '
            'whose background only is false').split(",") if a.strip()]
        front = _osa('tell application "System Events" to get name of first '
                     'process whose frontmost is true')
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "reason": f"System Events refused: {e}",
                "windows": []}

    found = []
    for app in apps[:25]:
        titles = []
        try:
            raw = _osa(f'tell application "System Events" to tell process '
                       f'"{app}" to get name of every window', timeout=6)
            titles = [t.strip() for t in raw.split(",") if t.strip()]
        except Exception:                                   # noqa: BLE001
            pass                       # many apps refuse; the app name still counts
        if titles:
            for t in titles[:4]:
                found.append({"app": app, "title": f"{app} — {t}",
                              "focused": app == front})
        else:
            found.append({"app": app, "title": app, "focused": app == front})
    return {"ok": True, "windows": found[:60], "focused": front}


def focus(title: str, origin: str = PRINCIPAL) -> dict:
    """Bring an application to the front by part of its name."""
    control.guard("desktop", "focus window", {"title": title}, origin)
    _require()
    low = title.lower()
    listing = windows(origin=origin)
    for w in listing.get("windows", []):
        if low in w["app"].lower() or low in w["title"].lower():
            try:
                _osa(f'tell application "{w["app"]}" to activate')
            except RuntimeError as e:
                return {"ok": False, "reason": str(e)}
            time.sleep(0.25)
            return {"ok": True, "focused": w["app"]}
    return {"ok": False, "reason": f"no application matching {title!r}"}


# ---------------------------------------------------------------- mouse

def move(x: int, y: int, origin: str = PRINCIPAL) -> dict:
    control.guard("desktop", "move mouse", {"x": x, "y": y}, origin)
    _require()
    AS.CGWarpMouseCursorPosition(CGPoint(float(x), float(y)))
    _post(AS.CGEventCreateMouseEvent(None, EV_MOUSE_MOVED,
                                     CGPoint(float(x), float(y)), BTN_LEFT))
    return {"ok": True, "at": {"x": x, "y": y}}


def click(x: int | None = None, y: int | None = None, button: str = "left",
          double: bool = False, origin: str = PRINCIPAL) -> dict:
    control.guard("desktop", "click",
                  {"x": x, "y": y, "button": button, "double": double}, origin)
    _require()
    if x is None or y is None:
        at = cursor()
        x, y = at["x"], at["y"]
    else:
        move(x, y, origin=origin)
        time.sleep(0.03)
    pt = CGPoint(float(x), float(y))
    down, up, btn = ((EV_RIGHT_DOWN, EV_RIGHT_UP, BTN_RIGHT) if button == "right"
                     else (EV_LEFT_DOWN, EV_LEFT_UP, BTN_LEFT))
    for _ in range(2 if double else 1):
        _post(AS.CGEventCreateMouseEvent(None, down, pt, btn))
        _post(AS.CGEventCreateMouseEvent(None, up, pt, btn))
        time.sleep(0.05)
    return {"ok": True, "clicked": button, "at": {"x": x, "y": y}}


def scroll(amount: int, origin: str = PRINCIPAL) -> dict:
    """Positive scrolls up, negative down, in lines."""
    control.guard("desktop", "scroll", {"amount": amount}, origin)
    _require()
    _post(AS.CGEventCreateScrollWheelEvent(None, SCROLL_LINE, 1, int(amount)))
    return {"ok": True, "scrolled": amount}


# ---------------------------------------------------------------- keyboard

def type_text(text: str, origin: str = PRINCIPAL) -> dict:
    """Unicode-accurate typing. The keycode is irrelevant when the event
    carries an explicit UTF-16 string, so no layout guessing."""
    control.guard("desktop", "type", {"chars": len(text)}, origin)
    _require()
    for ch in text:
        units = ch.encode("utf-16-le")
        n = len(units) // 2
        buf = (c_uint16 * n).from_buffer_copy(units)
        for is_down in (True, False):
            ev = AS.CGEventCreateKeyboardEvent(None, 0, is_down)
            if not ev:
                continue
            AS.CGEventKeyboardSetUnicodeString(ev, n, buf)
            AS.CGEventPost(HID_TAP, ev)
            CF.CFRelease(ev)
        time.sleep(0.005)
    return {"ok": True, "typed": len(text)}


def parse_combo(combo: str) -> tuple[int, int | None, str]:
    """'cmd+shift+t' -> (flags, keycode, error). Pure, so it is testable
    without a Mac; press() is a thin wrapper that posts the result."""
    flags, key = 0, None
    for part in [p.strip().lower() for p in combo.split("+") if p.strip()]:
        if part in FLAG:
            flags |= FLAG[part]
        elif part in VK:
            key = VK[part]
        else:
            return 0, None, f"unknown key {part!r} in {combo!r}"
    if key is None:
        return 0, None, f"no non-modifier key in {combo!r}"
    return flags, key, ""


def press(combo: str, origin: str = PRINCIPAL) -> dict:
    """A chord such as 'cmd+c', 'cmd+shift+t', 'ctrl+left'."""
    control.guard("desktop", "press keys", {"combo": combo}, origin)
    _require()
    flags, key, err = parse_combo(combo)
    if err:
        return {"ok": False, "reason": err}

    for is_down in (True, False):
        ev = AS.CGEventCreateKeyboardEvent(None, key, is_down)
        if not ev:
            continue
        if flags:
            AS.CGEventSetFlags(ev, flags)
        AS.CGEventPost(HID_TAP, ev)
        CF.CFRelease(ev)
        time.sleep(0.02)
    return {"ok": True, "pressed": combo}


# ---------------------------------------------------------------- panic key

def start_panic_key() -> dict:
    """No global hotkey without a run loop, so the honest answer is that the
    Mac's panic route is Esc in the page or the Halt button."""
    return {"ok": False, "combo": None,
            "reason": "macOS has no global hotkey here — use Esc in the JARVIS "
                      "page, or the Halt button, to stop everything"}


def status() -> dict:
    p = probe()
    return {"key": "desktop", "label": "Desktop control", "domain": "all",
            "backend": "macos",
            "connected": p["available"], "mode": "live", "readonly": False,
            "reason": p.get("reason", ""), "screen": p.get("screen"),
            "trusted": p.get("trusted"),
            "panic_key": "esc (in the page)",
            "provides": ["mouse", "keyboard", "windows", "screenshot"]}
