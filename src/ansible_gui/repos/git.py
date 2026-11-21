"""One bare clone per repository, and each commit written out once into its own directory.

    <home>/git/                 the bare clone: git's objects, no working files
    <home>/commits/<sha>/       one commit, whole, never written again; no .git inside

A commit directory is built under a temporary name beside it and renamed into place when it is complete,
so a directory that exists is whole. Nothing ever writes into one again: a pull lands on a new commit
in a new directory, and a run keeps reading the one it started with. Pruning removes the ones nobody uses.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..scratch import Scratch

SHA = re.compile(r"^[0-9a-f]{40}$")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(home: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(home), threading.Lock())


@contextmanager
def locked(home: Path, on_wait=None):
    """Hold the repository's lock, calling on_wait() first if someone else holds it: fetches into one bare
    clone take turns, and so do writing out and pruning its commit directories."""
    lock = _lock(home)
    if not lock.acquire(blocking=False):
        if on_wait:
            on_wait()
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


class GitError(Exception):
    pass


class CommitGone(GitError):
    """The bare clone doesn't have this commit, not even after fetching: it was force-pushed away."""


@dataclass
class Fetched:
    commit: str
    subject: str


def _git(args: list[str], cwd: Path, env: dict, timeout: int = 180) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise GitError("git is not installed on the ansible-gui server.") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} took longer than {timeout}s. Is the git server reachable from here?") from None
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or proc.stdout).strip().splitlines() if l.strip()]
        raise GitError(_plain(lines[-1] if lines else f"git {args[0]} failed with code {proc.returncode}."))
    return proc.stdout.strip()


def is_https(url: str) -> bool:
    return url.startswith(("https://", "http://"))


def _base_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "LC_ALL": "C",
            # No credential helpers (osxkeychain, store, a manager): nothing is saved or read from them.
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}


def fetch(home: Path, url: str, branch: str, ssh_config: Path, private_key: str | None = None,
          token: tuple[str, str] | None = None, *, scratch: Scratch) -> Fetched:
    """Fetch the branch, whole, into the bare clone. Call it holding locked(home). Raises GitError.

    private_key signs in over SSH; token is (user, token) for HTTPS, handed to git through GIT_ASKPASS so it
    never lands in the URL, the clone's config or a credential helper. The temp directory holding either is
    noted in scratch, so a server killed mid-pull has it wiped by the next start."""
    if token and not url.startswith("https://"):
        raise GitError("A git token is only sent over https://; this URL would send it unencrypted.")
    bare = home / "git"
    if (home / ".git").exists():  # a working copy from before commit directories: start again
        shutil.rmtree(home)
    workdir = scratch.make("git-")
    try:
        ssh = ["ssh", "-F", str(ssh_config), "-o", "BatchMode=yes"]
        if private_key:
            key = workdir / "id"
            fd = os.open(key, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(private_key)
            ssh += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-i", str(key)]
        env = {**_base_env(), "GIT_SSH_COMMAND": shlex.join(ssh)}
        if token:
            askpass = workdir / "askpass"
            fd = os.open(askpass, os.O_WRONLY | os.O_CREAT, 0o700)
            with os.fdopen(fd, "w") as f:
                f.write('#!/bin/sh\ncase "$1" in Username*) printf %s "$AGUI_GIT_USER";; *) printf %s "$AGUI_GIT_TOKEN";; esac\n')
            env.update(GIT_ASKPASS=str(askpass), AGUI_GIT_USER=token[0], AGUI_GIT_TOKEN=token[1])
        if not (bare / "HEAD").exists():
            shutil.rmtree(bare, ignore_errors=True)
            bare.mkdir(parents=True)
            _git(["init", "-q", "--bare"], bare, env)
            _git(["remote", "add", "origin", url], bare, env)
        else:
            _git(["remote", "set-url", "origin", url], bare, env)
        # The whole branch, not the last few commits: running an older run's commits again needs them.
        _git(["fetch", "-q", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], bare, env)
        commit = _git(["rev-parse", f"refs/remotes/origin/{branch}^{{commit}}"], bare, env)
        subject = _git(["log", "-1", "--format=%s", commit], bare, env)
        return Fetched(commit=commit, subject=subject)
    finally:
        scratch.drop(workdir)


def has_commit(home: Path, commit: str) -> bool:
    if not SHA.match(commit or "") or not (home / "git" / "HEAD").exists():
        return False
    try:
        _git(["cat-file", "-e", f"{commit}^{{commit}}"], home / "git", _base_env(), timeout=30)
    except GitError:
        return False
    return True


def commit_dir(home: Path, commit: str) -> Path:
    if not SHA.match(commit or ""):
        raise ValueError(f"not a full commit hash: {commit!r}")
    return home / "commits" / commit


def materialize(home: Path, commit: str) -> Path:
    """The commit's directory, written out from the bare clone if it isn't there. Call it holding
    locked(home). Raises CommitGone if the bare clone doesn't have the commit.

    Written through a temporary index, file for file as git checks it out: symlinks and executable bits
    kept, and nothing left out the way .gitattributes export-ignore would leave it out of an archive."""
    dest = commit_dir(home, commit)
    if dest.is_dir():
        return dest
    if not has_commit(home, commit):
        raise CommitGone(f"commit {commit[:12]} isn’t in the repository any more")
    tmp = dest.with_name(f".{commit}.partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    index = dest.with_name(f".{commit}.index")
    env = {**_base_env(), "GIT_INDEX_FILE": str(index), "GIT_DIR": str(home / "git"), "GIT_WORK_TREE": str(tmp)}
    try:
        _git(["read-tree", commit], tmp, env)
        _git(["checkout-index", "--all", "--force"], tmp, env)
    except GitError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        index.unlink(missing_ok=True)
    tmp.rename(dest)
    return dest


def prune(home: Path, keep: set[str]) -> list[str]:
    """Remove every commit directory not in keep, and anything half-written. Call it holding locked(home)."""
    gone = []
    commits = home / "commits"
    if not commits.is_dir():
        return gone
    for d in commits.iterdir():
        if d.name in keep:
            continue
        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
        if SHA.match(d.name):
            gone.append(d.name)
    return gone


def remove(home: Path) -> None:
    """Delete the bare clone and every commit directory."""
    with locked(home):
        shutil.rmtree(home, ignore_errors=True)


_PLAIN = [
    ("could not read Username", "The git server wants a sign-in. Pick a git token for this repository."),
    ("Authentication failed", "The git server refused the token. Check its user name, and that it hasn’t expired."),
    ("Permission denied (publickey", "The git server refused the SSH key. Add its public key as a deploy key."),
    ("Host key verification failed", "The git server’s SSH host key isn’t in known hosts, or doesn’t match "
     "it. Add the key under Known hosts on the Credentials page."),
    ("couldn't find remote ref", "That branch doesn’t exist in the repository."),
    ("Could not resolve host", "The git server’s name doesn’t resolve from the ansible-gui server."),
]


def _plain(line: str) -> str:
    for needle, says in _PLAIN:
        if needle in line:
            return f"{says} (git: {line.removeprefix('fatal: ')})"
    return line.removeprefix("fatal: ")
