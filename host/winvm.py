#!/usr/bin/env python3
"""
Shishya — the clone's sandbox on Windows, behind the same calls as vm.py.

vm.py drives a Lima VM through limactl. On Windows there is no VM: the clone
is an AppContainer (winbox.py), its programs live in one Job Object, and the
model reaches it through a named pipe (broker.py). This module is the
translation — every function vm.py has that shells out to limactl has one
here with the same name, the same arguments and the same shape of answer, and
vm.py swaps them in at import time (see the end of vm.py).

What stays the same is the point. The panel does not know which platform it
is on except where the difference is real and worth saying: the clone's
screen is a window on this desktop rather than a VNC stream, and the egress
lock is read from process tokens rather than from nftables.

── What the sandbox is made of, here ───────────────────────────────────────

  the clone           AppContainer "Shishya.Clone", created per user, no admin
  its disk            the container's own storage, %LOCALAPPDATA%\\Packages\\...\\AC
  its home            AC\\home, with ~\\shared a junction to the shared folder
  the mount list      ACL grants: the runtime and the code read-only, shared/
                      read-write, the ledger read-only. Nothing else.
  the network         capabilities in the process token; none means none
  the model           \\\\.\\pipe\\shishya-model, its ACL naming this container only
  its screen          one 1440x900 window, every app parented into it
  stopping it         TerminateJobObject — nothing it started survives
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import paths
import winbox

ROOT = paths.ROOT
CLONE = ROOT / "clone"
VMDIR = ROOT / "vm"

# The names vm.py replaces with ours. Everything else in vm.py — lessons,
# jobs, approvals, schedules, taking files back — is shared and stays.
REPLACES = (
    "status", "create", "start", "stop", "shell", "launch_app", "close_apps",
    "drive", "run_command", "_executor", "lesson_stop", "_launch_runner",
    "reset_display", "snapshot", "rollback", "_snapshot_age", "snapshots",
    "egress_state", "egress", "browsing", "browsing_state", "ssh_out",
    "ssh_state", "ssh_run", "_relock_if_needed", "resolved_config",
)


def _vm():
    """vm.py, imported lazily: it imports this module at its end, and
    reaching back at import time would find it half-built."""
    import importlib
    return sys.modules.get("vm") or importlib.import_module("vm")


def _error(message):
    return _vm().VMError(message)


# ─── the runtime the clone runs on ──────────────────────────────────────────

def runtime():
    """The Python the clone runs, or None.

    An installed copy carries its own in runtime\\. A checkout can name one
    with SHISHYA_RUNTIME. It must be a folder the container can be granted —
    never the developer's own Python install, whose ACL is not ours to change.
    """
    chosen = os.environ.get("SHISHYA_RUNTIME")
    for candidate in ([Path(chosen)] if chosen else []) + [ROOT / "runtime"]:
        if (candidate / "python.exe").exists():
            return candidate
    return None


def _need_runtime():
    rt = runtime()
    if rt is None:
        raise _error("no runtime for the clone — this checkout has no runtime\\ "
                     "folder. Build one with windows\\build.ps1, or point "
                     "SHISHYA_RUNTIME at a Python that has PySide6 installed.")
    return rt


def host_python():
    """The interpreter for host-side helpers (the executor, synthesis).
    python.exe rather than pythonw.exe: their output is read line by line."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


# ─── provisioning: what vm/provision.sh was ─────────────────────────────────

PROVISIONED = paths.VAR / "sandbox.json"
LEDGER = paths.VAR / "ledger.jsonl"


def clone_home():
    return winbox.container_home() / "home"


def provisioned():
    try:
        return json.loads(PROVISIONED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def provision():
    """Build the sandbox: container, grants, home. Idempotent, and quick —
    seconds, not the twenty minutes a VM's first boot took.

    The grants ARE the mount list. Each one is listed in sandbox.json with
    what it allows, so the panel can show the boundary as it actually is.
    """
    rt = _need_runtime()
    paths.ensure()
    sid = winbox.ensure_container()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.touch(exist_ok=True)

    grants = []
    for folder, write, why in ((rt, False, "the clone's Python"),
                               (CLONE, False, "the clone's desktop and apps"),
                               (VMDIR, False, "the runners and their shared code"),
                               (paths.SHARED, True, "shared/ — the one folder both sides use")):
        winbox.grant(folder, write=write)
        grants.append({"path": str(folder), "write": write, "why": why})
    # The ledger, read-only and by itself: the Activity app shows it, and an
    # audit log the audited party can edit is not an audit log.
    _grant_file(LEDGER)
    grants.append({"path": str(LEDGER), "write": False, "why": "the ledger, to read"})

    # The clone's home, as programs see it, is the data folder: the one that
    # HOLDS shared/. Granted on that folder alone — list it, step through it —
    # with no inheritance, so var/ beside shared/ (the ledger, the snapshots,
    # the network policy) stays as closed as anything else of yours.
    _grant_folder_only(paths.DATA)
    grants.append({"path": str(paths.DATA), "write": False,
                   "why": "the clone's home: this folder only, so ~/shared resolves"})

    home = clone_home()
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("Temp", "AppData/Roaming", "AppData/Local", ".matplotlib"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    record = {"sid": sid, "home": str(home), "runtime": str(rt), "grants": grants,
              "at": time.time(), "copy": _this_copy()}
    PROVISIONED.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _this_copy():
    """Which installed copy the grants were made for: the program folder and
    when its files were last laid down."""
    marker = ROOT / "INSTALLED" if (ROOT / "INSTALLED").exists() else ROOT / "serve.py"
    try:
        return f"{ROOT}|{marker.stat().st_mtime:.0f}"
    except OSError:
        return str(ROOT)


def _grant_file(path):
    sid = winbox.container_sid()
    subprocess.run(["icacls", str(path), "/grant", f"*{sid}:R", "/Q"],
                   capture_output=True, creationflags=winbox.CREATE_NO_WINDOW)


def _grant_folder_only(path):
    """Read and traverse one folder, inherited by nothing inside it.

    Why the clone's home is the data folder rather than a junction to
    shared/ inside the container: generated programs write
    Path.home() / "shared" / "work" because that is the path on the Mac, and
    Windows will not let a sandboxed process follow a junction that an
    ordinary process made — a deliberate mitigation, measured as "Access is
    denied". Making home the folder that really contains shared/ needs no
    link at all.
    """
    sid = winbox.container_sid()
    subprocess.run(["icacls", str(path), "/grant", f"*{sid}:RX", "/Q"],
                   capture_output=True, creationflags=winbox.CREATE_NO_WINDOW)


def screen_scale():
    """How many device pixels each of the clone's points gets: this display's
    scale (2 at 200%), in quarter steps, never so large that the screen
    popped out into a window of its own would not fit on this display."""
    try:
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForSystem() / 96
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)    # SPI_GETWORKAREA
        fits = min((work.right - work.left) / 1440, (work.bottom - work.top - 60) / 900)
        scale = max(1.0, min(dpi, fits))
    except (OSError, AttributeError, ZeroDivisionError):
        scale = 1.0
    return f"{int(scale * 4) / 4:g}"


def clone_env(**extra):
    """Everything a clone process is told, on top of what Windows needs.

    Named one by one. Nothing from the panel's own environment leaks in —
    see winbox.base_env.
    """
    rt = _need_runtime()
    home = clone_home()                  # the container's own, writable storage
    seen = paths.DATA                    # what Path.home() says: holds shared/
    env = {
        "USERPROFILE": str(seen), "HOME": str(seen),
        "HOMEDRIVE": seen.drive, "HOMEPATH": str(seen)[len(seen.drive):],
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "TEMP": str(home / "Temp"), "TMP": str(home / "Temp"),
        "PATH": f"{rt};{rt / 'Scripts'};" + winbox.base_env()["PATH"],
        "SHISHYA_SHARED": str(paths.SHARED),
        "SHISHYA_ROOT": str(ROOT),
        "SHISHYA_LEDGER": str(LEDGER),
        "SHISHYA_MODEL": "pipe",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLCONFIGDIR": str(home / ".matplotlib"), "MPLBACKEND": "Agg",
        # The clone's screen is 1440x900 *points*, always: taught procedures
        # reference its layout, and that must not change with the laptop.
        # It is drawn at this display's sharpness, though — 2x on a 200%
        # screen — so the picture in Shishya's window is shown pixel for
        # pixel instead of stretched and soft. Qt's own factor, not
        # Windows': the layout stays put, only the pixels multiply.
        "QT_ENABLE_HIGHDPI_SCALING": "0", "QT_SCALE_FACTOR": screen_scale(),
        "QT_FONT_DPI": "96",
        # Chromium's own sandbox cannot nest inside an AppContainer. The
        # container is the sandbox, exactly as the VM was on the Mac.
        "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --disable-gpu",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        # One window: the clone's screen lives inside Shishya's home page,
        # not as a second window on the person's desktop (clone/desktop.py).
        "SHISHYA_SCREEN": "embedded",
    }
    hwnd = _desktop_hwnd()
    if hwnd:
        env["SHISHYA_DESKTOP_HWND"] = str(hwnd)
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ─── the clone's screen ─────────────────────────────────────────────────────

HWND_FILE = paths.SHARED / ".desktop.hwnd"
_session = {"desktop": None}
_lock = threading.RLock()


def _desktop_hwnd():
    """The clone's screen, if it is up and really the clone's.

    The handle is written by the clone itself, so it is checked before it is
    believed: the window must exist and its process must carry the container
    SID. A handle that pointed at one of YOUR windows would otherwise become
    the thing the panel captures and the executor types into.
    """
    import ctypes
    try:
        hwnd = int(HWND_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return None
    user32 = ctypes.windll.user32
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd if winbox.in_container(pid.value) else None


def _launch_desktop():
    rt = _need_runtime()
    HWND_FILE.unlink(missing_ok=True)
    proc = winbox.launch([rt / "pythonw.exe", CLONE / "desktop.py"],
                         env=clone_env(), cwd=clone_home())
    _session["desktop"] = proc
    deadline = time.time() + 40
    while time.time() < deadline:
        if _desktop_hwnd():
            return True
        if proc.poll() is not None:
            _forget_status()
            raise _error(f"the clone's desktop exited at once (code {proc.returncode}) — "
                         f"see {clone_home() / 'Temp' / 'desktop.log'}")
        time.sleep(0.25)
    raise _error("the clone's desktop did not come up within 40 seconds")


# ─── lifecycle ──────────────────────────────────────────────────────────────

_STATUS_CACHE = {"at": 0.0, "value": None}


def status():
    """Everything the panel needs to paint its header, in one call.

    "running" means the clone's screen is up — the same thing it meant on the
    Mac, where a running VM always had its display. Cached for a second: the
    panel asks three things at once on every refresh and each one would
    otherwise walk the process list.
    """
    now = time.time()
    if _STATUS_CACHE["value"] and now - _STATUS_CACHE["at"] < 1.0:
        return _STATUS_CACHE["value"]
    rt = runtime()
    if rt is None:
        value = {"state": "no-runtime", "platform": "windows",
                 "detail": "no runtime folder — run windows\\build.ps1"}
    elif not provisioned():
        value = {"state": "absent", "platform": "windows"}
    else:
        pids = winbox.container_pids()
        hwnd = _desktop_hwnd()
        state = "running" if hwnd else ("starting" if pids else "stopped")
        value = {"state": state, "platform": "windows", "sandbox": "AppContainer",
                 "cpus": os.cpu_count(), "memory": f"{winbox.MEMORY_CAP // 1024 ** 3} GiB cap",
                 "processes": len(pids), "dir": str(clone_home()), "screen": hwnd}
    _STATUS_CACHE.update(at=now, value=value)
    return value


def _forget_status():
    _STATUS_CACHE.update(at=0.0, value=None)


def create():
    """First boot. Here it is a few seconds of grants, not an image download."""
    with _lock:
        provision()
        _forget_status()
        return start()


def start():
    with _lock:
        done = provisioned()
        if not done or done.get("runtime") != str(runtime()) or done.get("copy") != _this_copy():
            # A reinstall lays down new files; the grants are re-made for them.
            provision()
        if _desktop_hwnd():
            return status()
        _launch_desktop()
        _forget_status()
        return status()


def stop():
    """Everything the clone was running, ended — the job object does not miss
    a straggler, which `pkill -f` on the Mac sometimes did."""
    with _lock:
        winbox.stop_all()
        HWND_FILE.unlink(missing_ok=True)
        _session["desktop"] = None
        _forget_status()
        return status()


def _require_running():
    if status().get("state") != "running":
        raise _error("the sandbox is not running — press Start first")


# ─── running things inside ──────────────────────────────────────────────────

APPS = CLONE / "apps"


def _caps_for(name):
    mode = _mode()
    if mode == "open":
        return ("internetClient", "privateNetworkClientServer")
    if name == "browser_app.py" and mode == "browsing":
        return ("internetClient",)
    return ()


def launch_app(name, *args):
    """Start one of the clone's apps on its screen.

    Into the container and the job directly from here, not by asking the
    clone's desktop to do it — the same reason the Mac launched through
    limactl: it keeps working when clicking inside the sandbox does not.
    """
    _require_running()
    rt = _need_runtime()
    target = APPS / name
    if not target.exists():
        raise _error(f"{name} is not built for Windows")
    caps = _caps_for(name)
    proc = winbox.launch([rt / "python.exe", target, *args], caps=caps,
                         env=clone_env(), cwd=clone_home(), capture=True, window=False)
    _drain(proc, Path(name).stem)
    return {"launched": name, "pid": proc.pid, "network": list(caps) or "none"}


def _drain(proc, stem):
    """An app that dies on start should leave a trace somewhere, not just
    fail to appear: its output goes to var/logs/<app>.log."""
    logs = paths.VAR / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    def run():
        with open(logs / f"{stem}.log", "a", encoding="utf-8") as fh:
            fh.write(f"— {time.strftime('%Y-%m-%d %H:%M:%S')} pid {proc.pid} —\n")
            for line in proc.stdout:
                fh.write(line)
                fh.flush()
    threading.Thread(target=run, daemon=True).start()


def _clone_processes(match=None):
    out = []
    for pid in winbox.container_pids():
        line = winbox.command_line(pid)
        if match is None or match in line:
            out.append((pid, line))
    return out


def close_apps(name=None):
    """Close one app, or every app, leaving the clone's screen up."""
    target = name if name else "_app.py"
    for pid, line in _clone_processes(target):
        if "desktop.py" not in line:
            winbox.terminate(pid)
    time.sleep(0.5)
    left = len(_clone_processes("_app.py"))
    return {"closed": target, "still_open": str(left)}


def shell(script, user="clone", timeout=120):
    out = run_command(script, timeout=timeout)
    return {"code": out["code"], "out": out["out"], "err": out["err"]}


def run_command(command, timeout=45):
    """A command inside the sandbox, as the clone: cmd.exe in the container.

    Same honesty as on the Mac — not a terminal, no session that survives to
    the next command — and the same reach: the clone's home and shared/,
    nothing of yours, no network unless you opened it.
    """
    _require_running()
    rt = _need_runtime()
    try:
        # Through winshell.py, not cmd directly: inside an AppContainer cmd's
        # own `dir` is refused and PowerShell will not start, so the everyday
        # commands are answered in Python and the rest handed to cmd.
        r = winbox.run([str(rt / "python.exe"), "-I", "-X", "utf8",
                        str(VMDIR / "winshell.py"), command],
                       caps=_caps_for("shell"), env=clone_env(), cwd=clone_home(),
                       timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "", "err": "timed out"}
    return {"code": r["code"], "out": r["out"][-8000:].rstrip(), "err": ""}


def drive(action, text=""):
    """Type into the chat through UI Automation, from out here."""
    import hands
    if action == "ask":
        return {"result": hands.ask_chat(text)}
    return {"result": hands.read_chat()}


# ─── lessons ────────────────────────────────────────────────────────────────
# The executor runs HERE, on the host, not inside the container. UI
# Automation does not cross from an AppContainer into anything at all — not
# even the clone's own windows — so the hands have to be outside. What keeps
# them off your windows is hands.py: every action is addressed to a window
# handle whose process carries the container SID, checked right before it is
# sent, and nothing is ever typed into "whatever has focus".

def _executor(lesson_id, *flags):
    vm = _vm()
    if not vm.LESSON_ID.match(lesson_id or ""):
        raise _error(f"not a lesson id: {lesson_id!r}")
    folder = vm.TRACES / lesson_id
    if not (folder / "procedure.json").exists():
        raise _error(f"no lesson called {lesson_id!r}")
    return [host_python(), str(VMDIR / "executor.py"), str(folder), *flags]


def lesson_stop():
    vm = _vm()
    proc = vm._running
    was_running = proc is not None and proc.poll() is None
    if proc is not None:
        proc.terminate()
    return {"stopped": was_running,
            "detail": "stopped" if was_running else "nothing was running"}


def _launch_runner(runner, stamp):
    """A runner on one job, inside the container, not waited for. Its output
    goes to var/logs so a runner that dies says why somewhere."""
    rt = _need_runtime()
    vm = _vm()
    logs = paths.VAR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    proc = winbox.launch([rt / "python.exe", VMDIR / runner, vm.JOBS / stamp],
                         caps=_caps_for(runner), env=clone_env(), cwd=clone_home(),
                         capture=True, window=False)

    def drain():
        with open(logs / f"{Path(runner).stem}-{stamp}.log", "w", encoding="utf-8") as fh:
            for line in proc.stdout:
                fh.write(line)
    threading.Thread(target=drain, daemon=True).start()


def reset_display():
    """Rebuild the path between your eyes and the sandbox.

    Here that path is a window, not a VNC stack, and the only way it breaks
    is the desktop process dying. If it is alive this does nothing but say
    so; if it is gone it comes back. Apps that were on it went with it.
    """
    with _lock:
        if _desktop_hwnd():
            return {"desktop": "fine", "tuned": True,
                    "note": "the clone's screen is up — nothing needed rebuilding"}
        if status().get("state") == "absent":
            raise _error("the sandbox has not been created — press First boot")
        _launch_desktop()
        _forget_status()
        return {"desktop": "restarted", "tuned": True,
                "note": "the clone's screen was down and has been restarted"}


# ─── the undo button ────────────────────────────────────────────────────────
# What a rollback restores is the clone's own disk — its home, its settings,
# whatever it installed — and not shared/, exactly as on the Mac, where
# shared/ was a mount and never part of the VM image. The work the clone
# produced is versioned separately (history.py).
#
# A real copy, not a copy-on-write clone. NTFS has no user-level CoW, and a
# copy that says how long it took and how big it was is better than one that
# pretends to be instant.

SNAPS = paths.VAR / "snapshots"


def _copy_home(src, dst):
    def skip(folder, names):
        # Never into the junction: that is shared/, which is not ours to copy,
        # and following it would also mean a rollback DELETING shared/.
        return [n for n in names if os.path.isjunction(os.path.join(folder, n))]
    shutil.copytree(src, dst, ignore=skip, symlinks=True, dirs_exist_ok=True)


def _clear_home(home):
    link = home / "shared"
    if os.path.isjunction(link):
        os.rmdir(link)                      # removes the link, never the target
    if home.exists():
        shutil.rmtree(home)


def snapshot(tag=None):
    vm = _vm()
    tag = tag or vm.CLEAN_TAG
    with _lock:
        was_running = status().get("state") == "running"
        if was_running:
            stop()
        began = time.time()
        target = SNAPS / tag
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        _copy_home(clone_home(), target / "home")
        (target / "taken").write_text(str(time.time()), encoding="utf-8")
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        if was_running:
            start()
        return {"tag": tag, "at": time.time(), "bytes": size,
                "seconds": round(time.time() - began, 2)}


def rollback(tag=None):
    vm = _vm()
    tag = tag or vm.CLEAN_TAG
    src = SNAPS / tag / "home"
    if not src.exists():
        raise _error(f"no snapshot tagged {tag!r} — mark one clean first")
    with _lock:
        was_running = status().get("state") == "running"
        stop()
        home = clone_home()
        _clear_home(home)
        _copy_home(src, home)
        if was_running:
            start()
        return {"tag": tag, "at": time.time(), "snapshot_taken": _snapshot_age(tag)}


def _snapshot_age(tag=None):
    tag = tag or _vm().CLEAN_TAG
    stamp = SNAPS / tag / "taken"
    if not stamp.exists():
        return None
    return time.strftime("%d %b %H:%M", time.localtime(stamp.stat().st_mtime))


def snapshots():
    if not SNAPS.exists():
        return []
    return sorted(p.name for p in SNAPS.iterdir() if (p / "home").exists())


def resolved_config():
    """No lima.yaml here. The equivalent — what the sandbox may touch — is
    the grant list, written by provision() to var/sandbox.json."""
    return PROVISIONED


# ─── the sandbox's share of the machine ─────────────────────────────────────
# The dock's CPU and RAM dials. Remembered in var/, applied to the job when it
# is made and again the moment they change (winbox.set_limits).

LIMITS_FILE = paths.VAR / "limits.json"


def limits():
    return {**winbox.LIMITS, "machine_ram": winbox.machine_memory_gb(),
            "cpus": os.cpu_count()}


def set_limits(cpu=None, ram=None):
    winbox.set_limits(cpu, ram)
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIMITS_FILE.write_text(json.dumps({"cpu": winbox.LIMITS["cpu"], "ram": winbox.LIMITS["ram"]}),
                           encoding="utf-8")
    return limits()


def _load_limits():
    try:
        saved = json.loads(LIMITS_FILE.read_text(encoding="utf-8"))
        winbox.set_limits(saved.get("cpu"), saved.get("ram"))
    except (OSError, ValueError):
        pass


_load_limits()


# ─── the network ────────────────────────────────────────────────────────────
# Policy and measurement are kept apart, and only one of them is believed.
#
# The MODE is what programs started from now on will be given: locked (no
# network capability), browsing (the browser gets internetClient), ssh (the
# ssh client gets it), open (everything does). It has to be remembered —
# there is no firewall table to read it back from.
#
# Whether the sandbox can reach the network RIGHT NOW is measured: every
# process in the container, its token, its capabilities. That is what the
# Egress pill shows. A process holding a capability while the mode says
# locked is reported as open, because it is.

NET = paths.VAR / "network.json"
MODES = ("locked", "browsing", "ssh", "open")


def _mode():
    try:
        mode = json.loads(NET.read_text(encoding="utf-8")).get("mode")
        return mode if mode in MODES else "locked"
    except (OSError, ValueError):
        return "locked"


def _set_mode(mode):
    NET.parent.mkdir(parents=True, exist_ok=True)
    NET.write_text(json.dumps({"mode": mode, "at": time.time()}), encoding="utf-8")
    flag = paths.SHARED / ".browsing"
    try:
        flag.write_text("on" if mode in ("browsing", "open") else "off", encoding="utf-8")
    except OSError:
        pass


def _drop_holders():
    """End every clone process that holds a network capability. The only way
    to take one back: a token's capabilities are fixed for its lifetime."""
    ended = []
    for p in winbox.processes():
        if p["caps"]:
            winbox.terminate(p["pid"])
            ended.append(p["exe"])
    return ended


def egress_state(fresh=False):
    measured = winbox.network_state()
    if measured["locked"] is None:
        return {"locked": None, "detail": "sandbox is not running"}
    mode = _mode()
    if measured["locked"] and mode in ("locked", "browsing", "ssh"):
        return {"locked": True, "mode": mode,
                "detail": "no clone process holds a network capability — only the "
                          "model pipe crosses" + (f" ({mode} may be granted on request)"
                                                  if mode != "locked" else "")}
    if measured["locked"]:
        return {"locked": False, "mode": mode,
                "detail": "unlocked — every program the clone starts from now on "
                          "gets the network; none running has it yet"}
    return {"locked": False, "mode": mode, "detail": measured["detail"]}


def _relock_if_needed():
    try:
        if _mode() == "locked" and egress_state().get("locked") is False:
            _drop_holders()
    except Exception:
        pass


def egress(locked: bool):
    if locked:
        _set_mode("locked")
        _drop_holders()
    else:
        _set_mode("open")
    return egress_state(fresh=True)


def browsing_state(fresh=False):
    mode = _mode()
    holders = [p for p in winbox.processes() if "internetClient" in p["caps"]]
    allowed = mode in ("browsing", "open")
    if allowed:
        detail = ("the browser holds the web now" if holders
                  else "allowed — the browser gets the web when it opens")
    else:
        detail = "the web is blocked" if not holders else \
            "blocked by policy, but something still holds internetClient"
    return {"browsing": allowed, "detail": detail}


def browsing(on: bool):
    """Allow the web for the browser, and nothing else.

    A browser already open was started without the capability and cannot be
    given it, so it is restarted — the honest alternative to a browser that
    claims browsing is on and then fails to load anything.
    """
    if not on:
        return egress(True) and browsing_state(fresh=True)
    _set_mode("browsing")
    was_open = _clone_processes("browser_app.py")
    for pid, _ in was_open:
        winbox.terminate(pid)
    if was_open and status().get("state") == "running":
        launch_app("browser_app.py")
    return browsing_state(fresh=True)


def ssh_state(fresh=False):
    on = _mode() == "ssh"
    return {"ssh": on,
            "detail": "the sandbox may open SSH connections" if on
                      else "SSH out of the sandbox is blocked"}


def ssh_out(on: bool):
    if not on:
        egress(True)
        return ssh_state(fresh=True)
    _set_mode("ssh")
    return ssh_state(fresh=True)


SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"


def ssh_run(row, command, timeout=45):
    """One command on a saved host, run by ssh.exe inside the container.

    The key file, if one is named, is granted to the container read-only —
    that one file, at the moment of use. Without it the clone could not
    authenticate; with more than it, the grant would be a hole.
    """
    import hosts
    if not ssh_state()["ssh"]:
        return {"code": -1, "out": "",
                "err": "SSH out of the sandbox is switched off. Turn it on first — "
                       "it is a deliberate hole in the egress lock, and switching "
                       "it on is recorded in the ledger."}
    if not SSH.exists():
        return {"code": -1, "out": "", "err": "the Windows OpenSSH client is not installed"}
    argv = hosts.target(row)
    argv[0] = str(SSH)
    if row.get("key") and Path(row["key"]).is_file():
        _grant_file(row["key"])
    try:
        r = winbox.run(argv + [command], caps=("internetClient", "privateNetworkClientServer"),
                       env=clone_env(), cwd=clone_home(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "", "err": "timed out"}
    return {"code": r["code"], "out": r["out"][-8000:].rstrip(), "err": ""}
