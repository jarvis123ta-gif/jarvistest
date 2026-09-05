"""browser.py — driving Chrome through the DevTools Protocol.

Chrome exposes CDP on a local port when launched with
`--remote-debugging-port`. This talks to it over the hand-rolled WebSocket
in wsock.py, so there is still nothing to install.

A note on profiles, because it will otherwise waste an hour: since Chrome
136, `--remote-debugging-port` is IGNORED for the default user data
directory. That is a deliberate Google security change, not a bug. Chrome
must therefore run against a separate profile directory — `launch()` makes
one under the project. You log into your accounts once inside that profile
and it persists.

Every action routes through control.guard(), which enforces the kill switch,
the action log and the origin rule.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import control
from control import PRINCIPAL
from wsock import WebSocket, WebSocketError

PORT = int(os.environ.get("JARVIS_CHROME_PORT", "9222"))
PROFILE = Path(os.environ.get(
    "JARVIS_CHROME_PROFILE",
    str(Path(__file__).resolve().parent.parent / "chrome-profile")))

# Any Chromium-based browser speaks the DevTools Protocol, so Chrome is not
# required — Brave, Edge, Arc, Vivaldi and Chromium all work identically.
# Assuming Chrome specifically is a bad bet on a Mac, where plenty of people
# only have Safari (which has no CDP and cannot be driven this way).

WINDOWS_CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
]

MAC_APPS = [
    ("Google Chrome", "Google Chrome"),
    ("Google Chrome Canary", "Google Chrome Canary"),
    ("Brave Browser", "Brave Browser"),
    ("Microsoft Edge", "Microsoft Edge"),
    ("Chromium", "Chromium"),
    ("Arc", "Arc"),
    ("Vivaldi", "Vivaldi"),
    ("Opera", "Opera"),
]


def _mac_candidates() -> list[str]:
    out = []
    for folder in ("/Applications", os.path.expanduser("~/Applications")):
        for app, binary in MAC_APPS:
            path = f"{folder}/{app}.app/Contents/MacOS/{binary}"
            if os.path.isfile(path):
                out.append(path)
    return out


def _chrome_binary() -> str | None:
    explicit = os.environ.get("JARVIS_CHROME_BIN", "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    if sys.platform == "win32":
        for p in WINDOWS_CHROME:
            if os.path.isfile(p):
                return p
        return None
    if sys.platform == "darwin":
        found = _mac_candidates()
        return found[0] if found else None
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome",
                 "brave-browser", "microsoft-edge"):
        got = shutil.which(name)
        if got:
            return got
    for p in ("/opt/pw-browsers/chromium/chrome-linux/chrome",):
        if os.path.isfile(p):
            return p
    return None


def _no_browser_reason() -> str:
    if sys.platform == "darwin":
        return ("No Chromium-based browser found. JARVIS can drive Chrome, "
                "Brave, Edge, Arc, Vivaldi or Chromium — but NOT Safari, "
                "which has no DevTools Protocol. Install one (Chrome from "
                "google.com/chrome is the safe choice) and try again, or set "
                "JARVIS_CHROME_BIN to the binary inside the .app bundle.")
    if sys.platform == "win32":
        return ("No Chromium-based browser found. Install Chrome, Brave or "
                "Edge, or set JARVIS_CHROME_BIN to the .exe.")
    return ("No Chromium-based browser found. Install chromium or "
            "google-chrome, or set JARVIS_CHROME_BIN.")


# ---------------------------------------------------------------- http side

def _http(path: str, timeout: float = 4.0):
    url = f"http://127.0.0.1:{PORT}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def reachable() -> bool:
    try:
        _http("/json/version", timeout=2.0)
        return True
    except Exception:                                       # noqa: BLE001
        return False


def status() -> dict:
    binary = _chrome_binary()
    if reachable():
        try:
            v = _http("/json/version")
            return {"connected": True, "port": PORT,
                    "browser": v.get("Browser", "chrome"),
                    "profile": str(PROFILE), "binary": binary,
                    "tabs": len(tabs(quiet=True)), "reason": ""}
        except Exception as e:                              # noqa: BLE001
            return {"connected": False, "port": PORT, "binary": binary,
                    "reason": f"debug port answered oddly: {e}"}
    if not binary:
        return {"connected": False, "port": PORT, "binary": None,
                "profile": str(PROFILE), "reason": _no_browser_reason()}
    return {
        "connected": False, "port": PORT, "binary": binary,
        "profile": str(PROFILE),
        "reason": (f"{os.path.basename(binary)} is not listening on {PORT}. "
                   "Start it with "
                   f"`python3 agent/browser.py launch`, or run it yourself "
                   f"with --remote-debugging-port={PORT} "
                   f"--user-data-dir=\"{PROFILE}\". Note that Chrome ignores "
                   f"the debug port on your default profile, so a separate "
                   f"profile directory is required."),
    }


def launch(url: str = "about:blank", origin: str = PRINCIPAL) -> dict:
    """Start Chrome with the debug port open, against JARVIS's own profile."""
    control.guard("browser", "launch chrome", {"url": url}, origin)
    if reachable():
        return {"ok": True, "already": True, **status()}
    binary = _chrome_binary()
    if not binary:
        return {"ok": False, "reason": _no_browser_reason(),
                "looked_in": (_mac_candidates() or
                              ["/Applications", "~/Applications"])
                             if sys.platform == "darwin" else None}
    PROFILE.mkdir(parents=True, exist_ok=True)
    cmd = [binary, f"--remote-debugging-port={PORT}",
           f"--user-data-dir={PROFILE}", "--no-first-run",
           "--no-default-browser-check", url]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    for _ in range(40):
        time.sleep(0.25)
        if reachable():
            return {"ok": True, "already": False, **status()}
    return {"ok": False, "reason": "Chrome started but never opened the debug port."}


def tabs(quiet: bool = False) -> list[dict]:
    try:
        raw = _http("/json/list")
    except Exception:                                       # noqa: BLE001
        return []
    out = [{"id": t.get("id"), "title": t.get("title"), "url": t.get("url"),
            "ws": t.get("webSocketDebuggerUrl")}
           for t in raw if t.get("type") == "page"]
    if not quiet:
        control.log("browser", "list tabs", {"count": len(out)}, origin=PRINCIPAL)
    return out


def _pick(target: str | None) -> dict | None:
    open_tabs = tabs(quiet=True)
    if not open_tabs:
        return None
    if not target:
        return open_tabs[0]
    for t in open_tabs:
        if t["id"] == target:
            return t
    low = target.lower()
    for t in open_tabs:
        if low in (t["title"] or "").lower() or low in (t["url"] or "").lower():
            return t
    return None


# ---------------------------------------------------------------- cdp

class Tab:
    """One CDP session against one tab."""

    def __init__(self, ws_url: str):
        self.ws = WebSocket(ws_url)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue                       # an event; not our reply
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message", "cdp error"))
            return msg.get("result", {})
        raise TimeoutError(f"{method} timed out")

    def evaluate(self, expression: str):
        r = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True})
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text", "js threw"))
        return res.get("value")

    def close(self):
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _tab(target: str | None) -> Tab:
    t = _pick(target)
    if not t or not t.get("ws"):
        raise RuntimeError("no such tab — is Chrome running with the debug port?")
    return Tab(t["ws"])


# ---------------------------------------------------------------- reading

READ_JS = r"""
(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const links = [...document.querySelectorAll('a[href]')].filter(vis)
    .slice(0, 60).map(a => ({ text: clean(a.innerText).slice(0, 80), href: a.href }));
  const fields = [...document.querySelectorAll('input, textarea, select')]
    .filter(vis).slice(0, 40).map(el => ({
      tag: el.tagName.toLowerCase(), type: el.type || null,
      name: el.name || null, id: el.id || null,
      label: clean((el.labels && el.labels[0] && el.labels[0].innerText) ||
                   el.getAttribute('aria-label') || el.placeholder || ''),
      value: el.type === 'password' ? '(hidden)' : clean(el.value).slice(0, 80),
    }));
  const buttons = [...document.querySelectorAll('button, [role=button], input[type=submit]')]
    .filter(vis).slice(0, 40).map(b => ({
      text: clean(b.innerText || b.value), id: b.id || null }));
  return {
    title: document.title, url: location.href,
    text: clean(document.body ? document.body.innerText : '').slice(0, 12000),
    links, fields, buttons,
  };
})()
"""


def read(target: str | None = None, origin: str = PRINCIPAL) -> dict:
    """Read a tab. Reading is not an action, but it is still logged, because
    what JARVIS saw is half of why it later did something."""
    control.log("browser", "read tab", {"target": target}, origin=origin)
    try:
        with _tab(target) as t:
            page = t.evaluate(READ_JS)
        return {"ok": True, **page}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


def screenshot(target: str | None = None, origin: str = PRINCIPAL) -> dict:
    control.log("browser", "screenshot tab", {"target": target}, origin=origin)
    try:
        with _tab(target) as t:
            r = t.call("Page.captureScreenshot", {"format": "png"})
        return {"ok": True, "png_base64": r.get("data", "")}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


# ---------------------------------------------------------------- acting

def navigate(url: str, target: str | None = None, origin: str = PRINCIPAL) -> dict:
    control.guard("browser", "navigate", {"url": url}, origin)
    try:
        with _tab(target) as t:
            t.call("Page.enable")
            t.call("Page.navigate", {"url": url})
            time.sleep(0.8)
            page = t.evaluate("({title: document.title, url: location.href})")
        return {"ok": True, **(page or {})}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


CLICK_JS = """
(() => {
  const sel = %s;
  let el = document.querySelector(sel);
  if (!el) {
    const needle = sel.toLowerCase();
    el = [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]
      .find(e => (e.innerText || e.value || '').trim().toLowerCase().includes(needle));
  }
  if (!el) return { ok: false, reason: 'nothing matched ' + sel };
  el.scrollIntoView({ block: 'center' });
  el.click();
  return { ok: true, clicked: (el.innerText || el.value || el.tagName).trim().slice(0, 80) };
})()
"""


def click(selector: str, target: str | None = None, origin: str = PRINCIPAL) -> dict:
    """Click by CSS selector, or by visible button/link text if that fails."""
    control.guard("browser", "click", {"selector": selector}, origin)
    try:
        with _tab(target) as t:
            r = t.evaluate(CLICK_JS % json.dumps(selector))
        return r or {"ok": False, "reason": "no result"}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


FILL_JS = """
(() => {
  const sel = %s, val = %s;
  let el = document.querySelector(sel);
  if (!el) {
    const needle = sel.toLowerCase();
    el = [...document.querySelectorAll('input, textarea, select')].find(e =>
      (e.name || '').toLowerCase().includes(needle) ||
      (e.id || '').toLowerCase().includes(needle) ||
      (e.placeholder || '').toLowerCase().includes(needle) ||
      ((e.labels && e.labels[0] && e.labels[0].innerText) || '').toLowerCase().includes(needle));
  }
  if (!el) return { ok: false, reason: 'no field matched ' + sel };
  el.focus();
  el.value = val;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, field: el.name || el.id || el.tagName };
})()
"""


def fill(selector: str, value: str, target: str | None = None,
         origin: str = PRINCIPAL) -> dict:
    control.guard("browser", "fill field",
                  {"selector": selector, "chars": len(value)}, origin)
    try:
        with _tab(target) as t:
            r = t.evaluate(FILL_JS % (json.dumps(selector), json.dumps(value)))
        return r or {"ok": False, "reason": "no result"}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


def type_keys(text: str, target: str | None = None, origin: str = PRINCIPAL) -> dict:
    """Real key events into whatever has focus, for editors that ignore .value."""
    control.guard("browser", "type", {"chars": len(text)}, origin)
    try:
        with _tab(target) as t:
            for ch in text:
                t.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
                t.call("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch})
        return {"ok": True, "typed": len(text)}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


def run_js(expression: str, target: str | None = None, origin: str = PRINCIPAL) -> dict:
    control.guard("browser", "run javascript",
                  {"expression": expression[:200]}, origin)
    try:
        with _tab(target) as t:
            return {"ok": True, "value": t.evaluate(expression)}
    except (RuntimeError, WebSocketError, TimeoutError) as e:
        return {"ok": False, "reason": str(e)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "launch":
        print(json.dumps(launch(sys.argv[2] if len(sys.argv) > 2 else "about:blank"), indent=2))
    elif cmd == "tabs":
        for t in tabs():
            print(f"  {t['id'][:8]}  {(t['title'] or '')[:50]:<52} {t['url'][:60]}")
    elif cmd == "read":
        r = read(sys.argv[2] if len(sys.argv) > 2 else None)
        print(json.dumps({k: v for k, v in r.items() if k != "text"}, indent=2)[:2000])
        print("\n--- text ---\n" + (r.get("text", "")[:1200]))
    else:
        print(json.dumps(status(), indent=2))
