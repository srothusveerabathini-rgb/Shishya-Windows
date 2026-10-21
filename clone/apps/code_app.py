#!/usr/bin/env python3
"""
Code — write a script, or describe one, and run it. Runs INSIDE the sandbox.

Code written by a model is exactly the code you should not run on your own
machine. Here it runs inside the AppContainer — no route to your files, no
network — so you can run it, read the output, and decide.
"""

import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit   # noqa: E402
from PySide6.QtCore import QRegularExpression, QTimer   # noqa: E402
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat   # noqa: E402
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QVBoxLayout   # noqa: E402

import codegen   # noqa: E402
import common    # noqa: E402

RUN_TIMEOUT = 60

STARTER = '''# Anything you write here runs inside the sandbox.
# Your files and the network are out of reach, so it is safe to just run it.

from pathlib import Path

work = Path.home() / "shared" / "work"
for f in sorted(work.iterdir()):
    print(f"{f.name:<44} {f.stat().st_size:>8,} bytes")
'''

KEYWORDS = ("and as assert async await break class continue def del elif else except finally "
            "for from global if import in is lambda nonlocal not or pass raise return try while "
            "with yield True False None self print len range str int float dict list set").split()

SYSTEM = (
    "You write short, complete Python 3 scripts. Output the code and nothing else — no "
    f"explanation, no markdown fences. The script runs in a sandbox on {codegen.PLATFORM} with "
    "pandas, openpyxl, python-docx and python-pptx available. Build paths with pathlib — "
    "Path.home() / 'shared' / 'training' / '<name>' — never a string starting with ~. Print "
    "results rather than returning them, and keep it under 40 lines."
)


class Highlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)

        def fmt(colour, bold=False, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(colour))
            if bold:
                f.setFontWeight(QFont.Bold)
            f.setFontItalic(italic)
            return f
        self.rules = [(QRegularExpression(r"\b(" + "|".join(KEYWORDS) + r")\b"), fmt("#0550AE", True)),
                      (QRegularExpression(r"\b\d+\.?\d*\b"), fmt("#A6501E")),
                      (QRegularExpression(r"\"[^\"\n]*\"|'[^'\n]*'"), fmt("#0A7B47")),
                      (QRegularExpression(r"#[^\n]*"), fmt("#7A7F87", italic=True))]

    def highlightBlock(self, text):
        for rx, f in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


class CodeApp(kit.Window):
    def __init__(self):
        super().__init__("Code", 940, 720)
        self.running = False
        root = QVBoxLayout(self.body)
        root.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        self.brief = kit.entry("Describe what you want the code to do, then press Write it",
                               name="brief", accessible="What should the code do")
        self.brief.returnPressed.connect(self.on_write)
        top.addWidget(self.brief, 1)
        self.writebtn = kit.button("Write it", self.on_write)
        top.addWidget(self.writebtn)
        root.addLayout(top)

        mono = "font-family: Consolas, monospace; font-size: 13px;"
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("code")
        self.editor.setAccessibleName("Code")
        self.editor.setStyleSheet(mono)
        self.editor.setPlainText(STARTER)
        self.highlight = Highlighter(self.editor.document())
        root.addWidget(self.editor, 3)

        bar = QHBoxLayout()
        self.status = kit.label("Runs inside the sandbox — nothing here can reach your machine.",
                                role="muted")
        bar.addWidget(self.status, 1)
        bar.addWidget(kit.button("Save to Work", self.on_save))
        self.runbtn = kit.button("Run", self.on_run, primary=True)
        bar.addWidget(self.runbtn)
        root.addLayout(bar)

        self.out = QPlainTextEdit()
        self.out.setObjectName("output")
        self.out.setAccessibleName("Output")
        self.out.setReadOnly(True)
        self.out.setStyleSheet(mono)
        root.addWidget(self.out, 2)

    def on_write(self):
        brief = self.brief.text().strip()
        if not brief:
            return self.status.setText("Describe what you want first.")
        self.writebtn.setEnabled(False)
        self.status.setText("Writing it…")
        common.set_state("working")
        kit.background(lambda: codegen.repair(common.ask_job(
            "code", f"{codegen.describe_material()}\n\nWrite a Python script that: {brief}",
            system=SYSTEM, budget=6144)), self.wrote)

    def wrote(self, code):
        self.writebtn.setEnabled(True)
        common.set_state("idle")
        # Anything that is not code stays out of the editor, or the next Run
        # executes an error message and reports a SyntaxError.
        if not code.strip() or code.startswith(("RuntimeError", "I ran out of room")):
            self.status.setText("Couldn't write that one.")
            self.out.setPlainText(code or "The model ran out of room — try a narrower description.")
            return
        self.editor.setPlainText(code)
        self.status.setText("Read it before you run it.")

    def on_run(self):
        if self.running:
            return
        code = self.editor.toPlainText()
        if not code.strip():
            return self.status.setText("Nothing to run.")
        self.running = True
        self.runbtn.setEnabled(False)
        self.out.setPlainText("Running…")
        common.set_state("working")
        kit.background(lambda: self._run(code), self.ran)

    def _run(self, code):
        began = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.py"
            script.write_text(code, encoding="utf-8")
            try:
                p = common.run_python(script, cwd=common.WORK if common.WORK.exists() else None,
                                      timeout=RUN_TIMEOUT)
                out = (p.stdout or "") + (("\n" if p.stdout else "") + p.stderr if p.stderr else "")
                return (out.strip() or "(finished, printed nothing)",
                        f"exit {p.returncode} · {time.time() - began:.1f}s")
            except Exception as e:
                if "Timeout" in type(e).__name__:
                    return f"(stopped after {RUN_TIMEOUT}s — it was still going)", "timed out"
                return f"could not run it: {e}", "failed"

    def ran(self, result):
        out, note = result if isinstance(result, tuple) else (str(result), "failed")
        self.out.setPlainText(out)
        self.status.setText(note)
        common.set_state("idle")
        self.runbtn.setEnabled(True)
        self.running = False

    def on_save(self):
        common.WORK.mkdir(parents=True, exist_ok=True)
        path = common.WORK / f"script-{time.strftime('%H%M%S')}.py"
        path.write_text(self.editor.toPlainText(), encoding="utf-8")
        self.status.setText(f"Saved {path.name} to Work")


if __name__ == "__main__":
    kit.application("Code")
    sys.exit(kit.run(CodeApp()))
