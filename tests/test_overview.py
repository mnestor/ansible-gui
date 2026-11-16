import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui import overview as ov
from ansible_gui.config import Settings
from ansible_gui.runs.store import Run
from ansible_gui.web import create_app, rerun_hosts

FIXTURE = Path(__file__).parent / "fixtures" / "runner"


def run(i, stats, check=False, status="failed", **kw):
    return Run(id=i, playbook="site.yml", target="lab", tags="", skip_tags="", check_mode=check, diff=False,
               verbosity=0, expected_hosts=list(stats), status=status, trigger="t",
               created_at=datetime.now(timezone.utc), host_stats=stats, **kw)


def test_latest_result_per_host_decides():
    runs = [  # newest first
        run(3, {"a": {"ok": 1}}, status="successful"),                       # a fixed
        run(2, {"a": {"failed": 1}, "b": {"failed": 1}, "c": {"unreachable": 1}}),
        run(1, {"d": {"ok": 2, "changed": 1}}, check=True, status="successful"),  # drift
        run(0, {"gone": {"failed": 1}}),
    ]
    issues = ov.host_issues(runs, known_hosts={"a", "b", "c", "d"})
    assert [(g.kind, g.run.id, g.hosts) for g in issues] == [
        ("unreachable", 2, ["c"]), ("failed", 2, ["b"]), ("drift", 1, ["d"])]


def test_apply_with_changes_is_not_an_issue():
    assert ov.host_issues([run(1, {"a": {"ok": 2, "changed": 1}}, status="successful")], None) == []


def test_rerun_hosts_limits_to_those_hosts():
    url = rerun_hosts(run(2, {"b": {"failed": 1}}, credentials={}), "apply", ["b", "c"])
    assert "target=b%2Cc" in url and "playbook=site.yml" in url and "mode=apply" in url


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")
def test_home_shows_failure_until_a_later_run_fixes_it(tmp_path):
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                              secret_key="s" * 40))
    c = TestClient(app)
    assert "Nothing has run yet" in c.get("/").text

    def go(playbook):
        r = c.post("/runs", data={"playbook": playbook, "target": "box1", "mode": "apply", "expected": ["box1"]},
                   follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        for _ in range(200):
            if app.state.store.get(rid).finished:
                return rid
            time.sleep(0.2)

    bad = go("env.yml")  # fails: TEST_TOKEN isn't set
    page = c.get("/").text
    assert "1 thing to resolve" in page and f"run #{bad}" in page and "See what failed" in page
    go("site.yml")
    page = c.get("/").text
    assert "All clear" in page and "Last 7 days" in page
