#!/usr/bin/env python3
"""
Shishya control panel.

    python3 serve.py            # http://localhost:8200

Serves web/ with caching off (same reason as starforge: module edits should
show up on reload) and exposes a small JSON API over host/vm.py + the ledger.

Deliberately stdlib-only. This runs on a laptop at a demo table with the
network locked down; a dependency that needs to resolve at start-up is a
dependency that can end the demo.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "host"))

import paths             # noqa: E402
import experiments       # noqa: E402
import history           # noqa: E402
import dashboard         # noqa: E402
import home              # noqa: E402
import hosts             # noqa: E402
import ledger            # noqa: E402
import profile           # noqa: E402
import vault             # noqa: E402  — named so it never shadows the stdlib's secrets
import tasks             # noqa: E402
import vm                # noqa: E402

WEB = os.path.join(ROOT, "web")
INBOX = str(paths.SHARED / "inbox")
WINDOWS = sys.platform == "win32"

# Files come in over a loopback socket from a page anyone on this machine can
# open, and land in a directory the sandbox reads. Cap the size so a stray
# drop cannot fill the disk the VM is running on.
MAX_UPLOAD = 64 * 1024 * 1024

# An explicit list, not "whatever file name you send". This endpoint runs a
# program inside the sandbox; it takes a choice from a menu, never a path.
LAUNCHABLE = {
    "chat_app.py": "Shishya",
    "findings_app.py": "Findings",
    "reports_app.py": "Reports",
    "billing_app.py": "Billing",
    "training_app.py": "Training Data",
    "files_app.py": "Files",
    "activity_app.py": "Activity",
    "make_app.py": "Make a Document",
    "vscode_app.py": "VS Code",
    "inbox_app.py": "Inbox",
    "code_app.py": "Code",
    "models_app.py": "Models",
    "system_app.py": "System",
    "browser_app.py": "Browser",
    "sheet_app.py": "Sheets",
    "shishya_app.py": "Teach a lesson",
    "bank_app.py": "Banking",
    "credit_app.py": "Credit assistant",
    "settings_app.py": "Settings",
}


def safe_filename(raw):
    """The name arrives in a header the browser filled in from a dropped file.

    Treat it as hostile: strip any directory part, refuse dotfiles and
    traversal, and keep it to characters that cannot mean anything to a
    shell. A dropped file must never be able to choose where it lands.
    """
    # The browser sends the name percent-encoded, so a file called
    # "01 Retail Inventory.xlsx" arrives as "01%20Retail%20Inventory.xlsx" and
    # the sanitiser below turned the %20 into _20. Decode first, then treat
    # the result as hostile as before.
    raw = urllib.parse.unquote(raw or "")
    name = os.path.basename(raw).strip()
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", name)
    name = name.lstrip(".") or "dropped-file"
    return name[:120]


# ─── running a learned lesson ───────────────────────────────────────────────
# The panel starts a run and then polls it, the same shape as ask/answer: a
# procedure takes minutes, and holding the request open for it would stall the
# panel's own status polling behind it.
#
# One at a time, deliberately. Two procedures typing into the same spreadsheet
# at once is not a thing anyone should have to debug.

_lesson = {"state": "idle", "lesson": None, "title": "", "events": [], "started": None}
_lesson_lock = threading.Lock()

# Which jobs have had their finish written to the ledger. In memory: a panel
# restart may re-record one finish, which is honest — better than a database
# for what is only deduplication of a log line.
_job_seen = {}
_job_lock = threading.Lock()


def record_step(event):
    """Put every step into the ledger — and every refusal as a refusal.

    Refusing to guess is the whole design of the executor, so it gets the
    ledger's strongest treatment rather than a note attached to a run. A step
    that was attempted and could not be confirmed is also a deny: on this
    panel, "I could not verify that" must never look like "done".
    """
    kind = event.get("kind")
    if kind == "start":
        return None
    if kind == "ask":
        # A question in the audit trail, before anyone answers it. If the
        # run times out and moves on, the ledger still shows it asked.
        return ledger.record(ledger.INFO, "lesson.ask", f"step {event.get('n')}",
                             detail=event.get("text"))
    if kind == "taught":
        return ledger.record(ledger.ALLOW, "lesson.taught", f"step {event.get('n')}",
                             detail=event.get("text"))
    if kind == "skipped":
        # A skip is a refusal to guess, so it is a deny like any other. Kept
        # under its own action name because "I did not attempt this" and "I
        # stopped the work" are different events and the log has to say which.
        return ledger.record(ledger.DENY, "lesson.skip", f"step {event.get('n')}",
                             detail=event.get("text"))
    if kind == "halted":
        return ledger.record(ledger.DENY, "lesson.halt", f"step {event.get('n')}",
                             detail=event.get("message"))
    if kind == "error":
        return ledger.record(ledger.DENY, "lesson.error",
                             (event.get("message") or "")[:70])
    if kind == "done":
        return ledger.record(ledger.INFO, "lesson.done",
                             f"{event.get('performed')} performed, "
                             f"{event.get('skipped')} skipped")
    return ledger.record(ledger.ALLOW if event.get("ok") else ledger.DENY,
                         "lesson.step",
                         f"step {event.get('n')}: {(event.get('message') or '')[:60]}",
                         detail=event.get("text"))


def start_lesson(lesson_id):
    """Begin a run on a background thread and return at once."""
    plan = vm.lesson_plan(lesson_id)      # raises unless this really is a lesson
    if not plan.get("doable"):
        # Nothing in it can be performed, so a run would only write a column
        # of refusals into the ledger and call itself finished.
        return {"error": f"there is nothing in {plan.get('title')!r} I can perform "
                         f"— all {plan.get('total')} steps are ones I do not understand"}
    with _lesson_lock:
        if _lesson["state"] == "running":
            return {"error": f"{_lesson['title']!r} is already running"}
        _lesson.update(state="running", lesson=lesson_id,
                       title=plan.get("title", lesson_id), events=[],
                       started=time.time())

    # Logged before it runs, so a run that hangs still leaves a record of what
    # was asked for. Same rule as the terminal.
    ledger.record(ledger.INFO, "lesson.run", plan.get("title", lesson_id), actor="guru",
                  detail=f"{plan.get('doable')} of {plan.get('total')} steps performable")

    def step(event):
        with _lesson_lock:
            _lesson["events"].append(event)
        record_step(event)

    def go():
        try:
            vm.lesson_run(lesson_id, step)
        except Exception as e:
            step({"kind": "error", "ok": False, "message": f"{type(e).__name__}: {e}"})
        finally:
            with _lesson_lock:
                _lesson["state"] = "done"

    threading.Thread(target=go, daemon=True).start()
    return {"started": lesson_id, "plan": plan}


# ─── runs that happen without anybody pressing anything ─────────────────────
# One thread, one minute apart. Deliberately dumb: it re-reads the schedule
# file every tick rather than holding it, so editing a schedule in the Reports
# app takes effect within the minute and nothing has to be told to reload.
#
# This is what makes the difference between a tool somebody operates and a
# colleague who did the work before anybody arrived.

SCHEDULE_TICK = 45


def scheduler():
    while True:
        time.sleep(SCHEDULE_TICK)
        try:
            if vm.status().get("state") != "running":
                continue          # nothing to run it in; try again next tick
            items = vm.read_schedules()
            fired = False
            for item in items:
                if not vm.due(item):
                    continue
                runner = item.get("runner", "audit.py")
                if runner not in ("audit.py", "reporter.py", "tally.py",
                                  "backup.py", "alerts.py"):
                    continue      # a schedule names a runner from a menu, never a path
                label = item.get("label") or runner
                ledger.record(ledger.INFO, "schedule.fire", label, actor="system",
                              detail=f"due at {item.get('at')} — nobody pressed anything")
                # take_inbox False: the shop's books are the input, and a
                # scheduled run must not empty an inbox somebody is staging.
                vm.start_job(label, runner=runner, take_inbox=False)
                item["last"] = time.strftime("%Y-%m-%d")
                fired = True
            if fired:
                vm.write_schedules(items)
        except Exception as e:
            # A scheduler that dies takes every future run with it, silently.
            ledger.record(ledger.DENY, "schedule.error", type(e).__name__,
                          actor="system", detail=str(e)[:200])


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB, **kwargs)

    # ─── API ────────────────────────────────────────────────────────────────

    def do_GET(self):
        # Produced files ride a query string, which the JSON router strips.
        if self.path.split("?")[0] == "/api/job/file":
            return self.job_file()
        if self.path.split("?")[0] == "/api/take":
            return self.take()
        if self.path.split("?")[0] == "/api/screen":
            return self.screen()
        if self.path.startswith("/api/"):
            return self._api("GET")
        # The home screen is the front door; the full control panel — every
        # control it ever had — is one click away at /panel.
        bare = self.path.split("?")[0]
        if bare == "/":
            self.path = "/home.html" + self.path[1:]
        elif bare in ("/panel", "/panel/"):
            self.path = "/index.html" + self.path[len(bare):]
        return super().do_GET()

    def job_file(self):
        """Hand one produced file to the browser. vm.job_file does the
        arguing about paths; this only moves bytes."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            path = vm.job_file((q.get("job") or [""])[0], (q.get("name") or [""])[0])
        except vm.VMError as e:
            return self._json({"error": str(e)}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # Uploads are raw bytes, not JSON, so they bypass the usual API path.
        if self.path.split("?")[0] == "/api/upload":
            return self.upload()
        if self.path.startswith("/api/"):
            return self._api("POST")
        self.send_error(404)

    def upload(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > MAX_UPLOAD:
            return self._json({"error": f"file must be 1 byte to {MAX_UPLOAD // 1048576}MB"}, 413)

        name = safe_filename(self.headers.get("X-Filename"))
        os.makedirs(INBOX, exist_ok=True)
        target = os.path.join(INBOX, name)
        if os.path.exists(target):
            stem, ext = os.path.splitext(name)
            name = f"{stem}-{time.strftime('%H%M%S')}{ext}"
            target = os.path.join(INBOX, name)

        # Write to a temp name first and rename into place: the sandbox
        # watches this directory, and a half-written file appearing there
        # would be picked up as complete.
        tmp = target + ".part"
        remaining = size
        with open(tmp, "wb") as out:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            os.unlink(tmp)
            return self._json({"error": "upload cut short"}, 400)
        os.rename(tmp, target)

        ledger.record(ledger.ALLOW, "file.in", name, actor="guru",
                      detail=f"{size} bytes dropped from the host")
        return self._json({"name": name, "bytes": size})

    def take(self):
        """Hand one of the clone's files back to the browser.

        Logged as file.out, which is the counterpart to the file.in written
        when something is dropped on the panel. Both directions across the
        boundary belong in the ledger, and until now only one of them was.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        where = (q.get("where") or ["work"])[0]
        rel = (q.get("path") or [""])[0]
        try:
            path = vm.take_file(where, rel)
        except vm.VMError as e:
            return self._json({"error": str(e)}, 404)
        data = path.read_bytes()
        ledger.record(ledger.ALLOW, "file.out", path.name, actor="guru",
                      detail=f"{len(data)} bytes taken from the clone's {where}")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def screen(self):
        """The clone's screen as one JPEG (Windows). The page asks again for
        the next frame when this one has painted, so a slow link gets fewer
        frames rather than a backlog of stale ones."""
        if not WINDOWS:
            return self._json({"error": "the screen is noVNC on this platform"}, 404)
        import screen
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        width = int((q.get("w") or ["0"])[0] or 0) or None
        try:
            data = screen.capture(quality=88, max_width=width)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if not data:
            return self._json({"error": "the clone's screen is not up"}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api(self, method):
        route = self.path.split("?")[0][len("/api/"):].strip("/")
        try:
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                body = json.loads(self.rfile.read(n) or b"{}")
            self._json(self.route(method, route, body))
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e), "type": type(e).__name__}, code=500)

    def route(self, method, route, body):
        # ── read ──
        if route == "status":
            state = vm.status()
            live = state.get("state") == "running"
            return {
                "platform": "windows" if WINDOWS else sys.platform,
                "vm": state,
                "ledger": ledger.summary(),
                "snapshots": vm.snapshots() if state["state"] != "absent" else [],
                "snapshot_taken": vm._snapshot_age(),
                # Measured, never assumed. Unknown when the VM is down, because
                # "locked" would be a claim we cannot support.
                "browsing": vm.browsing_state() if live else {"browsing": None},
                "egress": vm.egress_state() if live else {"locked": None,
                                                          "detail": "sandbox is not running"},
            }
        if route == "launch":
            # Launch an app inside the sandbox from out here.
            #
            # Clicking inside the sandbox goes through noVNC, and that path
            # has failed twice under demo conditions — stale websockify
            # children swallow button releases and every control goes dead
            # while still looking fine. This route does not touch the display
            # at all, so it keeps working when that breaks. It is also the
            # better story: you drive from your side, the clone works on its.
            name = str(body.get("app", ""))
            if name not in LAUNCHABLE:
                return {"error": f"unknown app {name!r}"}
            ledger.record(ledger.INFO, "app.launch", name, actor="guru")
            return vm.launch_app(name)

        if route == "inbox":
            try:
                waiting = sorted(f for f in os.listdir(INBOX)
                                 if not f.startswith(".") and not f.endswith(".part"))
            except OSError:
                waiting = []
            return {"waiting": waiting}
        if route == "job/progress":
            state = vm.job_state(body.get("job"))
            # The worker cannot reach the host ledger, so the finish is
            # recorded here, the first time it is observed — once, not per
            # poll, and deny for a failure so it counts as one.
            if state.get("state") == "done" and state.get("job") \
                    and _job_seen.get(state.get("job")) != "done":
                # A finished job usually wrote something. Recording a version
                # here means the history follows the work rather than
                # depending on anybody pressing Save.
                try:
                    history.save(f"after {state.get('brief') or 'a job'}",
                                 by="system")
                except Exception:
                    pass
            if state.get("state") in ("done", "failed"):
                with _job_lock:
                    if _job_seen.get(state.get("job")) != state["state"]:
                        _job_seen[state.get("job")] = state["state"]
                        if state["state"] == "done":
                            ledger.record(ledger.ALLOW, "job.done",
                                          f"{len(state.get('files') or [])} file(s)",
                                          detail=state.get("step"))
                        else:
                            ledger.record(ledger.DENY, "job.fail",
                                          (state.get("step") or "failed")[:70],
                                          detail=state.get("error"))
            return state

        if route == "home":
            # The home screen's whole state in one call, from the real books
            # and the real sandbox (host/home.py). Polled, so cheap.
            with _lesson_lock:
                lesson = {"state": _lesson["state"], "title": _lesson["title"]}
            return home.everything(lesson)

        if route == "dashboard":
            # One call for the whole overview screen. The panel already makes
            # ten requests a tick; a dashboard that fetched per card would
            # have made it eighteen.
            return dashboard.everything()

        # The Banking and CSMA screens are apps in their own right, not views
        # of the dashboard. Reading them off /dashboard meant they went blank
        # for any profession whose card list happened not to include them —
        # a retail owner opening the credit assistant and being told there was
        # nothing recorded, when there was.
        if route == "money":
            return {"accounts": dashboard.card_commitments(),
                    "addons": dashboard.card_addons(),
                    "credit": dashboard.card_credit()}

        if route == "hosts":
            return {"hosts": hosts.read(), **vm.ssh_state()}

        if route == "profile":
            return {**profile.read(),
                    "professions": sorted(profile.PROFESSIONS),
                    "cards": profile.CARDS}

        if route == "schedules":
            return {"schedules": vm.read_schedules(), "role": vm.role()}
        if route == "alerts":
            return vm.alerts()
        if route == "tasks":
            return {"tasks": tasks.listing(), "counts": tasks.counts()}
        if route == "experiments":
            rows = experiments.runs(limit=20)
            for r in rows:
                r["stale"] = experiments.changed_since(r)
            return {"runs": rows, "datasets": experiments.datasets()}

        if route == "history":
            return {"status": history.status(), "log": history.log(20)}

        if route == "secrets":
            # Names and lengths only. There is deliberately no route that
            # returns a value: the panel is a web page on a machine somebody
            # else might be looking at, and a secret you can read off a
            # screen is not one.
            ok, detail = vault.permissions_ok()
            return {"secrets": vault.names(), "store": str(vault.STORE),
                    "private": ok, "detail": detail,
                    "leaked": vault.leaked_into(str(paths.SHARED))}

        if route == "approvals":
            return {"pending": vm.pending_approvals(), "role": vm.role()}
        if route == "take/list":
            return vm.take_list(str(body.get("where", "work")),
                                str(body.get("at", "")))
        if route == "lessons":
            return {"lessons": vm.lessons()}
        if route == "lesson/progress":
            with _lesson_lock:
                return dict(_lesson, events=list(_lesson["events"]))
        if route == "ledger":
            return {"entries": ledger.read(limit=int(body.get("limit", 300)))}

        if route == "models/state":
            import models_dl
            return models_dl.state()

        if route == "sandbox" and WINDOWS:
            # The boundary as it actually is: the grants on disk, what is
            # running and with which capabilities, what crossed the pipe.
            import winbox
            import winhost
            import winvm
            return {"provisioned": winvm.provisioned(), "processes": winbox.processes(),
                    "broker": winhost.broker_state(), "mode": winvm._mode()}

        if method != "POST":
            self.send_error(405)
            return None

        if route == "models/get":
            # Shishya's trained models from the latest release, into Ollama
            # (host/models_dl.py). The host downloads; the sandbox never does.
            import models_dl
            return models_dl.get_in_background(body.get("only"))

        if route == "limits" and WINDOWS:
            # The dock's dials: the sandbox's share of this machine, changed
            # live on its job object, and written down like every other change.
            import winvm
            cpu, ram = body.get("cpu"), body.get("ram")
            out = winvm.set_limits(cpu, ram)
            ledger.record(ledger.ALLOW, "sandbox.limits",
                          f"CPU {out['cpu']}% · RAM {out['ram']} GB", actor="guru",
                          detail="the most of this machine the sandbox may use")
            return out

        if route.startswith("screen/") and WINDOWS:
            import screen
            # Taking the wheel through the picture: each event is addressed
            # to the clone's window under the point, never to whatever has
            # focus on this machine (host/screen.py).
            if route == "screen/click":
                return screen.click(float(body.get("x", 0)), float(body.get("y", 0)))
            if route == "screen/key":
                return screen.key(text=body.get("text"), name=body.get("name"))
            if route == "screen/mouse":
                return screen.mouse(str(body.get("kind", "")), float(body.get("x", 0)),
                                    float(body.get("y", 0)), str(body.get("button", "left")),
                                    int(body.get("delta", 0)))
            if route == "screen/show":
                return screen.show()
            if route == "screen/dock":
                return screen.dock()
            if route == "screen/live":
                # The screen drawn live into this window by Windows' own
                # compositor, where the page says it goes (host/live.py).
                import live
                r = body.get("rect")
                return live.set_view(rect=r and [float(v) for v in r][:4],
                                     dpr=float(body.get("dpr", 1)),
                                     visible=bool(body.get("visible")))

        # ── write ── every one of these is logged, including from the CLI
        if route == "vm/create":
            ledger.record(ledger.INFO, "vm.create", vm.NAME, actor="guru")
            return vm.create()
        if route == "vm/start":
            ledger.record(ledger.INFO, "vm.start", vm.NAME, actor="guru")
            return vm.start()
        if route == "vm/stop":
            ledger.record(ledger.INFO, "vm.stop", vm.NAME, actor="guru")
            return vm.stop()
        if route == "vm/snapshot":
            tag = body.get("tag", vm.CLEAN_TAG)
            ledger.record(ledger.INFO, "vm.snapshot", tag, actor="guru")
            return vm.snapshot(tag)
        if route == "vm/rollback":
            tag = body.get("tag", vm.CLEAN_TAG)
            ledger.record(ledger.INFO, "vm.rollback", tag, actor="guru",
                          detail="everything the clone did since this tag is gone")
            return vm.rollback(tag)
        if route == "sources":
            out = vm.use_as_sources()
            for name in out["added"]:
                ledger.record(ledger.ALLOW, "source.add", name, actor="guru",
                              detail="moved from inbox into the clone's material")
            return out

        if route == "shell":
            command = str(body.get("cmd", "")).strip()
            if not command:
                return {"error": "nothing to run"}
            # Logged before it runs, so a command that hangs or breaks
            # something still leaves a record of what was asked for.
            ledger.record(ledger.INFO, "shell", command[:120], actor="guru",
                          detail="run in the sandbox as clone")
            try:
                return vm.run_command(command)
            except subprocess.TimeoutExpired:
                return {"code": -1, "out": "", "err": "timed out"}

        if route == "close":
            name = body.get("app")
            if name and name not in LAUNCHABLE:
                return {"error": f"unknown app {name!r}"}
            ledger.record(ledger.INFO, "app.close", name or "all", actor="guru")
            return vm.close_apps(name)

        if route == "ask":
            text = str(body.get("text", "")).strip()
            if not text:
                return {"error": "nothing to ask"}
            ledger.record(ledger.INFO, "ask", text[:70], actor="guru")
            return vm.drive("ask", text)

        if route == "answer":
            return vm.drive("read")

        if route == "job":
            brief = str(body.get("brief", "")).strip()
            if not brief:
                return {"error": "say what should be done with the files"}
            # Logged before it runs — same rule as the terminal and lessons:
            # a job that hangs must still leave a record of what was asked.
            out = vm.start_job(brief)
            ledger.record(ledger.ALLOW, "job.start", brief[:70], actor="guru",
                          detail=f"{len(out['files'])} file(s) handed over: "
                                 f"{', '.join(out['files']) or 'none'}")
            return out

        if route == "report":
            title = str(body.get("brief", "")).strip()
            out = vm.start_job(title, runner="reporter.py")
            ledger.record(ledger.ALLOW, "report.start", title[:70] or "report",
                          actor="guru",
                          detail=f"{len(out['files'])} file(s) handed over: "
                                 f"{', '.join(out['files']) or 'none'}")
            return out

        if route == "schedule":
            # Whole list, replaced. Four fields and one writer; merging
            # would be more code than the thing it protects.
            items = body.get("schedules")
            if not isinstance(items, list):
                return {"error": "schedules must be a list"}
            vm.write_schedules(items)
            ledger.record(ledger.ALLOW, "schedule.set",
                          f"{sum(1 for i in items if i.get('enabled', True))} active",
                          actor="guru",
                          detail="; ".join(f"{i.get('label')} at {i.get('at')}"
                                           for i in items)[:200])
            return {"schedules": items}

        if route == "backup":
            out = vm.start_job("Books backup", runner="backup.py", take_inbox=False)
            ledger.record(ledger.ALLOW, "backup.start", "shop books", actor="guru")
            return out

        if route == "alerts/check":
            out = vm.start_job("Alert check", runner="alerts.py", take_inbox=False)
            ledger.record(ledger.INFO, "alerts.check", "shop", actor="guru")
            return out

        if route == "task/add":
            made = tasks.add(str(body.get("what", "")), str(body.get("why", "")))
            if made:
                ledger.record(ledger.INFO, "task.add", made["what"][:60],
                              actor="guru")
            return {"task": made}

        if route == "task/state":
            out = tasks.set_state(str(body.get("id", "")), str(body.get("state", "")))
            if out:
                ledger.record(ledger.INFO, f"task.{out['state']}",
                              out["what"][:60], actor="guru")
            return {"task": out}

        if route == "tasks/from-alerts":
            # The alert is the symptom; the task is the work. Raising the
            # same alert every evening leaves one task, not seven.
            made = tasks.from_alerts(vm.alerts().get("alerts", []))
            return {"raised": len(made), "tasks": tasks.listing()}

        if route == "history/save":
            # Called after anything that changes the material, and safe when
            # nothing did. A history that only records when somebody
            # remembers to ask is not a history.
            out = history.save(str(body.get("why", "")) or "saved from the panel",
                               by="guru")
            if out.get("changed"):
                ledger.record(ledger.INFO, "history.save", out.get("at", "?"),
                              actor="guru", detail=out.get("why"))
            return out

        if route == "history/review":
            try:
                return history.review(str(body.get("at", "")))
            except history.HistoryError as e:
                return {"error": str(e)}

        if route == "secret/set":
            if vm.role() != "head":
                return {"error": "only the head sets a secret"}
            try:
                out = vault.put(str(body.get("name", "")), str(body.get("value", "")))
            except vault.SecretError as e:
                return {"error": str(e)}
            # The NAME is logged and the value never is. An audit trail that
            # records what a credential was set to is a place to steal it from.
            ledger.record(ledger.ALLOW, "secret.set", out["name"], actor="guru",
                          detail=f"{out['length']} characters, value not recorded")
            return out

        if route == "secret/forget":
            if vm.role() != "head":
                return {"error": "only the head forgets a secret"}
            try:
                out = vault.forget(str(body.get("name", "")))
            except vault.SecretError as e:
                return {"error": str(e)}
            ledger.record(ledger.DENY, "secret.forget", out["forgotten"],
                          actor="guru", detail="removed from the store")
            return out

        if route == "approve":
            # Only the head decides. Checked here rather than only in the
            # panel: the button being hidden is a courtesy, this is the rule.
            if vm.role() != "head":
                return {"error": "only the head can decide a request"}
            request_id = str(body.get("id", ""))
            yes = bool(body.get("approved"))
            decided = vm.decide_approval(request_id, yes,
                                         str(body.get("note", "")))
            if decided is None:
                return {"error": "that request is not open"}
            ledger.record(ledger.ALLOW if yes else ledger.DENY,
                          "approval." + ("granted" if yes else "refused"),
                          f"{decided['what']} {decided.get('target', '')}".strip(),
                          actor="guru", detail=decided.get("detail"))
            # Approving is the decision, not the doing. What happens next is
            # the app's business — it polls for the answer and acts on it,
            # so a decision recorded here can never be a half-finished action.
            return {"decided": decided}

        if route == "hosts/save":
            try:
                return {"hosts": hosts.write(body.get("hosts") or [])}
            except ValueError as e:
                return {"error": str(e)}

        if route == "ssh":
            on = bool(body.get("on"))
            out = vm.ssh_out(on)
            ledger.record(ledger.ALLOW if out.get("ssh") else ledger.DENY,
                          "net.ssh", "allowed" if out.get("ssh") else "blocked",
                          actor="guru", detail=out.get("detail"))
            return out

        if route == "ssh/run":
            rows = hosts.read()
            pick = [r for r in rows if r["name"] == str(body.get("host", ""))]
            if not pick:
                return {"error": "no such saved host"}
            cmd = str(body.get("cmd", "")).strip()
            if not cmd:
                return {"error": "nothing to run"}
            ledger.record(ledger.INFO, "ssh.run", pick[0]["name"], actor="guru",
                          detail=cmd[:120])
            try:
                return vm.ssh_run(pick[0], cmd)
            except subprocess.TimeoutExpired:
                return {"code": -1, "out": "", "err": "timed out"}

        if route == "score":
            money = dashboard._sandbox("money")
            try:
                return money.record_score(body.get("score"),
                                          str(body.get("bureau", "")))
            except ValueError as e:
                return {"error": str(e)}

        if route == "profile/set":
            try:
                out = profile.write(name=body.get("name"),
                                    profession=body.get("profession"))
            except ValueError as e:
                return {"error": str(e)}
            ledger.record(ledger.INFO, "profile", out.get("profession", "—"),
                          actor="guru",
                          detail=f"name {out.get('name') or '(unset)'}")
            return out

        if route == "role":
            # Not a security boundary and never described as one — the
            # sandbox is. This decides which controls are offered, so a slip
            # at the till cannot void yesterday or roll the machine back.
            out = vm.set_role(str(body.get("role", "")))
            ledger.record(ledger.ALLOW, "role.set", out["role"], actor="guru")
            return out

        if route == "audit":
            out = vm.start_job("Shop audit", runner="audit.py", take_inbox=False)
            ledger.record(ledger.ALLOW, "audit.start", "shop audit", actor="guru")
            return out

        if route == "tally":
            out = vm.start_job("Tally vouchers", runner="tally.py")
            ledger.record(ledger.ALLOW, "tally.start", "purchase vouchers",
                          actor="guru",
                          detail=f"{len(out['files'])} file(s) handed over: "
                                 f"{', '.join(out['files']) or 'none'}")
            return out

        if route == "lesson/plan":
            out = vm.lesson_plan(str(body.get("lesson", "")))
            ledger.record(ledger.INFO, "lesson.plan", out.get("title", "?"), actor="guru",
                          detail=f"{out.get('doable')} of {out.get('total')} steps performable")
            return out

        if route == "lesson/run":
            return start_lesson(str(body.get("lesson", "")))

        if route == "lesson/answer":
            # The person's reply to a mid-run question. Written into the
            # lesson's own folder, where the executor is polling for it —
            # the shared mount is the one channel that crosses the boundary.
            with _lesson_lock:
                running, lesson_id = _lesson["state"] == "running", _lesson["lesson"]
            if not running:
                return {"error": "nothing is running to answer"}
            n = int(body.get("n", 0))
            answer = ({"skip": True} if body.get("skip")
                      else {"text": str(body.get("text", "")).strip()})
            if not body.get("skip") and not answer["text"]:
                return {"error": "say how, or skip"}
            ledger.record(ledger.ALLOW, "lesson.answer", f"step {n}", actor="guru",
                          detail="skip" if body.get("skip") else answer["text"][:100])
            target = vm.TRACES / lesson_id / f"answer-{n}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(answer), encoding="utf-8")
            tmp.replace(target)
            return {"answered": n}

        if route == "lesson/stop":
            out = vm.lesson_stop()
            ledger.record(ledger.INFO, "lesson.stop", _lesson.get("title") or "—",
                          actor="guru", detail=out.get("detail"))
            return out

        if route == "vm/fix-input":
            ledger.record(ledger.INFO, "display.reset", "vnc stack", actor="guru")
            return vm.reset_display()

        if route == "vm/browsing":
            on = bool(body.get("on"))
            out = vm.set_browsing(on)
            ledger.record(ledger.ALLOW if out.get("browsing") else ledger.DENY,
                          "net.browse", "allowed" if out.get("browsing") else "blocked",
                          actor="guru", detail=out.get("detail"))
            return out

        if route == "vm/egress":
            locked = bool(body.get("locked", True))
            ledger.record(ledger.INFO, "net.egress",
                          "locked" if locked else "open", actor="guru")
            return vm.egress(locked)

        self.send_error(404)
        return None

    def _json(self, obj, code=200):
        if obj is None:
            return
        payload = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ─── static ─────────────────────────────────────────────────────────────

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else ""
        if status not in ("200", "304"):
            super().log_message(fmt, *args)


Handler.extensions_map.update({".js": "text/javascript", ".mjs": "text/javascript"})

def main(port=8200, block=True):
    """Serve the panel. Shishya.exe calls this with block=False and shows the
    page in a window of its own; `python serve.py` blocks, as it always did."""
    paths.ensure()
    if WINDOWS:
        import winhost
        winhost.start()

    # Listen on both loopback addresses, because "localhost" resolves to ::1
    # first on macOS and a server bound only to 127.0.0.1 then refuses its own
    # name. Binding "" would fix that too and would also expose a panel that
    # can start programs inside the sandbox to everyone on the wifi, so it is
    # two loopback sockets rather than one wildcard.
    import socket

    class V6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    servers = []
    for family, host in ((ThreadingHTTPServer, "127.0.0.1"), (V6, "::1")):
        try:
            servers.append(family((host, port), Handler))
        except OSError as e:
            # A machine with IPv6 switched off should still get a panel.
            print(f"  (not listening on {host}: {e})")

    if not servers:
        sys.exit(f"could not bind port {port} on either loopback address")

    threading.Thread(target=scheduler, daemon=True).start()

    print(f"Shishya → http://localhost:{port}   (Ctrl-C to stop)")
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    if not block:
        threading.Thread(target=servers[0].serve_forever, daemon=True).start()
        return servers
    servers[0].serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8200)
