"""Claude calls through the headless Claude Code CLI (`claude -p`), with a
hard USD budget recorded in a ledger file so a resumed run keeps counting."""
from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import time

_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


class ClaudeCLI:
    def __init__(self, ledger_path: pathlib.Path, budget_usd: float, model: str,
                 effort: str | None = None, timeout_sec: int = 300):
        self.ledger_path = ledger_path
        self.budget_usd = float(budget_usd)
        self.model = model
        self.effort = effort
        self.timeout_sec = timeout_sec
        if not ledger_path.exists():
            ledger_path.write_text(json.dumps({"spent_usd": 0.0, "calls": 0, "errors": 0}))

    def ledger(self) -> dict:
        return json.loads(self.ledger_path.read_text())

    def _record(self, cost: float, error: bool) -> None:
        with _lock:
            data = self.ledger()
            data["spent_usd"] = round(data["spent_usd"] + cost, 6)
            data["calls"] += 1
            data["errors"] += int(error)
            self.ledger_path.write_text(json.dumps(data))

    def remaining(self) -> float:
        return self.budget_usd - self.ledger()["spent_usd"]

    def ask(self, system: str, prompt: str) -> tuple[str, float]:
        """Return (text, cost_usd). Raises BudgetExceeded before calling if spent."""
        if self.remaining() <= 0:
            raise BudgetExceeded(f"LLM budget ${self.budget_usd:.2f} spent")
        cmd = [
            "claude", "-p", "--model", self.model, "--system-prompt", system,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--output-format", "json",
            "--max-budget-usd", f"{min(2.0, max(0.05, self.remaining())):.2f}",
        ]
        if self.effort:
            cmd += ["--effort", self.effort]
        started = time.time()
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=self.timeout_sec, cwd="/tmp")
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            self._record(0.0, True)
            raise RuntimeError(f"claude CLI non-JSON output: {proc.stdout[-300:]} {proc.stderr[-300:]}")
        cost = float(data.get("total_cost_usd") or 0.0)
        error = bool(data.get("is_error"))
        self._record(cost, error)
        if error:
            raise RuntimeError(f"claude CLI error: {str(data.get('result'))[:300]}")
        _ = time.time() - started
        return str(data.get("result") or ""), cost
