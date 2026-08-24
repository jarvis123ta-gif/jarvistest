#!/usr/bin/env python3
"""guardrails_test.py — proves the absolute rules are enforced in code,
not merely described in the prompt.

    python3 data/guardrails_test.py
"""
import os
import sys
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import memory                       # noqa: E402
import tools                        # noqa: E402
import data as datamod              # noqa: E402
from vault import Vault             # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("guardrails\n")

# 1. memory refuses to escape memory/
try:
    memory._confine(datamod.MEMORY_DIR / ".." / ".." / "escape.md")
    check("memory refuses paths outside memory/", False, "it allowed the write")
except PermissionError:
    check("memory refuses paths outside memory/", True)

# 2. a fact whose text looks like a path still lands in memory/
res = memory.remember("../../../etc/passwd is not a real fact", source="test")
inside = Path(res["path"]).resolve().parent == datamod.MEMORY_DIR.resolve()
check("traversal in the fact text cannot escape", inside, res["file"])
Path(res["path"]).unlink()

# 3. no write is silent
check("every memory write returns a spoken receipt",
      bool(res.get("receipt")) and res["fact"] in res["receipt"])

# 4. the vault opens nothing for writing
src = (ROOT / "agent" / "vault.py").read_text()
check("vault.py never opens a file for writing",
      ", \"w\"" not in src and "'w'" not in src and ".write_text" not in src)

# 5. only data.py reads the demo switch
# (a docstring may mention the variable; what matters is who reads it)
import re as _re
readers = sorted(p.name for p in (ROOT / "agent").glob("*.py")
                 if _re.search(r"environ(?:\.get)?\s*[.(\[]\s*[\"']JARVIS_DEMO",
                               p.read_text()))
check("JARVIS_DEMO is read in exactly one file", readers == ["data.py"], str(readers))

# 6. demo is the default — you opt in to real life
os.environ.pop("JARVIS_DEMO", None)
check("default mode is demo", datamod.demo_mode() is True)

# 7. no send path anywhere in the agent
agent_src = "\n".join(p.read_text() for p in (ROOT / "agent").glob("*.py"))
banned = ["smtplib", "sendmail", "send_message", "messages.send", "/send"]
hits = [b for b in banned if b in agent_src]
check("no mail or send capability is even importable", not hits, str(hits))

# 8. injection inside a file is flagged, not obeyed
v = Vault([str(ROOT / "data" / "vault")], "test").build()
inbox = tools.read_inbox(v)
check("hostile instruction in mail is reported as data",
      bool(inbox["card"]["flags"]) and "not followed" in inbox["spoken"].lower()
      or "flagged" in inbox["spoken"].lower())

# 9. derived numbers carry their qualifier
brief = tools.brief_me(v)
q = brief["card"]["unpaid_total"]["qualifier"]
check("part-paid invoices are qualified, not called discounts",
      "part-paid" in q and "still running" in q or "None of these" in q, q)

# 10. spoken line and card are never the same text
for name, out in (("search_brain", tools.search_brain(v, "margin model")),
                  ("brief_me", brief),
                  ("plan_day", tools.plan_day(v)),
                  ("read_inbox", inbox)):
    check(f"{name}: spoken differs from card",
          out["spoken"] not in json.dumps(out["card"]))

print()
if FAIL:
    print(f"{len(FAIL)} guardrail(s) FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("all guardrails hold")
