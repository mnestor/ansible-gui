import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.runs.schedules import Scheduler, check_cron
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                               secret_key="s" * 40))


@pytest.fixture
def c(app):
    return TestClient(app)


def wait(app, run_id):
    for _ in range(200):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def make(c, **over):
    data = {"name": "nightly", "cron": "0 3 * * *", "playbook": "site.yml", "target": "box1", "mode": "check",
            "diff": "1", "verbosity": "0", "purpose": "schedule", **over}
    return c.post("/schedules", data=data, follow_redirects=False)


def later():
    return datetime.now(timezone.utc) + timedelta(days=2)


def test_cron_checks():
    assert check_cron("0 3 * * *") is None and check_cron("@daily") is None
    assert "five fields" in check_cron("0 3 * *")
    assert "five fields" in check_cron("0 0 3 * * *")  # no seconds field
    assert check_cron("61 * * * *") and check_cron("@sometimes")


def test_create_lists_and_shows_next_runs(app, c):
    r = make(c)
    assert r.status_code == 303
    sched = app.state.schedules.all()[0]
    assert sched.next_run_at and sched.next_run_at > datetime.now(timezone.utc)
    assert sched.next_run_at.astimezone().hour == 3
    assert "nightly" in c.get("/schedules").text
    page = c.get(r.headers["location"]).text
    assert "Saved. Next run" in page and "Dry run now" in page
    assert "Next:" in c.get("/schedules/when", params={"cron": "*/15 * * * *"}).text
    assert "five fields" in c.get("/schedules/when", params={"cron": "* *"}).text


def test_refuses_bad_cron_and_no_hosts(app, c):
    r = make(c, cron="every night")
    assert r.status_code == 400 and "five fields" in r.text
    r = make(c, target="nothing-matches")
    assert r.status_code == 400 and "match no hosts" in r.text
    assert app.state.schedules.all() == []


def test_due_schedule_runs_and_is_labelled(app, c):
    make(c)
    sched = app.state.schedules.all()[0]
    app.state.scheduler.run_due(at=later())
    runs = app.state.store.for_schedule(sched.id)
    assert len(runs) == 1
    run = wait(app, runs[0].id)
    assert run.status == "successful" and run.trigger == "schedule nightly" and run.check_mode
    assert app.state.schedules.get(sched.id).next_run_at > later()  # moved on, no catch-up
    assert "Open its schedule" in c.get(f"/runs/{run.id}").text


def test_never_overlaps_itself(app, c):
    make(c, name="slow", playbook="slow.yml")
    sched = app.state.schedules.all()[0]
    first = app.state.scheduler.fire(sched)
    second = app.state.scheduler.fire(sched)
    assert second.status == "skipped" and f"#{first.id}" in second.error
    app.state.executor.cancel(first.id)
    wait(app, first.id)


def test_paused_does_not_run(app, c):
    make(c)
    sched = app.state.schedules.all()[0]
    c.post(f"/schedules/{sched.id}/pause")
    assert app.state.schedules.get(sched.id).next_run_at is None
    app.state.scheduler.run_due(at=later())
    assert app.state.store.for_schedule(sched.id) == []
    c.post(f"/schedules/{sched.id}/resume")
    assert app.state.schedules.get(sched.id).next_run_at is not None


def test_missed_runs_are_recorded_once_not_caught_up(app, c):
    make(c)
    sched = app.state.schedules.all()[0]
    app.state.schedules.set_next(sched.id, datetime.now(timezone.utc) - timedelta(hours=5))
    s2 = Scheduler(app.state.schedules, app.state.store, app.state.executor, app.state.creds, tick=3600)
    s2.stop()
    runs = app.state.store.for_schedule(sched.id)
    assert len(runs) == 1 and runs[0].status == "skipped" and "Missed" in runs[0].error
    assert app.state.schedules.get(sched.id).next_run_at > datetime.now(timezone.utc)


def test_deleted_credential_makes_a_visible_error(app, c):
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "pw"})
    cred = app.state.creds.by_kind("vault_password")[0]
    make(c, vault_password=str(cred.id))
    sched = app.state.schedules.all()[0]
    assert sched.credentials == {"vault_password": cred.id}
    app.state.creds.delete(cred.id)
    run = app.state.scheduler.fire(sched)
    assert run.status == "error" and "deleted" in run.error


def test_apply_schedule_run_now_needs_confirming_and_deleted_cred_is_kept_visible(app, c):
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "pw"})
    cred = app.state.creds.by_kind("vault_password")[0]
    make(c, mode="apply", vault_password=str(cred.id))
    sched = app.state.schedules.all()[0]
    page = c.get(f"/schedules/{sched.id}").text
    assert "Apply now…" in page and "data-confirm-twice" in page
    app.state.creds.delete(cred.id)
    page = c.get(f"/schedules/{sched.id}").text
    assert "Deleted: pick another" in page and "was deleted" in page
    # resaving without picking another is refused instead of silently dropping the credential
    r = c.post(f"/schedules/{sched.id}", data={"name": "nightly", "cron": "0 3 * * *", "playbook": "site.yml",
                                               "target": "box1", "mode": "apply", "vault_password": str(cred.id)})
    assert r.status_code == 400 and "no longer exists" in r.text


def test_the_account_is_saved_and_signs_in_every_run(app, c):
    c.post("/credentials", data={"kind": "account", "name": "dep", "username": "deploy"})
    acct = app.state.creds.by_kind("account")[0]
    assert make(c, account=str(acct.id)).status_code == 303
    sched = app.state.schedules.all()[0]
    assert sched.credentials == {"account": acct.id}
    assert f'value="{acct.id}" selected' in c.get(f"/schedules/{sched.id}").text
    run = wait(app, app.state.scheduler.fire(sched).id)
    assert '"ansible_user": "deploy"' in app.state.store.log_path(run.id).read_text()
