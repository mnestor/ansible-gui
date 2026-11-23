"""One bare clone per repository, and each commit written out once into its own directory.

    <home>/git/                 the bare clone: git's objects, no working files
    <home>/commits/<sha>/       one commit, whole, never written again; no .git inside

A commit directory is built under a temporary name beside it and renamed into place when it is complete,
so a directory that exists is whole. Nothing ever writes into one again: a pull lands on a new commit
in a new directory, and a run keeps reading the one it started with. Pruning removes the ones nobody uses.

"Never written again" is also in the file modes: seal() takes write permission off a finished directory. The
fetcher writes every repository as one user, so without that a program it runs for one repository, steered by
that repository's files, could write into another's. Only remove_tree() gives the permission back.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SHA = re.compile(r"^[0-9a-f]{40}$")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(home: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(home), threading.Lock())


@contextmanager
def locked(home: Path, on_wait=None):
    """Hold the repository's lock, calling on_wait() first if someone else holds it: fetches into one bare
    clone take turns, and so do writing out and pruning its commit directories. Held by the app around the
    jobs it sends, so it also orders them on a worker in another container."""
    lock = _lock(home)
    if not lock.acquire(blocking=False):
        if on_wait:
            on_wait()
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def seal(tree: Path) -> None:
    """Take write permission off every directory and file in tree, for everyone. Links are left alone:
    chmod would follow them out of the tree."""
    for root, _dirs, files in os.walk(tree):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode) & ~0o222)


def remove_tree(path: Path) -> None:
    """Remove a directory that may be sealed, or a file. Nothing happens if it isn't there."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for root, _dirs, _files in os.walk(path):  # entries can only be removed from a directory one may write
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


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


def is_ssh(url: str) -> bool:
    """A URL git reaches through ssh: ssh://host/path, or the scp-like user@host:path."""
    if "://" in url:
        return url.startswith(("ssh://", "git+ssh://", "ssh+git://"))
    return ":" in url.split("/", 1)[0] and not url.startswith(("/", "."))


def _base_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "LC_ALL": "C",
            # No credential helpers (osxkeychain, store, a manager): nothing is saved or read from them.
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}


# Answers git's request for a user name and password from the environment, and nothing else (git also tells
# a helper to store or erase). A shell function in git's configuration, so there is no file to run.
_TOKEN_HELPER = ('!f() { test "$1" = get && printf \'username=%s\\npassword=%s\\n\' '
                 '"$AGUI_GIT_USER" "$AGUI_GIT_TOKEN"; }; f')


def token_env(url: str, token: tuple[str, str]) -> dict:
    """What git's environment needs to sign in to url's server with (user, token), on top of _base_env().

    The token reaches git through a credential helper for that one server, so it never lands in the URL or
    the clone's config, nothing is saved, and a redirect to another server isn't offered it. Not through a
    GIT_ASKPASS script: the temp directory a worker has is a tmpfs mounted noexec, where git can't run one."""
    if not url.startswith("https://"):
        raise GitError("A git token is only sent over https://; this URL would send it unencrypted.")
    if any(ord(ch) < 32 for ch in "".join(token)):
        raise GitError("The git token has a line break or control character in it. Enter it again under Secrets.")
    parts = urlsplit(url)
    if not parts.hostname:
        raise GitError("The clone URL has no server name in it.")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    server = f"https://{host}" + (f":{parts.port}" if parts.port else "")
    return {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": f"credential.{server}.helper",
            "GIT_CONFIG_VALUE_1": _TOKEN_HELPER, "AGUI_GIT_USER": token[0], "AGUI_GIT_TOKEN": token[1]}


def fetch(home: Path, url: str, branch: str, ssh_config: Path, private_key: str | None = None,
          token: tuple[str, str] | None = None, *, workdir: Path) -> Fetched:
    """Fetch the branch, whole, into the bare clone. Raises GitError.

    private_key signs in over SSH, from a file in workdir, a private temp directory the caller wipes; token
    is (user, token) for HTTPS, handed to git as token_env() says."""
    if token and not url.startswith("https://"):
        raise GitError("A git token is only sent over https://; this URL would send it unencrypted.")
    bare = home / "git"
    if (home / ".git").exists():  # a working copy from before commit directories: start again
        remove_tree(home)
    ssh = ["ssh", "-F", str(ssh_config), "-o", "BatchMode=yes"]
    if private_key:
        key = workdir / "id"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key)
        ssh += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-i", str(key)]
    env = {**_base_env(), "GIT_SSH_COMMAND": shlex.join(ssh)}
    if token:
        env.update(token_env(url, token))
    if not (bare / "HEAD").exists():
        remove_tree(bare)
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
    kept, and nothing left out the way .gitattributes export-ignore would leave it out of an archive. Sealed
    before it gets its name."""
    dest = commit_dir(home, commit)
    if dest.is_dir():
        return dest
    if not has_commit(home, commit):
        raise CommitGone(f"commit {commit[:12]} isn’t in the repository any more")
    tmp = dest.with_name(f".{commit}.partial")
    remove_tree(tmp)
    tmp.mkdir(parents=True)
    index = dest.with_name(f".{commit}.index")
    env = {**_base_env(), "GIT_INDEX_FILE": str(index), "GIT_DIR": str(home / "git"), "GIT_WORK_TREE": str(tmp)}
    try:
        _git(["read-tree", commit], tmp, env)
        _git(["checkout-index", "--all", "--force"], tmp, env)
    except GitError:
        remove_tree(tmp)
        raise
    finally:
        index.unlink(missing_ok=True)
    seal(tmp)
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
        remove_tree(d)
        if SHA.match(d.name):
            gone.append(d.name)
    return gone


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
