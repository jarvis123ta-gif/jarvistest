#!/usr/bin/env python3
"""configure.py — set JARVIS up on this machine, with as little typing as
possible.

    python3 agent/configure.py

It looks for your school, business and DECA folders itself, offers what it
found, takes a Gemini key if you have one, switches off demo mode and
reports what actually got indexed. Your .env keeps its comments and every
value this does not touch.

Nothing here reaches the network except to validate a key you paste.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import data                                                 # noqa: E402
import envfile                                              # noqa: E402

data.load_env_file()

# Where people actually keep things on a Mac, including the iCloud path
# that trips up anyone looking only in ~/Documents.
SEARCH_ROOTS = [
    "~/Documents", "~/Desktop", "~/Downloads", "~",
    "~/Library/Mobile Documents/com~apple~CloudDocs",
    "~/Library/CloudStorage",
    "~/Google Drive", "~/OneDrive", "~/Dropbox",
]

HINTS = {
    "school": ["school", "class", "classes", "homework", "assignments",
               "coursework", "academics", "ap ", "junior", "senior",
               "sophomore", "freshman", "study", "notes"],
    "business": ["shopify", "store", "business", "ecommerce", "e-commerce",
                 "brand", "product", "orders", "youtube", "content"],
    "deca": ["deca", "competition", "roleplay", "written event", "icdc",
             "districts", "states"],
}

READABLE = {".md", ".markdown", ".txt", ".pdf"}


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled — nothing written.\n")
        sys.exit(1)


def count_readable(path: Path, cap: int = 400) -> int:
    """How many files JARVIS could actually read in there."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "node_modules"]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in READABLE:
                n += 1
                if n >= cap:
                    return n
    return n


def find_candidates() -> dict[str, list[tuple[Path, int]]]:
    """Look for folders whose names suggest one of the three worlds."""
    seen: set[Path] = set()
    found: dict[str, list[tuple[Path, int]]] = {k: [] for k in HINTS}

    for root in SEARCH_ROOTS:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            continue
        try:
            entries = [e for e in base.iterdir() if e.is_dir()]
        except OSError:
            continue
        # One level down as well: ~/Documents/School/AP Chem
        deeper = []
        for e in entries[:60]:
            try:
                deeper += [d for d in e.iterdir() if d.is_dir()][:40]
            except OSError:
                pass
        for folder in entries + deeper:
            if folder in seen or folder.name.startswith("."):
                continue
            seen.add(folder)
            low = folder.name.lower()
            for domain, words in HINTS.items():
                if any(w in low for w in words):
                    n = count_readable(folder)
                    if n:
                        found[domain].append((folder, n))
                    break
    for domain in found:
        found[domain].sort(key=lambda x: -x[1])
    return found


def choose(domain: str, options: list[tuple[Path, int]]) -> str:
    label = {"school": "SCHOOL", "business": "SHOPIFY / BUSINESS",
             "deca": "DECA"}[domain]
    print(f"\n  {label}")
    if not options:
        print("    Nothing obvious found.")
    for i, (path, n) in enumerate(options[:6], 1):
        print(f"    {i}. {path}   ({n} readable file{'s' if n != 1 else ''})")
    print("    0. skip this one")
    print("    Or paste a path — dragging the folder into this window works.")

    while True:
        answer = ask("    > ")
        if answer in ("", "0"):
            return ""
        if answer.isdigit() and 1 <= int(answer) <= min(len(options), 6):
            return str(options[int(answer) - 1][0])
        # A dragged path arrives quoted or backslash-escaped.
        p = answer.strip().strip("'\"").replace("\\ ", " ")
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            return p
        print(f"    Not a folder: {p}")


def main() -> None:
    print("\n  JARVIS — configure")
    print("  " + "-" * 66)
    print("\n  Looking for your folders...", flush=True)
    found = find_candidates()

    roots: dict[str, str] = {}
    for domain in ("school", "business", "deca"):
        roots[domain] = choose(domain, found[domain])

    updates = {
        "JARVIS_SCHOOL_ROOTS": roots["school"],
        "JARVIS_BUSINESS_ROOTS": roots["business"],
        "JARVIS_DECA_ROOTS": roots["deca"],
    }
    updates = {k: v for k, v in updates.items() if v}

    if not updates:
        print("\n  No folders chosen. Staying on demo data so nothing breaks.")
        print("  Run this again once you know where things live.\n")
        return

    current = envfile.read()
    if not current.get("GEMINI_API_KEY") and not current.get("ANTHROPIC_API_KEY"):
        print("\n  A cloud model is much faster than one on your laptop.")
        print("  Free key, no card: https://aistudio.google.com/apikey")
        print("  (Whatever that page shows is the key — the prefix varies.)")
        key = ask("\n  Paste a Gemini key (or Enter to keep using Ollama): ")
        if key:
            updates["GEMINI_API_KEY"] = key
            updates["JARVIS_LLM"] = "auto"

    # Calendar feeds: the no-OAuth route into Schoology, Canvas, PowerSchool.
    if not current.get("JARVIS_SCHOOL_ICS"):
        print("\n  SCHOOL PORTAL")
        print("  Schoology, Canvas and PowerSchool all publish a secret")
        print("  calendar link. One URL and JARVIS sees every due date —")
        print("  no login, no consent screen.")
        print("\n    Schoology : Calendar > iCal Feed (or Settings > iCal)")
        print("    Canvas    : Calendar > Calendar Feed (bottom right)")
        print("    Google    : Settings > your calendar > Secret address in")
        print("                iCal format")
        ics = ask("\n  Paste the .ics URL (or Enter to skip): ")
        if ics:
            updates["JARVIS_SCHOOL_ICS"] = ics
    if not current.get("JARVIS_DECA_ICS"):
        d = ask("  A separate DECA calendar link? (or Enter to skip): ")
        if d:
            updates["JARVIS_DECA_ICS"] = d

    if not current.get("SHOPIFY_ACCESS_TOKEN"):
        print("\n  SHOPIFY")
        print("  Admin > Settings > Apps and sales channels > Develop apps")
        print("  > Create an app > Configure Admin API scopes.")
        print("  Tick ONLY read_orders, read_products, read_customers.")
        print("  Install it, then reveal the Admin API access token.")
        shop = ask("\n  Your store (e.g. my-store.myshopify.com, Enter to skip): ")
        if shop:
            tok = ask("  Admin API access token: ")
            if tok:
                updates["SHOPIFY_SHOP"] = shop
                updates["SHOPIFY_ACCESS_TOKEN"] = tok

    updates["JARVIS_DEMO"] = "0"

    print("\n  About to write to .env:\n")
    for k, v in updates.items():
        shown = (v[:12] + "..." if "KEY" in k or "TOKEN" in k else v)
        print(f"    {k}={shown}")
    if ask("\n  Write it? [Y/n] ").lower() in ("n", "no"):
        print("\n  Nothing written.\n")
        return

    changed = envfile.write(updates)
    print(f"\n  Wrote {len(changed)} setting(s) to {envfile.ENV} (chmod 600).")

    # Prove it worked, rather than claiming it did.
    for k, v in updates.items():
        os.environ[k] = v
    print("\n  Indexing your files...", flush=True)
    try:
        from vault import Vault
        v = Vault(data.active_roots(), "live", data.domain_of).build()
        print(f"\n  {len(v.notes)} notes, {len(v.edges)} links.")
        for dom, n in v.counts_by_domain().items():
            print(f"    {dom:<10} {n}")
        if not v.notes:
            print("\n  Nothing readable in those folders. JARVIS reads .md,")
            print("  .txt and .pdf — Word and Google Docs are invisible to it.")
            print("  Your Drive connector can reach the Google ones.")
    except Exception as e:                                  # noqa: BLE001
        print(f"\n  Could not index: {e}")

    print("\n  Now restart JARVIS:  python3 agent/main.py\n")


if __name__ == "__main__":
    main()
