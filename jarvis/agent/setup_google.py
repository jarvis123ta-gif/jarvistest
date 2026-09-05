#!/usr/bin/env python3
"""setup_google.py — get JARVIS authorised with Google, once.

    python3 agent/setup_google.py

One flow unlocks Gmail, Calendar, Classroom, Drive and YouTube. It opens
the consent screen in your browser, catches the redirect on a local port,
exchanges the code for a refresh token, and writes it to .env.

Every scope requested is READ-ONLY. There is no send scope, no write scope,
and nothing in this project could use one if it were granted.

Standard library only, so there is still nothing to install.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import data                                                 # noqa: E402

ENV = data.ROOT / ".env"
REDIRECT_PORT = int(os.environ.get("JARVIS_OAUTH_PORT", "8721"))
REDIRECT = f"http://localhost:{REDIRECT_PORT}/"

# Read-only, all of them. Grouped so the summary can explain what each buys.
SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly":
        "Gmail — read your mail (never send)",
    "https://www.googleapis.com/auth/calendar.readonly":
        "Calendar — classes, deadlines, competitions",
    "https://www.googleapis.com/auth/drive.metadata.readonly":
        "Drive — find your documents by name",
    "https://www.googleapis.com/auth/classroom.courses.readonly":
        "Classroom — your courses",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly":
        "Classroom — your coursework and due dates",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly":
        "Classroom — what you have and have not turned in",
    "https://www.googleapis.com/auth/youtube.readonly":
        "YouTube — your channel and video stats",
}

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"

CONSOLE_STEPS = """
You need a Google OAuth client first. It is free, takes about four minutes,
and only has to be done once.

  1. Go to  https://console.cloud.google.com/projectcreate
     Name it anything (JARVIS), Create, and wait for it to switch to it.

  2. Enable the APIs you want. Open each link and press ENABLE:
       Gmail       https://console.cloud.google.com/apis/library/gmail.googleapis.com
       Calendar    https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
       Drive       https://console.cloud.google.com/apis/library/drive.googleapis.com
       Classroom   https://console.cloud.google.com/apis/library/classroom.googleapis.com
       YouTube     https://console.cloud.google.com/apis/library/youtube.googleapis.com

  3. Configure the consent screen:
     https://console.cloud.google.com/apis/credentials/consent
       - User type: External, Create
       - App name: JARVIS.  Support email and developer email: your own.
       - Save and continue through Scopes and Test users.
       - On Test users, ADD YOUR OWN GMAIL ADDRESS. Without this Google
         refuses the login later.

  4. Create the client:
     https://console.cloud.google.com/apis/credentials
       - Create Credentials > OAuth client ID
       - Application type: Desktop app.  Name: JARVIS.  Create.
       - Copy the Client ID and Client secret.

Then come back here and paste them in.
"""


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    b = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if b and os.path.exists(b):
        ctx.load_verify_locations(b)
    return ctx


def _read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def _write_env(updates: dict) -> None:
    """Update keys in place, preserving comments and everything else."""
    if not ENV.exists():
        sample = data.ROOT / ".env.example"
        ENV.write_text(sample.read_text(encoding="utf-8") if sample.exists() else "",
                       encoding="utf-8")
    lines = ENV.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append("")
        lines.append("# Written by setup_google.py")
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches the one redirect Google sends back."""

    result: dict = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.result = {k: v[0] for k, v in q.items()}
        ok = "code" in _Catcher.result
        body = (f"<!doctype html><meta charset=utf-8>"
                f"<title>JARVIS</title>"
                f"<body style='background:#080a0e;color:#e4e9f0;"
                f"font:14px ui-sans-serif,-apple-system,sans-serif;"
                f"display:grid;place-items:center;height:100vh;margin:0'>"
                f"<div style='text-align:center'>"
                f"<div style='letter-spacing:.3em;font-size:12px'>JARVIS</div>"
                f"<p style='color:{'#35d0ff' if ok else '#ff5f6d'}'>"
                f"{'Authorised. You can close this tab.' if ok else 'Refused: ' + _Catcher.result.get('error', 'unknown')}"
                f"</p></div>").encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ask(prompt: str, secret: bool = False) -> str:
    try:
        if secret:
            import getpass
            return getpass.getpass(prompt).strip()
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled.")
        sys.exit(1)


def main() -> None:
    print("\n  JARVIS — Google setup")
    print("  " + "-" * 66)
    print("\n  This asks Google for READ-ONLY access to:")
    for label in SCOPES.values():
        print(f"    - {label}")
    print("\n  Nothing here can send mail, change a calendar, or edit a file.")

    env = _read_env()
    cid = env.get("GOOGLE_CLIENT_ID", "")
    csec = env.get("GOOGLE_CLIENT_SECRET", "")

    if not (cid and csec):
        print(CONSOLE_STEPS)
        if ask("  Ready to paste the client ID and secret? [y/N] ").lower() not in ("y", "yes"):
            print("\n  Fine — run this again when you have them.\n")
            return
        cid = ask("\n  Client ID:     ")
        csec = ask("  Client secret: ", secret=True)
    else:
        print(f"\n  Using the client already in .env ({cid[:24]}...)")

    if not (cid and csec):
        print("\n  Need both. Nothing written.\n")
        return

    if not _port_free(REDIRECT_PORT):
        print(f"\n  Port {REDIRECT_PORT} is busy — close whatever is using it, "
              f"or set JARVIS_OAUTH_PORT.\n")
        return

    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",       # this is what mints a refresh token
        "prompt": "consent",            # force one, even on re-authorisation
        "state": state,
    })

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\n  Opening Google in your browser.")
    print("  If it does not open, paste this in yourself:\n")
    print(f"    {url}\n")
    print('  Google will warn that the app "isn\'t verified" — that is because')
    print("  it is yours and unpublished. Click Advanced, then Continue.\n")
    try:
        webbrowser.open(url)
    except Exception:                                       # noqa: BLE001
        pass

    print("  Waiting for you to approve it...", flush=True)
    for _ in range(600):                                    # about five minutes
        if _Catcher.result:
            break
        threading.Event().wait(0.5)
    server.server_close()

    res = _Catcher.result
    if not res:
        print("\n  Timed out. Run it again when you are ready.\n")
        return
    if res.get("state") != state:
        print("\n  The redirect did not match what we sent. Nothing written.\n")
        return
    if "code" not in res:
        print(f"\n  Google refused: {res.get('error', 'unknown')}")
        if res.get("error") == "access_denied":
            print("  If you have not added your own address under Test users")
            print("  on the consent screen, Google always refuses. Add it.\n")
        return

    print("  Got the code. Exchanging it for a token...")
    body = urllib.parse.urlencode({
        "code": res["code"], "client_id": cid, "client_secret": csec,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }).encode()
    try:
        req = urllib.request.Request(TOKEN, data=body, method="POST",
                                     headers={"Content-Type":
                                              "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"\n  Google rejected the exchange: {e.code}")
        print("  " + e.read()[:300].decode("utf-8", "replace") + "\n")
        return
    except Exception as e:                                  # noqa: BLE001
        print(f"\n  Could not reach Google: {e}\n")
        return

    refresh = tok.get("refresh_token")
    if not refresh:
        print("\n  Google returned no refresh token. That happens when the app")
        print("  was already authorised. Remove JARVIS at")
        print("  https://myaccount.google.com/permissions and run this again.\n")
        return

    _write_env({"GOOGLE_CLIENT_ID": cid, "GOOGLE_CLIENT_SECRET": csec,
                "GOOGLE_REFRESH_TOKEN": refresh, "GOOGLE_ACCESS_TOKEN": ""})

    print(f"\n  Written to {ENV} (chmod 600).")
    print("  Granted:")
    for s in (tok.get("scope") or "").split():
        print(f"    - {SCOPES.get(s, s)}")
    print("\n  Restart JARVIS, then `python3 agent/doctor.py` to confirm.\n")


if __name__ == "__main__":
    main()
