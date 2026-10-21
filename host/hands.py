#!/usr/bin/env python3
"""
Shishya — the clone's hands on Windows. Runs on the HOST.

What locate.py and xdotool were inside the VM: find a control by what it is
called, press it, type into a cell, read the cell back. Built on UI
Automation to find and read, and on posted window messages to act.

── Why the hands are outside the sandbox ───────────────────────────────────

UI Automation does not cross out of an AppContainer at all — measured: a
client inside the container sees the top of its own windows and nothing
below them. So the executor cannot live in the sandbox the way it did in the
VM. It lives here, and what keeps it off your windows is a rule enforced in
one place, _own():

    every action names a window handle, and before anything is sent to it
    the handle's process must carry the clone's container SID.

And nothing is ever sent to "whatever has focus". SendInput would type into
the foreground window, which is wherever your hand last was. PostMessage is
addressed to one window, so a keystroke meant for the clone's spreadsheet
cannot land in yours even if you click away mid-lesson. As a side effect the
clone works without taking focus from you at all.

The same contract as locate.py — find, click, cell_text, current_cell, grid —
so executor.py does not care which it was given.
"""

import ctypes
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winbox   # noqa: E402

user32 = ctypes.windll.user32
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND

WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
VK = {"Return": 0x0D, "Enter": 0x0D, "Tab": 0x09, "Escape": 0x1B, "BackSpace": 0x08,
      "Backspace": 0x08, "Delete": 0x2E, "Left": 0x25, "Up": 0x26, "Right": 0x27,
      "Down": 0x28, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
      "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28}

SETTLE = 0.6


class NotTheClone(RuntimeError):
    """Raised when an action would land on a window that is not the clone's.
    Never caught to retry: it means something is wrong, and stopping is right."""


# ─── UI Automation ──────────────────────────────────────────────────────────

_local = threading.local()


def _uia():
    """One automation object per thread — COM wants its apartment per thread,
    and the panel calls in from its request threads."""
    if getattr(_local, "uia", None) is None:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _local.U = U
        _local.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    return _local.uia, _local.U


# AT-SPI's role names, so procedures and traces read the same on both.
ROLE = {50000: "push button", 50002: "check box", 50003: "combo box", 50004: "text",
        50005: "link", 50007: "list item", 50008: "list", 50009: "menu",
        50011: "menu item", 50013: "radio button", 50015: "slider", 50016: "spin button",
        50019: "tab", 50018: "page tab list", 50020: "label", 50025: "table",
        50028: "data grid", 50029: "table cell", 50030: "text", 50032: "frame",
        50033: "panel", 50036: "table", 50026: "group"}
CLICKABLE = {"push button", "toggle button", "check box", "radio button", "menu item",
             "list item", "table cell", "text", "combo box", "tab", "link"}


# ─── whose window is this ───────────────────────────────────────────────────

def _pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _own(hwnd):
    """The one rule. Raise unless this window belongs to the clone."""
    if not hwnd or not user32.IsWindow(hwnd) or not winbox.in_container(_pid_of(hwnd)):
        raise NotTheClone(f"window {hwnd} is not the clone's — refusing to touch it")
    return hwnd


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def desktop():
    """The clone's screen window, verified."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import winvm
    return winvm._desktop_hwnd()


def app_windows():
    """Every window an app of the clone's owns: the children parented into
    the clone's screen, and any of its own top-level windows (dialogs,
    popups). (hwnd, title) pairs, front to back as Windows orders them."""
    out = []
    desk = desktop()
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    if desk:
        def child(h, _):
            if user32.GetParent(h) == desk and user32.IsWindowVisible(h):
                out.append((h, _title(h)))
            return True
        user32.EnumChildWindows(desk, EnumProc(child), 0)

    clone = set(winbox.container_pids())

    def top(h, _):
        if h != desk and user32.IsWindowVisible(h) and _pid_of(h) in clone and _title(h):
            out.append((h, _title(h)))
        return True
    user32.EnumWindows(EnumProc(top), 0)
    return out


def window_for(app):
    """The clone's window whose title contains this (case-insensitive)."""
    want = (app or "").lower()
    for hwnd, title in app_windows():
        if want in title.lower():
            return hwnd
    return None


def _element(hwnd):
    uia, _ = _uia()
    return uia.ElementFromHandle(_own(hwnd))


def _owner_of(element):
    """The top window inside the clone's screen that holds this element."""
    h = element.CurrentNativeWindowHandle
    if h:
        return h
    uia, _ = _uia()
    walker = uia.ControlViewWalker
    node = element
    for _ in range(40):
        node = walker.GetParentElement(node)
        if node is None:
            return None
        h = node.CurrentNativeWindowHandle
        if h:
            return h
    return None


# ─── finding ────────────────────────────────────────────────────────────────

def find(name, app=None, role=None, visible_only=True):
    """Every control whose name contains `name`, exact names first, then
    shortest — "Save" should beat "Save changes" when the step said Save.
    Searched in the clone's windows only; yours are never walked."""
    uia, U = _uia()
    wanted = (name or "").lower()
    hits = []
    for hwnd, title in app_windows():
        if app and app.lower() not in title.lower():
            continue
        try:
            root = _element(hwnd)
            found = root.FindAll(U.TreeScope_Descendants, uia.CreateTrueCondition())
        except Exception:
            continue
        for i in range(found.Length):
            el = found.GetElement(i)
            try:
                el_name = el.CurrentName or ""
                kind = ROLE.get(el.CurrentControlType, "")
            except Exception:
                continue
            if not el_name or wanted not in el_name.lower() or kind not in CLICKABLE:
                continue
            if role and role != kind:
                continue
            r = el.CurrentBoundingRectangle
            w, h = r.right - r.left, r.bottom - r.top
            if visible_only and (w <= 0 or h <= 0 or el.CurrentIsOffscreen):
                continue
            hits.append({"name": el_name, "role": kind, "app": title,
                         "x": r.left + w // 2, "y": r.top + h // 2, "w": w, "h": h,
                         "exact": el_name.lower() == wanted, "_el": el})
    hits.sort(key=lambda h: (not h["exact"], len(h["name"])))
    return hits


def by_id(hwnd, ident):
    """A control by its object name — Qt publishes objectName as the end of
    the AutomationId, so apps name the controls the hands must reach."""
    uia, U = _uia()
    root = _element(hwnd)
    found = root.FindAll(U.TreeScope_Descendants, uia.CreateTrueCondition())
    for i in range(found.Length):
        el = found.GetElement(i)
        try:
            if (el.CurrentAutomationId or "").split(".")[-1] == ident:
                return el
        except Exception:
            continue
    return None


# ─── acting ─────────────────────────────────────────────────────────────────

def _post_click(hwnd, x, y):
    """A click at screen point (x, y), addressed to one window."""
    _own(hwnd)
    pt = wintypes.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = (pt.y << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lp)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def post_text(hwnd, text, delay=0.02):
    _own(hwnd)
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
        time.sleep(delay)


def post_key(hwnd, name):
    _own(hwnd)
    vk = VK[name]
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1)
    if vk == 0x0D:
        user32.PostMessageW(hwnd, WM_CHAR, 13, 1)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)


def click(hit, name=None, app=None, role=None):
    """Press a control. Its own action first — Invoke, Toggle, Select — which
    needs no coordinates at all; a posted click at its centre otherwise."""
    _, U = _uia()
    el = hit["_el"]
    owner = _owner_of(el)
    _own(owner)
    for pattern, iface, act in ((U.UIA_InvokePatternId, U.IUIAutomationInvokePattern, "Invoke"),
                                (U.UIA_TogglePatternId, U.IUIAutomationTogglePattern, "Toggle"),
                                (U.UIA_SelectionItemPatternId,
                                 U.IUIAutomationSelectionItemPattern, "Select")):
        try:
            p = el.GetCurrentPattern(pattern)
            if p:
                getattr(p.QueryInterface(iface), act)()
                return True, f"pressed {hit['name']!r}"
        except Exception:
            continue
    _post_click(owner, hit["x"], hit["y"])
    return True, f"clicked {hit['name']!r}"


# ─── the spreadsheet ────────────────────────────────────────────────────────
# The clone's spreadsheet is the Sheets app. It names its parts for exactly
# this: nameBox (the cell reference field, as in Calc and Excel) and grid.

CELL_REF = re.compile(r"^([A-Z]{1,3})(\d{1,7})$", re.I)
SHEET_APP = "Sheets"


def _ref(ref):
    m = CELL_REF.match((ref or "").strip())
    if not m:
        return None
    col = 0
    for letter in m.group(1).upper():
        col = col * 26 + (ord(letter) - 64)
    return int(m.group(2)) - 1, col - 1


def _sheet():
    hwnd = window_for(SHEET_APP)
    return hwnd


def grid(app=SHEET_APP, tries=4):
    for attempt in range(tries):
        hwnd = window_for(app)
        if hwnd:
            el = by_id(hwnd, "grid")
            if el is not None:
                return el
        time.sleep(0.4)
    return None


def cell_text(ref, app=SHEET_APP):
    """What is in a named cell, read through the Grid pattern — the same
    question Calc's Table interface answered, by row and column."""
    _, U = _uia()
    where = _ref(ref)
    sheet = grid(app)
    if where is None or sheet is None:
        return None
    try:
        gp = sheet.GetCurrentPattern(U.UIA_GridPatternId).QueryInterface(U.IUIAutomationGridPattern)
        return gp.GetItem(where[0], where[1]).CurrentName or ""
    except Exception:
        return None


def current_cell(app=SHEET_APP):
    """Which cell the cursor is in: the Name Box says, as it does in Calc."""
    _, U = _uia()
    hwnd = window_for(app)
    if not hwnd:
        return None
    box = by_id(hwnd, "nameBox")
    try:
        return box.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(
            U.IUIAutomationValuePattern).CurrentValue or None
    except Exception:
        return None


def goto_cell(name):
    """Put the cursor in a named cell through the Name Box — set its text,
    press Enter. Sheets moves the cursor and hands focus to the grid."""
    _, U = _uia()
    if _ref(name) is None:
        return False, f"{name!r} is not a cell reference"
    hwnd = _sheet()
    if not hwnd:
        return False, "no spreadsheet open on the clone's screen — is Sheets open?"
    box = by_id(hwnd, "nameBox")
    if box is None:
        return False, "Sheets has no Name Box — is this an old copy of the app?"
    try:
        box.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(
            U.IUIAutomationValuePattern).SetValue(name.upper())
        box.SetFocus()
    except Exception as e:
        return False, f"could not use the Name Box: {e}"
    post_key(hwnd, "Return")
    time.sleep(SETTLE)
    return True, f"went to {name} via the Name Box"


def type_value(value, cell=None):
    if cell:
        ok, message = goto_cell(cell)
        if not ok:
            return False, message
    hwnd = _sheet()
    if not hwnd:
        return False, "no spreadsheet open on the clone's screen"
    # Through the cell-contents line, as a person types into Calc's formula
    # bar: set it, press Enter. Keystrokes straight into the grid raced the
    # cell editor opening — measured: 'Exam Eligible' arrived as 'ble' — and a
    # value that depends on how fast an editor appears is not one to put in
    # somebody's spreadsheet. The cell is read back either way.
    _, U = _uia()
    line = by_id(hwnd, "cellValue")
    if line is None:
        post_text(hwnd, value)
    else:
        try:
            line.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(
                U.IUIAutomationValuePattern).SetValue(value)
            line.SetFocus()
        except Exception as e:
            return False, f"could not reach the cell contents line: {e}"
    post_key(hwnd, "Return")
    time.sleep(SETTLE)
    return True, f"typed {value!r}" + (f" into {cell}" if cell else "")


def press_control(name):
    hits = find(name)
    if not hits:
        return False, f"could not find anything called {name!r}"
    ok, message = click(hits[0], name)
    time.sleep(SETTLE)
    return ok, message


OPENABLE = {"calc": "sheet_app.py", "sheets": "sheet_app.py", "spreadsheet": "sheet_app.py",
            "excel": "sheet_app.py", "files": "files_app.py",
            "training data": "training_app.py", "reports": "reports_app.py",
            "billing": "billing_app.py", "inbox": "inbox_app.py", "shishya": "chat_app.py",
            "chat": "chat_app.py", "make a document": "make_app.py", "documents": "make_app.py",
            "code": "code_app.py", "findings": "findings_app.py", "activity": "activity_app.py",
            "settings": "settings_app.py", "models": "models_app.py", "banking": "bank_app.py",
            "credit": "credit_app.py", "browser": "browser_app.py", "teach": "shishya_app.py",
            "task manager": "system_app.py"}


def open_app(what):
    import winvm
    name = OPENABLE.get((what or "").lower())
    if not name:
        return False, f"I do not know how to open {what!r}"
    winvm.launch_app(name)
    time.sleep(3)
    return True, f"opened {what}"


DO = {"goto": lambda a: goto_cell(a["cell"]),
      "type": lambda a: type_value(a["value"], a.get("cell")),
      "press": lambda a: press_control(a["what"]),
      "open": lambda a: open_app(a["what"])}


# ─── the chat, driven from the panel ────────────────────────────────────────

def ask_chat(question):
    """Put a question in the chat and press Send, without the pointer."""
    _, U = _uia()
    for app, field, button in (("Shishya", "message", "send"),
                               ("Training Data", "question", "ask")):
        hwnd = window_for(app)
        if not hwnd:
            continue
        box, go = by_id(hwnd, field), by_id(hwnd, button)
        if box is None or go is None:
            continue
        box.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(
            U.IUIAutomationValuePattern).SetValue(question)
        go.GetCurrentPattern(U.UIA_InvokePatternId).QueryInterface(
            U.IUIAutomationInvokePattern).Invoke()
        return f"asked: {question}"
    return "Nothing is open to ask. Open Shishya from the panel first."


WORKING = ("Thinking about", "Let me check", "I'll ", "I will ", "That needs counting",
           "The program hit an error", "I am ", "I'm ", "Reading…", "Building the")


def read_chat(timeout=420):
    """The newest thing the chat said, once it stops changing. The chat
    publishes its latest reply on a control named lastReply."""
    _, U = _uia()
    deadline = time.time() + timeout
    last, stable = "", 0
    while time.time() < deadline:
        now = ""
        for app, ident in (("Shishya", "lastReply"), ("Training Data", "answer")):
            hwnd = window_for(app)
            if not hwnd:
                continue
            el = by_id(hwnd, ident)
            if el is None:
                continue
            try:
                now = el.GetCurrentPattern(U.UIA_ValuePatternId).QueryInterface(
                    U.IUIAutomationValuePattern).CurrentValue or ""
            except Exception:
                now = el.CurrentName or ""
            break
        if now and now == last and not now.startswith(WORKING):
            stable += 1
            if stable >= 2:
                return now
        else:
            stable = 0
        last = now
        time.sleep(2)
    return last or "(still working)"
