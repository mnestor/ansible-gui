"""Workers: where git, Galaxy and Ansible run, away from the app's secrets.

The app never starts git or Ansible itself. It sends a job, a JSON object naming what to do, to a worker
and reads JSON back: messages while the job runs (a run's events), then one result. A job carries
everything it needs, credentials included, and nothing else of the app's: not its environment, not a
path into the data directory. Files a job needs (a key, a vault password, known hosts) travel as text
and are written into the worker's own temp directory; SCRATCH in an argument or an environment value
stands for that directory.

InlineWorker takes the jobs in this process: the test suite and a local checkout use it. A socket
worker (worker/socket.py) sends them to `ansible-gui worker` in another container.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Protocol

from ..scratch import Scratch

log = logging.getLogger(__name__)

SCRATCH = "@@AGUI_SCRATCH@@"
VAULT = SCRATCH + "/vault-pass"


class Cancel:
    """Asks a job to stop. Thread-safe: cancel() may come from any thread, and each callback runs once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._set = False
        self._callbacks: list[Callable[[], None]] = []

    @property
    def requested(self) -> bool:
        return self._set

    def cancel(self) -> None:
        with self._lock:
            if self._set:
                return
            self._set = True
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            try:
                fn()
            except Exception:
                log.exception("stopping a job")

    def on_cancel(self, fn: Callable[[], None]) -> None:
        """Call fn when the job is canceled, or now if it already is."""
        with self._lock:
            if not self._set:
                self._callbacks.append(fn)
                return
        fn()


class Worker(Protocol):
    def call(self, kind: str, job: dict, on_message: Callable[[dict], None] | None = None,
             cancel: Cancel | None = None) -> dict:
        """Run one job and return its result. Messages the job sends meanwhile go to on_message.
        A result with "error" says what failed; the worker itself never raises for a job that failed."""
        ...


def _json(value):
    """What the value is after crossing a socket: catches a Path or a set in a job before a socket worker would."""
    return json.loads(json.dumps(value))


class InlineWorker:
    """Takes every kind of job in this process, exactly as a worker in another container would."""

    def __init__(self, scratch: Scratch, roots: list[Path] | None = None, tools: set[str] | None = None):
        self.scratch = scratch
        self.roots = roots  # None: any path; otherwise every path a job names must be inside one of these
        self.tools = tools  # None: any program for a cli job; otherwise only these

    def call(self, kind: str, job: dict, on_message: Callable[[dict], None] | None = None,
             cancel: Cancel | None = None) -> dict:
        from . import jobs

        handler = jobs.HANDLERS.get(kind)
        if handler is None:
            return {"error": f"this worker doesn’t take {kind} jobs"}
        send = (lambda m: on_message(_json(m))) if on_message else (lambda m: None)
        ctx = jobs.Context(self.scratch, send, cancel or Cancel(), self.roots, self.tools)
        try:
            return _json(handler(_json(job), ctx))
        except jobs.Refused as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("%s job", kind)
            return {"error": f"the worker failed: {e}"}


# --- helpers for the app's side ---------------------------------------------------------------------------

def vault_files(path: Path | None) -> tuple[dict[str, str], list[str]]:
    """({file name: text}, arguments) that hand a job the vault password the file at path gives.

    The worker can't read the app's files, so the password travels as text. An executable file is run, as
    Ansible would run it, and what it prints is the password."""
    if not path:
        return {}, []
    try:
        if os.access(path, os.X_OK) and path.is_file():
            out = subprocess.run([str(path)], capture_output=True, text=True, timeout=30)
            text = out.stdout if out.returncode == 0 else ""
        else:
            text = path.read_text()
    except (OSError, subprocess.SubprocessError):
        return {}, ["--vault-password-file", str(path)]  # Ansible then says it can't read it
    return {"vault-pass": text}, ["--vault-password-file", VAULT]


def cli(worker: Worker | None, argv: list[str], cwd: Path, env: dict[str, str], timeout: int,
        files: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """subprocess.run(argv, cwd=cwd, ...) on a worker, with env added to the worker's environment.
    Raises FileNotFoundError or subprocess.TimeoutExpired like subprocess.run, OSError if the worker failed."""
    worker = worker or default()
    res = worker.call("cli", {"argv": list(argv), "cwd": str(cwd), "env": env, "timeout": timeout,
                              "files": files or {}})
    if res.get("error") == "not_found":
        raise FileNotFoundError(argv[0])
    if res.get("error") == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout)
    if "error" in res:
        raise OSError(res["error"])
    return subprocess.CompletedProcess(argv, res["rc"], res["stdout"], res["stderr"])


_default: Worker | None = None


def default() -> Worker:
    """For code called without a worker (a test calling inventory.cli directly): one in this process."""
    global _default
    if _default is None:
        import tempfile
        _default = InlineWorker(Scratch(Path(tempfile.gettempdir()) / "ansible-gui-inline"))
    return _default
