"""connectors.py — the outside world, read-only, one adapter per service.

Modular by design: a connector is a small class with `status()` and a few
fetch methods. Adding a service means adding a class and registering it;
nothing else in the codebase changes.

Three rules hold for every connector in this file, without exception:

  1. READ ONLY. Every request below is a GET. There is no POST, PUT, PATCH
     or DELETE against any user service anywhere in this module, and
     data/guardrails_test.py asserts that. The one POST is Google's OAuth
     token refresh, which mints a read scope and touches no user data.
  2. NOT CONNECTED MEANS NO DATA. A connector that is not configured
     returns {"ok": False, "reason": ...}. It never returns something
     plausible. Fabricating an order or a price is the worst failure this
     system can have.
  3. CONFIG COMES FROM data.py. Tokens, shop domain and the demo switch are
     resolved there, so data.py stays the only file that decides what real
     data means.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import data as _cfg

# Read-only scopes. If a token carries more than this, that is the operator's
# doing, not ours — we still only ever call the endpoints below.
GOOGLE_READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    b = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if b and os.path.exists(b):
        ctx.load_verify_locations(b)
    return ctx


def _get_json(url: str, headers: dict, timeout: int = 25) -> dict | list:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return json.loads(r.read() or b"{}")


def _fixture(name: str, default):
    path = _cfg.DEMO_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return default


# ------------------------------------------------------------------ base

class Connector:
    key = "base"
    label = "Base"
    domain = "none"          # school | business | deca | all
    provides: list[str] = []

    def configured(self) -> tuple[bool, str]:
        return False, "not implemented"

    def status(self) -> dict:
        if _cfg.demo_mode():
            return {"key": self.key, "label": self.label, "domain": self.domain,
                    "connected": True, "mode": "demo", "readonly": True,
                    "reason": "demo fixtures — these numbers are invented",
                    "provides": self.provides}
        ok, why = self.configured()
        return {"key": self.key, "label": self.label, "domain": self.domain,
                "connected": ok, "mode": "live", "readonly": True,
                "reason": "" if ok else why, "provides": self.provides}

    def unavailable(self, what: str) -> dict:
        """The honest empty answer. Never a plausible-looking one."""
        st = self.status()
        return {"ok": False, "connector": self.key, "what": what,
                "reason": st["reason"] or f"{self.label} is not connected",
                "spoken": f"{self.label} is not connected, Sir. I have no {what} to give you."}


# ------------------------------------------------------------------ google

class _Google(Connector):
    """Shared OAuth for Gmail, Calendar and Drive."""

    _token: str | None = None
    _expires: float = 0.0

    def configured(self) -> tuple[bool, str]:
        c = _cfg.connector_config()
        if c.get("google_access_token"):
            return True, ""
        if c.get("google_refresh_token") and c.get("google_client_id") \
                and c.get("google_client_secret"):
            return True, ""
        return False, ("Google is not connected — set GOOGLE_ACCESS_TOKEN, or "
                       "GOOGLE_REFRESH_TOKEN with GOOGLE_CLIENT_ID and "
                       "GOOGLE_CLIENT_SECRET, in .env")

    def _access_token(self) -> str | None:
        c = _cfg.connector_config()
        if c.get("google_access_token"):
            return c["google_access_token"]
        if _Google._token and time.time() < _Google._expires - 60:
            return _Google._token
        if not (c.get("google_refresh_token") and c.get("google_client_id")):
            return None
        # The only POST in this module. It mints a read token; it reads,
        # writes and sends nothing.
        body = urllib.parse.urlencode({
            "client_id": c["google_client_id"],
            "client_secret": c["google_client_secret"],
            "refresh_token": c["google_refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=25, context=_ssl_ctx()) as r:
            out = json.loads(r.read())
        _Google._token = out.get("access_token")
        _Google._expires = time.time() + int(out.get("expires_in", 3000))
        return _Google._token

    def _api(self, url: str) -> dict | list:
        tok = self._access_token()
        if not tok:
            raise RuntimeError("no Google access token")
        return _get_json(url, {"Authorization": f"Bearer {tok}"})


class GmailConnector(_Google):
    key = "gmail"
    label = "Gmail"
    domain = "all"
    provides = ["mail"]

    def messages(self, limit: int = 15, unread_only: bool = False) -> dict:
        if _cfg.demo_mode():
            msgs = _fixture("inbox.json", [])
            if unread_only:
                msgs = [m for m in msgs if m.get("unread")]
            return {"ok": True, "source": "demo", "messages": msgs[:limit]}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("mail")
        try:
            q = "is:unread" if unread_only else "in:inbox"
            listing = self._api(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages"
                f"?maxResults={int(limit)}&q={urllib.parse.quote(q)}")
            out = []
            for ref in (listing.get("messages") or [])[:limit]:
                m = self._api(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                    f"{ref['id']}?format=metadata"
                    "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date")
                head = {h["name"].lower(): h["value"]
                        for h in m.get("payload", {}).get("headers", [])}
                out.append({
                    "id": m.get("id"),
                    "from": head.get("from", "unknown"),
                    "subject": head.get("subject", "(no subject)"),
                    "body": m.get("snippet", ""),
                    "received": head.get("date", ""),
                    "unread": "UNREAD" in (m.get("labelIds") or []),
                    "client": None,
                })
            return {"ok": True, "source": "gmail", "messages": out}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key, "reason": f"Gmail failed: {e}",
                    "spoken": "Gmail did not answer, Sir."}


class CalendarConnector(_Google):
    key = "calendar"
    label = "Google Calendar"
    domain = "all"
    provides = ["events"]

    def events(self, days: int = 7, limit: int = 20) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo",
                    **_fixture("calendar.json", {"events": [], "slipped": []})}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("calendar")
        try:
            now = datetime.now(timezone.utc)
            url = ("https://www.googleapis.com/calendar/v3/calendars/primary/events"
                   f"?timeMin={urllib.parse.quote(now.isoformat())}"
                   f"&timeMax={urllib.parse.quote((now + timedelta(days=days)).isoformat())}"
                   f"&maxResults={int(limit)}&singleEvents=true&orderBy=startTime")
            raw = self._api(url)
            evs = []
            for it in raw.get("items", []):
                start = (it.get("start") or {}).get("dateTime") or \
                        (it.get("start") or {}).get("date") or ""
                evs.append({"title": it.get("summary", "(untitled)"),
                            "start": start, "minutes": None,
                            "location": it.get("location")})
            return {"ok": True, "source": "google-calendar",
                    "events": evs, "slipped": []}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key,
                    "reason": f"Calendar failed: {e}",
                    "spoken": "The calendar did not answer, Sir."}


class DriveConnector(_Google):
    key = "drive"
    label = "Google Drive"
    domain = "all"
    provides = ["files"]

    def files(self, query: str = "", limit: int = 15) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo", "files": [],
                    "note": "In demo mode the local vault stands in for Drive."}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("files")
        try:
            q = f"name contains '{query}' and trashed = false" if query else "trashed = false"
            url = ("https://www.googleapis.com/drive/v3/files"
                   f"?q={urllib.parse.quote(q)}&pageSize={int(limit)}"
                   "&fields=files(id,name,mimeType,modifiedTime,webViewLink)")
            raw = self._api(url)
            return {"ok": True, "source": "drive", "files": raw.get("files", [])}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key, "reason": f"Drive failed: {e}",
                    "spoken": "Drive did not answer, Sir."}


class ClassroomConnector(_Google):
    """Where school deadlines actually live, for most students.

    Coursework comes with real due dates and a submission state, so
    `deadlines` can stop depending on someone having typed the assignment
    into a markdown file.
    """

    key = "classroom"
    label = "Google Classroom"
    domain = "school"
    provides = ["courses", "coursework", "deadlines"]

    def configured(self) -> tuple[bool, str]:
        ok, why = super().configured()
        if not ok:
            return ok, why.replace("Google is not connected",
                                   "Classroom is not connected")
        return True, ""

    def coursework(self, limit: int = 60) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo", "courses": [], "items": [],
                    "note": "In demo mode the local vault stands in for Classroom."}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("coursework")
        try:
            courses = (self._api(
                "https://classroom.googleapis.com/v1/courses"
                "?courseStates=ACTIVE&pageSize=50").get("courses") or [])

            # Which items are already turned in, so finished work stops
            # being reported as outstanding.
            done: set[str] = set()
            items: list[dict] = []
            for c in courses:
                cid = c.get("id")
                work = (self._api(
                    "https://classroom.googleapis.com/v1/courses/"
                    f"{cid}/courseWork?pageSize=40").get("courseWork") or [])
                try:
                    subs = (self._api(
                        "https://classroom.googleapis.com/v1/courses/"
                        f"{cid}/courseWork/-/studentSubmissions"
                        "?pageSize=200").get("studentSubmissions") or [])
                    done |= {sb.get("courseWorkId") for sb in subs
                             if sb.get("state") in ("TURNED_IN", "RETURNED")}
                except Exception:                           # noqa: BLE001
                    pass            # submissions can be refused per course

                for w in work:
                    d, t = w.get("dueDate"), w.get("dueTime") or {}
                    due = (f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
                           if d else None)
                    items.append({
                        "id": w.get("id"), "title": w.get("title"),
                        "course": c.get("name"), "due": due,
                        "due_time": (f"{t.get('hours', 0):02d}:"
                                     f"{t.get('minutes', 0):02d}") if t else None,
                        "link": w.get("alternateLink"),
                        "points": w.get("maxPoints"),
                        "type": (w.get("workType") or "assignment").lower(),
                        "submitted": w.get("id") in done,
                    })
            items.sort(key=lambda i: (i["due"] or "9999-99-99"))
            return {"ok": True, "source": "classroom",
                    "courses": [{"id": c.get("id"), "name": c.get("name"),
                                 "section": c.get("section")} for c in courses],
                    "items": items[:limit]}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key,
                    "reason": f"Classroom failed: {e}",
                    "spoken": "Classroom did not answer, Sir."}


class YouTubeConnector(_Google):
    """The channel side of the business."""

    key = "youtube"
    label = "YouTube"
    domain = "business"
    provides = ["channel", "videos"]

    def configured(self) -> tuple[bool, str]:
        ok, why = super().configured()
        if not ok:
            return ok, why.replace("Google is not connected",
                                   "YouTube is not connected")
        return True, ""

    def channel(self, videos: int = 8) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo", "demo": True,
                    "channel": None, "videos": [],
                    "note": "No demo channel — connect YouTube for the real one."}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("channel")
        try:
            me = self._api("https://www.googleapis.com/youtube/v3/channels"
                           "?part=snippet,statistics,contentDetails&mine=true")
            chans = me.get("items") or []
            if not chans:
                return {"ok": True, "source": "youtube", "channel": None,
                        "videos": [],
                        "note": "That Google account has no YouTube channel."}
            ch = chans[0]
            stats = ch.get("statistics", {})
            uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists")
                       or {}).get("uploads")
            recent = []
            if uploads:
                pl = self._api("https://www.googleapis.com/youtube/v3/playlistItems"
                               f"?part=snippet,contentDetails&maxResults={int(videos)}"
                               f"&playlistId={uploads}")
                ids = [i["contentDetails"]["videoId"]
                       for i in (pl.get("items") or [])
                       if i.get("contentDetails", {}).get("videoId")]
                if ids:
                    vs = self._api("https://www.googleapis.com/youtube/v3/videos"
                                   "?part=snippet,statistics&id=" + ",".join(ids))
                    for v in vs.get("items", []):
                        st = v.get("statistics", {})
                        recent.append({
                            "id": v.get("id"),
                            "title": v.get("snippet", {}).get("title"),
                            "published": v.get("snippet", {}).get("publishedAt"),
                            "views": int(st.get("viewCount", 0) or 0),
                            "likes": int(st.get("likeCount", 0) or 0),
                            "comments": int(st.get("commentCount", 0) or 0),
                        })
            return {"ok": True, "source": "youtube",
                    "channel": {
                        "title": ch.get("snippet", {}).get("title"),
                        "subscribers": int(stats.get("subscriberCount", 0) or 0),
                        "views": int(stats.get("viewCount", 0) or 0),
                        "videos": int(stats.get("videoCount", 0) or 0),
                        "hidden_subs": stats.get("hiddenSubscriberCount", False),
                    },
                    "videos": recent}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key,
                    "reason": f"YouTube failed: {e}",
                    "spoken": "YouTube did not answer, Sir."}


# ------------------------------------------------------------------ shopify

class ShopifyConnector(Connector):
    key = "shopify"
    label = "Shopify"
    domain = "business"
    provides = ["orders", "products", "customers"]

    API = "2024-10"

    def configured(self) -> tuple[bool, str]:
        c = _cfg.connector_config()
        if c.get("shopify_shop") and c.get("shopify_token"):
            return True, ""
        return False, ("Shopify is not connected — set SHOPIFY_SHOP and "
                       "SHOPIFY_ACCESS_TOKEN in .env. Until then I have no "
                       "products, prices, margins or orders, and I will not "
                       "invent any.")

    def _api(self, path: str) -> dict:
        c = _cfg.connector_config()
        shop = c["shopify_shop"].replace("https://", "").replace("/", "")
        if not shop.endswith(".myshopify.com"):
            shop += ".myshopify.com"
        return _get_json(f"https://{shop}/admin/api/{self.API}/{path}",
                         {"X-Shopify-Access-Token": c["shopify_token"],
                          "Accept": "application/json"})

    def orders(self, limit: int = 25) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo", "demo": True,
                    "orders": _fixture("shopify_orders.json", [])[:limit]}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("orders")
        try:
            raw = self._api(f"orders.json?status=any&limit={int(limit)}")
            out = [{
                "id": o.get("id"), "name": o.get("name"),
                "total": o.get("total_price"), "currency": o.get("currency"),
                "created": o.get("created_at"),
                "financial": o.get("financial_status"),
                "fulfilment": o.get("fulfillment_status") or "unfulfilled",
                "customer": ((o.get("customer") or {}).get("first_name", "") + " " +
                             (o.get("customer") or {}).get("last_name", "")).strip() or None,
                "items": [li.get("title") for li in (o.get("line_items") or [])],
            } for o in raw.get("orders", [])]
            return {"ok": True, "source": "shopify", "demo": False, "orders": out}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key, "reason": f"Shopify failed: {e}",
                    "spoken": "Shopify did not answer, Sir."}

    def products(self, limit: int = 50) -> dict:
        if _cfg.demo_mode():
            return {"ok": True, "source": "demo", "demo": True,
                    "products": _fixture("shopify_products.json", [])[:limit]}
        ok, why = self.configured()
        if not ok:
            return self.unavailable("products")
        try:
            raw = self._api(f"products.json?limit={int(limit)}")
            out = []
            for p in raw.get("products", []):
                for v in (p.get("variants") or [{}])[:1]:
                    out.append({"id": p.get("id"), "title": p.get("title"),
                                "price": v.get("price"), "sku": v.get("sku"),
                                "inventory": v.get("inventory_quantity"),
                                "status": p.get("status")})
            return {"ok": True, "source": "shopify", "demo": False, "products": out}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "connector": self.key, "reason": f"Shopify failed: {e}",
                    "spoken": "Shopify did not answer, Sir."}


# ------------------------------------------------------------------ registry

REGISTRY: dict[str, Connector] = {
    "gmail": GmailConnector(),
    "calendar": CalendarConnector(),
    "drive": DriveConnector(),
    "classroom": ClassroomConnector(),
    "youtube": YouTubeConnector(),
    "shopify": ShopifyConnector(),
}


def get(key: str) -> Connector:
    return REGISTRY[key]


def status_all() -> list[dict]:
    return [c.status() for c in REGISTRY.values()]


def missing() -> list[str]:
    return [s["label"] for s in status_all() if not s["connected"]]
