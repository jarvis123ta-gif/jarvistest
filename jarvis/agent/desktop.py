"""desktop.py — Windows mouse, keyboard, windows and screen, via ctypes.

No dependencies: `ctypes` is standard library and reaches user32 and gdi32
directly, so the no-package-manager promise survives full desktop control.

    !! Written for Windows and NOT executable on the Linux build machine.
    !! Syntax and shape are checked; the win32 calls themselves are
    !! unverified until this runs on the target machine. `probe()` reports
    !! what is actually available at runtime rather than assuming.

Every action routes through control.guard() — kill switch, action log,
origin rule. Reading the screen is logged but not gated; moving the mouse is
gated.

A physical panic key is registered when possible: CTRL+ALT+Q disarms
everything, because when something else is driving your mouse, reaching a
button on screen is exactly what you cannot do.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
import zlib

import control
from control import PRINCIPAL

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.SetProcessDPIAware()
else:                                   # importable everywhere; refuses to act
    ctypes = None
    wintypes = None
    user32 = gdi32 = None


def probe() -> dict:
    """What is actually available, decided at runtime, not assumed."""
    if not IS_WINDOWS:
        return {"available": False, "platform": sys.platform,
                "reason": f"desktop control is Windows-only; this is {sys.platform}"}
    try:
        return {"available": True, "platform": "win32", "reason": "",
                "screen": {"w": user32.GetSystemMetrics(0),
                           "h": user32.GetSystemMetrics(1)}}
    except Exception as e:                                  # noqa: BLE001
        return {"available": False, "platform": "win32",
                "reason": f"user32 unreachable: {e}"}


def _require() -> None:
    p = probe()
    if not p["available"]:
        raise RuntimeError(p["reason"])


# ---------------------------------------------------------------- SendInput

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800

if IS_WINDOWS:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    class _IU(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _IU)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    def _send(*inputs) -> int:
        arr = (INPUT * len(inputs))(*inputs)
        return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0):
        return INPUT(type=INPUT_MOUSE,
                     u=_IU(mi=MOUSEINPUT(dx, dy, data, flags, 0, None)))

    def _key(vk: int = 0, scan: int = 0, flags: int = 0):
        return INPUT(type=INPUT_KEYBOARD,
                     u=_IU(ki=KEYBDINPUT(vk, scan, flags, 0, None)))

# Virtual key codes for the chords worth naming.
VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "cmd": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}


# ---------------------------------------------------------------- screen

def screen_size() -> dict:
    return probe().get("screen") or {"w": 0, "h": 0}


def cursor() -> dict:
    _require()
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


def _png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down BGRA rows as PNG using zlib, so no Pillow is needed.

    The channel shuffle is done with extended slice assignment rather than a
    per-pixel loop — two million pixels through a Python loop would take
    seconds; this is memcpy speed.
    """
    stride = width * 4
    rows = bytearray()
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]        # R
        rgb[1::3] = row[1::4]        # G
        rgb[2::3] = row[0::4]        # B
        rows.append(0)               # filter type: none
        rows += rgb

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def screenshot(origin: str = PRINCIPAL) -> dict:
    """Capture the whole virtual screen as PNG bytes."""
    _require()
    control.log("desktop", "screenshot", {}, origin=origin)
    x = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)

    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)   # SRCCOPY

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h        # negative height: top-down rows
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)
    return {"ok": True, "width": w, "height": h, "png": _png(w, h, buf.raw)}


# ---------------------------------------------------------------- windows

def windows(origin: str = PRINCIPAL) -> dict:
    """Every visible top-level window, and which one has focus."""
    _require()
    control.log("desktop", "list windows", {}, origin=origin)
    found: list[dict] = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({"hwnd": int(hwnd), "title": buf.value,
                      "rect": [rect.left, rect.top, rect.right, rect.bottom]})
        return True

    user32.EnumWindows(CB(cb), 0)
    fg = int(user32.GetForegroundWindow())
    for w in found:
        w["focused"] = (w["hwnd"] == fg)
    return {"ok": True, "windows": found[:60], "focused_hwnd": fg}


def focus(title: str, origin: str = PRINCIPAL) -> dict:
    """Bring a window to the front by a substring of its title."""
    control.guard("desktop", "focus window", {"title": title}, origin)
    _require()
    low = title.lower()
    for w in windows(origin=origin)["windows"]:
        if low in w["title"].lower():
            user32.ShowWindow(w["hwnd"], 9)                # SW_RESTORE
            user32.SetForegroundWindow(w["hwnd"])
            time.sleep(0.15)
            return {"ok": True, "focused": w["title"]}
    return {"ok": False, "reason": f"no window matching {title!r}"}


# ---------------------------------------------------------------- mouse

def move(x: int, y: int, origin: str = PRINCIPAL) -> dict:
    control.guard("desktop", "move mouse", {"x": x, "y": y}, origin)
    _require()
    s = screen_size()
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                 int(x * 65535 / max(s["w"] - 1, 1)),
                 int(y * 65535 / max(s["h"] - 1, 1))))
    return {"ok": True, "at": {"x": x, "y": y}}


def click(x: int | None = None, y: int | None = None, button: str = "left",
          double: bool = False, origin: str = PRINCIPAL) -> dict:
    control.guard("desktop", "click",
                  {"x": x, "y": y, "button": button, "double": double}, origin)
    _require()
    if x is not None and y is not None:
        move(x, y, origin=origin)
        time.sleep(0.03)
    down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    for _ in range(2 if double else 1):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.04)
    return {"ok": True, "clicked": button, "at": cursor()}


def scroll(amount: int, origin: str = PRINCIPAL) -> dict:
    """Positive scrolls up, negative down. One notch is 120."""
    control.guard("desktop", "scroll", {"amount": amount}, origin)
    _require()
    _send(_mouse(MOUSEEVENTF_WHEEL, data=amount * 120))
    return {"ok": True, "scrolled": amount}


# ---------------------------------------------------------------- keyboard

def type_text(text: str, origin: str = PRINCIPAL) -> dict:
    """Unicode-accurate typing — no keyboard-layout guessing."""
    control.guard("desktop", "type", {"chars": len(text)}, origin)
    _require()
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:                                  # surrogate pair
            code -= 0x10000
            units = (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
        else:
            units = (code,)
        for u in units:
            _send(_key(0, u, KEYEVENTF_UNICODE),
                  _key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        time.sleep(0.004)
    return {"ok": True, "typed": len(text)}


def press(combo: str, origin: str = PRINCIPAL) -> dict:
    """A chord such as 'ctrl+c', 'alt+tab', 'win+d', 'ctrl+shift+t'."""
    control.guard("desktop", "press keys", {"combo": combo}, origin)
    _require()
    codes = []
    for p in [p.strip().lower() for p in combo.split("+") if p.strip()]:
        if p not in VK:
            return {"ok": False, "reason": f"unknown key {p!r} in {combo!r}"}
        codes.append(VK[p])
    _send(*[_key(vk) for vk in codes])
    time.sleep(0.03)
    _send(*[_key(vk, flags=KEYEVENTF_KEYUP) for vk in reversed(codes)])
    return {"ok": True, "pressed": combo}


# ---------------------------------------------------------------- panic key

_panic_thread: threading.Thread | None = None


def start_panic_key() -> dict:
    """CTRL+ALT+Q disarms everything."""
    global _panic_thread
    if not IS_WINDOWS:
        return {"ok": False, "reason": "panic hotkey is Windows-only"}
    if _panic_thread and _panic_thread.is_alive():
        return {"ok": True, "already": True, "combo": "ctrl+alt+q"}

    def loop():
        MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x0001, 0x0002, 0x0312
        if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL, ord("Q")):
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                control.disarm("panic key (ctrl+alt+q)")

    _panic_thread = threading.Thread(target=loop, daemon=True, name="jarvis-panic")
    _panic_thread.start()
    return {"ok": True, "combo": "ctrl+alt+q"}


def status() -> dict:
    p = probe()
    return {"key": "desktop", "label": "Desktop control", "domain": "all",
            "connected": p["available"], "mode": "live", "readonly": False,
            "reason": p.get("reason", ""), "screen": p.get("screen"),
            "panic_key": "ctrl+alt+q",
            "provides": ["mouse", "keyboard", "windows", "screenshot"]}


if __name__ == "__main__":
    import json
    print(json.dumps(status(), indent=2))
