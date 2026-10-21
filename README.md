<div align="center">

# SHISHYA · शिष्य

**An apprentice that watches you do a job once, writes down what it learned
in words you can edit, and then does that job — on your own laptop, sandboxed,
with the network switched off.**

[![repo](https://img.shields.io/badge/github-FireFoxWay%2Fshishya-1B1B3A?logo=github)](https://github.com/FireFoxWay/shishya)
[![runs](https://img.shields.io/badge/runs-entirely%20offline-2F7A55)](#the-network-claim)
[![model](https://img.shields.io/badge/model-local%20via%20Ollama-3A3A7C)](#the-model)
[![python](https://img.shields.io/badge/python-3.12%20stdlib%20only-C68414?logo=python&logoColor=white)](#run-it)
[![sandbox](https://img.shields.io/badge/sandbox-Lima%20%C2%B7%20Apple%20vz-6E6A63)](#how-it-holds-together)

</div>

## Download for Windows

**[⬇ Download Shishya-Setup-1.0.7.exe](https://github.com/FireFoxWay/Shishya-Windows/releases/latest/download/Shishya-Setup-1.0.7.exe)**
(125 MB, Windows 10/11, per-user install, no administrator rights needed)

Prefer no installer? Get the
[portable zip](https://github.com/FireFoxWay/Shishya-Windows/releases/latest/download/Shishya-1.0.7-portable.zip)
(188 MB), unzip it anywhere, and run `Shishya.exe`. All versions are on the
[Releases page](https://github.com/FireFoxWay/Shishya-Windows/releases).

---

## What it is

It is not RPA and it is not an autonomous agent.

| | how it works | why it breaks |
|---|---|---|
| **RPA** (UiPath, Automator) | replays recorded coordinates | move a window, resize a column, change a value — it shatters |
| **Autonomous agents** | re-derive the task from scratch each run | unpredictable, unauditable, and confidently wrong |
| **Shishya** | compiles your demonstration into a **plain-language procedure**, then executes it against the live accessibility tree | you can read what it learned, and correct it |

That middle artefact is the whole idea. A lesson comes out looking like this,
and you can edit any line:

```
1. Open the applicant PDF from the intake folder
2. Read the applicant's name, ID number, and district
3. In the registry spreadsheet, find the row matching that ID
4. If the district doesn't match, flag it and stop
5. Otherwise mark the row verified and save
```

Not `click at (412, 890)`. A clerk can read that. So can a judge.

---

## Run it

```bash
python3 doctor.py            # what works on this machine, and what does not
brew install lima            # once
python3 -c "import host.vm as vm; vm.create()"   # first boot: slow
./demo.sh                    # everything, ready
```

Then open <http://localhost:8200>.

`vm.create()` rather than `limactl start … vm/lima.yaml` directly: the mount
list in that file **is** the security boundary, so it is a template carrying
`__SHISHYA_ROOT__` and gets resolved against *your* checkout into `var/`. It
used to name one developer's home directory in full, which meant it described
a boundary that existed on exactly one laptop.

`./demo.sh` brings the whole stack up in dependency order and is the thing to
run when something looks wrong. It exists because a dead Xvfb presents as
"input is broken" rather than "there is no display" — every symptom points at
the wrong layer, and guessing which component failed wastes the time you do
not have. `./demo.sh reset` also rolls the sandbox back to clean.

### The terminal

The panel is a browser on localhost, which is awkward when the laptop is in
one country and you are in another. `./shishya` is not:

```bash
ln -sf "$PWD/shishya" /opt/homebrew/bin/shishya    # once
shishya                                            # the loop
shishya board                                      # or one thing, and exit
shishya sum ~/Downloads/sales.xlsx
shishya ask "what did we take last week?"
```

Four things, and the restraint is the design: summarise a document already on
the machine, summarise one you name, read the dashboard as text, and ask it
things. Teaching a procedure and watching the clone work need to be *seen* and
stay in the panel.

Standard library only, because this runs on a machine you have just `ssh`'d
into. Colour switches off when it is not a terminal, so `shishya board >
today.txt` gives you plain text.

### On Windows

**Windows is a first-class platform, and it needs no VM.** Run
[`Shishya-Setup-<version>.exe`](#download-for-windows): a per-user install, no administrator rights,
and nothing else to install — except Ollama, for the model. Shishya appears
in Start; the clone lives only while its window is open.

The sandbox is made from Windows' own isolation instead of a virtual machine,
because Windows 11 Home has no Hyper-V and the VM was never the point — the
three properties were:

| The property | On the Mac | On Windows |
|---|---|---|
| it cannot read what it was not given | the Lima mount list | an **AppContainer**: every file refuses the clone's token unless its ACL names it. `shared\` is granted read-write, the runtime and the code read-only, the ledger read-only; your Documents are not, so the kernel says no |
| it cannot reach the network unless you say so | nftables inside the guest | **capabilities in the process token**. None means none — not even loopback. Browsing starts the browser with `internetClient`; locked means no process holds one. The pill is *measured*, from every live process's token |
| the model, and nothing else, crosses | one hole in the firewall | **a named pipe** whose ACL names the container (`host/broker.py`). It forwards questions to Ollama and refuses everything else; adding a model needs the head |
| it cannot touch your windows | it had its own X display | the clone's screen is one 1440x900 window with every app parented into it — **cloaked**, so it never appears on your desktop or taskbar; Shishya shows it inside its own window, a swipe to the right of the home screen, and forwards your clicks, drags, wheel and typing to it. Its **hands** (`host/hands.py`) address every action to a window handle whose process carries the container SID — checked before anything is sent — and never type into "whatever has focus" |
| stopping it stops all of it | `limactl stop` | one **Job Object** around everything the clone runs, killed together, with an 8 GB memory ceiling |

What changed to get there, so nobody has to rediscover it:

- **The clone's apps are Qt** (`clone/`), not GTK — native on Windows,
  published to UI Automation the way GTK was to AT-SPI. Their logic did not
  move: it was split out of the GTK files into `vm/shell/{library,documents,
  codegen,checks}.py` and both front ends use it. The Mac build is unchanged.
- **Sheets is the clone's spreadsheet** — Name Box, A1 references, Enter
  moves down — because lessons are taught and performed against the same
  application, and LibreOffice is not part of the install.
- **The recorder lives inside each app** (`clone/kit.py`), because UI
  Automation does not cross out of an AppContainer and the only outside
  alternative is a global keyboard hook, which would also hear *your* typing.
  It writes the same trace format, so `host/synth.py` is unchanged.
- **The executor runs on the host** (`vm/executor.py` with `host/hands.py`),
  for the same reason. It goes to a cell through the Name Box and types
  through the cell-contents line, then reads the cell back through UIA's
  Grid pattern. A step it cannot verify still stops the run.
- **Snapshots copy the clone's home** (its container storage). NTFS has no
  user-level copy-on-write; a copy that says how long it took beats one that
  pretends to be instant.

Building it: `powershell -ExecutionPolicy Bypass -File windows\build.ps1`
makes `Shishya\` (an embeddable CPython with every package, the source, a
small C# launcher) and the installer, with Inno Setup 6. Running from a
checkout works too — `python serve.py` — with `SHISHYA_RUNTIME` pointing at
a Python that has PySide6, at a path shorter than Windows' 260 characters.

One thing to know before you write a line: **Windows defaults text files to
cp1252, not UTF-8**, and every price in this system carries a `₹`. Saving a
bill on a machine with a cp1252 locale used to raise `UnicodeEncodeError` in
the middle of writing the file. Every text read and write in the repository
now names `encoding="utf-8"` explicitly, and `doctor.py` walks the AST and
fails if any call has stopped doing so. **Keep it that way** — the failure is
silent on a Mac and only shows up on your machine.

**Not done on Windows yet**, said plainly: OCR of scanned PDFs (pypdf reads
text layers; scans need tesseract, which is not bundled), the GnuCash / GIMP /
Inkscape slots of the Mac's menu, and SSH out of the sandbox, which can only
be "allowed" as a whole capability rather than port 22 alone — Windows does
not let an ordinary user write a firewall rule for an AppContainer.

---

## What the clone has

An empty desktop with an **Apps** button in the bottom-left corner.

| | |
|---|---|
| **Files** | browse Work, Inbox, Training Data, Lessons |
| **Inbox** | files sent in from the host — the only way in |
| **Training Data** | drop in any document, then ask about it |
| **Shishya** | teach a lesson, read it, edit it, run it |
| **Make a Document** | describe what you need; get a real `.xlsx`, `.docx` or `.pptx` |
| **Sheets** | open and edit spreadsheets instantly |
| **LibreOffice** | on the Mac only; on Windows, Sheets is what lessons drive |
| **Activity** | the permission ledger, seen from inside |
| **Settings** | wallpaper, accent, icon size — applied live |

Shut down is at the bottom of the Apps menu rather than being the corner
button itself. The corner is where a hand goes without looking, and that
should not be the control that ends the session.

---

## How it holds together

```
YOUR MAC                          │  THE SANDBOX
                                  │
  control panel  (browser)        │    Xvfb + openbox      1440×900, frozen
  serve.py       stdlib only      │    AT-SPI tree         widget names, not pixels
  Ollama         on the GPU       │    LibreOffice         warm, gtk3, accessible
  shared/        the only way in  │    recorder → executor runs as `clone`, no sudo
                                  │
                            hypervisor
```

Exactly three things cross that line: **the screen out** (`:6080`), **the
model in** (`:11434`), and **one shared folder** (virtiofs). Nothing else.

### Three things that are load-bearing

**The mount list is the security model.** The clone cannot read `~/Documents`
because it is not mounted — not because we check a path somewhere. Grant a
folder by adding a line to `vm/lima.yaml`; revoke it by deleting the line.

**The accessibility tree, not pixels.** The executor resolves "the field
labelled *District*" instead of guessing at a screen region. LibreOffice
exposes its grid cell by cell — `A1`, `B1`, `C1` — so a procedure can name a
cell the way a person would. If a recording comes back empty, run
`python3 vm/tree.py` inside the sandbox: that is the tool for it.

**The display resolution is frozen at 1440×900.** Recorded traces reference
on-screen layout. Changing the resolution after recording starts invalidates
every trace already captured.

### The model

Runs on the **host** via Ollama, reached from inside at
`host.lima.internal:11434`. Deliberately not inside the VM: a model on
emulated CPU instead of the Apple Silicon GPU is slow enough to sink a live
demonstration.

On Windows, Shishya starts Ollama itself if it is installed and not
running, and reaches it through a named pipe only the sandbox may use.

**Models for each job.** Every job (answering, code, documents) names its
own model on the clone's Models screen. Open models that suit this machine:

| Model | Licence | For |
|---|---|---|
| `qwen3:4b` | Apache-2.0 | quick answers, routing, documents |
| `qwen2.5-coder:7b` | Apache-2.0 | writing code |
| `deepseek-r1:8b` | MIT | careful answers; lessons into steps |
| `gpt-oss:20b` | Apache-2.0 | large and general (no JSON-shaped output under Ollama) |

`host/tune.py` makes Shishya's own versions of them (`shishya-code`,
`shishya-answers`, `shishya-documents`) with each job's rules built in,
using Ollama's create, so nothing is retrained and nothing leaves the
machine; Shishya makes them at start for any base that is downloaded. The
Models screen's **Test them on Shishya's jobs** runs every installed model
on each job's real tasks inside the sandbox (`vm/shell/jobbench.py`) and
gives each job the model that got the most right, fastest.

### Trained models

Each job's model is fine-tuned for Shishya's own work and published with the
release: tasks with known answers, only replies that pass the job's own check
(a script that prints the right answer in the sandbox; an answer that quotes
the right fact or honestly declines; a document with every given fact in it),
a LoRA trained on those, and a comparison on held-out material against the
model it would replace before it is shipped. The Models screen's **Get
Shishya's trained models** downloads them and gives each its job; the
results are in each release's notes. On material none of them trained on:

| Job | Trained model | Built on | Right | The model it replaces |
|---|---|---|---|---|
| Code | `shishya-code-trained` | Qwen2.5-Coder 7B | 74% | 59% |
| Answers | `shishya-answers-trained` | DeepSeek-R1-0528-Qwen3 8B | 100% (89 of 89) | 99% (`deepseek-r1:8b`) |
| Documents | `shishya-documents-trained` | Qwen3 4B Instruct | 82% (49 of 60) | 75% (`shishya-answers`) |
| Answers, larger | `shishya-oss-trained` | gpt-oss 20B | 87% (71 of 82) | 82% (`gpt-oss:20b`) |

The gpt-oss model is 14 GB and needs a 16 GB graphics card; it takes no job
by itself — pick it for Answers on the Models screen if your machine runs it.

### VS Code

*Not in the release downloads yet* — build with `windows/build.ps1` to include it.
The real VS Code is bundled and runs **inside the sandbox** like every other
app: no network, its settings and extensions in the clone's own home,
`shared\work` as its folder, its window on the clone's screen. Open it from
the clone's start panel (VS Code).

VS Code is where you run the clone's OS; the Code app stays for writing
scripts for your work. Its tasks (Terminal → Run Task) and shortcuts open
the clone's apps and run taught lessons — Ctrl+Alt+B Billing, Ctrl+Alt+F
Files, Ctrl+Alt+I Inbox, Ctrl+Alt+S the chat, Ctrl+Alt+T teach, Ctrl+Alt+L
run a lesson — and `shishya` in its terminal does the same (`shishya help`).
Add your own tasks and shortcuts the same way: each is one `shishya` command,
and the host checks and logs every one.

It needs one step Windows only lets an administrator take. Node, which VS
Code runs on, looks at the root of C: before it opens any file, and a
sandboxed app may not by default. Run this once, in a terminal opened as
administrator. It lets Shishya's sandbox read the attributes of `C:\`
itself, and nothing else: no folder listing, no files, nothing below it.

```
icacls C:\ /grant "*S-1-15-2-1700322577-3344956600-3688150473-1092480530-2905792460-468937178-3008308160:(S,RA)"
```

Keep the quotes: without them PowerShell reads `(S,RA)` as code and the
grant does not happen. It should end with "Successfully processed 1 files".

Undo it with `icacls C:\ /remove *S-1-15-2-1700322577-3344956600-3688150473-1092480530-2905792460-468937178-3008308160`.
Uninstalling Shishya does not remove it (that needs an administrator too).

### The network claim

`host/vm.py:egress(True)` drops all outbound traffic from the sandbox except
the host's model, applied inside the guest where the clone has no sudo and
cannot lift it. Verified: `github.com` blocked, `1.1.1.1` blocked, the model
still answering.

The indicator in the panel is **measured** on every poll by reading nftables
in the guest — it has three states, because *unknown* is its own answer when
the sandbox is down. It used to be a variable initialised to `true`, which
displayed `LOCKED` while nothing was actually blocked.

**And it survives a reboot.** nftables rules live only in the running kernel,
so until 6 September a sandbox that came back after the Mac slept came back
*open* — found only because the panel asks the guest instead of remembering,
and it said `OPEN`. `shishya-egress.service` re-applies the lock before the
network comes up, so there is no window in which the sandbox is running and
unlocked. That measured indicator is the reason the bug was ever visible; an
interface that reported what it had been told would have hidden it for
months.

### The undo button

Whole-VM snapshots, so a rollback also catches the app that had a document
open and unsaved.

Not `limactl snapshot` — that is unimplemented for the `vz` driver, which
stores a raw disk. macOS offers something better: the disk lives on APFS, so
`cp -c` clones it copy-on-write. A 3.2 GB image clones in about **4
milliseconds** and costs no extra space until blocks diverge.

---

## Layout

```
shishya              the terminal — ask, summarise, read the dashboard
doctor.py            what works on THIS machine, and what does not
demo.sh              bring everything to a known-good state
serve.py             control panel server + JSON API

host/vm.py           lima wrapper: snapshot, rollback, egress lock, ssh mode
host/brain.py        asking the model and reading a file, host-side
host/dashboard.py    the overview screen's data, assembled in one call
host/profile.py      who is using this and what they do for a living
host/hosts.py        saved ssh hosts — never a password, only a key path
host/ledger.py       append-only audit log, including refusals
host/vault.py        credentials, kept OUTSIDE this repo on purpose
host/synth.py        trace → editable plain-language procedure

vm/lima.yaml         the sandbox — its mount list IS the boundary
vm/egress.sh         the network lock, re-applied before the network is up
vm/forecast.py       demand forecasting, written from scratch in numpy
vm/provision.sh      builds the machine, once
vm/display.sh        Xvfb, openbox, x11vnc, noVNC — re-runnable
vm/recorder.py       records AT-SPI events, not mouse coordinates
vm/executor.py       performs a learned procedure against live apps
vm/locate.py         find a control by name → a point on screen

vm/shell/books.py    the shop: stock, sales, margin, GST, void, returns
vm/shell/invoice.py  GST tax invoices
vm/shell/money.py    accounts, commitments, what a credit score is made of
vm/shell/            the desktop and its apps

web/                 the control panel — plain ES modules, no build step
```

Stdlib Python and plain ES modules throughout. Nothing that has to resolve a
dependency at a demo table.

---

## Status

**Works, and is checked.** On Windows, `runtime\python.exe windows\check.py` (with
Shishya open) exercises 23 advertised features for real and prints PASS or FAIL
for each; on the Mac, `./demo.sh check` runs 24 assertions in about a
minute — including typing a key into a real window and reading it back, and
asking the *guest* whether the egress lock is on rather than remembering
what it was told.

The sandbox and the clone's desktop; take-the-wheel; APFS snapshot and
rollback; the permission ledger; the egress lock; document Q&A over dropped
files; document generation (`.xlsx` / `.docx` / `.pptx`); **the executor**,
which performs a taught procedure against live LibreOffice and reads each
cell back to check it landed; GST billing, tax invoices, void and returns;
stock audit, scheduled runs, alerts, backups; approvals (clerk/head roles are
in the code; the switch is hidden while one person runs a copy); a Tally XML
round trip; the shop screens (billing counter and bills, banking, credit,
reports, findings); and the terminal.

**It trains its own model.** Running someone else's model locally is ordinary
now. `vm/forecast.py` trains a demand forecaster on the shop's own purchase
history, on the shop's own laptop — forward pass, backpropagation, momentum
and initialisation all written out, numpy doing array arithmetic and nothing
else. Measured against the baselines that matter, on a month it never saw:

| On a held-out month | MAE |
|---|---|
| **this model** | **7.469** |
| same as last month | 10.862 |
| recent average | 12.572 |

**31.2% better than the best baseline.** 12,079 products, 17 months, 120,790
training windows, **3.9 seconds** to train. Nothing uploaded. A forecaster
that cannot beat *"next month looks like last month"* is worse than useless,
so `forecast.py check` prints that comparison whether or not it flatters us.

**Not proved yet, which is a different thing.**

- **Nobody outside this project has used it.** Every bill so far was rung up
  by a script. One shopkeeper for one afternoon is worth more than any
  feature still on the list.
- **Tally has never accepted the XML.** It passes every check that can run
  here; that is not the same claim. It needs a Windows machine.
- **Nobody who reads Telugu has read the Telugu answers**, and the English
  and Telugu versions are known to differ in emphasis. Marked *do not
  demonstrate* until they have.
- **The egress lock does not survive a VM restart.** nftables rules are
  runtime-only and nothing re-applies them at boot, so a sandbox that comes
  back after the Mac slept comes back *open*. The panel reports this
  honestly, which is the only reason it was found. Until a boot service
  fixes it, locking is a step in the pre-flight rather than a property of
  the machine.
- **Keyboard injection into the sandbox can die on its own.** Mouse fine,
  queries fine, every typed key silently swallowed — which stops the
  executor dead. It happened once while the machine sat idle. `demo.sh
  check` types a probe and catches it; `vm/display.sh --all` fixes it.

**Not public, and not yet publishable.** `shared/training/` holds a real
pharmacy's purchase history, and it is in git history rather than only in the
working tree. That comes out with `git filter-repo` before this repository
goes anywhere.
