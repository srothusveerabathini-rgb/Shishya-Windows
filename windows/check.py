#!/usr/bin/env python3
"""
Shishya for Windows — every advertised feature, exercised for real.

    runtime\\python.exe windows\\check.py            with Shishya open
    runtime\\python.exe windows\\check.py --quick    skip the slow model checks

The Windows counterpart of the Mac's `./demo.sh check`. Each line asks the
thing itself — the panel's own API, or a probe run inside the sandbox — and
prints PASS or FAIL with what it saw, never what it was told. It takes a
snapshot and rolls back to it, which restarts the sandbox: open clone apps
close. Nothing of yours is touched: the billing, documents and report checks
run on throwaway copies in the clone's own Temp folder.
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "host"))
import winbox   # noqa: E402
import winvm    # noqa: E402

# A job of its own: Shishya's named job belongs to the running panel, and
# joining it from here is refused (AssignProcessToJobObject: access denied).
winbox.JOB_NAME = "Local\\Shishya.Check.Job"
PANEL = "http://127.0.0.1:8200/api/"
RESULTS = []


def api(route, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PANEL + route, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {str(detail)[:110]}", flush=True)
    if not ok and len(str(detail)) > 110:
        print("      " + str(detail).replace("\n", "\n      "), flush=True)   # the whole reason


def check(name):
    def wrap(fn):
        def run(*a):
            try:
                ok, detail = fn(*a)
            except Exception as e:                       # noqa: BLE001 — a failure is a result
                ok, detail = False, f"{type(e).__name__}: {e}"
            record(name, ok, detail)
        run.__name__ = fn.__name__
        return run
    return wrap


def in_sandbox(code, timeout=900):
    """Run a Python snippet inside the container, as the clone, with Shishya's
    shared code importable. Returns its printed JSON."""
    probe = winvm.clone_home() / "Temp" / "shishya-check.py"
    probe.write_text(textwrap.dedent(code), encoding="utf-8")
    rt = winvm._need_runtime()
    r = winbox.run([str(rt / "python.exe"), "-I", "-X", "utf8", str(probe),
                    str(APP / "vm" / "shell"), str(APP / "vm")],
                   caps=(), env=winvm.clone_env(), cwd=str(winvm.clone_home()), timeout=timeout)
    lines = [l for l in r["out"].splitlines() if l.startswith("{")]
    if not lines:
        raise RuntimeError(r["out"].strip()[-1500:] or f"exit {r['code']}")
    return json.loads(lines[-1])


def wait_job(job, limit=600):
    t = time.time()
    while time.time() - t < limit:
        s = api("job/progress", {"job": job})
        if s.get("state") in ("done", "failed"):
            return s
        time.sleep(2)
    return {"state": "timed out"}


# ─── the sandbox ────────────────────────────────────────────────────────────

@check("sandbox running")
def c_running():
    s = api("status")["vm"]
    return s.get("state") == "running", f"{s.get('sandbox')} · {s.get('memory')}"


@check("model pipe served by the broker")
def c_broker():
    b = api("sandbox")["broker"]
    return Path(b["pipe"]).name == "shishya-model" or "shishya-model" in b["pipe"], b


@check("network locked: clone cannot connect")
def c_egress():
    out = in_sandbox("""
        import json, socket
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=4).close(); reached = True
        except OSError as e:
            reached = False; why = type(e).__name__
        print(json.dumps({"reached": reached}))
    """)
    return not out["reached"], "1.1.1.1:443 refused from inside"


@check("permission ledger records actions")
def c_ledger():
    before = len(api("ledger", {"limit": 2000}).get("entries", []))
    api("launch", {"app": "activity_app.py"})
    time.sleep(2)
    after = len(api("ledger", {"limit": 2000}).get("entries", []))
    return after > before, f"{before} -> {after} entries"


@check("apps open on the clone's screen")
def c_apps():
    api("launch", {"app": "files_app.py"})
    time.sleep(6)
    import hands
    titles = [t for _, t in hands.app_windows()]
    return any("Files" in t or "files" in t.lower() for t in titles), titles[:6]


@check("live screen in Shishya's window")
def c_live():
    out = api("screen/live", {"visible": True, "dpr": 1, "rect": [0, 0, 400, 250]})
    api("screen/live", {"visible": False})
    return bool(out.get("live")), out


@check("snapshot and rollback")
def c_snapshot():
    api("vm/snapshot", {"tag": "check"}, timeout=900)
    marker = winvm.clone_home() / "rollback-marker.txt"
    marker.write_text("made after the snapshot", encoding="utf-8")
    api("vm/rollback", {"tag": "check"}, timeout=900)
    gone = not marker.exists()
    return gone and api("status")["vm"].get("state") == "running", "marker gone after rollback" if gone else "marker survived"


# ─── the shop ───────────────────────────────────────────────────────────────

SHOP = """
    import json, os, sys, shutil
    from pathlib import Path
    # A plain folder in the clone's real Temp: tempfile.mkdtemp's owner-only
    # ACL locks a sandboxed process out of the folder it just made.
    import time as _t
    tmp = Path(os.environ["SHISHYA_TEMP"]) / f"check-shared-{_t.time_ns()}"
    tmp.mkdir(parents=True)
    (tmp / "billing").mkdir(); (tmp / "work").mkdir(); (tmp / "training").mkdir()
    os.environ["SHISHYA_SHARED"] = str(tmp)
    sys.path[:0] = sys.argv[1:3]
"""


@check("GST billing, void and returns")
def c_billing():
    out = in_sandbox(SHOP + """
    import books
    item = {"id": "P1", "name": "Paracetamol", "category": "Pharma", "cost": "6", "price": "10",
            "stock": "100", "gst": "12", "reorder": "10"}
    books.write_catalogue([dict(item)])
    rows = [books.line("B1", "sale", item, 2), books.line("B1", "sale", item, 3)]
    books.record(rows)
    tax = sum(float(r["tax"]) for r in rows)
    books.take_back("B1", "P1", 1)
    books.void("B1")
    net = sum(float(r["amount"]) for r in books.bill_rows("B1"))
    print(json.dumps({"tax": round(tax, 2), "net": round(net, 2), "voided": books.is_voided("B1")}))
    """)
    ok = abs(out["net"]) < 0.005 and out["voided"] and out["tax"] > 0
    return ok, out


@check("approvals: ask and decide")
def c_approvals():
    out = in_sandbox(SHOP + """
    import approvals
    rid = approvals.ask("void", "Void B1, Rs 50")
    rid = rid.get("id") if isinstance(rid, dict) else rid
    print(json.dumps({"id": rid, "open": [a.get("id") for a in approvals.pending()]}))
    """)
    return bool(out["id"]) and out["id"] in out["open"], out


@check("stock audit job")
def c_audit():
    s = wait_job(api("audit", {})["job"])
    return s.get("state") == "done", f"{s.get('state')}: {s.get('step')} {s.get('files', '')}"


@check("alert check job")
def c_alerts():
    s = wait_job(api("alerts/check", {})["job"])
    return s.get("state") == "done", f"{s.get('state')}: {s.get('step')}"


@check("books backup job")
def c_backup():
    s = wait_job(api("backup", {})["job"])
    return s.get("state") == "done", f"{s.get('state')}: {s.get('step')}"


@check("scheduled runs read and write")
def c_schedules():
    items = api("schedules").get("schedules", [])
    back = api("schedule", {"schedules": items}).get("schedules")
    return back == items, f"{len(items)} schedule(s) round-tripped"


@check("report from a spreadsheet")
def c_report():
    out = in_sandbox(SHOP + """
    import pandas as pd, reporter
    f = tmp / "work" / "sales.xlsx"
    pd.DataFrame({"Month": ["Jul", "Aug", "Sep"], "Sales": [1200, 1500, 1350],
                  "Profit": [300, 420, 360]}).to_excel(f, index=False)
    charts, findings = [], []
    for name, frame in reporter.tables_from(f):
        reporter.chart_table(name, frame, reporter.by_heuristic(frame), tmp / "work", charts, findings)
    print(json.dumps({"charts": [str(c) for c in charts], "findings": findings[:3]}))
    """)
    return bool(out["charts"]), f"{len(out['charts'])} chart(s); {out['findings'][:1]}"


@check("Tally XML from a purchase register")
def c_tally():
    out = in_sandbox(SHOP + """
    import pandas as pd, tally
    job = tmp / "job"; (job / "in").mkdir(parents=True)
    pd.DataFrame({"Date": ["2026-08-01", "2026-08-03"], "Supplier": ["Sri Ram Pharma", "Sri Ram Pharma"],
                  "Invoice": ["SR-1", "SR-2"], "Product": ["Paracetamol", "ORS"], "Qty": [10, 5],
                  "Rate": [6.0, 12.0], "Amount": [60.0, 60.0]}).to_excel(job / "in" / "purchases.xlsx", index=False)
    tally.main(str(job))
    xml = list((job / "out").glob("*.xml"))
    res = tally.check(xml[0]) if xml else None
    print(json.dumps({"xml": [x.name for x in xml], "check": res}))
    """)
    ok = bool(out["xml"]) and out["check"] and not out["check"][2]
    return ok, out


# ─── the models ─────────────────────────────────────────────────────────────

@check("each job's model is installed")
def c_models():
    roles = json.loads((winvm.paths.SHARED / ".models.json").read_text(encoding="utf-8"))
    tags = {m["name"] for m in api_ollama("/api/tags")["models"]}
    tags |= {t.split(":")[0] + ":latest" for t in tags if ":" not in t}
    missing = [f"{j}={m}" for j, m in roles.items() if m not in tags and f"{m}:latest" not in tags]
    return not missing, missing or roles


def api_ollama(path):
    with urllib.request.urlopen("http://127.0.0.1:11434" + path, timeout=20) as r:
        return json.loads(r.read())


@check("document Q&A over dropped files")
def c_answers():
    out = in_sandbox(SHOP + """
    import common, library
    (tmp / "training" / "policy.md").write_text("Students need 75% attendance to sit the annual exam.", encoding="utf-8")
    lib = library.Library(); lib.add(tmp / "training" / "policy.md")
    found = lib.search("attendance needed for the exam", k=2)
    passages = "\\n\\n".join(f"[from {s}]\\n{t}" for s, t in found)
    a = common.ask_job("answers", f"PASSAGES:\\n{passages}\\n\\nQUESTION: What attendance is needed to sit the exam?", system=library.SYSTEM)
    print(json.dumps({"answer": a[:200]}))
    """)
    return "75" in out["answer"], out["answer"]


@check("document generation (.xlsx/.docx/.pptx)")
def c_documents():
    out = in_sandbox(SHOP + """
    import common, documents
    made = []
    for kind in ("Spreadsheet", "Document", "Presentation"):
        spec = json.loads(common.ask_job("documents", documents.ASKS[kind] + "\\n\\nWHAT IS NEEDED:\\na short exam timetable for 10 to 12 March\\n\\nGive it a short filename, no extension.",
                                         schema=documents.SCHEMAS[kind], budget=3000))
        made.append(documents.WRITERS[kind](spec).name)
    print(json.dumps({"made": made}))
    """)
    exts = {Path(m).suffix for m in out["made"]}
    return exts == {".xlsx", ".docx", ".pptx"}, out["made"]


@check("Code app: write and run a script")
def c_code():
    out = in_sandbox(SHOP + """
    import common, codegen
    code = codegen.repair(common.ask_job("code", "Write a Python script that: print the sum of 17 and 25",
                                          system=codegen.CODE_SYSTEM, budget=2048))
    o, e = codegen.run_script(code)
    print(json.dumps({"out": o, "err": e[-200:]}))
    """)
    return "42" in out["out"], out["out"] or out["err"]


# ─── lessons ────────────────────────────────────────────────────────────────

@check("lesson: read back as plain steps")
def c_lesson_plan():
    lesson = "check-lesson"
    folder = winvm.paths.SHARED / "traces" / lesson
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "procedure.json").write_text(json.dumps({
        "title": "Check: put 42 in B2",
        "steps": [{"text": "Open the Sheets application"},
                  {"text": "Go to cell B2"},
                  {"text": "Type '42' into cell B2"}]}), encoding="utf-8")
    plan = api("lesson/plan", {"lesson": lesson})
    return plan.get("doable") == plan.get("total") == 3, f"{plan.get('doable')} of {plan.get('total')} steps performable"


@check("lesson: performed and read back")
def c_lesson_run():
    out = api("lesson/run", {"lesson": "check-lesson"})
    if out.get("error"):
        return False, out["error"]
    t = time.time()
    while time.time() - t < 240:
        p = api("lesson/progress")
        if p.get("state") not in ("running",):
            break
        time.sleep(2)
    steps = [e for e in p.get("events", []) if e.get("kind") == "step"]
    ok = len(steps) == 3 and all(e.get("ok") for e in steps)
    return ok, " | ".join(f"{e.get('n')}:{'ok' if e.get('ok') else 'X'} {e.get('message', '')[:40]}" for e in steps) or p


@check("the terminal: shishya board")
def c_terminal():
    import subprocess
    r = subprocess.run(["cmd", "/c", str(APP / "shishya.cmd"), "board"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    return r.returncode == 0 and "Takings" in r.stdout, r.stdout.strip().splitlines()[:1]


@check("forecaster trains on a shop's history")
def c_forecast():
    import datetime as dt, math, random, subprocess, tempfile
    import openpyxl
    home = Path(tempfile.mkdtemp(prefix="forecast-check-"))
    (home / "shared" / "training").mkdir(parents=True)
    rng, months = random.Random(3), [dt.datetime(2025 + m // 12, m % 12 + 1, 1) for m in range(4, 21)]
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Code", "Name", "Category", "Unit"] + months)
    for i in range(200):
        base = rng.uniform(5, 120)
        ws.append([f"P{i}", f"Product {i}", "Pharma", "box"] +
                  [max(0, round(base * (1 + .4 * math.sin(t / 2)) + rng.gauss(0, base * .1))) for t in range(17)])
    wb.save(home / "shared" / "training" / "history.xlsx")
    r = subprocess.run([sys.executable, "-X", "utf8", str(APP / "vm" / "forecast.py"), "train"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                       env={**os.environ, "USERPROFILE": str(home), "HOME": str(home)})
    made = (home / "shared" / "models" / "forecast.json").exists()
    line = next((l.strip() for l in r.stdout.splitlines() if "this model" in l), r.stderr[-120:])
    return r.returncode == 0 and made, line


def main():
    # Answers can hold any character (the answers model writes "75 %" with a
    # narrow no-break space); a console or log in cp1252 must not end the run.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the model and snapshot checks")
    ap.add_argument("--only", nargs="+", metavar="CHECK", help="run just these, e.g. --only answers documents")
    args = ap.parse_args()
    fast = [c_running, c_broker, c_egress, c_ledger, c_apps, c_live, c_billing, c_approvals,
            c_schedules, c_audit, c_alerts, c_backup, c_report, c_tally, c_lesson_plan, c_models,
            c_terminal, c_forecast]
    slow = [c_lesson_run, c_answers, c_documents, c_code, c_snapshot]
    chosen = fast + ([] if args.quick else slow)
    if args.only:
        chosen = [c for c in fast + slow if c.__name__.removeprefix("c_") in args.only]
    for c in chosen:
        c()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
