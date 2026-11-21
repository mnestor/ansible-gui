"""Where the inventory and the playbooks come from: git clones, or ANSIBLE_GUI_PROJECT_DIR.

One inventory repository, any number of playbook repositories. Without git repos the
directory in ANSIBLE_GUI_PROJECT_DIR serves as both, as before. A git repo of a role
replaces the local directory for that role.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from ..credentials import ssh as sshtools
from ..inventory.cli import CliOptions
from ..runs import playbooks
from ..scratch import Scratch
from ..worker import InlineWorker, Worker
from . import git
from .store import Repo, RepoStore

log = logging.getLogger(__name__)

GIT_CANCELED = "Canceled: couldn’t update"


@dataclass
class Playbook:
    repo: Repo
    path: str

    @property
    def key(self) -> str:
        return f"{self.repo.id}:{self.path}"


def split_key(key: str) -> tuple[int, str]:
    rid, _, path = (key or "").partition(":")
    return (int(rid), path) if rid.isdigit() and path else (-1, "")


class Workspace:
    def __init__(self, settings, repos: RepoStore, creds, fetcher: Worker | None = None,
                 runner: Worker | None = None):
        self.settings = settings
        self.repos = repos
        self.creds = creds
        inline = InlineWorker(Scratch(settings.data_dir)) if not (fetcher and runner) else None
        self.fetcher = fetcher or inline  # writes the repos volume: pulls, commit directories, collections
        self.runner = runner or inline  # runs what is in it: the inventory, previews, settings, runs
        self.on_inventory_changed = lambda inv_id: None
        self._lock = threading.Lock()
        self._pins: dict[int, set[tuple[int, str]]] = {}  # run id -> {(repo id, commit)} it is using
        self._fresh: set[str] = set()  # collections just installed, not yet named by a recorded commit
        self._digest_locks: dict[str, threading.Lock] = {}
        self._at: dict[int, str] = {}  # in a view from at(): {repo id: commit} to read instead of the current one

    def at(self, commits: dict[int, str] | None) -> "Workspace":
        """A view of the workspace that reads these repos at these commits, and the rest at their current one."""
        if not commits:
            return self
        view = copy.copy(self)
        view._at = dict(commits)
        return view

    def _view(self, repos: list[Repo]) -> list[Repo]:
        return [r.at(self._at[r.id]) if r.id in self._at else r for r in repos]

    # --- which repos --------------------------------------------------------------
    def _local(self, role: str) -> Repo | None:
        s = self.settings
        if not s.project_dir:
            return None
        paths = ",".join(s.inventory) if role == "inventory" else s.playbook_dir
        return Repo(id=0, name=s.project_dir.name or "local", role=role, url="", branch="", ssh_key_id=None,
                    paths=paths, edit_url=s.edit_url or "", path=s.project_dir, local=True)

    def git_repos(self) -> list[Repo]:
        return self.repos.all()

    def inventory_repos(self) -> list[Repo]:
        return self._view([r for r in self.repos.all() if r.role == "inventory"]) or [
            x for x in [self._local("inventory")] if x]

    def inventory_repo(self, inv_id: int | None = None) -> Repo | None:
        """By id; with None (a run from before there could be several), the only inventory if there is one."""
        invs = self.inventory_repos()
        if inv_id is None:
            return invs[0] if len(invs) == 1 else None
        return next((r for r in invs if r.id == inv_id), None)

    def playbook_repos(self) -> list[Repo]:
        return self._view([r for r in self.repos.all() if r.role == "playbooks"]) or [
            x for x in [self._local("playbooks")] if x]

    def playbook_repo(self, rid: int) -> Repo | None:
        return next((r for r in self.playbook_repos() if r.id == rid), None)

    # --- ansible options ------------------------------------------------------------
    @staticmethod
    def inventory_sources(inv: Repo | None) -> list[str]:
        return [str((inv.path / s).resolve()) for s in inv.sources] if inv else []

    def inventory_opts(self, inv: Repo) -> CliOptions | None:
        if not inv.path.is_dir():
            return None
        playbook_dir = self.settings.playbook_dir if inv.local else "."
        return CliOptions(project_dir=inv.path, sources=inv.sources, playbook_dir=playbook_dir,
                          vault_password_file=self.settings.vault_password_file, binary=self.settings.inventory_binary,
                          worker=self.runner)

    def run_opts(self, repo: Repo, inv: Repo | None) -> CliOptions:
        """Ansible starts in the repo's run directory, so its ansible.cfg applies; playbooks are passed
        as absolute paths and the inventory as absolute sources."""
        return CliOptions(project_dir=repo.run_dir, sources=self.inventory_sources(inv), playbook_dir=repo.playbook_dir,
                          vault_password_file=self.settings.vault_password_file, env=self.ansible_env(repo),
                          worker=self.runner)

    def ssh_job(self) -> dict:
        """The ssh settings every pull and run job carries."""
        s = self.settings
        return sshtools.for_job(s.data_dir, s.ssh_config, s.ssh_host_key_checking)

    def learn_host_keys(self, result: dict) -> None:
        if result.get("host_keys"):
            sshtools.learn_host_keys(self.settings.data_dir, result["host_keys"])

    # --- collections ----------------------------------------------------------------
    # Kept like commits: one directory per requirements file, named after its SHA-256, written once.
    @property
    def collections_root(self) -> Path:
        return self.repos.root / "collections"

    def requirements_file(self, repo: Repo) -> Path | None:
        if repo.requirements:
            return repo.path / repo.requirements
        run = Path(repo.playbook_dir)
        for rel in (run / "collections/requirements.yml", run / "requirements.yml",
                    Path("collections/requirements.yml"), Path("requirements.yml")):
            if (repo.path / rel).is_file():
                return repo.path / rel
        return None

    def collections_digest(self, repo: Repo) -> str | None:
        """The SHA-256 of the requirements file in the repo's commit, which names its collections directory."""
        if repo.local or repo.role != "playbooks":
            return None
        req = self.requirements_file(repo)
        if req is None or not req.is_file():
            return None
        return hashlib.sha256(req.read_bytes()).hexdigest()

    def collections_dir(self, repo: Repo) -> Path | None:
        digest = self.collections_digest(repo)
        return self.collections_root / digest if digest else None

    def ansible_env(self, repo: Repo) -> dict:
        if repo.local:
            return {}
        own = self.collections_dir(repo)
        base = ["~/.ansible/collections", "/usr/share/ansible/collections"]
        return {"ANSIBLE_COLLECTIONS_PATH": ":".join([*([str(own)] if own else []), *base])}

    def install_collections(self, repo: Repo, log_line=lambda _l: None) -> str | None:
        """Install the collections the repo's commit asks for, unless their directory is already there.
        None, or why it failed."""
        if repo.local or repo.role != "playbooks":
            return None
        req = self.requirements_file(repo)
        if req is None:
            return None
        if not req.is_file():
            return f"its collections file {repo.requirements} isn’t in the repository"
        digest = hashlib.sha256(req.read_bytes()).hexdigest()
        target = self.collections_root / digest
        with self._collections_lock(digest):
            with self._lock:
                self._fresh.add(digest)  # until the commit asking for it is recorded or pinned: see _prune_collections
            if target.is_dir():
                return None
            log_line(f"[ansible-gui] Installing collections from {req.relative_to(repo.path)}")
            res = self.fetcher.call("collections", {"requirements": str(req), "run_dir": str(repo.run_dir),
                                                    "target": str(target)})
            if "error" in res:
                return f"installing its collections failed: {res['error']}"
            log_line("[ansible-gui] Collections installed")
            return None

    def _collections_lock(self, digest: str) -> threading.Lock:
        with self._lock:
            return self._digest_locks.setdefault(digest, threading.Lock())

    def _prune_collections(self) -> None:
        """Remove every collections directory that no repository's current commit and no running run asks for."""
        keep = set()
        for repo in self.playbook_repos():
            if d := self.collections_digest(repo):
                keep.add(d)
        with self._lock:
            pinned = [(rid, c) for pins in self._pins.values() for rid, c in pins]
            keep |= self._fresh
        for rid, commit in pinned:
            repo = self.repos.get(rid)
            if repo and (d := self.collections_digest(repo.at(commit))):
                keep.add(d)
        if self.collections_root.is_dir():
            self.fetcher.call("prune_collections", {"root": str(self.collections_root), "keep": sorted(keep)})

    def discover(self) -> list[Playbook]:
        out = []
        for repo in self.playbook_repos():
            if not repo.path.is_dir():
                continue
            try:
                out += [Playbook(repo, p) for p in playbooks.discover(repo.path, repo.playbook_dir)]
            except OSError:
                log.warning("can't list playbooks in %s", repo.path)
        return out

    def find(self, key: str) -> Playbook | None:
        """By "repo_id:path", or by a bare path when exactly one repo has it (old links, the API)."""
        books = self.discover()
        hit = next((p for p in books if p.key == key), None)
        if hit or ":" in (key or ""):
            return hit
        same = [p for p in books if p.path == key]
        return same[0] if len(same) == 1 else None

    def canonical(self, key: str) -> str:
        pb = self.find(key)
        return pb.key if pb else key

    def label(self, repo_id: int, path: str) -> str:
        """How a playbook is named on screen: with its repo once there is more than one."""
        if len(self.playbook_repos()) <= 1:
            return path
        repo = self.playbook_repo(repo_id)
        return f"{repo.name if repo else 'removed repo'} / {path}"

    # --- git ----------------------------------------------------------------------------
    def _git_auth(self, repo: Repo) -> tuple[str | None, tuple[str, str] | None, str | None]:
        """(private key, (user, token), error) for pulling the repo."""
        if not repo.ssh_key_id:
            return None, None, None
        cred = self.creds.get(repo.ssh_key_id)
        want = "git_token" if git.is_https(repo.url) else "ssh_key"
        if not cred or not cred.readable or cred.kind != want:
            return None, None, ("its sign-in credential was deleted, can’t be decrypted, or doesn’t suit the URL. "
                                "Pick another on the Repositories page.")
        secret = self.creds.secret(cred)
        if want == "ssh_key":
            return secret["private_key"], None, None
        return None, (cred.meta["username"], secret["token"]), None

    def _fetch(self, repo: Repo) -> git.Fetched:
        """Fetch into the bare clone. Call it holding git.locked(repo.home). Raises GitError."""
        key, token, error = self._git_auth(repo)
        if error:
            raise git.GitError(error)
        res = self.fetcher.call("fetch", {"home": str(repo.home), "url": repo.url, "branch": repo.branch,
                                          "private_key": key, "token": list(token) if token else None,
                                          "ssh": self.ssh_job()})
        self.learn_host_keys(res)
        if "error" in res:
            raise git.GitError(str(res["error"]))
        if not git.SHA.match(str(res.get("commit"))):  # it names a directory: never take a path from a worker
            raise git.GitError(f"the fetcher answered with something that isn’t a commit: {res.get('commit')!r}")
        return git.Fetched(commit=res["commit"], subject=str(res.get("subject") or ""))

    def _materialize(self, repo: Repo, commit: str) -> None:
        """Write the commit's directory out if it isn't there. Call it holding git.locked(repo.home).
        Raises CommitGone, or GitError."""
        res = self.fetcher.call("materialize", {"home": str(repo.home), "commit": commit})
        if res.get("gone"):
            raise git.CommitGone(str(res["error"]))
        if "error" in res:
            raise git.GitError(str(res["error"]))

    def _prune(self, repo: Repo) -> None:
        """Call it holding git.locked(repo.home)."""
        self.fetcher.call("prune", {"home": str(repo.home), "keep": sorted(self._keep(repo.id))})

    def sync(self, repo: Repo, pin_for: int | None = None, on_wait=None) -> str | None:
        """Pull one repo: fetch the branch, write its tip out into its own directory, install the collections it
        asks for, and make it the repo's current commit. Returns None, or why it failed; on a failure the
        current commit stays what it was. With pin_for, the commit is kept for that run until release_run().
        on_wait() is called if another pull of this repo has to finish first."""
        if repo.local:
            return None
        try:
            with git.locked(repo.home, on_wait):
                fetched = self._fetch(repo)
                self._materialize(repo, fetched.commit)
                try:
                    error = self.install_collections(repo.at(fetched.commit))
                    if error:
                        error = error[0].upper() + error[1:] + "."
                        self.repos.record_fetch(repo.id, None, None, error)
                        return error
                    previous = (self.repos.get(repo.id) or repo).last_commit
                    self.repos.record_fetch(repo.id, fetched.commit, fetched.subject, None)
                    if pin_for is not None:
                        self._pin(pin_for, repo.id, fetched.commit)
                finally:
                    self._unfresh(repo.at(fetched.commit))
                self._prune(repo)
        except git.GitError as e:
            self.repos.record_fetch(repo.id, None, None, str(e))
            return str(e)
        self._prune_collections()
        if repo.role == "inventory" and fetched.commit != previous:
            self.on_inventory_changed(repo.id)
        return None

    def ensure_commit(self, repo: Repo, commit: str, pin_for: int | None = None,
                      on_wait=None) -> tuple[str | None, bool]:
        """Make sure the commit's directory and its collections are there, writing them out again if pruning
        removed them, fetching first if the bare clone lacks the commit. Returns (None, or why it can't be had;
        whether its collections had to be installed again)."""
        if repo.local:
            return None, False
        try:
            with git.locked(repo.home, on_wait):
                try:
                    self._materialize(repo, commit)
                except git.CommitGone:
                    self._fetch(repo)  # it may only be missing from this copy of the clone
                    self._materialize(repo, commit)
                if pin_for is not None:
                    self._pin(pin_for, repo.id, commit)
        except git.CommitGone:
            return (f"commit {commit[:10]} of {repo.name} isn’t in the repository any more: the branch was probably "
                    "force-pushed."), False
        except git.GitError as e:
            return f"couldn’t get commit {commit[:10]} of {repo.name}: {e}", False
        own = self.collections_dir(repo.at(commit))
        missing = own is not None and not own.is_dir()
        try:
            error = self.install_collections(repo.at(commit))
        finally:
            self._unfresh(repo.at(commit))
        return (f"{repo.name}: {error}." if error else None), missing and not error

    def _unfresh(self, repo: Repo) -> None:
        if digest := self.collections_digest(repo):
            with self._lock:
                self._fresh.discard(digest)

    def _pin(self, run_id: int, repo_id: int, commit: str) -> None:
        with self._lock:
            self._pins.setdefault(run_id, set()).add((repo_id, commit))

    def pinned(self, run_id: int) -> dict[int, str]:
        """{repo id: commit} the run is using."""
        with self._lock:
            return dict(self._pins.get(run_id, set()))

    def user(self, repo_id: int) -> int | None:
        """A run using one of this repo's commits, if any."""
        with self._lock:
            return next((run for run, pins in self._pins.items() if any(r == repo_id for r, _ in pins)), None)

    def _keep(self, repo_id: int) -> set[str]:
        """Commits of the repo that pruning must leave: the current one and every running run's."""
        repo = self.repos.get(repo_id)
        with self._lock:
            pinned = {c for pins in self._pins.values() for r, c in pins if r == repo_id}
        return pinned | ({repo.last_commit} if repo and repo.last_commit else set())

    def release_run(self, run_id: int) -> None:
        """The run is over: its commits may go, unless they are current or another run uses them."""
        with self._lock:
            pins = self._pins.pop(run_id, set())
        for rid in {r for r, _ in pins}:
            repo = self.repos.get(rid)
            if repo:
                with git.locked(repo.home):
                    self._prune(repo)
        if pins:
            self._prune_collections()

    def restore(self) -> None:
        """Write out each repository's current commit if its directory is missing: the repositories volume
        was recreated while data/ (which records the commits) was kept. Fetches if it has to."""
        for repo in self.repos.all():
            if repo.last_commit and not repo.path.is_dir():
                error, _ = self.ensure_commit(repo, repo.last_commit)
                if error:
                    log.warning("couldn’t restore %s at %s: %s", repo.name, repo.last_commit[:10], error)

    def remove(self, repo: Repo) -> None:
        if not repo.local:
            with git.locked(repo.home):
                self.fetcher.call("remove", {"home": str(repo.home)})
            self._prune_collections()

    def sync_for_run(self, repo: Repo, inv: Repo | None, log_line, run_id: int) -> tuple[str | None, dict[int, str]]:
        """Pull the run's inventory repo and playbook repo, keeping the commits they land on for the run.
        (None, {repo id: commit}), or (a message saying what failed, {})."""
        targets = [r for r in (inv, repo) if r and not r.local]
        for r in dict((x.id, x) for x in targets).values():
            log_line(f"[ansible-gui] Pulling {r.name} ({r.branch}) from {r.url}")
            error = self.sync(r, pin_for=run_id, on_wait=lambda r=r: log_line(
                f"[ansible-gui] Waiting for a pull of {r.name} started from the web UI to finish"))
            if error:
                log_line(f"[ansible-gui] Pull failed: {error}")
                return f"{GIT_CANCELED} {r.name} from git: {error.rstrip('.')}. Nothing ran.", {}
            commit = self.pinned(run_id)[r.id]
            fresh = self.repos.get(r.id)
            subject = fresh.last_subject if fresh and fresh.last_commit == commit else ""
            log_line(f"[ansible-gui] {r.name} is at {commit[:10]} {subject or ''}")
        return None, self.pinned(run_id)
