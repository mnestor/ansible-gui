import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui import notify
from ansible_gui.config import Settings, SmtpSettings
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


class FakeSMTP:
    sent: list = []
    fail: str | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise notify.smtplib.SMTPException(FakeSMTP.fail)
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.fail = None
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def app(tmp_path):
    smtp = SmtpSettings(host="mail.test", to=["ops@example.test"], sender="agui@example.test", username="agui",
                        password="pw")
    return create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                               secret_key="s" * 40, smtp=smtp, public_url="https://ansible.example.test"))


@pytest.fixture
def c(app):
    return TestClient(app, base_url="https://ansible.example.test")


def schedule(c, app, **over):
    data = {"name": "nightly", "cron": "0 3 * * *", "playbook": "site.yml", "target": "box1", "mode": "check",
            "diff": "1", "verbosity": "0", "notify": "problems", **over}
    assert c.post("/schedules", data=data, follow_redirects=False).status_code == 303
    return app.state.schedules.all()[-1]


def fire_and_wait(app, sched):
    run = app.state.scheduler.fire(sched)
    for _ in range(200):
        run = app.state.store.get(run.id)
        if run.finished:
            break
        time.sleep(0.2)
    app.state.notifier.wait_idle()
    return app.state.store.get(run.id)


def test_dry_run_with_changes_is_drift(app, c):
    # site.yml's command task is skipped in check mode, so a dry run of it reports no changes: no mail.
    run = fire_and_wait(app, schedule(c, app))
    assert run.status == "successful" and FakeSMTP.sent == [] and run.notified is None


def test_failure_mails_with_hosts_link_and_failing_task(app, c, tmp_path):
    sched = schedule(c, app, name="broken", playbook="env.yml", mode="apply")  # env.yml fails without TEST_TOKEN
    run = fire_and_wait(app, sched)
    assert run.status == "failed"
    (msg,) = FakeSMTP.sent
    assert msg["Subject"].startswith("[ansible-gui] FAILED broken: 1 failed of 1 host")
    body = msg.get_content()
    assert f"https://ansible.example.test/runs/{run.id}" in body
    assert "box1" in body and "TASK [Report environment]" in body and "fatal:" in body
    assert run.notified == "Emailed ops@example.test"
    assert "Emailed ops@example.test" in c.get(f"/runs/{run.id}").text


def test_recovery_mails_once(app, c):
    sched = schedule(c, app, name="flaky", playbook="env.yml", mode="apply")
    fire_and_wait(app, sched)
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "x"})
    fire_and_wait(app, sched)
    fire_and_wait(app, sched)
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert len(subjects) == 2 and "FAILED" in subjects[0] and "RECOVERED" in subjects[1]


def test_never_and_manual_runs_stay_quiet(app, c):
    fire_and_wait(app, schedule(c, app, name="quiet", playbook="env.yml", mode="apply", notify="never"))
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    for _ in range(200):
        if app.state.store.get(run_id).finished:
            break
        time.sleep(0.2)
    app.state.notifier.wait_idle()
    assert FakeSMTP.sent == []


def test_couldnt_start_and_smtp_failure_are_recorded(app, c):
    sched = schedule(c, app, name="gone")
    app.state.schedules.save({**vars_of(sched), "target": "nothing-matches"}, "t", sched.id)
    FakeSMTP.fail = "550 relay denied"
    run = app.state.scheduler.fire(app.state.schedules.get(sched.id))
    for _ in range(100):
        if app.state.store.get(run.id).finished:
            break
        time.sleep(0.1)
    app.state.notifier.wait_idle()
    run = app.state.store.get(run.id)
    assert run.status == "error" and "match no hosts" in run.error and run.notified.startswith("Email failed: 550 relay denied")


def test_test_email_button(app, c):
    c.post("/schedules/test-email")
    assert FakeSMTP.sent[-1]["Subject"] == "[ansible-gui] Test email"
    FakeSMTP.fail = "connection refused"
    page = c.post("/schedules/test-email").text
    assert "didn’t go out: connection refused" in page


def vars_of(s):
    return {"name": s.name, "cron": s.cron, "playbook": s.playbook, "target": s.target, "tags": s.tags,
            "skip_tags": s.skip_tags, "mode": "check" if s.check_mode else "apply", "diff": s.diff,
            "verbosity": s.verbosity, "notify": s.notify, "enabled": s.enabled}


def test_classify():
    from ansible_gui.runs.store import Run
    from datetime import datetime, timezone
    base = dict(id=1, playbook="p", target="", tags="", skip_tags="", diff=False, verbosity=0, expected_hosts=[],
                trigger="t", created_at=datetime.now(timezone.utc))
    drift = Run(**base, check_mode=True, status="successful",
                host_stats={"a": {"ok": 2, "changed": 1}, "b": {"ok": 2, "changed": 0}})
    assert notify.classify(drift) == ("drift", "Dry run found changes on 1 of 2 hosts")
    down = Run(**base, check_mode=False, status="failed",
               host_stats={"a": {"unreachable": 1}, "b": {"ok": 1, "failed": 1}, "c": {"ok": 3}})
    assert notify.classify(down) == ("failed", "1 failed and 1 unreachable of 3 hosts")
