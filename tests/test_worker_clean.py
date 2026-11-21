"""A clean container between jobs (worker/clean.py).

Nothing here kills anything: every test runs with os.kill replaced. The real kill(-1) runs in a container,
in deploy/smoke-test.sh, against a process a playbook left behind.
"""

import errno
import inspect
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ansible_gui import run_worker
from ansible_gui.worker import clean
from ansible_gui.worker.socket import Server, SocketWorker


@pytest.fixture(autouse=True)
def killed(monkeypatch):
    """kill(-1) outside a container ends every process of whoever runs the tests: here it only takes notes."""
    calls = []
    monkeypatch.setattr(clean.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


@pytest.fixture
def sockdir():
    # Short: a Unix socket's path must fit in about 100 bytes, and pytest's tmp_path on macOS doesn't.
    with tempfile.TemporaryDirectory(prefix="agui-t-", dir="/tmp") as d:
        yield Path(d)


def until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "didn't happen in time"
        time.sleep(0.01)


def at(monkeypatch, platform="linux", uid=10003, pid=7, parent=1, pid1_uid=10003):
    """Stand where a worker might be started."""
    real = os.stat
    monkeypatch.setattr(clean.sys, "platform", platform)
    monkeypatch.setattr(clean.os, "getuid", lambda: uid)
    monkeypatch.setattr(clean.os, "getpid", lambda: pid)
    monkeypatch.setattr(clean.os, "getppid", lambda: parent)
    monkeypatch.setattr(clean.os, "stat", lambda p, *a, **k: SimpleNamespace(st_uid=pid1_uid) if str(p) == "/proc/1"
                        else real(p, *a, **k))


# --- where it may kill --------------------------------------------------------------------------------------

def test_a_worker_kills_only_in_a_container_of_its_own(monkeypatch):
    at(monkeypatch)  # the image: tini is PID 1 as the runner's user, and started the worker
    assert clean.own_container()
    at(monkeypatch, pid=1, parent=0, pid1_uid=0)  # no tini: the worker is PID 1 itself
    assert clean.own_container()

    at(monkeypatch, pid1_uid=0)  # a machine people use: PID 1 is root's, whatever started the worker
    assert not clean.own_container()
    at(monkeypatch, parent=4242)  # started by a shell, which the kill would take with it
    assert not clean.own_container()
    at(monkeypatch, uid=0, pid1_uid=0)  # root may signal everything
    assert not clean.own_container()
    at(monkeypatch, platform="darwin")
    assert not clean.own_container()


def test_outside_a_container_of_its_own_nothing_is_killed(monkeypatch, killed):
    at(monkeypatch, pid1_uid=0)
    with pytest.raises(clean.Unclean, match="container of its own"):
        clean.kill_others()
    assert killed == []


def test_the_worker_command_refuses_to_clean_where_it_isnt_alone(monkeypatch, sockdir, killed):
    at(monkeypatch, pid1_uid=0)
    (sockdir / "mine").write_text("still here")
    with pytest.raises(SystemExit, match="container of its own"):
        run_worker(["--socket", str(sockdir / "w.sock"), "--allow-uid", "1", "--clean-between-jobs", str(sockdir)])
    assert killed == [] and (sockdir / "mine").read_text() == "still here" and not (sockdir / "w.sock").exists()


def test_one_kill_for_every_other_process_then_it_waits_until_they_are_gone(monkeypatch, killed):
    monkeypatch.setattr(clean, "own_container", lambda: True)
    monkeypatch.setattr(clean.os, "waitpid", lambda *a: (0, 0))
    still = [[41, 42], [42], []]
    monkeypatch.setattr(clean, "others", lambda: still.pop(0))
    clean.kill_others()
    assert killed == [(-1, signal.SIGKILL)] and still == []

    monkeypatch.setattr(clean, "others", lambda: [42])
    with pytest.raises(clean.Unclean, match="still running"):
        clean.kill_others(patience=0.05)


def test_nothing_else_running_is_not_an_error(monkeypatch):
    def nobody(pid, sig):
        raise ProcessLookupError  # what kill(-1) says when there was no one to signal
    monkeypatch.setattr(clean, "own_container", lambda: True)
    monkeypatch.setattr(clean.os, "kill", nobody)
    monkeypatch.setattr(clean.os, "waitpid", lambda *a: (0, 0))
    monkeypatch.setattr(clean, "others", lambda: [])
    clean.kill_others()


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.getuid() == 0, reason="Linux, and not as root")
def test_a_hidden_process_cant_be_read_by_another_of_its_user():
    peek = "import os; open(f'/proc/{os.getppid()}/environ', 'rb').read(1)"
    parent = ("import subprocess, sys\nfrom ansible_gui.worker import clean\n"
              "if sys.argv[1] == 'hidden': clean.hide()\n"
              f"sys.exit(subprocess.run([sys.executable, '-c', {peek!r}], stderr=subprocess.DEVNULL).returncode)")
    assert subprocess.run([sys.executable, "-c", parent, "as started"]).returncode == 0
    assert subprocess.run([sys.executable, "-c", parent, "hidden"]).returncode != 0


# --- what it removes ----------------------------------------------------------------------------------------

def test_emptying_a_directory_takes_whatever_a_job_made_of_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("not the job's")
    top = tmp_path / "top"
    top.mkdir()
    (top / "file").write_text("x")
    (top / "read-only").write_text("x")
    (top / "read-only").chmod(0o400)
    (top / "link-to-a-file").symlink_to(outside / "kept")
    (top / "link-to-a-directory").symlink_to(outside)
    (top / "link-to-nothing").symlink_to(top / "nowhere")
    os.mkfifo(top / "fifo")
    (top / "closed" / "inside").mkdir(parents=True)
    (top / "closed" / "inside" / "file").write_text("x")
    (top / "closed" / "inside").chmod(0)
    (top / "closed").chmod(0)
    # Up to 60 directories deep, each name 200 letters: no path names the last one. (Linux with AppArmor
    # refuses to make one past about 8000 characters, which is still twice what a path can hold.)
    fd, levels = os.open(top, os.O_RDONLY), 0
    for _ in range(60):
        try:
            os.mkdir("d" * 200, dir_fd=fd)
        except OSError as e:
            assert e.errno == errno.ENAMETOOLONG
            break
        below = os.open("d" * 200, os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd, levels = below, levels + 1
    assert levels * 201 > os.pathconf(top, "PC_PATH_MAX")
    os.close(os.open("b", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    os.close(fd)
    (top / "the-socket").write_text("the worker's own")
    top.chmod(0o500)  # a job that closed its own home

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 30)  # nor may it go a call deeper for each directory
    try:
        clean.empty(top, frozenset({"the-socket"}))
    finally:
        sys.setrecursionlimit(limit)

    assert os.listdir(top) == ["the-socket"]
    assert sorted(os.listdir(outside)) == ["kept"] and (outside / "kept").read_text() == "not the job's"


def listening(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def test_cleaning_kills_first_then_empties_and_keeps_the_workers_socket(sockdir, tmp_path):
    ours = listening(sockdir / "w.sock")
    done = []
    cleaner = clean.Cleaner([sockdir, tmp_path], sockdir / "w.sock",
                            kill=lambda: done.append(sorted(os.listdir(tmp_path))))
    (sockdir / "left-by-a-job").write_text("x")
    (tmp_path / "plugins" / "vars").mkdir(parents=True)
    (tmp_path / "plugins" / "vars" / "stay.py").write_text("x")
    cleaner()
    assert done == [["plugins"]]  # the processes went while their files were still there, not after
    assert os.listdir(sockdir) == ["w.sock"] and os.listdir(tmp_path) == []
    ours.close()


def test_cleaning_fails_if_a_job_put_its_own_socket_where_the_workers_was(sockdir):
    ours = listening(sockdir / "w.sock")
    cleaner = clean.Cleaner([sockdir], sockdir / "w.sock", kill=lambda: None)
    cleaner()
    (sockdir / "w.sock").unlink()
    theirs = listening(sockdir / "w.sock")
    with pytest.raises(clean.Unclean, match="no longer the socket this worker opened"):
        cleaner()
    ours.close()
    theirs.close()


def test_cleaning_fails_if_a_job_changed_who_may_use_the_socket(sockdir):
    ours = listening(sockdir / "w.sock")
    cleaner = clean.Cleaner([sockdir], sockdir / "w.sock", kill=lambda: None)
    (sockdir / "w.sock").chmod(0o666)
    with pytest.raises(clean.Unclean, match="no longer the socket this worker opened"):
        cleaner()
    ours.close()


def test_cleaning_fails_if_a_directory_cant_be_emptied(sockdir):
    ours = listening(sockdir / "w.sock")
    cleaner = clean.Cleaner([sockdir / "not-there"], sockdir / "w.sock", kill=lambda: None)
    with pytest.raises(clean.Unclean, match="not-there couldn’t be emptied"):
        cleaner()
    with pytest.raises(clean.Unclean, match="isn’t a socket"):
        clean.Cleaner([sockdir], sockdir / "not-a-socket", kill=lambda: None)
    ours.close()


# --- when the worker does it --------------------------------------------------------------------------------

class Held:
    """A worker whose jobs, and whose cleaning, last until the test lets them end."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def gate(self, name: str) -> threading.Event:
        with self._lock:
            return self._gates.setdefault(name, threading.Event())

    def call(self, kind, job, send, cancel):
        self.log.append(f"{job['name']} starts")
        self.gate(job["name"]).wait(10)
        self.log.append(f"{job['name']} ends")
        return {"did": job["name"]}

    def clean(self) -> None:
        self.log.append("cleaning")
        self.gate("cleaning").wait(10)
        self.log.append("clean")


def start(sockdir: Path, held: Held, clean_with) -> tuple[Server, threading.Thread, list[dict]]:
    server = Server(sockdir / "w.sock", {"cli"}, os.getuid(), held)
    server.clean = clean_with
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    return server, serving, []


def send(sockdir: Path, name: str, results: list[dict]) -> None:
    threading.Thread(target=lambda: results.append(SocketWorker(sockdir / "w.sock").call("cli", {"name": name})),
                     daemon=True).start()


def test_a_worker_cleans_when_its_last_job_ends_and_the_next_job_waits_for_that(sockdir):
    held = Held()
    server, _, results = start(sockdir, held, held.clean)
    try:
        send(sockdir, "a", results)
        until(lambda: "a starts" in held.log)
        send(sockdir, "b", results)
        until(lambda: "b starts" in held.log)
        held.gate("a").set()
        until(lambda: {"did": "a"} in results)
        time.sleep(0.2)
        assert "cleaning" not in held.log  # b is still running: killing everything would take it too

        held.gate("b").set()
        until(lambda: "cleaning" in held.log)
        until(lambda: {"did": "b"} in results)  # b's answer didn't wait for the cleaning, which is still going
        assert "clean" not in held.log
        send(sockdir, "c", results)
        time.sleep(0.3)
        assert "c starts" not in held.log  # not in a container the jobs before haven't been cleared out of

        held.gate("cleaning").set()
        until(lambda: "c starts" in held.log)
        assert held.log == ["a starts", "b starts", "a ends", "b ends", "cleaning", "clean", "c starts"]
        held.gate("c").set()
        until(lambda: held.log[-2:] == ["cleaning", "clean"] and len(results) == 3)
        assert not server.unclean
    finally:
        server.close()


def test_a_worker_that_cant_clean_up_stops_and_starts_no_job_that_was_waiting(sockdir):
    held = Held()

    def fails() -> None:
        held.clean()
        raise clean.Unclean("42 is still running")

    server, serving, results = start(sockdir, held, fails)
    send(sockdir, "a", results)
    until(lambda: "a starts" in held.log)
    held.gate("a").set()
    until(lambda: "cleaning" in held.log)
    send(sockdir, "b", results)  # arrives while the container is being cleaned
    time.sleep(0.3)
    held.gate("cleaning").set()

    until(lambda: len(results) == 2)
    assert {"error": "the worker stopped before the job finished", "lost": True} in results
    assert "b starts" not in held.log
    serving.join(10)
    assert not serving.is_alive() and server.unclean  # `ansible-gui worker` then exits, and Docker starts it again
    assert "isn’t answering" in SocketWorker(sockdir / "w.sock").call("cli", {"name": "c"})["error"]
