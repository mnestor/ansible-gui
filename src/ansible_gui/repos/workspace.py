"""Where the inventory and the playbooks come from: git clones, or ANSIBLE_GUI_PROJECT_DIR.

One inventory repository, any number of playbook repositories. Without git repos the
directory in ANSIBLE_GUI_PROJECT_DIR serves as both, as before. A git repo of a role
replaces the local directory for that role.
"""

from __future__ import annotations

import logging
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
        return CliOptions(project_dir=repo.path, sources=self.inventory_sources(), playbook_dir=repo.playbook_dir,
                          vault_password_file=self.settings.vault_password_file)

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
        key = None
        if repo.ssh_key_id:
            cred = self.creds.get(repo.ssh_key_id)
            if not cred or not cred.readable:
                error = "its deploy key was deleted or can’t be decrypted. Pick another on the Repositories page."
                self.repos.record_fetch(repo.id, None, None, error)
                return error
            key = self.creds.secret(cred)["private_key"]
        config = sshtools.write_ssh_config(self.settings.data_dir, self.settings.ssh_config)
        try:
            result = git.sync(repo, config, key)
        except git.GitError as e:
            self.repos.record_fetch(repo.id, None, None, str(e))
            return str(e)
        self.repos.record_fetch(repo.id, result.commit, result.subject, None)
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
                return f"{GIT_CANCELED} {r.name} from git: {error}. Nothing ran."
            fresh = self.repos.get(r.id)
            log_line(f"[ansible-gui] {r.name} is at {fresh.short} {fresh.last_subject or ''}")
        return None
