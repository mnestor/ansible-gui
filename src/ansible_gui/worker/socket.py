"""Jobs over a Unix socket: `ansible-gui worker` serves them, SocketWorker sends them.

One connection per job. The app writes one line, {"kind": ..., "job": ...}, and may later write
{"cancel": true}. The worker writes {"message": ...} lines while the job runs, then one {"result": ...}.
Closing the connection cancels the job, so a worker never carries on with a run the app has stopped
watching, and a worker that dies mid-job is a run the app marks interrupted.

The worker accepts a connection only from the app's uid, which the kernel reports for the other end of
the socket (SO_PEERCRED; LOCAL_PEERCRED on macOS). There is no TCP listener.

The app doesn't take the worker's word for anything it can check. A playbook runs as the runner's user and
could take over its end of the socket: so a reply is read up to MAX_REPLY at a time, every job but a run has
a time by which it must have answered (DEADLINES), and a run that was told to stop has CANCEL_GRACE to say so.
After that the app closes the connection and calls the job lost.

A worker given something to clean with (worker/clean.py) calls it each time its last job in flight has ended,
and starts no job meanwhile. If the cleaning fails it stops serving: its container then starts again clean.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Callable

from . import Cancel, InlineWorker

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024 * 1024  # what a worker reads at once: a job line holds its files
# What the app reads at once: a run's event can carry every fact of a host, which is a few megabytes. A longer
# line is left out and the run's log says so: parsing one of MAX_LINE would take most of the app's memory.
MAX_REPLY = 16 * 1024 * 1024
# Seconds the app waits for each kind of job. Longer than the job's own limits (git 180 s a command, Galaxy
# 600 s), so only a worker that hangs, or something else answering on its socket, gets here. A cli job
# carries its own timeout and gets CLI_MARGIN on top. A run has none: the run timeout cancels it.
DEADLINES = {"fetch": 1200, "materialize": 600, "collections": 660, "prune": 300, "prune_collections": 300,
             "remove": 300}
CLI_MARGIN = 30
CANCEL_GRACE = 30  # seconds a canceled job has to answer: stopping Ansible's processes takes a moment


def peer_uid(conn: socket.socket) -> int | None:
    """The uid of the process at the other end of a Unix socket, as the kernel reports it."""
    if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid, uid, gid}
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    if sys.platform == "darwin":  # SOL_LOCAL, LOCAL_PEERCRED: struct xucred {version, uid, ...}
        raw = conn.getsockopt(0, 1, 76)
        return struct.unpack_from("=II", raw)[1]
    return None


def _writer(conn: socket.socket) -> Callable[[dict], bool]:
    lock = threading.Lock()

    def write(obj: dict) -> bool:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with lock:
            try:
                conn.sendall(data)
                return True
            except OSError:
                return False

    return write


class Server:
    """Serves jobs of the given kinds on a Unix socket, to one uid."""

    def __init__(self, path: Path, kinds: set[str], allow_uid: int, worker: InlineWorker):
        self.path, self.kinds, self.allow_uid, self.worker = path, kinds, allow_uid, worker
        path.unlink(missing_ok=True)  # left by a worker that was killed
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)  # the socket is rw for its owner and group: the app reaches it through the group
        try:
            self.sock.bind(str(path))
        finally:
            os.umask(old)
        self.sock.listen(16)
        self.clean: Callable[[], None] | None = None  # set before serving, once the socket above is open
        self.unclean = False  # the cleaning failed, and this server stopped because of it
        self._closed = False
        self._active: set[Cancel] = set()
        self._lock = threading.Lock()  # held while the container is cleaned: a job starts only with it

    def serve_forever(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                if self._closed:
                    return
                raise
            threading.Thread(target=self._handle, args=(conn,), daemon=True, name="agui-job").start()

    def close(self) -> None:
        """Stop taking jobs, and stop the ones running: a run's Ansible must not outlive its worker."""
        self._closed = True
        with self._lock:
            active = list(self._active)
        for cancel in active:
            cancel.cancel()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.path.unlink(missing_ok=True)

    def _ended(self, cancel: Cancel) -> None:
        """A job is over. If it was the last one in flight, clean the container before the next one starts."""
        with self._lock:
            self._active.discard(cancel)
            if self.clean is None or self._active or self._closed:
                return
            try:
                self.clean()
                return
            except Exception as e:
                log.error("couldn’t clean up after the last job (%s): stopping, so that the container starts "
                          "again clean", e)
                self.unclean = self._closed = True  # under the lock: no job waiting for it starts
        self.close()

    def _handle(self, conn: socket.socket) -> None:
        write = _writer(conn)
        try:
            uid = peer_uid(conn)
            if uid != self.allow_uid:
                log.warning("refused a connection from uid %s", uid)
                write({"result": {"error": f"this worker takes jobs from uid {self.allow_uid} only, not {uid}"}})
                return
            rfile = conn.makefile("rb")
            first = rfile.readline(MAX_LINE)
            try:
                req = json.loads(first)
                kind, job = req["kind"], req["job"]
                if not isinstance(job, dict):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                write({"result": {"error": "not a job"}})
                return
            if kind not in self.kinds:
                write({"result": {"error": f"this worker doesn’t take {kind} jobs"}})
                return
            cancel = Cancel()

            def listen() -> None:  # a cancel message, or the app going away
                try:
                    for line in iter(lambda: rfile.readline(MAX_LINE), b""):
                        try:
                            if json.loads(line).get("cancel"):
                                cancel.cancel()
                        except (ValueError, AttributeError):
                            pass
                except OSError:
                    pass
                cancel.cancel()

            threading.Thread(target=listen, daemon=True, name="agui-job-cancel").start()

            def send(msg: dict) -> None:
                if not write({"message": msg}):
                    cancel.cancel()

            with self._lock:  # waits here while the container is cleaned after the jobs before
                if self._closed:
                    write({"result": {"lost": True}})
                    return
                self._active.add(cancel)
            try:
                result = self.worker.call(kind, job, send, cancel)
                # Stopped by close(): whatever the job made of that, for the app it was cut short.
                write({"result": {"lost": True} if self._closed else result})
            finally:
                self._ended(cancel)
        except Exception:
            log.exception("serving a job")
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()


class SocketWorker:
    """Sends jobs to `ansible-gui worker` listening on path."""

    def __init__(self, path: Path, name: str = "worker", deadlines: dict[str, float] | None = None,
                 cancel_grace: float = CANCEL_GRACE):
        self.path, self.name = path, name
        self.deadlines = DEADLINES if deadlines is None else deadlines
        self.cancel_grace = cancel_grace

    def deadline(self, kind: str, job: dict) -> float | None:
        """Seconds this job may take to answer, or None for no limit."""
        if kind == "cli" and isinstance(job.get("timeout"), (int, float)) and "cli" not in self.deadlines:
            return job["timeout"] + CLI_MARGIN
        return self.deadlines.get(kind)

    def call(self, kind: str, job: dict, on_message: Callable[[dict], None] | None = None,
             cancel: Cancel | None = None) -> dict:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(self.path))
        except OSError as e:
            conn.close()
            return {"error": f"the {self.name} isn’t answering on {self.path}: {e.strerror or e}"}
        write = _writer(conn)
        waiting = _Patience(conn)
        try:
            if not write({"kind": kind, "job": job}):
                return {"error": f"the {self.name} closed the connection", "lost": True}
            if (limit := self.deadline(kind, job)) is not None:
                waiting.until(limit)
            if cancel:
                cancel.on_cancel(lambda: (write({"cancel": True}), waiting.until(self.cancel_grace)))
            result = _read(conn, on_message, self.name)
            if waiting.ran_out and result.get("lost"):
                return {"error": f"the {self.name} didn’t answer in time", "lost": True}
            return result
        except OSError as e:
            return {"error": f"the connection to the {self.name} failed: {e}", "lost": True}
        finally:
            waiting.over()
            conn.close()


class _Patience:
    """Closes a connection when the time allowed for an answer has passed, which ends the read waiting on it."""

    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.ran_out = False
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._over = False

    def until(self, seconds: float) -> None:
        timer = threading.Timer(seconds, self._give_up)
        timer.daemon = True
        with self._lock:
            if self._over:
                return
            self._timers.append(timer)
        timer.start()

    def _give_up(self) -> None:
        with self._lock:
            if self._over:
                return
            self.ran_out = True
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def over(self) -> None:
        with self._lock:
            self._over = True
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()


def _read(conn: socket.socket, on_message, name: str) -> dict:
    rfile = conn.makefile("rb")
    for line in iter(lambda: rfile.readline(MAX_REPLY), b""):
        if len(line) >= MAX_REPLY and not line.endswith(b"\n"):
            size = len(line)
            for rest in iter(lambda: rfile.readline(MAX_REPLY), b""):  # the rest of the line, unread
                size += len(rest)
                if rest.endswith(b"\n"):
                    break
            if on_message:
                on_message({"oversized": size})
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if "result" in msg:
            res = msg["result"]
            if not isinstance(res, dict):
                return {"error": f"the {name} answered nonsense"}
            return {"error": f"the {name} stopped before the job finished", "lost": True} if res.get("lost") else res
        if "message" in msg and on_message and isinstance(msg["message"], dict):
            on_message(msg["message"])
    return {"error": f"the {name} stopped before the job finished", "lost": True}
