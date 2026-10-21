#!/usr/bin/env python3
"""
Models — choose which AI does which job, and hold only what you need.
Runs INSIDE the sandbox.

Each job names its own model, and you install only the ones you use. Adding
or removing a model goes through the model pipe like everything else — and
the broker on the host allows it only when the person at the machine is
working as the head, and writes it to the ledger. Downloading is a decision
somebody makes on purpose, not something the sandbox can do for itself.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit   # noqa: E402
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QTableWidget, QTableWidgetItem,   # noqa: E402
                               QVBoxLayout, QWidget)

import common   # noqa: E402

JOBS = [("answers", "Answering questions",
         "Reads your documents and answers from them. Wants care, not size."),
        ("code", "Writing code",
         "Writes the small programs that do the counting. A code-trained model earns its space here."),
        ("documents", "Writing documents",
         "Drafts notes, letters and decks. Follows structure more than it reasons.")]
# Open models (Apache-2.0 or MIT) that run on this machine. The shishya-*
# models are made here from these by host/tune.py, not downloaded, and
# vm/jobbench.py measures which one each job should use.
CATALOGUE = [("qwen3:4b", "2.5 GB", "Small and very quick. Answers, routing, documents."),
             ("qwen2.5-coder:7b", "4.7 GB", "Trained on code. Best for the Code app."),
             ("deepseek-r1:8b", "5.2 GB", "Reasons step by step. Turns lessons into steps."),
             ("gpt-oss:20b", "13 GB", "OpenAI's open model. Large and careful; wants a GPU."),
             ("phi4-mini", "2.5 GB", "Microsoft's small model. Quick general answers."),
             ("llama3.2:3b", "2.0 GB", "Small, general purpose.")]


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setStretchLastSection(True)
    t.setColumnWidth(0, 200)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.verticalHeader().hide()
    return t


class ModelsApp(kit.Window):
    def __init__(self):
        super().__init__("Models", 780, 660)
        self.installed = {}
        try:
            self.choice = json.loads(common.ROLES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.choice = {}
        root = QVBoxLayout(self.body)
        root.setContentsMargins(14, 12, 14, 12)
        head = QHBoxLayout()
        head.addWidget(kit.label("Models", role="title"))
        self.total = kit.label("", role="muted")
        head.addWidget(self.total, 1)
        head.addWidget(kit.button("Refresh", self.reload))
        root.addLayout(head)

        root.addWidget(kit.label("WHICH MODEL DOES WHICH JOB", role="section"))
        self.pickers = {}
        for job, text, why in JOBS:
            row = QHBoxLayout()
            words = QWidget()
            col = QVBoxLayout(words)
            col.setContentsMargins(0, 0, 0, 0)
            col.addWidget(kit.label(text))
            col.addWidget(kit.label(why, role="muted", wrap=True))
            row.addWidget(words, 1)
            pick = QComboBox()
            pick.setAccessibleName(f"Model for {text}")
            pick.setMinimumWidth(220)
            pick.currentTextChanged.connect(lambda name, j=job: self.on_pick(j, name))
            self.pickers[job] = pick
            row.addWidget(pick)
            root.addLayout(row)

        root.addWidget(kit.label("INSTALLED ON THIS MACHINE", role="section"))
        self.view = table(["Model", "Size", "Used for"])
        self.view.setAccessibleName("Installed models")
        root.addWidget(self.view, 1)
        root.addWidget(kit.label("AVAILABLE TO ADD", role="section"))
        self.avail = table(["Model", "Size", "What it is for"])
        self.avail.setAccessibleName("Available models")
        root.addWidget(self.avail, 1)

        feet = QHBoxLayout()
        self.status = kit.label("", role="muted", wrap=True)
        feet.addWidget(self.status, 1)
        feet.addWidget(kit.button("Get Shishya's trained models", self.on_get_trained))
        feet.addWidget(kit.button("Test them on Shishya's jobs", self.on_test))
        feet.addWidget(kit.button("Remove", self.on_remove))
        feet.addWidget(kit.button("Add selected", self.on_add, primary=True))
        root.addLayout(feet)
        self.loading = False
        self.reload()

    def reload(self):
        try:
            data = common.model_request("GET", "/api/tags", timeout=10)
        except common.ModelUnreachable as e:
            self.status.setText(f"Can't reach the model service: {e}")
            return
        self.installed = {m["name"]: m.get("size", 0) for m in data.get("models", [])
                          if "cloud" not in m["name"]}
        used = {}
        for job, text, _ in JOBS:
            used.setdefault(self.choice.get(job) or common.MODEL, []).append(text.split()[0].lower())
        self.view.setRowCount(0)
        for name, size in sorted(self.installed.items()):
            r = self.view.rowCount()
            self.view.insertRow(r)
            for c, v in enumerate((name, f"{size / 1e9:.1f} GB", ", ".join(used.get(name, [])) or "—")):
                self.view.setItem(r, c, QTableWidgetItem(v))
        self.total.setText(f"{len(self.installed)} installed · "
                           f"{sum(self.installed.values()) / 1e9:.1f} GB on disk")
        self.avail.setRowCount(0)
        for name, size, why in CATALOGUE:
            if name not in self.installed:
                r = self.avail.rowCount()
                self.avail.insertRow(r)
                for c, v in enumerate((name, size, why)):
                    self.avail.setItem(r, c, QTableWidgetItem(v))
        self.loading = True
        for job, _, _ in JOBS:
            pick = self.pickers[job]
            pick.clear()
            pick.addItems(sorted(self.installed))
            chosen = self.choice.get(job) or common.MODEL
            if chosen in self.installed:
                pick.setCurrentText(chosen)
        self.loading = False
        self.status.setText("Each job uses the model chosen above." if self.installed else
                            "No model is installed yet. Pick one below and press Add selected "
                            "(you must be working as the head).")

    def on_pick(self, job, name):
        if self.loading or not name:
            return
        self.choice[job] = name
        common.ROLES.write_text(json.dumps(self.choice, indent=2), encoding="utf-8")
        self.status.setText(f"{job} → {name}")

    def picked(self, view):
        rows = view.selectionModel().selectedRows()
        return view.item(rows[0].row(), 0).text() if rows else None

    def on_add(self):
        name = self.picked(self.avail)
        if not name:
            return self.status.setText("Pick one from the list below first.")
        self.status.setText(f"Downloading {name}…")
        common.set_state("working")

        def pull():
            for step in common.model_stream("POST", "/api/pull", {"name": name}):
                if step.get("total") and step.get("completed"):
                    kit.ui(self.status.setText,
                           f"{name}: {100 * step['completed'] / max(1, step['total']):.0f}%")
            return f"{name} is ready."
        kit.background(pull, self.done)

    def on_remove(self):
        name = self.picked(self.view)
        if not name:
            return self.status.setText("Pick an installed model first.")
        if name in self.choice.values():
            return self.status.setText(f"{name} is doing a job — give that job to another model first.")
        kit.background(lambda: (common.model_request("DELETE", "/api/delete", {"name": name}),
                                f"Removed {name}.")[1], self.done)

    def on_get_trained(self):
        """Ask the host for the models trained for Shishya's jobs. It
        downloads them from the release (several GB), checks each against its
        checksum, puts them into Ollama and gives each its job."""
        (common.SHARED / ".models-get-requested").write_text("1", encoding="utf-8")
        self.status.setText("Asked for Shishya's trained models — several GB; this can take a while.")
        self._poll_get()

    def _poll_get(self):
        from PySide6.QtCore import QTimer
        try:
            s = json.loads((common.SHARED / ".models-get.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {"state": "waiting"}
        words = {"downloading": f"Downloading {s.get('model', '')}: {s.get('pct', 0)}%",
                 "installing": f"Putting {s.get('model', '')} into Ollama…",
                 "done": "Trained models installed: " + ", ".join(s.get("models", [])),
                 "failed": ("Installed: " + ", ".join(s["models"]) + ". " if s.get("models") else "")
                           + f"Could not get: {s.get('error', '')}"}
        self.status.setText(words.get(s.get("state"), "Waiting for the host…"))
        if s.get("state") in ("done", "failed"):
            self.choice = json.loads(common.ROLES.read_text(encoding="utf-8")) if common.ROLES.exists() else {}
            self.reload()
        else:
            QTimer.singleShot(2000, self._poll_get)

    def on_test(self):
        """Run every installed model on each job's real tests here in the
        sandbox (vm/shell/jobbench.py) and give each job the one that got
        the most right, fastest. Minutes, not seconds: every model loads."""
        if not self.installed:
            return self.status.setText("Add a model first.")
        self.status.setText("Testing every model on each job — this takes a few minutes…")
        common.set_state("working")

        def test():
            import jobbench
            _, winners = jobbench.measure(
                sorted(self.installed),
                say=lambda line: kit.ui(self.status.setText, "Testing: " + " ".join(line.split()[:4])))
            self.choice = jobbench.apply(winners)
            return "Best for each job: " + ", ".join(f"{j} → {m}" for j, m in winners.items())
        kit.background(test, self.done)

    def done(self, message):
        self.status.setText(str(message))
        common.set_state("idle")
        self.reload()


if __name__ == "__main__":
    kit.application("Models")
    sys.exit(kit.run(ModelsApp()))
