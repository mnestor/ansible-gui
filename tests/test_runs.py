import re
import shutil
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
    r = c.post("/runs", data={"playbook": "slow.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    store = app.state.store
    for _ in range(50):
        if store.get(run_id).status == "running":
            break
        time.sleep(0.2)
    c.post(f"/runs/{run_id}/cancel")
    run = wait(app, run_id, timeout=30)
    assert run.status == "canceled"


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
