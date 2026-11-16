"""Run history in SQLite. The run's log lives next to it as a plain file."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FINISHED = {"successful", "failed", "canceled", "error", "interrupted", "skipped"}

SCHEMA = """
create table if not exists runs (
  id integer primary key autoincrement,
  playbook text not null,
  target text not null default '',
  tags text not null default '',
  skip_tags text not null default '',
  check_mode integer not null,
  diff integer not null,
  verbosity integer not null default 0,
  expected_hosts text not null default '[]',
  status text not null,
  trigger text not null,
  created_at text not null,
  started_at text,
  finished_at text,
  rc integer,
  commit_sha text,
  error text,
  host_stats text not null default '{}',
  credentials text not null default '{}'
);
"""
# Columns added after the first release; created on start if missing.
MIGRATIONS = {
    "credentials": "alter table runs add column credentials text not null default '{}'",
    "schedule_id": "alter table runs add column schedule_id integer",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Run:
    id: int
    playbook: str
    target: str
    tags: str
    skip_tags: str
    check_mode: bool
    diff: bool
    verbosity: int
    expected_hosts: list[str]
    status: str
    trigger: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    rc: int | None = None
    commit_sha: str | None = None
    error: str | None = None
    host_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    # {"ssh_key": {"id": 1, "name": "lab"}, ..., "env_secret": [{"id": 4, "name": "x", "env": "X"}]}
    credentials: dict = field(default_factory=dict)
    schedule_id: int | None = None

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    @property
    def duration(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    @property
    def mode(self) -> str:
        return "Dry run" if self.check_mode else "Apply"


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row(r: sqlite3.Row) -> Run:
    return Run(
        id=r["id"],
        playbook=r["playbook"],
        target=r["target"],
        tags=r["tags"],
        skip_tags=r["skip_tags"],
        check_mode=bool(r["check_mode"]),
        diff=bool(r["diff"]),
        verbosity=r["verbosity"],
        expected_hosts=json.loads(r["expected_hosts"]),
        status=r["status"],
        trigger=r["trigger"],
        created_at=_dt(r["created_at"]),
        started_at=_dt(r["started_at"]),
        finished_at=_dt(r["finished_at"]),
        rc=r["rc"],
        commit_sha=r["commit_sha"],
        error=r["error"],
        host_stats=json.loads(r["host_stats"]),
        credentials=json.loads(r["credentials"]),
        schedule_id=r["schedule_id"],
    )


class RunStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "ansible-gui.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("pragma table_info(runs)")}
        for col, sql in MIGRATIONS.items():
            if col not in have:
                self._db.execute(sql)
        # A restart kills whatever was running; say so instead of showing it as running forever.
        self._db.execute(
            "update runs set status='interrupted', finished_at=?, error=? where status in ('queued','running')",
            (now(), "The server restarted before this run finished."),
        )
        self._db.commit()

    def run_dir(self, run_id: int) -> Path:
        d = self.data_dir / "runs" / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_path(self, run_id: int) -> Path:
        return self.run_dir(run_id) / "output.log"

    def create(self, *, playbook: str, target: str, tags: str, skip_tags: str, check_mode: bool,
               diff: bool, verbosity: int, expected_hosts: list[str], trigger: str,
               credentials: dict | None = None, schedule_id: int | None = None) -> Run:
        with self._lock:
            cur = self._db.execute(
                "insert into runs (playbook, target, tags, skip_tags, check_mode, diff, verbosity,"
                " expected_hosts, status, trigger, created_at, credentials, schedule_id) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (playbook, target, tags, skip_tags, int(check_mode), int(diff), verbosity,
                 json.dumps(expected_hosts), "queued", trigger, now(), json.dumps(credentials or {}), schedule_id),
            )
            self._db.commit()
            return self.get(cur.lastrowid)

    def update(self, run_id: int, **fields) -> None:
        if "host_stats" in fields:
            fields["host_stats"] = json.dumps(fields["host_stats"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"update runs set {cols} where id=?", (*fields.values(), run_id))  # noqa: S608 - keys are ours
            self._db.commit()

    def get(self, run_id: int) -> Run | None:
        r = self._db.execute("select * from runs where id=?", (run_id,)).fetchone()
        return _row(r) if r else None

    def recent(self, limit: int = 100) -> list[Run]:
        return [_row(r) for r in self._db.execute("select * from runs order by id desc limit ?", (limit,))]

    def for_schedule(self, schedule_id: int, limit: int = 10) -> list[Run]:
        return [_row(r) for r in self._db.execute(
            "select * from runs where schedule_id=? order by id desc limit ?", (schedule_id, limit))]

    def last_status_by_host(self) -> dict[str, tuple[str, int]]:
        """Each host's result in the most recent finished run that targeted it."""
        out: dict[str, tuple[str, int]] = {}
        for run in self.recent(200):
            if not run.finished:
                continue
            for host, stats in run.host_stats.items():
                result = host_result(stats)
                if result != "pending":  # the host never ran (canceled, failed to start)
                    out.setdefault(host, (result, run.id))
        return out


def host_result(stats: dict[str, int]) -> str:
    if stats.get("unreachable"):
        return "unreachable"
    if stats.get("failed"):
        return "failed"
    if stats.get("changed"):
        return "changed"
    if stats.get("ok") or stats.get("skipped"):
        return "ok"
    return "pending"
