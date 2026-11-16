"""Starting on a database written by the first versions: every later column is added on start,
and what was there keeps working (old runs show, old schedules still run)."""

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")

# Each table exactly as first committed (from git history), before any migration.
FIRST_SCHEMA = """
create table runs (
  id integer primary key autoincrement, playbook text not null, target text not null default '',
  tags text not null default '', skip_tags text not null default '', check_mode integer not null,
  diff integer not null, verbosity integer not null default 0, expected_hosts text not null default '[]',
  status text not null, trigger text not null, created_at text not null, started_at text, finished_at text,
  rc integer, commit_sha text, error text, host_stats text not null default '{}'
);
create table schedules (
  id integer primary key autoincrement, name text not null unique, cron text not null,
  enabled integer not null default 1, playbook text not null, target text not null default '',
  tags text not null default '', skip_tags text not null default '', check_mode integer not null default 1,
  diff integer not null default 1, verbosity integer not null default 0, credentials text not null default '{}',
  next_run_at text, created_at text not null, updated_at text not null, updated_by text not null default ''
);
create table credentials (
  id integer primary key autoincrement, name text not null unique, kind text not null,
  is_default integer not null default 0, meta text not null default '{}', secret blob not null,
  created_at text not null, updated_at text not null, updated_by text not null default ''
);
create table repos (
  id integer primary key autoincrement, name text not null unique, role text not null, url text not null,
  branch text not null default 'main', ssh_key_id integer, paths text not null default '',
  edit_url text not null default '', last_fetch_at text, last_commit text, last_subject text, last_error text,
  created_at text not null, updated_at text not null, updated_by text not null default ''
);
"""

T = "2026-01-01T03:00:00+00:00"


@pytest.fixture
def old_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = sqlite3.connect(data / "ansible-gui.db")
    db.executescript(FIRST_SCHEMA)
    db.execute("insert into runs (playbook, target, check_mode, diff, expected_hosts, status, trigger, created_at,"
               " started_at, finished_at, rc, host_stats) values (?,?,?,?,?,?,?,?,?,?,?,?)",
               ("site.yml", "box1", 0, 1, json.dumps(["box1"]), "failed", "mike", T, T, T, 2,
                json.dumps({"box1": {"ok": 1, "failures": 1, "failed": 1}})))
    db.execute("insert into schedules (name, cron, playbook, target, created_at, updated_at) values (?,?,?,?,?,?)",
               ("nightly", "0 3 * * *", "site.yml", "box1", T, T))
    db.commit()
    db.close()
    return data


def test_first_version_database_upgrades_in_place(old_data):
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=old_data))
    c = TestClient(app)

    for path in ("/", "/runs", "/runs/1", "/schedules", "/schedules/1", "/repos", "/credentials",
                 "/inventory", "/inventory/hosts/box1"):
        assert c.get(path).status_code == 200, path

    run = app.state.store.get(1)
    assert run.repo_id == 0 and run.inventory_id is None and run.credentials == {}
    # the old run's result still marks the host, and Run again opens a working launcher
    assert "Last run #1" in c.get("/inventory/hosts/box1").text
    page = c.get("/runs/1").text
    assert c.get("/runs/new?playbook=site.yml&target=box1").status_code == 200 and "Run again" in page

    sched = app.state.schedules.get(1)
    assert sched.notify == "problems" and sched.repo_id == 0 and sched.inventory_id is None
    fired = app.state.scheduler.fire(sched)
    end = time.time() + 60
    while not app.state.store.get(fired.id).finished:
        assert time.time() < end
        time.sleep(0.2)
    done = app.state.store.get(fired.id)
    assert done.status == "successful", done.error

    cols = {r[1] for r in sqlite3.connect(old_data / "ansible-gui.db").execute("pragma table_info(runs)")}
    assert {"credentials", "schedule_id", "notified", "repo_id", "inventory_commit", "inventory_id"} <= cols
