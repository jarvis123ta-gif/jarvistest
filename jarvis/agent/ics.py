"""ics.py — reading calendar feeds without OAuth.

Schoology, Canvas, PowerSchool, Google Calendar and almost every school
portal will hand you a secret .ics URL. One link, no consent screen, no
client secret, no Cloud Console. For getting assignment due dates into
JARVIS that is a far better trade than a portal-specific API client, and it
works for portals nobody has written a client for.

A small, forgiving iCalendar parser: enough of RFC 5545 for real feeds,
standard library only.

Treat the URL itself as a credential — anyone holding it can read that
calendar. They belong in .env like any other key.
"""

from __future__ import annotations

import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

UA = "Mozilla/5.0 (compatible; jarvis/1.0)"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    b = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if b and os.path.exists(b):
        ctx.load_verify_locations(b)
    return ctx


def fetch(url: str, timeout: int = 25) -> str:
    # webcal:// is just https with a different hat on.
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return r.read().decode("utf-8", "replace")


def unfold(text: str) -> list[str]:
    """RFC 5545 folds long lines by starting the continuation with a space."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(v: str) -> str:
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
             .replace("\;", ";").replace("\\\\", "\\")).strip()


def _parse_dt(value: str, params: str) -> tuple[str | None, str | None]:
    """Return (YYYY-MM-DD, HH:MM or None). Times are left in the feed's own
    zone: a due date is a due date, and shifting it by an hour to satisfy a
    timezone conversion is how deadlines get quietly reported wrong."""
    v = value.strip()
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", v):
        if len(v) >= 8:
            return f"{v[0:4]}-{v[4:6]}-{v[6:8]}", None
        return None, None
    m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})", v)
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{y}-{mo}-{d}", f"{hh}:{mm}"
    return None, None


def parse(text: str) -> list[dict]:
    """Every VEVENT with a start date, as plain dicts."""
    events: list[dict] = []
    current: dict | None = None

    for line in unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.startswith("END:VEVENT"):
            if current and current.get("date"):
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        name, _, value = line.partition(":")
        key = name.split(";", 1)[0].upper()
        params = name[len(key):]

        if key == "SUMMARY":
            current["title"] = _unescape(value)
        elif key == "DESCRIPTION":
            current["description"] = _unescape(value)[:400]
        elif key == "URL":
            current["url"] = value.strip()
        elif key == "UID":
            current["uid"] = value.strip()
        elif key == "LOCATION":
            current["location"] = _unescape(value)
        elif key in ("DTSTART", "DUE"):
            d, t = _parse_dt(value, params)
            if d:
                current["date"], current["time"] = d, t
        elif key == "DTEND" and not current.get("date"):
            d, t = _parse_dt(value, params)
            if d:
                current["date"], current["time"] = d, t

    for e in events:
        e.setdefault("title", "(untitled)")
        e.setdefault("time", None)
        e.setdefault("uid", f"{e['date']}-{e['title'][:40]}")
    events.sort(key=lambda e: (e["date"], e["time"] or ""))
    return events


def upcoming(events: list[dict], back_days: int = 14,
             forward_days: int = 60) -> list[dict]:
    """A calendar full of last year's classes is noise, so trim the window."""
    today = date.today()
    lo = (today - timedelta(days=back_days)).isoformat()
    hi = (today + timedelta(days=forward_days)).isoformat()
    return [e for e in events if lo <= e["date"] <= hi]


def load(url: str) -> dict:
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"feed returned {e.code}"}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "reason": f"could not fetch the feed: {e}"}
    if "BEGIN:VCALENDAR" not in raw:
        return {"ok": False,
                "reason": "that URL did not return a calendar — check you "
                          "copied the iCal/ICS address rather than the page"}
    events = parse(raw)
    return {"ok": True, "events": events, "total": len(events)}
