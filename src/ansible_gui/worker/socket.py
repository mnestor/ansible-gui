"""Jobs over a Unix socket: `ansible-gui worker` serves them, SocketWorker sends them.

One connection per job. The app writes one line, {"kind": ..., "job": ...}, and may later write
{"cancel": true}. The worker writes {"message": ...} lines while the job runs, then one {"result": ...}.
Closing the connection cancels the job, so a worker never carries on with a run the app has stopped
watching, and a worker that dies mid-job is a run the app marks interrupted.

The worker accepts a connection only from the app's uid, which the kernel reports for the other end of
the socket (SO_PEERCRED; LOCAL_PEERCRED on macOS). There is no TCP listener.
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

MAX_LINE = 64 * 1024 * 1024  # a run's events can carry every fact of a host; a job line holds its files


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
        self._closed = False
        self._active: set[Cancel] = set()
        self._lock = threading.Lock()

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

            with self._lock:
                self._active.add(cancel)
            try:
                result = self.worker.call(kind, job, send, cancel)
                # Stopped by close(): whatever the job made of that, for the app it was cut short.
                write({"result": {"lost": True} if self._closed else result})
            finally:
                with self._lock:
                    self._active.discard(cancel)
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

    def __init__(self, path: Path, name: str = "worker"):
        self.path, self.name = path, name

    def call(self, kind: str, job: dict, on_message: Callable[[dict], None] | None = None,
             cancel: Cancel | None = None) -> dict:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(self.path))
        except OSError as e:
            conn.close()
            return {"error": f"the {self.name} isn’t answering on {self.path}: {e.strerror or e}"}
        write = _writer(conn)
        try:
            if not write({"kind": kind, "job": job}):
                return {"error": f"the {self.name} closed the connection", "lost": True}
            if cancel:
                cancel.on_cancel(lambda: write({"cancel": True}))
            return _read(conn, on_message, self.name)
        except OSError as e:
            return {"error": f"the connection to the {self.name} failed: {e}", "lost": True}
        finally:
            conn.close()


def _read(conn: socket.socket, on_message, name: str) -> dict:
    rfile = conn.makefile("rb")
    for line in iter(lambda: rfile.readline(MAX_LINE), b""):
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
