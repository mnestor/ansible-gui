"""Temp directories for keys, passwords and secret environment variables, each noted in the data directory.

They are made in the system temp directory, not under data/: the path has to be short enough for an
ssh-agent socket, and nothing under data/ holds a secret in plain text. While one exists, a note in
data/scratch/ says where it is and which processes belong to it. A server that stops normally wipes
its own; one that is killed (SIGKILL, the OOM killer, a power cut) can't, so the next start reads the
notes and finishes the job: the run's private key, vault password and environment secrets, its ssh-agent
(which still holds the key) and the playbook it left running.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .wipe import wipe_tree

PREFIX = "agui-"


class Scratch:
    def __init__(self, data_dir: Path):
        self.notes = data_dir / "scratch"

    def make(self, kind: str = "") -> Path:
        """A new private directory (0700, like any mkdtemp), noted before anything is written into it."""
        path = Path(tempfile.mkdtemp(prefix=PREFIX + kind))
        self.notes.mkdir(parents=True, exist_ok=True)
        (self.notes / path.name).write_text(f"{path}\n")
        return path

    def note(self, path: Path, role: str, pid: int) -> None:
        """A process that must not outlive the directory: its ssh-agent ("agent"), its playbook ("play")."""
        try:
            fd = os.open(self.notes / path.name, os.O_WRONLY | os.O_APPEND)  # never creates: a dropped one stays dropped
        except OSError:
            return
        with os.fdopen(fd, "w") as f:
            f.write(f"{role} {pid}\n")

    def drop(self, path: Path) -> None:
        """Wipe the directory and forget it."""
        wipe_tree(path)
        (self.notes / path.name).unlink(missing_ok=True)

    def leftovers(self) -> list[tuple[Path, dict[str, int]]]:
        """(directory, {role: pid}) for every note: what a server that didn't stop normally left behind.

        A note is only believed if it names a directory this module could have made and this user owns;
        any other is removed here, with nothing done to what it points at."""
        found = []
        for note in sorted(self.notes.iterdir()) if self.notes.is_dir() else []:
            try:
                lines = note.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                lines = []
            path = Path(lines[0]) if lines else Path()
            pids = {role: int(pid) for role, _, pid in (line.partition(" ") for line in lines[1:]) if pid.isdigit()}
            if self._ours(path, note.name):
                found.append((path, pids))
            else:
                note.unlink(missing_ok=True)
        return found

    @staticmethod
    def _ours(path: Path, name: str) -> bool:
        if not (path.is_absolute() and path.name == name and name.startswith(PREFIX)):
            return False
        try:
            st = os.lstat(path)
        except OSError:  # already gone
            return False
        return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
