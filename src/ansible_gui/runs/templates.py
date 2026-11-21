"""Run templates: how a playbook is meant to be run, saved under a name.

A template only fills in the launcher. Whoever runs it can change any field; the run records which
template it came from and what was changed. A schedule made from a template copies it once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from ..credentials.store import NAME_RE, PER_RUN
from ..db import Database
from .store import now

SCHEMA = """
create table if not exists templates (
  id integer primary key autoincrement,
  name text not null unique,
  note text not null default '',
  playbook text not null,
  repo_id integer not null default 0,
  inventory_id integer,
  target text not null default '',
  tags text not null default '',
  skip_tags text not null default '',
  check_mode integer not null default 1,
  diff integer not null default 1,
  verbosity integer not null default 0,
  credentials text not null default '{}',
  prompt_vars text not null default '{}',
  extra_vars text not null default '',
  created_at text not null,
  updated_at text not null,
  updated_by text not null default ''
);
"""


class Invalid(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


@dataclass
class Template:
    id: int
    name: str
    note: str
    playbook: str
    repo_id: int
    inventory_id: int | None
    target: str
    tags: str
    skip_tags: str
    check_mode: bool
    diff: bool
    verbosity: int
    credentials: dict  # {"account": 3, ...}; a kind left out means None
    updated_at: datetime
    updated_by: str
    prompt_vars: dict = field(default_factory=dict)  # secrets sealed (variables.py)
    extra_vars: str = ""

    @property
    def mode(self) -> str:
        return "Dry run" if self.check_mode else "Apply"

    def as_form(self) -> dict:
        """The launcher's form, filled in from this template."""
        return {"playbook": f"{self.repo_id}:{self.playbook}", "target": self.target,
                "mode": "check" if self.check_mode else "apply", "diff": self.diff, "tags": self.tags,
                "skip_tags": self.skip_tags, "verbosity": self.verbosity,
                "inventory": "" if self.inventory_id is None else str(self.inventory_id),
                "prompt_vars": dict(self.prompt_vars), "extra_vars": self.extra_vars,
                **{k: str(self.credentials.get(k, "")) for k in PER_RUN}}


def _tags(text: str) -> list[str]:
    return sorted({t.strip() for t in text.split(",") if t.strip()})


# The launcher fields a run can change from its template, in the order the launcher shows them.
FIELDS = [("playbook", "Playbook"), ("inventory", "Inventory"), ("target", "Targets"), ("mode", "Mode"),
          ("diff", "Show diffs"), *((k, None) for k in PER_RUN),
          ("tags", "Only tags"), ("skip_tags", "Skip tags"), ("verbosity", "Verbosity"),
          ("extra_vars", "Extra variables")]


def changed_fields(form: dict, template: dict) -> list[str]:
    """The keys of `form` that differ from the template's form. Tags compare as sets."""
    out = []
    for key, _ in FIELDS:
        a, b = form.get(key, ""), template.get(key, "")
        if key in ("tags", "skip_tags"):
            a, b = _tags(str(a)), _tags(str(b))
        elif key in ("diff", "verbosity"):
            pass
        else:
            a, b = str(a or ""), str(b or "")
        if a != b:
            out.append(key)
    return out


class TemplateStore:
    def __init__(self, data_dir):
        self._db = Database(data_dir / "ansible-gui.db")
        self._lock = self._db.lock
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("pragma table_info(templates)")}
        if "prompt_vars" not in have:
            self._db.execute("alter table templates add column prompt_vars text not null default '{}'")
        if "extra_vars" not in have:
            self._db.execute("alter table templates add column extra_vars text not null default ''")

    def _row(self, r) -> Template:
        return Template(id=r["id"], name=r["name"], note=r["note"], playbook=r["playbook"], repo_id=r["repo_id"],
                        inventory_id=r["inventory_id"], target=r["target"], tags=r["tags"], skip_tags=r["skip_tags"],
                        check_mode=bool(r["check_mode"]), diff=bool(r["diff"]), verbosity=r["verbosity"],
                        credentials=json.loads(r["credentials"]),
                        updated_at=datetime.fromisoformat(r["updated_at"]), updated_by=r["updated_by"],
                        prompt_vars=json.loads(r["prompt_vars"]), extra_vars=r["extra_vars"])

    def all(self) -> list[Template]:
        return [self._row(r) for r in self._db.execute("select * from templates order by lower(name)")]

    def get(self, tid: int) -> Template | None:
        r = self._db.execute("select * from templates where id=?", (tid,)).fetchone()
        return self._row(r) if r else None

    def save(self, form: dict, user: str, tid: int | None = None) -> Template:
        """`form` is the launcher's form plus name, note, playbook_path and repo_id."""
        errors = {}
        name = form["name"].strip()
        if not NAME_RE.match(name):
            errors["name"] = "Use 1–64 letters, digits, dots, dashes or underscores, starting with a letter or digit."
        else:
            clash = self._db.execute("select id from templates where lower(name)=lower(?)", (name,)).fetchone()
            if clash and clash["id"] != tid:
                errors["name"] = f"There is already a template called {name}."
        note = " ".join(form.get("note", "").split())
        if len(note) > 200:
            errors["note"] = f"Keep the note to 200 characters; it has {len(note)}. Put the detail in the playbook."
        if errors:
            raise Invalid(errors)
        creds = {k: int(form[k]) for k in PER_RUN if str(form.get(k) or "").isdigit()}
        values = (name, note, form["playbook_path"], form["repo_id"], form.get("inventory_id"), form["target"],
                  form["tags"], form["skip_tags"], int(form["mode"] != "apply"), int(form["diff"]), form["verbosity"],
                  json.dumps(creds), json.dumps(form.get("prompt_vars") or {}),
                  form.get("extra_vars", ""), now(), user)
        with self._lock:
            if tid:
                self._db.execute("update templates set name=?, note=?, playbook=?, repo_id=?, inventory_id=?, target=?,"
                                 " tags=?, skip_tags=?, check_mode=?, diff=?, verbosity=?, credentials=?,"
                                 " prompt_vars=?, extra_vars=?, updated_at=?, updated_by=? where id=?", (*values, tid))
            else:
                cur = self._db.execute("insert into templates (name, note, playbook, repo_id, inventory_id, target, tags,"
                                       " skip_tags, check_mode, diff, verbosity, credentials, prompt_vars,"
                                       " extra_vars, updated_at, updated_by, created_at)"
                                       " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (*values, now()))
                tid = cur.lastrowid
            self._db.commit()
        return self.get(tid)

    def delete(self, tid: int) -> None:
        with self._lock:
            self._db.execute("delete from templates where id=?", (tid,))
            self._db.commit()
