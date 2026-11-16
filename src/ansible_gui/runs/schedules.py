"""Cron schedules. A scheduler thread starts due runs; a schedule never overlaps itself.

Times are cron in the server's local time zone. Runs missed while the server was
down are not caught up: each one is recorded once as "missed" and the schedule
carries on from now.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from croniter import croniter

from ..credentials.store import PER_RUN, CredentialStore, NAME_RE
from .store import RunStore, now
from ..db import Database

log = logging.getLogger(__name__)

SCHEMA = """
create table if not exists schedules (
  id integer primary key autoincrement,
  name text not null unique,
  cron text not null,
  enabled integer not null default 1,
  playbook text not null,
  target text not null default '',
  tags text not null default '',
  skip_tags text not null default '',
  check_mode integer not null default 1,
  diff integer not null default 1,
  verbosity integer not null default 0,
  credentials text not null default '{}',
  next_run_at text,
  notify text not null default 'problems',
  created_at text not null,
  updated_at text not null,
  updated_by text not null default ''
);
"""
ALIASES = {"@hourly", "@daily", "@midnight", "@weekly", "@monthly", "@yearly", "@annually"}


class Invalid(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def local_now() -> datetime:
    return datetime.now().astimezone()


def check_cron(expr: str) -> str | None:
    """None if valid, else what is wrong, in words."""
    expr = " ".join(expr.split())
    if not expr:
        return "Enter when it runs, like 0 3 * * * for every night at 03:00."
    if expr.startswith("@"):
        return None if expr in ALIASES else f"Unknown shortcut {expr}. Use one of {', '.join(sorted(ALIASES))}."
    if len(expr.split()) != 5:
        return "Use five fields: minute hour day-of-month month day-of-week, like 0 3 * * 1-5."
    if not croniter.is_valid(expr):
        return "That isn’t a valid cron expression. Each field takes numbers, *, ranges (1-5), lists (1,3) and steps (*/15)."
    return None


def next_times(expr: str, count: int = 3, after: datetime | None = None) -> list[datetime]:
    it = croniter(" ".join(expr.split()), after or local_now())
    return [it.get_next(datetime) for _ in range(count)]


@dataclass
class Schedule:
    id: int
    name: str
    cron: str
    enabled: bool
    playbook: str
    target: str
    tags: str
    skip_tags: str
    check_mode: bool
    diff: bool
    verbosity: int
    credentials: dict  # {"ssh_key": 3, ...}
    next_run_at: datetime | None
    updated_at: datetime
    updated_by: str
    notify: str = "problems"
    repo_id: int = 0
    inventory_id: int | None = None
    last_run: object | None = field(default=None)

    @property
    def mode(self) -> str:
        return "Dry run" if self.check_mode else "Apply"


class ScheduleStore:
    def __init__(self, data_dir):
        self._db = Database(data_dir / "ansible-gui.db")
        self._lock = self._db.lock
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("pragma table_info(schedules)")}
        if "notify" not in have:
            self._db.execute("alter table schedules add column notify text not null default 'problems'")
        if "repo_id" not in have:
            self._db.execute("alter table schedules add column repo_id integer not null default 0")
        if "inventory_id" not in have:
            self._db.execute("alter table schedules add column inventory_id integer")

    def _row(self, r) -> Schedule:
        return Schedule(id=r["id"], name=r["name"], cron=r["cron"], enabled=bool(r["enabled"]), playbook=r["playbook"],
                        target=r["target"], tags=r["tags"], skip_tags=r["skip_tags"], check_mode=bool(r["check_mode"]),
                        diff=bool(r["diff"]), verbosity=r["verbosity"], credentials=json.loads(r["credentials"]),
                        next_run_at=datetime.fromisoformat(r["next_run_at"]) if r["next_run_at"] else None,
                        updated_at=datetime.fromisoformat(r["updated_at"]), updated_by=r["updated_by"],
                        notify=r["notify"], repo_id=r["repo_id"], inventory_id=r["inventory_id"])

    def all(self) -> list[Schedule]:
        return [self._row(r) for r in self._db.execute("select * from schedules order by lower(name)")]

    def get(self, sid: int) -> Schedule | None:
        r = self._db.execute("select * from schedules where id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def save(self, form: dict, user: str, sid: int | None = None) -> Schedule:
        errors = {}
        name = form["name"].strip()
        if not NAME_RE.match(name):
            errors["name"] = "Use 1–64 letters, digits, dots, dashes or underscores, starting with a letter or digit."
        else:
            clash = self._db.execute("select id from schedules where lower(name)=lower(?)", (name,)).fetchone()
            if clash and clash["id"] != sid:
                errors["name"] = f"There is already a schedule called {name}."
        cron = " ".join(form["cron"].split())
        if problem := check_cron(cron):
            errors["cron"] = problem
        if errors:
            raise Invalid(errors)
        enabled = bool(form.get("enabled", True))
        nxt = next_times(cron, 1)[0].astimezone(timezone.utc).isoformat(timespec="seconds") if enabled else None
        creds = {k: int(form[k]) for k in PER_RUN if str(form.get(k) or "").isdigit()}
        notify = form.get("notify") if form.get("notify") in ("problems", "always", "never") else "problems"
        values = (name, cron, int(enabled), form.get("playbook_path", form.get("playbook")), form["target"], form["tags"], form["skip_tags"],
                  int(form["mode"] != "apply"), int(form["diff"]), form["verbosity"], json.dumps(creds), nxt, notify,
                  form.get("repo_id", 0), form.get("inventory_id"), now(), user)
        with self._lock:
            if sid:
                self._db.execute("update schedules set name=?, cron=?, enabled=?, playbook=?, target=?, tags=?, skip_tags=?,"
                                 " check_mode=?, diff=?, verbosity=?, credentials=?, next_run_at=?, notify=?, repo_id=?, inventory_id=?, updated_at=?, updated_by=?"
                                 " where id=?", (*values, sid))
            else:
                cur = self._db.execute("insert into schedules (name, cron, enabled, playbook, target, tags, skip_tags,"
                                       " check_mode, diff, verbosity, credentials, next_run_at, notify, repo_id, inventory_id,"
                                       " updated_at, updated_by, created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, now()))
                sid = cur.lastrowid
            self._db.commit()
        return self.get(sid)

    def set_enabled(self, sid: int, enabled: bool, user: str) -> None:
        s = self.get(sid)
        if not s:
            return
        nxt = next_times(s.cron, 1)[0].astimezone(timezone.utc).isoformat(timespec="seconds") if enabled else None
        with self._lock:
            self._db.execute("update schedules set enabled=?, next_run_at=?, updated_at=?, updated_by=? where id=?",
                             (int(enabled), nxt, now(), user, sid))
            self._db.commit()

    def set_next(self, sid: int, when: datetime | None) -> None:
        with self._lock:
            self._db.execute("update schedules set next_run_at=? where id=?",
                             (when.astimezone(timezone.utc).isoformat(timespec="seconds") if when else None, sid))
            self._db.commit()

    def delete(self, sid: int) -> None:
        with self._lock:
            self._db.execute("delete from schedules where id=?", (sid,))
            self._db.commit()


class Scheduler:
    def __init__(self, schedules: ScheduleStore, runs: RunStore, executor, creds: CredentialStore,
                 tick: float = 15.0, on_finish=None):
        self.on_finish = on_finish or (lambda run_id: None)
        self.schedules = schedules
        self.runs = runs
        self.executor = executor
        self.creds = creds
        self.tick = tick
        self._stop = threading.Event()
        self._fire_lock = threading.Lock()
        self._mark_missed()
        self._thread = threading.Thread(target=self._loop, name="ansible-gui-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _mark_missed(self) -> None:
        cutoff = datetime.now(timezone.utc)
        for s in self.schedules.all():
            if s.enabled and s.next_run_at and s.next_run_at < cutoff:
                when = s.next_run_at.astimezone()
                self._record(s, "skipped", f"Missed: the server wasn’t running at {when:%Y-%m-%d %H:%M}. "
                                           "Missed runs aren’t caught up.")
                self.schedules.set_next(s.id, next_times(s.cron, 1)[0])

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            try:
                self.run_due()
            except Exception:  # never let one bad schedule stop the others
                log.exception("scheduler tick failed")

    def run_due(self, at: datetime | None = None) -> None:
        at = at or datetime.now(timezone.utc)
        for s in self.schedules.all():
            if s.enabled and s.next_run_at and s.next_run_at <= at:
                # Move on first, so a crash in fire() can't make it fire every tick.
                self.schedules.set_next(s.id, next_times(s.cron, 1, after=at.astimezone())[0])
                self.fire(s)

    def _record(self, s: Schedule, status: str, error: str, trigger: str | None = None):
        run = self.runs.create(playbook=s.playbook, target=s.target, tags=s.tags, skip_tags=s.skip_tags,
                               check_mode=s.check_mode, diff=s.diff, verbosity=s.verbosity, expected_hosts=[],
                               trigger=trigger or f"schedule {s.name}", schedule_id=s.id, repo_id=s.repo_id,
                               inventory_id=s.inventory_id)
        self.runs.update(run.id, status=status, finished_at=now(), error=error)
        self.on_finish(run.id)
        return self.runs.get(run.id)

    def fire(self, s: Schedule, by: str | None = None):
        """Start one run of the schedule now. Returns the run (possibly skipped or errored)."""
        trigger = f"schedule {s.name}" + (f", started by {by}" if by else "")
        with self._fire_lock:
            busy = next((r for r in self.runs.for_schedule(s.id, 20) if not r.finished), None)
            if busy:
                return self._record(s, "skipped", f"Skipped: run #{busy.id} of this schedule was still going.", trigger)
            chosen: dict = {}
            for kind, cid in s.credentials.items():
                cred = self.creds.get(cid)
                if not cred or cred.kind != kind:
                    return self._record(s, "error", "Didn’t start: a credential this schedule uses was deleted. "
                                                    "Edit the schedule to pick another.", trigger)
                chosen[kind] = {"id": cred.id, "name": cred.name}
            chosen["env_secret"] = [{"id": c.id, "name": c.name, "env": c.meta.get("env")}
                                    for c in self.creds.by_kind("env_secret")]
            run = self.runs.create(playbook=s.playbook, target=s.target, tags=s.tags, skip_tags=s.skip_tags,
                                   check_mode=s.check_mode, diff=s.diff, verbosity=s.verbosity,
                                   expected_hosts=[], trigger=trigger, credentials=chosen, schedule_id=s.id,
                                   repo_id=s.repo_id, inventory_id=s.inventory_id)
        self.executor.submit(run.id)
        return run

