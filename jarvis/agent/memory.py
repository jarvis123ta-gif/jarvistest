"""memory.py — the only module in this project that writes anything.

One remembered fact, one dated markdown file, in memory/ and nowhere else.
Every write returns the exact text written so the caller can say it out
loud. There is no silent-write path in this file: the write function has
no flag that suppresses the receipt.
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime
from pathlib import Path

from data import MEMORY_DIR


def _safe_slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-") or "fact")


def _confine(path: Path) -> Path:
    """Refuse to write anywhere but memory/. Belt and braces against a
    fact whose text contains something like ../../ in it."""
    root = MEMORY_DIR.resolve()
    full = path.resolve()
    if root != full and root not in full.parents:
        raise PermissionError(f"refusing to write outside memory/: {full}")
    return full


def remember(fact: str, source: str = "spoken", tags: list[str] | None = None) -> dict:
    """Write one fact. Returns the receipt — always. Never silent."""
    fact = (fact or "").strip()
    if not fact:
        return {"ok": False, "error": "nothing to remember"}

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    name = f"{stamp}-{_safe_slug(fact)}.md"
    path = _confine(MEMORY_DIR / name)

    n = 2
    while path.exists():
        path = _confine(MEMORY_DIR / f"{stamp}-{_safe_slug(fact)}-{n}.md")
        n += 1

    body = "\n".join([
        "---",
        f"date: {now.isoformat(timespec='seconds')}",
        f"source: {source}",
        f"tags: [{', '.join(tags or [])}]",
        "---",
        "",
        fact,
        "",
    ])
    path.write_text(body, encoding="utf-8")
    return {
        "ok": True,
        "file": path.name,
        "path": str(path),
        "date": stamp,
        "fact": fact,
        "receipt": f"Written to memory/{path.name}: {fact}",
    }


def recall(limit: int = 40) -> list[dict]:
    """Read back what has been remembered, newest first."""
    if not MEMORY_DIR.exists():
        return []
    out = []
    for p in sorted(MEMORY_DIR.glob("*.md"), reverse=True)[:limit]:
        raw = p.read_text(encoding="utf-8", errors="replace")
        meta = {}
        body = raw
        if raw.startswith("---"):
            _, _, rest = raw.partition("---\n")
            head, _, body = rest.partition("---\n")
            for line in head.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
        out.append({
            "file": p.name,
            "date": meta.get("date", p.name[:10]),
            "source": meta.get("source", "unknown"),
            "fact": body.strip(),
        })
    return out


def search_memory(query: str, limit: int = 5) -> list[dict]:
    q = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    if not q:
        return []
    scored = []
    for m in recall(200):
        low = m["fact"].lower()
        score = sum(low.count(w) for w in q)
        if score:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:limit]]


def stats() -> dict:
    files = list(MEMORY_DIR.glob("*.md")) if MEMORY_DIR.exists() else []
    return {"count": len(files),
            "latest": max((f.name for f in files), default=None),
            "dir": str(MEMORY_DIR)}
