"""cloud_batch/job_runner.py — Run a job definition as a sequence of subprocesses.

Each step is run as:
    python -m app.<module> [args with {param} substituted]

Firestore (gcloud-batch-jobs) is updated after every step so the frontend
can show live progress. Each step's last 50 lines of combined stdout+stderr
are saved as log_tail.

Usage (called from entrypoint.py in a background thread):
    runner = JobRunner(defn, run_id, params, triggered_by="manual")
    runner.run()
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from cloud_batch.job_status import (
    create_run,
    finish_run,
    update_step_done,
    update_step_start,
    now_iso,
)

# Project root — two levels up from this file (cloud_batch/job_runner.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOG_TAIL_LINES = 50


class JobRunner:
    def __init__(
        self,
        defn: dict,
        run_id: str,
        params: dict,
        triggered_by: str = "manual",
    ):
        self.defn         = defn
        self.job_name     = defn["name"]
        self.run_id       = run_id
        self.params       = params
        self.triggered_by = triggered_by

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> str:
        """Execute all steps. Returns final status: 'done' | 'failed'."""
        steps = self.defn.get("steps", [])
        create_run(self.job_name, self.run_id, self.params, self.triggered_by, steps)
        print(f"[batch] {self.job_name} run={self.run_id} starting ({len(steps)} steps)", flush=True)

        final_status = "done"

        for i, step in enumerate(steps):
            step_name = step["name"]
            cmd = self._build_cmd(step)

            if cmd is None:
                # step was skipped due to missing optional param
                print(f"[batch]   step {i+1}/{len(steps)} {step_name}: SKIPPED (empty param)", flush=True)
                update_step_done(self.job_name, self.run_id, i, 0, "", "skipped")
                continue

            print(f"[batch]   step {i+1}/{len(steps)} {step_name}: {' '.join(cmd)}", flush=True)
            update_step_start(self.job_name, self.run_id, i)

            exit_code, log_tail = self._run_subprocess(cmd)

            step_status = "done" if exit_code == 0 else "failed"
            update_step_done(self.job_name, self.run_id, i, exit_code, log_tail, step_status)

            if exit_code != 0:
                print(f"[batch]   step {step_name} FAILED (exit {exit_code})", flush=True)
                if step.get("on_error", "abort") == "abort":
                    final_status = "failed"
                    # mark remaining steps as skipped
                    for j in range(i + 1, len(steps)):
                        update_step_done(self.job_name, self.run_id, j, None, "", "skipped")
                    break
                # on_error == "continue": keep going but remember failure
                final_status = "failed"
            else:
                print(f"[batch]   step {step_name} done", flush=True)

        finish_run(self.job_name, self.run_id, final_status)
        print(f"[batch] {self.job_name} run={self.run_id} finished: {final_status}", flush=True)
        return final_status

    # ── Command builder ───────────────────────────────────────────────────────

    def _build_cmd(self, step: dict) -> list[str] | None:
        """Build the subprocess argv list, substituting {param} placeholders.

        Returns None if a required-but-empty param causes the step to be skipped
        (skip_if_empty list).
        """
        skip_if_empty = step.get("skip_if_empty", [])
        for key in skip_if_empty:
            if not self.params.get(key):
                return None

        raw_args: list[str] = step.get("args", [])
        resolved: list[str] = []
        skip_next = False

        for token in raw_args:
            if skip_next:
                skip_next = False
                continue

            # Substitute {param} placeholders
            if token.startswith("{") and token.endswith("}"):
                key   = token[1:-1]
                value = str(self.params.get(key, ""))
                if not value:
                    # Drop this flag and its preceding --flag token
                    if resolved and resolved[-1].startswith("--"):
                        resolved.pop()
                    continue
                resolved.append(value)
            else:
                resolved.append(token)

        # Append --dry-run / --force flags from params
        if self.params.get("dry_run") and step.get("dry_run_flag"):
            resolved.append(step["dry_run_flag"])
        if self.params.get("force") and step.get("force_flag"):
            resolved.append(step["force_flag"])
        # gdisk: default True — add flag unless explicitly set to False
        if self.params.get("gdisk", True) and step.get("gdisk_flag"):
            resolved.append(step["gdisk_flag"])

        module = f"app.{step['module']}"
        return [sys.executable, "-m", module] + resolved

    # ── Subprocess runner ─────────────────────────────────────────────────────

    def _run_subprocess(self, cmd: list[str]) -> tuple[int, str]:
        """Run cmd, stream output to stdout, return (exit_code, last N lines)."""
        tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
            )

            for line in proc.stdout:
                line = line.rstrip("\n")
                tail.append(line)
                print(line, flush=True)

            proc.wait()
            return proc.returncode, "\n".join(tail)

        except Exception as exc:
            msg = f"[runner error] {exc}"
            print(msg, flush=True)
            return 1, msg


# ── Convenience: run in background thread ────────────────────────────────────

def run_in_background(defn: dict, run_id: str, params: dict, triggered_by: str = "manual") -> threading.Thread:
    """Start the job runner in a daemon thread and return it."""
    runner = JobRunner(defn, run_id, params, triggered_by)
    t = threading.Thread(target=runner.run, daemon=True, name=f"batch-{run_id}")
    t.start()
    return t
