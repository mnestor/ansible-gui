"""A clean container for the next job, without restarting it.

The runner and the playbooks it runs share one user. A playbook can leave a process behind, or a file Ansible
loads on its own (a plugin under ~/.ansible, a ~/.ansible.cfg), and either would be there for the next job:
another repository's, with its key and its vault password.

So whenever the last job in flight has ended, and before the next one starts, the worker puts the container
back as it started: it kills every process in it but PID 1 and itself, and empties every directory that can
be written. If that can't be done, the worker stops, and the container's restart does it instead.

Two jobs that run at the same time get nothing from this: they share the user, and each can read the other's
files while both run.

kill(-1) signals every process the caller may signal. In a container of the worker's own that is what is
wanted; anywhere else it is every process of whoever started the worker. own_container() tells the two apart,
and is asked again right before each kill.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Callable


class Unclean(Exception):
    """Something a job left is still there, or can't be looked for."""


def own_container() -> bool:
    """Whether every other process here is this worker's to kill.

    True for a process that is not root and is PID 1, or was started by a PID 1 that runs as the same user:
    tini, in a container started with `user:`. On a machine people use, PID 1 is root's."""
    if not sys.platform.startswith("linux") or os.getuid() == 0:
        return False
    if os.getpid() == 1:
        return True
    try:
        return os.getppid() == 1 and os.stat("/proc/1").st_uid == os.getuid()
    except OSError:
        return False


def hide() -> None:
    """Close this process to others of its user: its memory, its environment, its open files (Linux).

    A playbook runs as the worker's user. Where the host lets a user attach to its own processes
    (kernel.yama.ptrace_scope 0) it could otherwise change the worker itself, which cleaning doesn't replace."""
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")


def others() -> list[int]:
    """This user's processes that still run, apart from PID 1 and this one."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in (1, os.getpid()):
            continue
        try:
            status = Path("/proc", name, "status").read_text()
        except OSError:  # ended since it was listed
            continue
        fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
        # Ended, and waiting for its parent to notice. (With more than one thread, only its first has ended.)
        if fields.get("State", "").startswith("Z") and fields.get("Threads", "1").strip() == "1":
            continue
        if fields.get("Uid", "").split()[:1] == [str(os.getuid())]:
            found.append(int(name))
    return found


def kill_others(patience: float = 5.0) -> None:
    """Kill every process in the container but PID 1 and this one, and wait until they are gone.

    One kill(-1) gets them all: the kernel signals every process in one pass, and a fork in progress fails
    once its parent has a fatal signal waiting, so a process that forks as fast as it can gets no child out."""
    if not own_container():
        raise Unclean("this worker isn’t alone in a container of its own")
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:  # nothing else is running
        pass
    deadline = time.monotonic() + patience
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:  # those that were this process's own children
                pass
        except ChildProcessError:
            pass
        left = others()
        if not left:
            return
        if time.monotonic() >= deadline:
            raise Unclean(f"{len(left)} of this user’s processes are still running after SIGKILL: {left[:10]}")
        time.sleep(0.005)


def _files_out(fd: int, keep: frozenset[str] = frozenset()) -> list[str]:
    """Remove everything in the open directory that isn't a directory itself, and return the directories."""
    with os.scandir(fd) as entries:
        found = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries if e.name not in keep]
    for name, is_dir in found:
        if not is_dir:
            os.unlink(name, dir_fd=fd)  # a link is removed, never followed
    return [name for name, is_dir in found if is_dir]


def empty(top: Path, keep: frozenset[str] = frozenset()) -> None:
    """Remove everything in top, apart from the names to keep, whatever a job made of it: directories without
    permissions, links, a tree deeper than a path can name or Python can recurse into.

    One directory is open at a time, and every name is taken relative to it."""
    try:
        os.chmod(top, stat.S_IMODE(os.stat(top).st_mode) | 0o700)  # a job may have closed its own home
    except OSError:
        pass  # not this user's directory (/tmp), so not a job's to change either
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    names: list[str] = []  # the open directory's name in its parent, and so on up to top
    todo = [_files_out(fd, keep)]  # for each of those and for top: the directories in it still to remove
    try:
        while True:
            if todo[-1]:
                name = todo[-1].pop()
                os.chmod(name, 0o700, dir_fd=fd)
                below = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = below
                names.append(name)
                todo.append(_files_out(fd))
            elif names:
                above = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = above
                todo.pop()
                os.rmdir(names.pop(), dir_fd=fd)
            else:
                return
    finally:
        os.close(fd)


class Cleaner:
    """Puts the container back as it was when the worker started. Calling it raises Unclean if it couldn't.

    Made once the worker's socket is open and before the first job: the socket is in a directory a playbook
    can write, and one that put its own there would get the jobs that follow."""

    def __init__(self, directories: list[Path], socket: Path, kill: Callable[[], None] = kill_others):
        self.directories = [Path(d).resolve() for d in directories]
        self.socket = socket.parent.resolve() / socket.name
        self.kill = kill
        self._opened = self._socket()
        if self._opened is None:
            raise Unclean(f"{self.socket} isn’t a socket")

    def _socket(self) -> tuple[int, int, int] | None:
        try:
            st = os.lstat(self.socket)
        except OSError:
            return None
        return (st.st_dev, st.st_ino, st.st_mode) if stat.S_ISSOCK(st.st_mode) else None

    def __call__(self) -> None:
        self.kill()
        for d in self.directories:
            try:
                empty(d, frozenset({self.socket.name}) if d == self.socket.parent else frozenset())
            except OSError as e:
                raise Unclean(f"{d} couldn’t be emptied: {e}") from None
        if self._socket() != self._opened:
            raise Unclean(f"{self.socket} is no longer the socket this worker opened")
