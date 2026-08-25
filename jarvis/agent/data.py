"""data.py — THE ONLY FILE THAT TOUCHES REAL DATA.

Every path and every credential that points at the principals' actual life
is resolved here and nowhere else. `JARVIS_DEMO` is read in this file only;
the rest of the codebase asks this module and never checks the variable
itself.

Default is DEMO. You have to opt *in* to real life.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEMO_DIR = ROOT / "data"

# Central Time, per the principals. Everything dated is rendered in this zone.
TIMEZONE = os.environ.get("JARVIS_TZ", "America/Chicago")


def demo_mode() -> bool:
    return os.environ.get("JARVIS_DEMO", "1").strip() not in ("0", "false", "no", "off")


def mode_label() -> str:
    return "demo" if demo_mode() else "live"


# ------------------------------------------------------------------
# THE REAL FOLDERS
#
# Absolute paths, read-only, indexed recursively. They are grouped by domain
# so JARVIS knows whether a note is school, business or DECA without
# guessing from its contents.
#
# These are deliberately EMPTY. The principals said not to assume or invent
# paths, so nothing is assumed. Fill these in on the machine where JARVIS
# runs, or set JARVIS_SCHOOL_ROOTS / JARVIS_BUSINESS_ROOTS / JARVIS_DECA_ROOTS
# (os.pathsep-separated) instead of editing code.
# ------------------------------------------------------------------

REAL_ROOTS: dict[str, list[str]] = {
    "school":   [],     # e.g. "/Users/sai/Documents/School"
    "business": [],     # e.g. "/Users/sai/Documents/Shopify"
    "deca":     [],     # e.g. "/Users/sai/Documents/DECA"
}

_ENV_KEYS = {"school": "JARVIS_SCHOOL_ROOTS",
             "business": "JARVIS_BUSINESS_ROOTS",
             "deca": "JARVIS_DECA_ROOTS"}

DEMO_ROOTS: dict[str, list[str]] = {
    "school":   [str(DEMO_DIR / "vault" / "school")],
    "business": [str(DEMO_DIR / "vault" / "business")],
    "deca":     [str(DEMO_DIR / "vault" / "deca")],
}


def roots_by_domain() -> dict[str, list[str]]:
    if demo_mode():
        return {k: list(v) for k, v in DEMO_ROOTS.items()}
    out: dict[str, list[str]] = {}
    for dom, envkey in _ENV_KEYS.items():
        env = os.environ.get(envkey, "").strip()
        out[dom] = ([p for p in env.split(os.pathsep) if p.strip()]
                    if env else list(REAL_ROOTS.get(dom, [])))
    # A single JARVIS_ROOTS still works; it just lands in "school" by default,
    # which matches "school is the default priority".
    legacy = os.environ.get("JARVIS_ROOTS", "").strip()
    if legacy:
        out.setdefault("school", []).extend(
            p for p in legacy.split(os.pathsep) if p.strip())
    return out


def active_roots() -> list[str]:
    seen, flat = set(), []
    for paths in roots_by_domain().values():
        for p in paths:
            if p not in seen:
                seen.add(p)
                flat.append(p)
    return flat


def domain_of(path: str) -> str:
    """Which world a file belongs to, decided by which root it sits under."""
    ap = os.path.abspath(os.path.expanduser(path))
    best, best_len = "unsorted", -1
    for dom, roots in roots_by_domain().items():
        for r in roots:
            ar = os.path.abspath(os.path.expanduser(r))
            if ap.startswith(ar) and len(ar) > best_len:
                best, best_len = dom, len(ar)
    return best


def roots_status() -> dict:
    """Degrade loudly: the UI renders this so a bad path is visible."""
    by = roots_by_domain()
    roots = active_roots()
    return {
        "mode": mode_label(),
        "timezone": TIMEZONE,
        "by_domain": by,
        "roots": roots,
        "missing": [r for r in roots if not os.path.isdir(os.path.expanduser(r))],
        "unconfigured": [d for d, v in by.items() if not v],
        "configured": bool(roots),
    }


# ------------------------------------------------------------------
# CONNECTOR CREDENTIALS
#
# Read here so connectors.py holds no configuration of its own. All of these
# are read-only scopes. Nothing in this project has a send or write scope.
# ------------------------------------------------------------------

def connector_config() -> dict:
    e = os.environ.get
    return {
        "shopify_shop":          (e("SHOPIFY_SHOP") or "").strip(),
        "shopify_token":         (e("SHOPIFY_ACCESS_TOKEN") or "").strip(),
        "google_access_token":   (e("GOOGLE_ACCESS_TOKEN") or "").strip(),
        "google_refresh_token":  (e("GOOGLE_REFRESH_TOKEN") or "").strip(),
        "google_client_id":      (e("GOOGLE_CLIENT_ID") or "").strip(),
        "google_client_secret":  (e("GOOGLE_CLIENT_SECRET") or "").strip(),
    }


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
