#!/usr/bin/env python3
"""
Shishya — perform a procedure that was learned by demonstration.

    python3 executor.py <trace-dir>            what it would do, no action
    python3 executor.py <trace-dir> --run      actually do it
    python3 executor.py <trace-dir> --json     the same, for the control panel

`--json` prints one object per line and flushes each: a seventeen-step
procedure takes minutes, and a panel that shows nothing until it finishes is
a panel you cannot stop in time.

── What this will and will not attempt ─────────────────────────────────────

A recorded procedure is prose with a loose target attached. The prose carries
the intent — "Type 'Exam Eligible' into the selected cell" — while the target
is frequently generic, "Panel" or "Text Input", because that is genuinely
what AT-SPI reported at the time. So the text is read and the target is only
consulted when it is specific.

Four actions are supported, and they are the four the recorded lessons
actually contain: go to a named cell, type a value, press a named control,
and open an application. Anything else stops the run.

Stopping is the design, not a shortfall. A model this size guessing at an
unrecognised instruction is how an apprentice ends up confidently filing the
wrong thing, and the entire argument for this project is that you can see
what it did. A refusal you can read beats an action you cannot.

Every step verifies before moving on. A step that cannot be confirmed is
retried once and then halts the run with the reason.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    # On Windows the hands are host/hands.py: UI Automation to find and read,
    # posted messages to act, and only ever on the clone's own windows. Same
    # contract as locate.py — find, click, cell_text, current_cell, grid — and
    # its own DO table, swapped in below.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
    import hands as locate   # noqa: E402
    pyatspi = None
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import locate   # noqa: E402

    try:
        import pyatspi
    except ImportError:
        pyatspi = None

SETTLE = 0.6           # what a person's hand costs between actions
ASK_TIMEOUT = 120      # how long a question waits before the step is skipped
ASK_LIMIT = 3          # how many times one run may interrupt a person
VERIFY_TIMEOUT = 6


# ─── reading a step ─────────────────────────────────────────────────────────

CELL = re.compile(r"\bcell\s+([A-Z]{1,3}\d{1,5})\b", re.I)
QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{1,80})['\"‘’“”]")

TYPING = ("type", "enter", "write", "input", "fill", "put")
PRESSING = ("click", "press", "choose", "select the", "push")
OPENING = ("open", "launch", "start")
# The clone's apps by the names a person writes. A lesson edited by hand says
# "Open the Sheets application", with no quotes and no recorded target, and
# the README promises any line can be edited — so the name in the sentence is
# enough. Longest first: "training data" before "data". (host/hands.py
# OPENABLE says which app each name opens.)
APP_NAMES = sorted(("calc", "sheets", "spreadsheet", "excel", "files", "training data", "reports",
                    "billing", "inbox", "shishya", "chat", "make a document", "documents", "code",
                    "findings", "activity", "settings", "models", "banking", "credit", "browser",
                    "teach", "task manager"), key=len, reverse=True)


def understand(step):
    """Turn one written step into something performable, or None.

    Reads the sentence rather than the target field. The target recorded
    against a spreadsheet step is usually the sheet, not the cell, because
    that is what had focus when the recorder looked.
    """
    text = (step.get("text") or "").strip()
    low = text.lower()
    target = step.get("target") or {}
    named = (target.get("name") or "").strip()

    cell = CELL.search(text) or CELL.search(named)
    quoted = QUOTED.search(text)

    # "Type 'X' into cell I4" — the two together are one action.
    if any(v in low for v in TYPING) and quoted:
        return {"do": "type", "value": quoted.group(1),
                "cell": cell.group(1).upper() if cell else None, "text": text}

    if cell and any(v in low for v in ("navigate", "go to", "move to", "select")):
        return {"do": "goto", "cell": cell.group(1).upper(), "text": text}

    if any(v in low for v in OPENING):
        what = quoted.group(1) if quoted else named
        if not what:
            what = next((n for n in APP_NAMES if re.search(rf"\b{re.escape(n)}\b", low)), None)
        if what:
            return {"do": "open", "what": what, "text": text}

    if any(v in low for v in PRESSING):
        what = quoted.group(1) if quoted else named
        # A generic target name is not a control worth hunting for.
        if what and what.lower() not in ("panel", "text input", "table", "frame"):
            return {"do": "press", "what": what, "text": text}

    return None


# ─── doing it ───────────────────────────────────────────────────────────────

def sh(*args, timeout=10):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def focus_grid():
    """Put real keyboard focus into the sheet, by clicking it.

    Raising the window is not enough. WM-active and widget-focus are
    different things: Calc reopens with whatever bar was showing when it
    last closed, and a lingering Find bar holds widget focus at launch —
    every keystroke then lands in its search box while the sheet ignores
    the lot. That cost an hour of "typing does nothing" on 23 Aug, blamed
    in turn on VNC, on Xvfb, and on AT-SPI before a single test click put
    an "a" in C5 and settled it. GTK focuses on press; so press.

    The click lands just inside the grid's own rectangle, asked of the
    accessibility tree — never a guessed coordinate.
    """
    sh("bash", "-c",
       "xdotool search --onlyvisible --name 'LibreOffice Calc' windowactivate --sync")
    time.sleep(0.3)
    if pyatspi is None:
        return False, "no accessibility bus to find the sheet with"
    sheet = locate.grid()
    if sheet is not None:
        try:
            x, y, w, h = sheet.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
            if w > 0 and h > 0:
                sh("xdotool", "mousemove", str(x + 60), str(y + 30), "click", "1")
                time.sleep(0.4)
                return True, "clicked into the sheet"
        except Exception:
            pass

    # The accessible was not available. That is not the same as the sheet
    # not being there: over a long run these lookups fail intermittently
    # while Calc sits plainly on screen with the cursor in the right cell —
    # once ending a lesson four steps from the end for no reason a person
    # watching could see. So fall back to the window's own geometry, which
    # is what worked before any of this used the accessibility tree.
    #
    # The cost is honest and small: the click still lands in the grid, and
    # anything that follows is verified by reading the cell back, which does
    # not depend on this at all.
    box = sh("bash", "-c",
             "xdotool search --onlyvisible --name 'LibreOffice Calc' "
             "getwindowgeometry --shell 2>/dev/null")
    values = {}
    for line in (box.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    try:
        x, y = int(values["X"]), int(values["Y"])
    except (KeyError, ValueError):
        return False, "no spreadsheet grid on screen — is Calc open?"
    # Below the toolbars and the formula bar, inside the grid on any layout
    # this display uses. The display is frozen at 1440×900 for exactly this
    # kind of reason (§20).
    sh("xdotool", "mousemove", str(x + 200), str(y + 260), "click", "1")
    time.sleep(0.4)
    return True, "clicked into the sheet (by window, not by tree)"


def goto_cell(name):
    """Put the cursor in a named cell.

    Two routes, tried in order. The Name Box is the designed one — always
    present, always in the same place — but on this LibreOffice build its
    accessible is nameless, so it cannot always be found by name. The
    fallback needs no control at all: Ctrl+Home puts the cursor in A1 with
    certainty, and arrow keys walk from there — I4 is eight Rights and three
    Downs. Slower, but every keystroke is deterministic, and the landing is
    verified by reading the cell back either way.
    """
    ok, how = focus_grid()
    if not ok:
        return False, how

    box = locate.find("Name Box", visible_only=True)
    if box:
        hit = box[0]
        sh("xdotool", "mousemove", str(hit["x"]), str(hit["y"]))
        time.sleep(0.2)
        sh("xdotool", "click", "1")
        time.sleep(0.3)
        sh("xdotool", "key", "ctrl+a")
        sh("xdotool", "type", "--delay", "40", name)
        sh("xdotool", "key", "Return")
        time.sleep(SETTLE)
        return True, f"went to {name} via the Name Box"

    letters = "".join(ch for ch in name if ch.isalpha()).upper()
    digits = "".join(ch for ch in name if ch.isdigit())
    if not letters or not digits:
        return False, f"{name!r} is not a cell reference"
    column = 0
    for letter in letters:
        column = column * 26 + (ord(letter) - 64)
    rights, downs = column - 1, int(digits) - 1
    if rights > 60 or downs > 200:
        # Walking hundreds of arrow presses is asking for one lost keystroke
        # to land everything one cell off. Refuse rather than drift.
        return False, f"no Name Box, and {name} is too far to walk from A1"
    sh("xdotool", "key", "ctrl+Home")
    time.sleep(0.4)
    if rights:
        sh("xdotool", "key", "--repeat", str(rights), "--repeat-delay", "60", "Right")
    if downs:
        sh("xdotool", "key", "--repeat", str(downs), "--repeat-delay", "60", "Down")
    time.sleep(SETTLE)
    return True, f"walked to {name} from A1"


def type_value(value, cell=None):
    """Type a value, going to the cell first when the step named one.

    "Type 'Yes' into cell I2" is one sentence and one action. The cell was
    parsed and then dropped, so a step written that way typed into whatever
    cell happened to be selected — silently, into somebody's spreadsheet.
    The recorded lessons split the two, which is why this never showed up.
    """
    if cell:
        ok, message = goto_cell(cell)
        if not ok:
            return False, message
    sh("xdotool", "type", "--delay", "45", value)
    sh("xdotool", "key", "Return")
    time.sleep(SETTLE)
    return True, f"typed {value!r}" + (f" into {cell}" if cell else "")


def press_control(name):
    hits = locate.find(name)
    if not hits:
        return False, f"could not find anything called {name!r}"
    ok, message = locate.click(hits[0], name)
    time.sleep(SETTLE)
    return ok, message


def open_app(what):
    known = {
        "calc": "libreoffice --calc", "writer": "libreoffice --writer",
        "files": "python3 /opt/shishya/vm/shell/files_app.py",
        "training data": "python3 /opt/shishya/vm/shell/training_app.py",
        "reports": "python3 /opt/shishya/vm/shell/reports_app.py",
        "billing": "python3 /opt/shishya/vm/shell/billing_app.py",
    }
    command = known.get(what.lower())
    if not command:
        return False, f"I do not know how to open {what!r}"
    subprocess.Popen(command.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    return True, f"opened {what}"


DO = {"goto": lambda a: goto_cell(a["cell"]),
      "type": lambda a: type_value(a["value"], a.get("cell")),
      "press": lambda a: press_control(a["what"]),
      "open": lambda a: open_app(a["what"])}

if sys.platform == "win32":
    DO = locate.DO


# ─── verifying ──────────────────────────────────────────────────────────────

def attempt(action):
    """Do one action and check it: (ok, message, done).

    done says the action itself went through, so a failed check is worth one
    retry. An application that closed mid-run makes the hands raise — the
    window is gone, or is no longer the clone's — and that used to end the
    whole run with no word to the person watching. It is a failed step, with
    its reason, like any other.
    """
    try:
        done, message = DO[action["do"]](action)
        if not done:
            return False, message, False
        ok, verified = confirm(action)
        return ok, f"{message} — {verified}", True
    except Exception as e:                       # noqa: BLE001 — reported, never raised
        return False, f"could not do it ({type(e).__name__}: {str(e)[:160]})", False


def confirm(action):
    """Check the thing that was supposed to happen actually did.

    A typed value is read back out of the cell it was typed into. The earlier
    version searched the accessibility tree for the text, which answered a
    question nobody asked: "Yes" appearing somewhere on screen — in a save
    dialog's button, say — is not evidence that "Yes" reached I2.

    A press is not verifiable in general, and saying so is better than a
    green tick that means nothing.
    """
    if action["do"] == "goto":
        # Verifiable now. The grid answers "which of your cells has focus"
        # through Collection, so arriving at the wrong cell is caught here
        # rather than three steps later when a typed value fails to read
        # back from somewhere it was never written.
        wanted = (action.get("cell") or "").upper()
        deadline = time.time() + VERIFY_TIMEOUT
        where = None
        while time.time() < deadline:
            where = locate.current_cell()
            if where and where.upper() == wanted:
                return True, f"the cursor is in {where}"
            time.sleep(0.3)
        if where is None:
            return True, "arrived, but the sheet would not say where — unverified"
        return False, f"the cursor is in {where}, not {wanted}"

    if action["do"] != "type":
        return True, "not verifiable, assumed"

    wanted = (action.get("value") or "").strip()
    cell = action.get("cell")
    if not cell:
        return True, "typed, but into a cell I cannot name — unverified"

    deadline = time.time() + VERIFY_TIMEOUT
    got = None
    while time.time() < deadline:
        got = locate.cell_text(cell)
        if got is not None and autocorrected(got.strip()) == autocorrected(wanted):
            return True, f"{cell} now reads {got.strip()!r}"
        time.sleep(0.4)
    if got is None:
        return False, f"could not read {cell} back"
    return False, f"{cell} reads {got.strip()!r}, not {wanted!r}"


def autocorrected(text):
    """What Calc's AutoCorrect makes of typed text, folded for comparison.

    Typed 'No - below 65%', the cell reads 'No – below 65%': AutoCorrect
    swaps a spaced hyphen for an en dash, and straight quotes for curly
    ones. The person teaching the lesson typed the plain characters and
    Calc did this exact rewrite to their demonstration too — so the folded
    forms being equal IS the value arriving faithfully, and demanding the
    raw characters back would fail every lesson taught by a human hand.
    """
    for fancy, plain in (("–", "-"), ("—", "-"), ("−", "-"),
                         ("‘", "'"), ("’", "'"),
                         ("“", '"'), ("”", '"')):
        text = text.replace(fancy, plain)
    return text


# ─── asking ─────────────────────────────────────────────────────────────────
# The apprentice's move: a step it does not understand is asked about, not
# guessed at and not silently dropped. The question travels out as a run
# event; the answer comes back as a file in the trace directory, because the
# executor is a subprocess inside the sandbox and the person is at the panel
# — the shared folder is the one channel that already crosses that boundary.
#
# The answer is folded back into the procedure. Teaching happens once: the
# person rephrases the step in words the executor knows ("type 'Yes' into
# cell B2"), it performs it, and the procedure file now carries the working
# wording — the next run never asks again.

def wait_answer(ask_dir, n):
    """The person's reply for step n, or None when nobody answers in time."""
    path = Path(ask_dir) / f"answer-{n}.json"
    deadline = time.time() + ASK_TIMEOUT
    while time.time() < deadline:
        if path.exists():
            try:
                answer = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                answer = None
            path.unlink(missing_ok=True)      # consumed — a re-run asks afresh
            return answer
        time.sleep(2)
    return None


# ─── running ────────────────────────────────────────────────────────────────

def load(trace_dir):
    path = Path(trace_dir)
    if path.is_dir():
        path = path / "procedure.json"
    return json.loads(path.read_text(encoding="utf-8"))


def read_plan(proc):
    """Every step with what it would do — the only place understand() is
    called for a whole procedure, so the plan and the run can never disagree
    about a step. That mattered: the panel showing eight of seventeen and the
    run refusing a different eight would be worse than no plan at all."""
    steps = []
    for i, step in enumerate(proc.get("steps", []), 1):
        action = understand(step)
        steps.append({
            "n": step.get("n", i),
            "can": bool(action),
            "do": action["do"] if action else None,
            # The value first: "type 'Yes'" says more about a step than the
            # cell it lands in, and a type action now carries both.
            "what": (action.get("value") or action.get("cell")
                     or action.get("what")) if action else None,
            "cell": action.get("cell") if action else None,
            "text": step.get("text", ""),
        })
    return {"title": proc.get("title", "untitled"),
            "steps": steps,
            "doable": sum(1 for s in steps if s["can"]),
            "total": len(steps)}


def plan(proc):
    read = read_plan(proc)
    lines = [read["title"]]
    for s in read["steps"]:
        mark = "  ok " if s["can"] else "  ?? "
        summary = (f"{s['do']} {s['what'] or ''}"
                   + (f" → {s['cell']}" if s["do"] == "type" and s["cell"] else "")
                   if s["can"] else "cannot perform this one")
        lines.append(f"{mark}{s['n']:>2}. {summary:<28} {s['text'][:60]}")
    lines.append(f"\n  {read['doable']} of {read['total']} steps are performable")
    return "\n".join(lines)


def run(proc, on_event=None, ask_dir=None):
    """Perform the procedure, reporting each step as it lands.

    A step it does not recognise is skipped, not fatal. Halting on the first
    one made the feature inert: the only real recorded lesson opens with
    "Navigate to the main panel", which is recorder noise, so a run refused
    at step 1 and never reached the eight substantive steps behind it.

    Skipping is not guessing, and the distinction is the whole design. Three
    outcomes, and they are different things that must never be shown as one:

      skipped  I do not know how to do this — not attempted, recorded, next
      step     attempted; ok=False means it was tried and could not be
               confirmed, and that still halts the run
      halted   I could do it, but not safely from here

    The last one exists because skipping is what makes it possible. Skip
    "switch to the term 1 sheet" and the next instruction to type lands in
    whatever sheet happened to be in front — silently, in the right-looking
    cell of the wrong table. So the cursor's position is tracked, and a step
    that types without naming its cell, after something was skipped, halts
    instead. Never type into a cell you cannot name.
    """
    events = []

    def emit(event):
        events.append(event)
        if on_event:
            on_event(event)
        return event

    placed = False        # do we know which cell the cursor is in?
    last_cell = None      # and if so, which one
    taught = False        # did any answer rewrite a step?
    asked = 0             # questions put to the person so far

    for i, step in enumerate(proc.get("steps", []), 1):
        n = step.get("n", i)
        action = understand(step)

        # Ask only while someone is there to answer, and only a few times.
        # The recorded lesson has nine steps it cannot perform; asking about
        # every one turns a 34-second run into half an hour of a panel
        # waiting on a person who left. After the third question it goes back
        # to skipping, which is what it did before and is never wrong.
        if not action and ask_dir is not None and asked < ASK_LIMIT:
            asked += 1
            emit({"kind": "ask", "n": n, "ok": False,
                  "text": step.get("text", ""),
                  "message": "I do not know how to do this — say it in words "
                             "I know (type 'X' into cell Y / go to cell Y / "
                             "press 'Name' / open 'App'), or skip it"})
            answer = wait_answer(ask_dir, n)
            if answer and (answer.get("text") or "").strip():
                new_text = answer["text"].strip()
                action = understand({"text": new_text, "target": {}})
                if action:
                    step["text"] = new_text
                    taught = True
                    emit({"kind": "taught", "n": n, "ok": True, "text": new_text,
                          "message": f"understood — the step now reads {new_text!r}"})
                else:
                    emit({"kind": "skipped", "n": n, "ok": False, "text": new_text,
                          "message": "I still do not understand that wording — skipping"})
                    placed = False
                    continue
            else:
                why = ("skipped at your request" if answer
                       else "no answer arrived — skipping it")
                emit({"kind": "skipped", "n": n, "ok": False,
                      "text": step.get("text", ""), "message": why})
                placed = False
                continue

        if not action:
            emit({"kind": "skipped", "n": n, "ok": False,
                  "text": step.get("text", ""),
                  "message": "I do not know how to do this one — skipping it"})
            placed = False
            continue

        if action["do"] == "type" and not action["cell"] and placed and last_cell:
            # The step says "type into the selected cell" and the goto before
            # it named which. Filling it in makes the value verifiable, and
            # makes a retry overwrite that cell rather than whatever the
            # cursor wandered to after the first attempt pressed Return.
            action["cell"] = last_cell

        if action["do"] == "type" and not action["cell"] and not placed:
            emit({"kind": "halted", "n": n, "ok": False,
                  "text": step.get("text", ""),
                  "message": "I skipped a step and no longer know which cell "
                             "is selected — stopping rather than typing blind"})
            break

        ok, message, done = attempt(action)
        if done and not ok:
            # One retry, because a slow application is the usual cause. Its
            # own result counts: a retry that fails says why.
            time.sleep(1.2)
            ok, again, _ = attempt(action)
            message = f"{message}; retried — {again}"

        if ok:
            # A goto names a cell; a type either named one or followed a goto,
            # and Return moves predictably down the same column. Pressing a
            # control or opening an application can put anything in front,
            # so neither leaves us able to name where the cursor is.
            placed = action["do"] in ("goto", "type")
            if placed:
                last_cell = action.get("cell") or last_cell

        emit({"kind": "step", "n": n, "ok": ok, "do": action["do"],
              "text": step.get("text", ""), "message": message})
        if not ok:
            break
    return events, taught



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--plan", action="store_true", help="what it would do (the default)")
    ap.add_argument("--run", action="store_true", help="actually do it")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable, one object per line")
    ap.add_argument("--ask", action="store_true",
                    help="pause on a step it does not understand and ask. Only "
                         "when somebody is watching: unattended, it skips.")
    args = ap.parse_args()

    procedure = load(args.trace)

    if not args.run:
        print(json.dumps(read_plan(procedure)) if args.as_json else plan(procedure))
        raise SystemExit(0)

    trace_dir = Path(args.trace)
    if trace_dir.is_file():
        trace_dir = trace_dir.parent

    def save_if_taught(taught):
        """Fold what was learned by asking back into the lesson. The person
        answered once; the file must remember, or the same question comes
        back on every run and 'apprentice' stays a metaphor."""
        if not taught:
            return
        procedure["taught_by_asking"] = True
        (trace_dir / "procedure.json").write_text(
            json.dumps(procedure, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.as_json:
        # Flushed per line. The panel is reading this pipe while the clone is
        # still typing, and a buffered run is one you cannot stop in time.
        def emit(event):
            print(json.dumps(event), flush=True)

        emit({"kind": "start", **read_plan(procedure)})
        performed, taught = run(procedure, emit,
                                ask_dir=trace_dir if args.ask else None)
        save_if_taught(taught)
        emit({"kind": "done",
              "performed": sum(1 for e in performed if e["ok"]),
              "skipped": sum(1 for e in performed if e["kind"] == "skipped"),
              "taught": taught,
              "attempted": len(performed)})
    else:
        performed, taught = run(procedure, ask_dir=trace_dir if args.ask else None)
        save_if_taught(taught)
        for event in performed:
            print(f"  {'ok ' if event['ok'] else 'STOP'} {event['n']:>2}. {event['message']}")
