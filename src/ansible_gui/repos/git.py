"""Keeps each clone at the tip of its branch: fetch, then a forced detached checkout.

The clones belong to ansible-gui. Local edits and untracked files in them are thrown
away on every sync, so what runs is always exactly what is in git.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .store import Repo

_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(rid: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(rid, threading.Lock())


class GitError(Exception):
    pass


@dataclass
class Synced:
    commit: str
    subject: str
    changed: bool


def _git(args: list[str], cwd: Path, env: dict, timeout: int = 180) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise GitError("git is not installed on the ansible-gui server.") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {timeout}s. Is the git server reachable from here?") from None
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or proc.stdout).strip().splitlines() if l.strip()]
        raise GitError(lines[-1] if lines else f"git {args[0]} failed with code {proc.returncode}.")
    return proc.stdout.strip()


def sync(repo: Repo, ssh_config: Path, private_key: str | None) -> Synced:
    """Fetch the branch and check out its tip. Raises GitError with a one-line reason."""
    with _lock(repo.id):
        workdir = Path(tempfile.mkdtemp(prefix="agui-git-"))
        try:
            ssh = ["ssh", "-F", str(ssh_config), "-o", "BatchMode=yes"]
            if private_key:
                key = workdir / "id"
                fd = os.open(key, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(private_key)
                ssh += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-i", str(key)]
            env = {**os.environ, "GIT_SSH_COMMAND": shlex.join(ssh), "GIT_TERMINAL_PROMPT": "0",
                   "GIT_ASKPASS": "/bin/false", "LC_ALL": "C"}
            path = repo.path
            path.mkdir(parents=True, exist_ok=True)
            if not (path / ".git").exists():
                _git(["init", "-q"], path, env)
                _git(["remote", "add", "origin", repo.url], path, env)
            else:
                _git(["remote", "set-url", "origin", repo.url], path, env)
            before = _head(path, env)
            _git(["fetch", "-q", "--prune", "--depth", "50", "origin",
                  f"+refs/heads/{repo.branch}:refs/remotes/origin/{repo.branch}"], path, env)
            _git(["checkout", "-q", "--force", "--detach", f"origin/{repo.branch}"], path, env)
            _git(["clean", "-q", "-ffdx"], path, env)
            commit = _git(["rev-parse", "HEAD"], path, env)
            subject = _git(["log", "-1", "--format=%s"], path, env)
            return Synced(commit=commit, subject=subject, changed=commit != before)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _head(path: Path, env: dict) -> str | None:
    try:
        return _git(["rev-parse", "-q", "--verify", "HEAD"], path, env)
    except GitError:
        return None


def remove_clone(repo: Repo) -> None:
    with _lock(repo.id):
        shutil.rmtree(repo.path, ignore_errors=True)
