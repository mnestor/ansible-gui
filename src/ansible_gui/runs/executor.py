"""Runs playbooks one at a time through ansible-runner, in a background thread."""

from __future__ import annotations

import logging
import queue
import shlex
import subprocess
import threading
from pathlib import Path

from ..inventory.cli import CliOptions
from .store import RunStore, host_result, now

log = logging.getLogger(__name__)

_COUNTERS = ("ok", "changed", "failed", "unreachable", "skipped")


def git_commit(project_dir: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=project_dir,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class Executor:
    def __init__(self, store: RunStore, opts: CliOptions):
        self.store = store
        self.opts = opts
        self._queue: queue.Queue[int] = queue.Queue()
        self._cancel: set[int] = set()
        self._live: dict[int, dict[str, dict[str, int]]] = {}
        self._thread = threading.Thread(target=self._loop, name="ansible-gui-runner", daemon=True)
        self._thread.start()

    # --- public ----------------------------------------------------------------
    def submit(self, run_id: int) -> None:
        self._queue.put(run_id)

    def cancel(self, run_id: int) -> None:
        run = self.store.get(run_id)
        if run and run.status == "queued":
            self.store.update(run_id, status="canceled", finished_at=now(), error="Canceled before it started.")
        else:
            self._cancel.add(run_id)

    def cancel_requested(self, run_id: int) -> bool:
        return run_id in self._cancel

    def live_stats(self, run_id: int) -> dict[str, dict[str, int]] | None:
        return self._live.get(run_id)

    def queue_position(self, run_id: int) -> int:
        with self._queue.mutex:
            items = list(self._queue.queue)
        return items.index(run_id) + 1 if run_id in items else 0

    # --- worker ----------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                self._execute(run_id)
            except Exception as e:  # never let one run kill the worker
                log.exception("run %s crashed", run_id)
                self.store.update(run_id, status="error", finished_at=now(), error=f"Internal error: {e}")
            finally:
                self._live.pop(run_id, None)
                self._cancel.discard(run_id)

    def _execute(self, run_id: int) -> None:
        import ansible_runner  # imported lazily: slow, and only the worker needs it

        run = self.store.get(run_id)
        if run is None or run.status != "queued":
            return
        project = self.opts.project_dir
        run_dir = self.store.run_dir(run_id)
        (run_dir / "runner").mkdir(exist_ok=True)
        log_file = self.store.log_path(run_id).open("a", encoding="utf-8", buffering=1)
        live: dict[str, dict[str, int]] = {h: dict.fromkeys(_COUNTERS, 0) for h in run.expected_hosts}
        self._live[run_id] = live
        final: dict[str, dict[str, int]] = {}

        self.store.update(run_id, status="running", started_at=now(), commit_sha=git_commit(project))

        def on_event(ev: dict) -> bool:
            if ev.get("stdout"):
                log_file.write(ev["stdout"] + "\n")
            data = ev.get("event_data") or {}
            host = data.get("host")
            kind = ev.get("event", "")
            if host and kind.startswith("runner_on_"):
                c = live.setdefault(host, dict.fromkeys(_COUNTERS, 0))
                if kind == "runner_on_ok":
                    c["ok"] += 1
                    if (data.get("res") or {}).get("changed"):
                        c["changed"] += 1
                elif kind == "runner_on_failed" and not data.get("ignore_errors"):
                    c["failed"] += 1
                elif kind == "runner_on_unreachable":
                    c["unreachable"] += 1
                elif kind == "runner_on_skipped":
                    c["skipped"] += 1
            if kind == "playbook_on_stats":
                for key, field in (("ok", "ok"), ("changed", "changed"), ("failures", "failed"),
                                   ("dark", "unreachable"), ("skipped", "skipped")):
                    for h, n in (data.get(key) or {}).items():
                        final.setdefault(h, dict.fromkeys(_COUNTERS, 0))[field] = n
            return True

        cmdline = []
        if run.check_mode:
            cmdline.append("--check")
        if run.diff:
            cmdline.append("--diff")
        if self.opts.vault_password_file:
            cmdline += ["--vault-password-file", str(self.opts.vault_password_file)]

        log_file.write(f"$ ansible-playbook {shlex.join(self._args(run, cmdline))}\n\n")
        try:
            result = ansible_runner.run(
                private_data_dir=str(run_dir / "runner"),
                project_dir=str(project),
                playbook=run.playbook,
                inventory=[str((project / s).resolve()) for s in self.opts.sources],
                limit=run.target or None,
                tags=run.tags or None,
                skip_tags=run.skip_tags or None,
                verbosity=run.verbosity or None,
                cmdline=shlex.join(cmdline) if cmdline else None,
                envvars={"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0"},
                event_handler=on_event,
                cancel_callback=lambda: run_id in self._cancel,
                quiet=True,
                rotate_artifacts=1,
            )
            if result.status == "canceled":
                log_file.write("\n[ansible-gui] Run canceled from the web UI.\n")
        finally:
            log_file.close()

        stats = final or live
        status = result.status
        if status == "timeout":
            status = "failed"
        if status == "successful" and any(host_result(s) in ("failed", "unreachable") for s in stats.values()):
            status = "failed"
        self.store.update(run_id, status=status, finished_at=now(), rc=result.rc, host_stats=stats)

    def _args(self, run, cmdline: list[str]) -> list[str]:
        args = []
        for s in self.opts.sources:
            args += ["-i", s]
        if run.target:
            args += ["--limit", run.target]
        if run.tags:
            args += ["--tags", run.tags]
        if run.skip_tags:
            args += ["--skip-tags", run.skip_tags]
        if run.verbosity:
            args.append("-" + "v" * run.verbosity)
        return [*args, *cmdline, run.playbook]
