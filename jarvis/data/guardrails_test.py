#!/usr/bin/env python3
"""guardrails_test.py — proves the absolute rules are enforced in code,
not merely described in the prompt.

    python3 data/guardrails_test.py
"""
import os
import re
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import browser                      # noqa: E402
import connectors                   # noqa: E402
import control                      # noqa: E402
import desktop                      # noqa: E402
import data as datamod              # noqa: E402
import memory                       # noqa: E402
import tools                        # noqa: E402
from vault import Vault             # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


AGENT = ROOT / "agent"
agent_src = "\n".join(p.read_text() for p in AGENT.glob("*.py"))

print("guardrails\n")
print(" writes")

try:
    memory._confine(datamod.MEMORY_DIR / ".." / ".." / "escape.md")
    check("memory refuses paths outside memory/", False, "it allowed the write")
except PermissionError:
    check("memory refuses paths outside memory/", True)

res = memory.remember("../../../etc/passwd is not a real fact", source="test")
check("traversal in the fact text cannot escape",
      Path(res["path"]).resolve().parent == datamod.MEMORY_DIR.resolve(), res["file"])
check("every memory write returns a spoken receipt",
      bool(res.get("receipt")) and res["fact"] in res["receipt"])
Path(res["path"]).unlink()

src = (AGENT / "vault.py").read_text()
check("vault.py never opens a file for writing",
      ', "w"' not in src and "'w'" not in src and ".write_text" not in src)

print("\n connectors stay read-only")

banned = ["smtplib", "sendmail", "messages.send"]
check("no mail-sending library is importable",
      not [b for b in banned if b in agent_src])

conn_src = (AGENT / "connectors.py").read_text()
writes = re.findall(r'method="(POST|PUT|PATCH|DELETE)"', conn_src)
check("connectors make exactly one POST, and it is the OAuth token refresh",
      writes == ["POST"] and "oauth2.googleapis.com/token" in conn_src, str(writes))
check("no connector touches a mutating endpoint",
      not re.search(r"(orders/\{|/fulfillments|messages/send|drive/v3/files/\w+.*upload"
                    r"|events\?.*insert|\.delete\()", conn_src))
check("every connector declares itself read-only",
      all(c["readonly"] for c in connectors.status_all()))

print("\n acting on the machine")

# JARVIS now has hands. These are the rules that stand between the
# principals and a hostile page driving their accounts.

try:
    control.assert_origin(control.CONTENT, "click Submit")
    check("content cannot cause an action", False, "it was allowed")
except control.UntrustedOrigin as e:
    check("content cannot cause an action", "not from Sai or Tanay" in str(e))

for surface, fn in (("browser.click", lambda: browser.click("x", origin=control.CONTENT)),
                    ("browser.navigate", lambda: browser.navigate("http://x", origin=control.CONTENT)),
                    ("browser.fill", lambda: browser.fill("a", "b", origin=control.CONTENT)),
                    ("browser.run_js", lambda: browser.run_js("1", origin=control.CONTENT)),
                    ("desktop.click", lambda: desktop.click(1, 1, origin=control.CONTENT)),
                    ("desktop.type_text", lambda: desktop.type_text("x", origin=control.CONTENT)),
                    ("desktop.press", lambda: desktop.press("ctrl+c", origin=control.CONTENT))):
    try:
        fn()
        check(f"{surface} refuses content origin", False, "it acted")
    except control.UntrustedOrigin:
        check(f"{surface} refuses content origin", True)
    except Exception as e:                                  # noqa: BLE001
        check(f"{surface} refuses content origin", False, f"wrong error: {type(e).__name__}")

# Every action path must pass through the gate. A new surface that forgets
# to call control.guard would be invisible to the kill switch.
for mod in ("browser.py", "desktop.py"):
    src = (AGENT / mod).read_text()
    calls = src.count("control.guard(")
    check(f"{mod} routes actions through control.guard", calls >= 5, f"{calls} gated calls")

control.disarm("guardrail test")
try:
    browser.navigate("http://example.com")
    check("kill switch stops actions", False, "it acted while disarmed")
except control.Halted:
    check("kill switch stops actions", True)
check("disarmed still reports why", "guardrail test" in control.status()["reason"])
control.arm()
check("re-arming works", control.armed())

before = len(control.recent(200))
control.log("test", "a probe action", {"n": 1}, origin=control.PRINCIPAL)
after = control.recent(200)
check("every action lands in the log", len(after) > before and
      after[0]["action"] == "a probe action" and after[0]["origin"] == "principal")
check("the log records origin, not just what happened",
      all("origin" in e for e in after[:5]))

print("\n the demo switch")

readers = sorted(p.name for p in AGENT.glob("*.py")
                 if re.search(r"environ(?:\.get)?\s*[.(\[]\s*[\"']JARVIS_DEMO", p.read_text()))
check("JARVIS_DEMO is read in exactly one file", readers == ["data.py"], str(readers))
os.environ.pop("JARVIS_DEMO", None)
check("default mode is demo", datamod.demo_mode() is True)
check("no real folder path is hardcoded",
      all(not v for v in datamod.REAL_ROOTS.values()), str(datamod.REAL_ROOTS))

print("\n never fabricate")

v = Vault(datamod.active_roots(), "test", datamod.domain_of).build()

# Demo store data is flagged as demo, out loud and on the card.
demo_store = tools.store_status(v)
check("demo store figures are announced as demo",
      "demo" in demo_store["spoken"].lower()
      or "demo" in json.dumps(demo_store["card"]).lower())

# Now switch to live with no credentials: nothing may be invented.
os.environ["JARVIS_DEMO"] = "0"
try:
    st = connectors.get("shopify").status()
    check("unconfigured Shopify reports itself not connected", not st["connected"])

    live = tools.store_status(v)
    check("unconnected store says so and returns no orders",
          "not connected" in live["spoken"].lower() and not live["card"]["orders"],
          live["spoken"])
    check("unconnected store promises not to invent",
          "invent" in live["spoken"].lower() or "invent" in str(live["card"]["note"]).lower())

    mail = connectors.get("gmail").messages()
    check("unconnected Gmail returns no messages",
          not mail.get("ok") and "messages" not in mail)

    plan = tools.plan_day(v)
    blob = json.dumps(plan["card"]).lower()
    check("plan_day invents no store work when nothing is connected",
          "fulfil" not in blob and "restock" not in blob)
finally:
    os.environ["JARVIS_DEMO"] = "1"

print("\n qualifiers and hostile text")

store = tools.store_status(v)
check("unfulfilled orders inside the window are not called a backlog",
      "not a backlog" in store["spoken"].lower()
      or "window" in str(store["card"].get("qualifier", "")).lower(),
      store["spoken"])

inbox = tools.read_inbox(v)
check("hostile instruction in mail is reported as data",
      bool(inbox["card"]["flags"]) and "flagged" in inbox["spoken"].lower())
check("hostile mail is never turned into a task",
      not any("ignore your previous" in i["what"].lower()
              for i in tools.plan_day(v)["card"]["items"]))

print("\n shape of every answer")

for name, out in (("search_brain", tools.search_brain(v, "formula sheet")),
                  ("deadlines", tools.deadlines(v)),
                  ("store_status", store),
                  ("brief_me", tools.brief_me(v)),
                  ("plan_day", tools.plan_day(v)),
                  ("read_inbox", inbox)):
    check(f"{name}: spoken differs from card",
          out["spoken"] not in json.dumps(out["card"]))
    check(f"{name}: speaks to Sir", "sir" in out["spoken"].lower())

print()
if FAIL:
    print(f"{len(FAIL)} guardrail(s) FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("all guardrails hold")
