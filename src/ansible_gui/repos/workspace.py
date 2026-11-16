"""Where the inventory and the playbooks come from: git clones, or ANSIBLE_GUI_PROJECT_DIR.

One inventory repository, any number of playbook repositories. Without git repos the
directory in ANSIBLE_GUI_PROJECT_DIR serves as both, as before. A git repo of a role
replaces the local directory for that role.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..credentials import ssh as sshtools
from ..inventory.cli import CliOptions
from ..runs import playbooks
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
    def __init__(self, settings, repos: RepoStore, creds):
        self.settings = settings
        self.repos = repos
        self.creds = creds
        self.on_inventory_changed = lambda: None

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

    def inventory_repo(self) -> Repo | None:
        return next((r for r in self.repos.all() if r.role == "inventory"), None) or self._local("inventory")

    def playbook_repos(self) -> list[Repo]:
        return [r for r in self.repos.all() if r.role == "playbooks"] or [x for x in [self._local("playbooks")] if x]

    def playbook_repo(self, rid: int) -> Repo | None:
        return next((r for r in self.playbook_repos() if r.id == rid), None)

    # --- ansible options ------------------------------------------------------------
    def inventory_sources(self) -> list[str]:
        inv = self.inventory_repo()
        return [str((inv.path / s).resolve()) for s in inv.sources] if inv else []

    def inventory_opts(self) -> CliOptions | None:
        inv = self.inventory_repo()
        if not inv or not inv.path.is_dir():
            return None
        playbook_dir = self.settings.playbook_dir if inv.local else "."
        return CliOptions(project_dir=inv.path, sources=inv.sources, playbook_dir=playbook_dir,
                          vault_password_file=self.settings.vault_password_file, binary=self.settings.inventory_binary)

    def run_opts(self, repo: Repo) -> CliOptions:
        """Ansible starts in the repo's run directory, so its ansible.cfg applies; playbooks are passed
        as absolute paths and the inventory as absolute sources."""
        return CliOptions(project_dir=repo.run_dir, sources=self.inventory_sources(), playbook_dir=repo.playbook_dir,
                          vault_password_file=self.settings.vault_password_file, env=self.ansible_env(repo))

    # --- collections ----------------------------------------------------------------
    def collections_dir(self, repo: Repo) -> Path:
        return self.settings.data_dir / "collections" / str(repo.id)

    def ansible_env(self, repo: Repo) -> dict:
        if repo.local:
            return {}
        base = ["~/.ansible/collections", "/usr/share/ansible/collections"]
        return {"ANSIBLE_COLLECTIONS_PATH": ":".join([str(self.collections_dir(repo)), *base])}

    def requirements_file(self, repo: Repo) -> Path | None:
        if repo.requirements:
            return repo.path / repo.requirements
        run = Path(repo.playbook_dir)
        for rel in (run / "collections/requirements.yml", run / "requirements.yml",
                    Path("collections/requirements.yml"), Path("requirements.yml")):
            if (repo.path / rel).is_file():
                return repo.path / rel
        return None

    def install_collections(self, repo: Repo, log_line=lambda _l: None) -> str | None:
        """Install the repo's collections when its requirements file changed. None, or why it failed."""
        if repo.local:
            return None
        req = self.requirements_file(repo)
        target = self.collections_dir(repo)
        if req is None:
            return None
        if not req.is_file():
            return f"its collections file {repo.requirements} isn’t in the repository"
        digest = hashlib.sha256(req.read_bytes()).hexdigest()
        stamp = target / ".requirements.sha256"
        if stamp.is_file() and stamp.read_text().strip() == digest:
            return None
        log_line(f"[ansible-gui] Installing collections from {req.relative_to(repo.path)}")
        fresh = target.with_name(target.name + ".new")
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir(parents=True)
        cmd = ["ansible-galaxy", "collection", "install", "-r", str(req), "-p", str(fresh), "--force"]
        try:
            proc = subprocess.run(cmd, cwd=repo.run_dir, capture_output=True, text=True, timeout=600,
                                  env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
        except (OSError, subprocess.TimeoutExpired) as e:
            shutil.rmtree(fresh, ignore_errors=True)
            return f"installing its collections failed: {e}"
        if proc.returncode != 0:
            shutil.rmtree(fresh, ignore_errors=True)
            lines = [l for l in (proc.stderr or proc.stdout).splitlines() if l.strip()]
            return f"installing its collections failed: {lines[-1] if lines else 'ansible-galaxy exited with ' + str(proc.returncode)}"
        (fresh / ".requirements.sha256").write_text(digest)
        shutil.rmtree(target, ignore_errors=True)
        fresh.rename(target)
        log_line(f"[ansible-gui] Collections installed")
        return None

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
    def sync(self, repo: Repo) -> str | None:
        """Pull one repo. Returns None, or why it failed."""
        if repo.local:
            return None
        key = token = None
        if repo.ssh_key_id:
            cred = self.creds.get(repo.ssh_key_id)
            want = "git_token" if git.is_https(repo.url) else "ssh_key"
            if not cred or not cred.readable or cred.kind != want:
                error = ("its sign-in credential was deleted, can’t be decrypted, or doesn’t suit the URL. "
                         "Pick another on the Repositories page.")
                self.repos.record_fetch(repo.id, None, None, error)
                return error
            secret = self.creds.secret(cred)
            if want == "ssh_key":
                key = secret["private_key"]
            else:
                token = (cred.meta["username"], secret["token"])
        config = sshtools.write_ssh_config(self.settings.data_dir, self.settings.ssh_config)
        try:
            result = git.sync(repo, config, key, token)
        except git.GitError as e:
            self.repos.record_fetch(repo.id, None, None, str(e))
            return str(e)
        self.repos.record_fetch(repo.id, result.commit, result.subject, None)
        if repo.role == "playbooks":
            error = self.install_collections(repo)
            if error:
                self.repos.record_fetch(repo.id, None, None, error[0].upper() + error[1:] + ".")
                return error
        if repo.role == "inventory" and result.changed:
            self.on_inventory_changed()
        return None

    def sync_for_run(self, repo: Repo, log_line) -> str | None:
        """Pull the inventory repo and the run's playbook repo. None, or a message saying what failed."""
        targets = [r for r in (self.inventory_repo(), repo) if r and not r.local]
        for r in dict((x.id, x) for x in targets).values():
            log_line(f"[ansible-gui] Pulling {r.name} ({r.branch}) from {r.url}")
            error = self.sync(r)
            if error:
                log_line(f"[ansible-gui] Pull failed: {error}")
                return f"{GIT_CANCELED} {r.name} from git: {error.rstrip('.')}. Nothing ran."
            fresh = self.repos.get(r.id)
            log_line(f"[ansible-gui] {r.name} is at {fresh.short} {fresh.last_subject or ''}")
        return None
