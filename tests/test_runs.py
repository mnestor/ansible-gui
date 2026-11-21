import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path))


@pytest.fixture
def c(app):
    return TestClient(app)


def wait(app, run_id, timeout=60):
    store = app.state.store
    end = time.time() + timeout
    while time.time() < end:
        run = store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def test_launcher_lists_playbooks_only(c):
    html = c.get("/runs/new").text
    assert "site.yml" in html and "slow.yml" in html
    assert "requirements.yml" not in html


def test_preview_shows_matched_hosts_and_button_count(c):
    html = c.get("/runs/preview", params={"playbook": "site.yml", "target": "box2", "mode": "check"}).text
    assert "Dry run on 1 host" in html
    assert 'value="box2"' in html and 'value="box1"' not in html
    assert '<option value="hello">' in html
    html = c.get("/runs/preview", params={"playbook": "site.yml", "target": "nope", "mode": "apply"}).text
    assert "No hosts match" in html and "disabled" in html


def test_dry_run_then_apply(app, c):
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "check", "diff": "1",
                              "expected": ["box1"]}, follow_redirects=False)
    assert r.status_code == 303
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    run = wait(app, run_id)
    assert run.status == "successful" and run.check_mode
    # command tasks are skipped in check mode
    assert run.host_stats["box1"]["ok"] == 1 and run.host_stats["box1"]["skipped"] == 1
    page = c.get(f"/runs/{run_id}").text
    assert "Dry run passed" in page and "Apply these changes" in page and "hello from box1" in page
    assert "mode=apply" in page

    log = c.get(f"/runs/{run_id}/log.txt").text
    assert "--check" in log and "PLAY RECAP" in log


def test_refuses_when_targets_changed_since_preview(c):
    r = c.post("/runs", data={"playbook": "site.yml", "target": "", "mode": "apply", "expected": ["box1"]})
    assert r.status_code == 409
    assert "changed since the preview" in r.text


def test_refuses_unknown_playbook(c):
    r = c.post("/runs", data={"playbook": "../../etc/passwd", "target": "", "mode": "check"})
    assert r.status_code == 400


def test_cancel_running(app, c):
    """Cancel while ansible-playbook is mid-task (not merely queued or pulling): the process is stopped,
    the run says so, and it doesn't wait out the 20 s pause."""
    r = c.post("/runs", data={"playbook": "slow.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    log = app.state.store.log_path(run_id)
    end = time.time() + 30
    while "TASK [Wait]" not in (log.read_text() if log.exists() else ""):
        assert time.time() < end, "the pause task never started"
        time.sleep(0.2)
    started = time.time()
    c.post(f"/runs/{run_id}/cancel")
    run = wait(app, run_id, timeout=30)
    assert run.status == "canceled"
    assert time.time() - started < 15
    assert "Run canceled from the web UI" in log.read_text()
    # Every process of the run is gone, the worker running the pause too: Ansible's workers call setsid(),
    # so killing the playbook's process group alone leaves them running.
    data_dir = str(app.state.store.data_dir)
    end = time.time() + 5
    while (left := subprocess.run(["pgrep", "-f", data_dir], capture_output=True, text=True).stdout.split()):
        assert time.time() < end, f"still running after cancel: {left}"
        time.sleep(0.2)



def test_a_run_that_takes_too_long_is_stopped(app, c):
    """Runs go one at a time, so a hung playbook would hold up every run queued behind it."""
    from ansible_gui.notify import classify
    app.state.executor.run_timeout = 3  # ANSIBLE_GUI_RUN_TIMEOUT_MINUTES, in seconds here
    r = c.post("/runs", data={"playbook": "slow.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    started = time.time()
    run = wait(app, run_id, timeout=30)
    assert time.time() - started < 15  # not the playbook's 20 s pause
    assert run.status == "failed" and run.error.startswith("Stopped after 3 s: the longest a run may take")
    assert "[ansible-gui] Stopped after 3 s" in app.state.store.log_path(run_id).read_text()
    assert classify(run) == ("failed", "Stopped after 3 s")
    data_dir = str(app.state.store.data_dir)
    end = time.time() + 5
    while (left := subprocess.run(["pgrep", "-f", data_dir], capture_output=True, text=True).stdout.split()):
        assert time.time() < end, f"still running after the timeout: {left}"
        time.sleep(0.2)


def test_run_timeout_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_RUN_TIMEOUT_MINUTES", "45")
    assert Settings.from_env().run_timeout_minutes == 45
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                              run_timeout_minutes=45))
    assert app.state.executor.run_timeout == 45 * 60
    monkeypatch.setenv("ANSIBLE_GUI_RUN_TIMEOUT_MINUTES", "2h")
    with pytest.raises(SystemExit, match="whole number of minutes"):
        Settings.from_env()


def test_executor_wait_idle_covers_on_finish(app, c):
    """A run is marked finished before its on_finish (the email) runs; wait_idle waits for both."""
    calls = []
    real = app.state.executor.on_finish

    def slow_finish(run_id):
        time.sleep(1)
        calls.append(run_id)
        real(run_id)

    app.state.executor.on_finish = slow_finish
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "check", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait(app, run_id)
    assert app.state.executor.wait_idle(timeout=30)
    assert calls == [run_id]


def test_live_endpoint_streams_from_offset(app, c):
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box2", "mode": "apply", "expected": ["box2"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    wait(app, run_id)
    first = c.get(f"/runs/{run_id}/live", params={"offset": 0})
    assert "PLAY RECAP" in first.text and first.headers.get("HX-Refresh") == "true"
    size = app.state.store.log_path(run_id).stat().st_size
    rest = c.get(f"/runs/{run_id}/live", params={"offset": size}).text
    assert "data-log-append" not in rest


def test_history_and_inventory_last_result(app, c):
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert re.search(r"Succeeded", c.get("/runs").text)
    assert "Last run #" in c.get("/inventory/hosts/box1").text


def test_vault_password_file_from_settings(tmp_path):
    """ANSIBLE_GUI_VAULT_PASSWORD_FILE reaches the run, with no vault credential stored."""
    pw = tmp_path / "vault-pass"
    pw.write_text("fixture-password\n")
    pw.chmod(0o600)
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                              vault_password_file=pw))
    c = TestClient(app)
    r = c.post("/runs", data={"playbook": "vault.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    assert r.status_code == 303, r.text[-300:]
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log[-800:]
    assert "word=opened-the-vault" in log
    assert "fixture-password" not in log


# --- retention (ANSIBLE_GUI_RUN_RETENTION_DAYS) --------------------------------------------
def test_prune_deletes_old_runs_and_logs_but_keeps_each_schedules_latest(tmp_path):
    from datetime import datetime, timedelta, timezone

    from ansible_gui.runs.store import RunStore
    store = RunStore(tmp_path)

    def run(days_ago, status="successful", schedule_id=None):
        r = store.create(playbook="site.yml", target="", tags="", skip_tags="", check_mode=True, diff=False,
                         verbosity=0, expected_hosts=[], trigger="t", schedule_id=schedule_id)
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        store.update(r.id, status=status, finished_at=at if status != "running" else None)
        store.log_path(r.id).write_text("log")
        return r.id

    old, recent = run(100), run(10)
    sched_old, sched_latest = run(120, schedule_id=5), run(95, schedule_id=5)
    still_running = run(200, status="running")
    assert store.prune(0) == 0 and store.get(old)  # 0 keeps everything
    assert store.prune(90) == 2
    assert store.get(old) is None and store.get(sched_old) is None
    assert not (tmp_path / "runs" / str(old)).exists()
    for kept in (recent, sched_latest, still_running):
        assert store.get(kept) and store.log_path(kept).exists()


def test_retention_runs_at_start(tmp_path):
    from datetime import datetime, timedelta, timezone

    from ansible_gui.runs.store import RunStore
    store = RunStore(tmp_path)
    r = store.create(playbook="site.yml", target="", tags="", skip_tags="", check_mode=True, diff=False,
                     verbosity=0, expected_hosts=[], trigger="t")
    store.update(r.id, status="failed",
                 finished_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds"))
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                              run_retention_days=30))
    for _ in range(50):
        if app.state.store.get(r.id) is None:
            break
        time.sleep(0.1)
    assert app.state.store.get(r.id) is None
