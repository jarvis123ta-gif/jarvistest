"""watch.py — noticing things without being asked.

Answering a question is not managing. Managing is saying "the chemistry lab
went overdue an hour ago" before anyone thinks to ask.

The mechanism is deliberately dull: take a snapshot of the things worth
watching, compare it with the last one, and report only what CHANGED. That
gives three properties that matter more than cleverness —

  * it cannot nag. A thing is reported once, when it changes, and then it is
    part of the baseline.
  * it cannot invent. Every notice names the real record it came from.
  * it survives a restart. The baseline lives in memory/watch.json, so
    closing the laptop does not produce a flood of stale news in the morning.

The first run of a fresh install records the baseline and says nothing at
all, which is the correct behaviour and the one that is easy to get wrong.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import connectors
import tools
import tz as _tz
from data import MEMORY_DIR, TIMEZONE

TZ = _tz.get(TIMEZONE)
STATE = MEMORY_DIR / "watch.json"

# How close a deadline has to get before it is worth interrupting for.
URGENT_DAYS = 2
SOON_DAYS = 7

_lock = threading.Lock()
_pending: list[dict] = []


# ---------------------------------------------------------------- snapshot

def snapshot(vault) -> dict:
    """Everything worth watching, keyed so it can be compared later."""
    snap: dict = {}

    for r in tools._deadline_rows(vault):
        if r["days"] > 30:
            continue
        snap[f"deadline:{r['id']}"] = {
            "what": r["what"], "days": r["days"], "domain": r["domain"],
            "due": r["due"], "source": r.get("source", "files"),
        }

    store = tools.store_status(vault)["card"]
    if store.get("connected"):
        for o in store.get("orders", []):
            snap[f"order:{o.get('id')}"] = {
                "name": o.get("name"), "total": o.get("total"),
                "fulfilment": o.get("fulfilment"),
                "hours": o.get("hours_old"),
                "demo": bool(store.get("demo")),
            }
        for p in store.get("low_stock", []):
            snap[f"stock:{p.get('title')}"] = {
                "title": p.get("title"), "inventory": p.get("inventory")}

    mail = connectors.get("gmail").messages(limit=25)
    if mail.get("ok"):
        for m in mail.get("messages", []):
            if not m.get("unread"):
                continue
            snap[f"mail:{m.get('id')}"] = {
                "from": m.get("from"), "subject": m.get("subject"),
                "domain": m.get("domain")}

    return snap


# ---------------------------------------------------------------- diff

def _notice(key: str, level: str, spoken: str, detail: dict) -> dict:
    return {"key": key, "level": level, "spoken": spoken, "detail": detail,
            "at": datetime.now(TZ).isoformat(timespec="minutes")}


def diff(old: dict, new: dict) -> list[dict]:
    """Only what changed, and only what is worth saying out loud."""
    out: list[dict] = []

    for key, now in new.items():
        was = old.get(key)
        kind = key.split(":", 1)[0]

        if kind == "deadline":
            d, what = now["days"], now["what"]
            if was is None:
                # New work. Only interrupt if it is already close.
                if d < 0:
                    out.append(_notice(key, "high",
                        f"{what} is already overdue, Sir.", now))
                elif d <= SOON_DAYS:
                    out.append(_notice(key, "normal",
                        f"New: {what}, {tools._due_phrase(d)}.", now))
                continue
            before = was["days"]
            if before >= 0 > d:
                out.append(_notice(key, "high",
                    f"{what} just went overdue, Sir.", now))
            elif before > URGENT_DAYS >= d:
                out.append(_notice(key, "high",
                    f"{what} is {tools._due_phrase(d)}.", now))

        elif kind == "order":
            if was is None:
                out.append(_notice(key, "normal",
                    f"New order {now['name']}"
                    + (f", {now['total']}" if now.get("total") else "")
                    + ("." if not now.get("demo") else " — demo data."), now))
            elif (was.get("fulfilment") != now.get("fulfilment")
                  and now.get("fulfilment") == "fulfilled"):
                continue                       # good news, not worth a word
            elif (now.get("fulfilment") != "fulfilled"
                  and (now.get("hours") or 0) >= tools.FULFILMENT_WINDOW_HOURS
                  and (was.get("hours") or 0) < tools.FULFILMENT_WINDOW_HOURS):
                out.append(_notice(key, "high",
                    f"Order {now['name']} is past the "
                    f"{tools.FULFILMENT_WINDOW_HOURS}-hour window.", now))

        elif kind == "stock":
            if was is None:
                out.append(_notice(key, "normal",
                    f"{now['title']} is down to {now['inventory']}.", now))

        elif kind == "mail":
            if was is None and now.get("domain") in ("school", "deca"):
                out.append(_notice(key, "normal",
                    f"Mail from {now['from']} — {now['subject']}.", now))

    order = {"high": 0, "normal": 1}
    out.sort(key=lambda n: order.get(n["level"], 2))
    return out


# ---------------------------------------------------------------- state

def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def _save(snap: dict) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    except OSError:
        pass


def check(vault) -> list[dict]:
    """One pass. Returns new notices and folds them into the baseline."""
    new = snapshot(vault)
    stored = _load()
    first_run = not stored

    notices = [] if first_run else diff(stored.get("snap", {}), new)
    _save({"snap": new,
           "checked": datetime.now(TZ).isoformat(timespec="seconds")})

    if notices:
        with _lock:
            _pending.extend(notices)
            del _pending[:-30]                 # never hoard
    return notices


def pending(clear: bool = True) -> list[dict]:
    with _lock:
        out = list(_pending)
        if clear:
            _pending.clear()
    return out


def status() -> dict:
    stored = _load()
    return {"watching": len(stored.get("snap", {})),
            "last_check": stored.get("checked"),
            "queued": len(_pending),
            "state_file": str(STATE)}
