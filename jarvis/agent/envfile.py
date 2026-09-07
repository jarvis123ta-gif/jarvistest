"""envfile.py — editing .env without wrecking it.

Someone's .env holds their own comments, their own ordering and values no
tool should touch. Rewriting the whole file from a template loses all of
that, so keys are updated in place and anything new is appended.
"""

from __future__ import annotations

import os
from pathlib import Path

from data import ROOT

ENV = ROOT / ".env"
SAMPLE = ROOT / ".env.example"


def read(path: Path | None = None) -> dict:
    path = path or ENV
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def write(updates: dict, path: Path | None = None) -> list[str]:
    """Apply updates, preserving comments, order and everything else.

    A key that exists — even commented out as `# KEY=...` — is replaced in
    place, so the file does not grow a second copy further down. Returns the
    keys that were changed.
    """
    path = path or ENV
    if not path.exists():
        path.write_text(SAMPLE.read_text(encoding="utf-8") if SAMPLE.exists()
                        else "", encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    changed: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        # An assignment, or a commented-out one waiting to be filled in.
        body = stripped[1:].strip() if stripped.startswith("#") else stripped
        if "=" not in body:
            continue
        key = body.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
            changed.append(key)

    if remaining:
        lines.append("")
        lines.append("# Added by configure.py")
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
            changed.append(k)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return changed
