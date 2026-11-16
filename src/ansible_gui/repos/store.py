"""Git repositories ansible-gui keeps clones of: one inventory repo, any number of playbook repos."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..credentials.store import NAME_RE
from ..runs.store import now

SCHEMA = """
create table if not exists repos (
  id integer primary key autoincrement,
  name text not null unique,
  role text not null,              -- inventory | playbooks
  url text not null,
  branch text not null default 'main',
  ssh_key_id integer,              -- a stored SSH key used as the deploy key, or null
  paths text not null default '',  -- inventory: comma-separated sources; playbooks: playbook directory
  edit_url text not null default '',
  requirements text not null default '',
  last_fetch_at text,
  last_commit text,
  last_subject text,
  last_error text,
  created_at text not null,
  updated_at text not null,
  updated_by text not null default ''
);
"""

URL_RE = re.compile(r"^(https?://\S+|ssh://\S+|[\w.-]+@[\w.-]+:\S+|file://\S+|/\S+)$")


class Invalid(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass
class Repo:
    id: int
    name: str
    role: str
    url: str
    branch: str
    ssh_key_id: int | None
    paths: str
    edit_url: str
    path: Path  # the working copy
    requirements: str = ""  # collections requirements file in the repo; empty means look for the usual places
    last_fetch_at: datetime | None = None
    last_commit: str | None = None
    last_subject: str | None = None
    last_error: str | None = None
    local: bool = False  # a directory from ANSIBLE_GUI_PROJECT_DIR, not a clone

    @property
    def sources(self) -> list[str]:
        return [p.strip() for p in self.paths.split(",") if p.strip()]

    @property
    def playbook_dir(self) -> str:
        return self.paths.strip() or "."

    @property
    def run_dir(self) -> Path:
        """Where Ansible starts: its ansible.cfg applies, and its playbooks are listed."""
        return self.path if self.local else (self.path / self.playbook_dir).resolve()

    @property
    def short(self) -> str:
        return (self.last_commit or "")[:10]


class RepoStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "repos"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "ansible-gui.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        if "requirements" not in {r["name"] for r in self._db.execute("pragma table_info(repos)")}:
            self._db.execute("alter table repos add column requirements text not null default ''")

    def _row(self, r) -> Repo:
        dt = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return Repo(id=r["id"], name=r["name"], role=r["role"], url=r["url"], branch=r["branch"],
                    ssh_key_id=r["ssh_key_id"], paths=r["paths"], edit_url=r["edit_url"],
                    path=self.root / str(r["id"]), requirements=r["requirements"], last_fetch_at=dt(r["last_fetch_at"]),
                    last_commit=r["last_commit"], last_subject=r["last_subject"], last_error=r["last_error"])

    def all(self) -> list[Repo]:
        return [self._row(r) for r in self._db.execute("select * from repos order by role, lower(name)")]

    def get(self, rid: int) -> Repo | None:
        r = self._db.execute("select * from repos where id=?", (rid,)).fetchone()
        return self._row(r) if r else None

    def save(self, form: dict, user: str, rid: int | None = None) -> Repo:
        errors = {}
        role = form.get("role")
        if role not in ("inventory", "playbooks"):
            errors["role"] = "Choose what this repository holds."
        name = form.get("name", "").strip()
        if not NAME_RE.match(name):
            errors["name"] = "Use 1–64 letters, digits, dots, dashes or underscores, starting with a letter or digit."
        else:
            clash = self._db.execute("select id from repos where lower(name)=lower(?)", (name,)).fetchone()
            if clash and clash["id"] != rid:
                errors["name"] = f"There is already a repository called {name}."
        if role == "inventory":
            other = self._db.execute("select id, name from repos where role='inventory'").fetchone()
            if other and other["id"] != rid:
                errors["role"] = f"{other['name']} is already the inventory repository. There can be one."
        url = form.get("url", "").strip()
        if not URL_RE.match(url):
            errors["url"] = "Use a clone URL, like https://git.example.com/me/ansible.git or git@git.example.com:me/ansible.git."
        elif re.match(r"^https?://[^/]*@", url):
            errors["url"] = "Leave the user name and token out of the URL: pick a git token below instead, so it’s stored encrypted."
        branch = form.get("branch", "").strip() or "main"
        if not re.match(r"^[\w./-]+$", branch) or ".." in branch or branch.startswith("-"):
            errors["branch"] = "That isn’t a valid branch name."
        paths = form.get("paths", "").strip().strip("/")
        reqs = form.get("requirements", "").strip()
        bad = [p for p in [*paths.split(","), reqs] if p.strip().startswith(("/", "..")) or "/../" in p or p.strip().endswith("/..")]
        if bad:
            errors["paths"] = "Use paths inside the repository, like inventory/hosts.yml."
        if role == "inventory" and not paths:
            errors["paths"] = "Name the inventory file or directory in the repository, like inventory/hosts.yml."
        if errors:
            raise Invalid(errors)
        key = int(form["ssh_key_id"]) if str(form.get("ssh_key_id") or "").isdigit() else None
        edit = form.get("edit_url", "").strip()
        values = (name, role, url, branch, key, paths, edit, reqs if role == "playbooks" else "", now(), user)
        with self._lock:
            if rid:
                old = self.get(rid)
                # A new URL or branch means a new clone; forget the old state.
                reset = old and (old.url != url or old.branch != branch)
                self._db.execute("update repos set name=?, role=?, url=?, branch=?, ssh_key_id=?, paths=?, edit_url=?,"
                                 " requirements=?, updated_at=?, updated_by=? where id=?", (*values, rid))
                if reset:
                    self._db.execute("update repos set last_commit=null, last_subject=null, last_fetch_at=null,"
                                     " last_error=null where id=?", (rid,))
            else:
                cur = self._db.execute("insert into repos (name, role, url, branch, ssh_key_id, paths, edit_url,"
                                       " requirements, updated_at, updated_by, created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                                       (*values, now()))
                rid = cur.lastrowid
            self._db.commit()
        return self.get(rid)

    def record_fetch(self, rid: int, commit: str | None, subject: str | None, error: str | None) -> None:
        with self._lock:
            if error:
                self._db.execute("update repos set last_error=?, last_fetch_at=? where id=?", (error, now(), rid))
            else:
                self._db.execute("update repos set last_commit=?, last_subject=?, last_error=null, last_fetch_at=?"
                                 " where id=?", (commit, subject, now(), rid))
            self._db.commit()

    def delete(self, rid: int) -> None:
        with self._lock:
            self._db.execute("delete from repos where id=?", (rid,))
            self._db.commit()
