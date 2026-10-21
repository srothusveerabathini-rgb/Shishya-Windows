#!/usr/bin/env python3
"""
Shishya's trained models, fetched from the release and put into Ollama.
Runs on the HOST — the sandbox has no network and never will for this.

A model is too large for one release file (GitHub allows 2 GB), so each is
published in parts with a manifest, models.json:

    {"version": "1.0.5", "models": [{"name": "shishya-code-trained", "job": "code",
      "size": 4683071488, "sha256": "...", "parts": ["shishya-code-trained.gguf.part0", ...],
      "modelfile": "TEMPLATE ...", "score": "74% vs 59%"}]}

get() downloads each part, joins them, checks the whole file's sha256 against
the manifest — a file that does not match is deleted, never imported — writes
a Modelfile pointing at it, runs `ollama create`, and gives the model its job
on the Models screen. Progress is in var/models-get.json for the panel.
"""

import hashlib
import http.client
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import ledger
import paths

RELEASE = "https://github.com/FireFoxWay/Shishya-Windows/releases/latest/download/"
STORE = paths.DATA / "models"
STATE = paths.SHARED / ".models-get.json"   # in shared/, so the clone's Models app can show it
ROLES = paths.SHARED / ".models.json"
OLLAMA = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
_lock = threading.Lock()


def _say(**kw):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({**kw, "at": time.time()}), encoding="utf-8")


def state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}


def manifest():
    with urllib.request.urlopen(RELEASE + "models.json", timeout=30) as r:
        return json.loads(r.read())


def _fetch(url, dest, label, done_bytes, total, tries=8):
    """One release file, checked against its length. A connection that drops
    — the laptop sleeping, the Wi-Fi changing — can end a read early with no
    error at all, so a short file is resumed from where it stopped (a Range
    request) instead of being joined in."""
    tmp = dest.with_suffix(dest.suffix + ".part-download")
    tmp.unlink(missing_ok=True)
    want = None
    for attempt in range(tries):
        have = tmp.stat().st_size if tmp.exists() else 0
        if want is not None and have == want:
            break
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "ab") as fh:
                if have and r.status != 206:         # the server sent the whole file again
                    fh.truncate(0)
                    have = 0
                want = have + int(r.headers["Content-Length"])
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
                    done_bytes[0] += len(chunk)
                    if done_bytes[0] % (64 << 20) < (1 << 20):
                        _say(state="downloading", model=label, pct=round(100 * done_bytes[0] / max(1, total)))
        except (OSError, http.client.HTTPException, KeyError, ValueError):
            time.sleep(min(60, 5 * (attempt + 1)))
    if want is None or tmp.stat().st_size != want:
        raise RuntimeError(f"{dest.name} did not download completely — try again")
    tmp.replace(dest)


def _stay_awake(on):
    """Hold off idle sleep while models download (several GB); per thread."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
    except (AttributeError, OSError):
        pass


def _install(m, done_bytes, total):
    STORE.mkdir(parents=True, exist_ok=True)
    gguf = STORE / f"{m['name']}.gguf"
    if not (gguf.exists() and gguf.stat().st_size == m["size"]):
        h = hashlib.sha256()
        with open(gguf, "wb") as out:
            for part in m["parts"]:
                piece = STORE / part
                # A model unchanged since an earlier release stays in that one.
                _fetch((m.get("base") or RELEASE) + part, piece, m["name"], done_bytes, total)
                with open(piece, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        out.write(chunk)
                        h.update(chunk)
                piece.unlink()
        if h.hexdigest() != m["sha256"]:
            gguf.unlink(missing_ok=True)
            raise RuntimeError(f"{m['name']} did not match its checksum — not installed")
    mf = STORE / f"{m['name']}.Modelfile"
    mf.write_text(f"FROM {gguf}\n{m['modelfile']}\n", encoding="utf-8")
    _say(state="installing", model=m["name"])
    r = subprocess.run([str(OLLAMA), "create", m["name"], "-f", str(mf)], capture_output=True,
                       text=True, creationflags=0x08000000)
    if r.returncode:
        raise RuntimeError(f"ollama create {m['name']} failed: {(r.stderr or r.stdout)[-200:]}")


def get(only=None):
    """Download and install every trained model in the latest release (or
    those named), then give each its job. One run at a time."""
    if not _lock.acquire(blocking=False):
        return {"error": "already downloading"}
    _stay_awake(True)
    try:
        man = manifest()
        models = [m for m in man.get("models", []) if not only or m["name"] in only]
        total = sum(m["size"] for m in models)
        done, installed, failed = [0], [], []
        ledger.record(ledger.ALLOW, "models.get", f"{len(models)} model(s), {total / 1e9:.1f} GB",
                      actor="guru", detail=f"from release {man.get('version')}")
        for m in models:
            # One model failing does not hold back the others, and each takes
            # its job as soon as it is in.
            try:
                _install(m, done, total)
            except Exception as e:                      # noqa: BLE001
                failed.append(f"{m['name']}: {str(e)[:150]}")
                ledger.record(ledger.DENY, "models.get.fail", m["name"], actor="system", detail=str(e)[:200])
                continue
            installed.append(m["name"])
            if m.get("job"):
                try:
                    roles = json.loads(ROLES.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    roles = {}
                roles[m["job"]] = m["name"] + ":latest"
                ROLES.write_text(json.dumps(roles, indent=2), encoding="utf-8")
        if failed:
            _say(state="failed", models=installed, error="; ".join(failed)[:300], version=man.get("version"))
        else:
            _say(state="done", models=installed, version=man.get("version"))
        return state()
    except Exception as e:                              # noqa: BLE001
        _say(state="failed", error=str(e)[:300])
        ledger.record(ledger.DENY, "models.get.fail", type(e).__name__, actor="system", detail=str(e)[:200])
        return state()
    finally:
        _stay_awake(False)
        _lock.release()


def get_in_background(only=None):
    threading.Thread(target=get, args=(only,), daemon=True, name="models-get").start()
    return {"started": True}
