"""A run gets only the environment secrets picked for it, and what it was handed stays out of its log."""

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.credentials.store import CredentialStore, Invalid
from ansible_gui.keys import Keys
from ansible_gui.runs.redact import MASK, Redactor
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")

TOKEN, OTHER = "tok-4f1c9e77", "other-8a02d6b3"


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    # Says which of the two secrets it can see, without printing either.
    (project / "sees.yml").write_text(
        "- hosts: lab\n  gather_facts: false\n  tasks:\n    - ansible.builtin.debug:\n"
        "        msg: \"token={{ lookup('env', 'TEST_TOKEN') | length }} other={{ lookup('env', 'OTHER_TOKEN') | length }}"
        " file={{ lookup('env', 'OTHER_TOKEN_FILE') | length }}\"\n")
    # What a playbook does by accident: a command that carries the token fails, and Ansible prints it.
    (project / "leak.yml").write_text(
        "- hosts: lab\n  gather_facts: false\n  tasks:\n"
        "    - name: Call the API\n"
        "      ansible.builtin.command: /bin/sh -c 'echo calling with $TEST_TOKEN; exit 3'\n")
    return project


@pytest.fixture
def app(tmp_path, project):
    return create_app(Settings(project_dir=project, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                               secret_key="s" * 40))


@pytest.fixture
def c(app):
    return TestClient(app)


@pytest.fixture
def secrets(app, c) -> dict[str, str]:
    """Two environment secrets, as {variable: credential id}."""
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": TOKEN})
    c.post("/credentials", data={"kind": "env_secret", "name": "other", "env": "OTHER_TOKEN", "value": OTHER})
    return {s.meta["env"]: str(s.id) for s in app.state.creds.by_kind("env_secret")}


def wait(app, run_id):
    for _ in range(300):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def start(app, c, playbook, **fields):
    r = c.post("/runs", data={"playbook": playbook, "target": "box1", "mode": "apply", "expected": ["box1"], **fields},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return wait(app, int(r.headers["location"].rsplit("/", 1)[1]))


def envs(run) -> list[str]:
    return [e["env"] for e in run.credentials.get("env_secret", [])]


# --- which secrets a run gets -------------------------------------------------------------------------------------
@needs_ansible
def test_a_run_gets_only_the_environment_secrets_picked_for_it(app, c, secrets):
    run = start(app, c, "sees.yml", env_secret=secrets["TEST_TOKEN"])
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert envs(run) == ["TEST_TOKEN"]
    assert f"token={len(TOKEN)} other=0 file=0" in log

    none = start(app, c, "sees.yml")
    assert envs(none) == []
    assert "token=0 other=0 file=0" in app.state.store.log_path(none.id).read_text()

    both = start(app, c, "sees.yml", env_secret=list(secrets.values()))
    assert envs(both) == ["OTHER_TOKEN", "TEST_TOKEN"]


def test_the_launcher_offers_each_secret_unticked_and_run_again_ticks_what_the_run_had(app, c, secrets):
    page = c.get("/runs/new").text
    assert "Every run also gets" not in page
    for cid in secrets.values():
        assert f'name="env_secret" value="{cid}" >' in page  # offered, not ticked
    run = app.state.store.create(
        playbook="sees.yml", target="box1", tags="", skip_tags="", check_mode=True, diff=True, verbosity=0,
        expected_hosts=["box1"], trigger="t",
        credentials={"env_secret": [{"id": int(secrets["TEST_TOKEN"]), "name": "tok", "env": "TEST_TOKEN"}]})
    app.state.store.update(run.id, status="successful", finished_at="2026-01-01T00:00:00+00:00")
    from ansible_gui.web import rerun_url
    again = c.get(rerun_url(app.state.store.get(run.id), "check")).text
    assert f'name="env_secret" value="{secrets["TEST_TOKEN"]}" checked>' in again
    assert f'name="env_secret" value="{secrets["OTHER_TOKEN"]}" >' in again


@needs_ansible
def test_a_template_and_a_schedule_keep_the_secrets_picked_for_them(app, c, secrets):
    tok = secrets["TEST_TOKEN"]
    r = c.post("/runs/templates", data={"name": "with-token", "playbook": "sees.yml", "target": "box1", "mode": "apply",
                                        "diff": "1", "verbosity": "0", "purpose": "template", "env_secret": tok},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    t = app.state.templates.all()[0]
    assert t.credentials["env_secret"] == [int(tok)]
    page = c.get(f"/runs/new?template={t.id}").text
    assert f'name="env_secret" value="{tok}" checked>' in page
    # Unticking it on the way to a run is a change from the template, and the run says so.
    run = start(app, c, "sees.yml", template=str(t.id), env_secret="")
    assert envs(run) == [] and any("Environment secrets" in ch for ch in run.template_changes)

    r = c.post("/schedules", data={"name": "nightly", "cron": "0 3 * * *", "playbook": "sees.yml", "target": "box1",
                                   "mode": "check", "diff": "1", "verbosity": "0", "purpose": "schedule",
                                   "env_secret": tok}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sched = app.state.schedules.all()[0]
    assert sched.credentials["env_secret"] == [int(tok)]
    assert f'name="env_secret" value="{tok}" checked>' in c.get(f"/schedules/{sched.id}").text
    fired = wait(app, app.state.scheduler.fire(sched).id)
    assert envs(fired) == ["TEST_TOKEN"]
    # The secret's own page warns before a delete, and a schedule left without it says so instead of running.
    assert "Used by 1 schedule" in c.get(f"/credentials/{tok}").text
    app.state.creds.delete(int(tok))
    gone = app.state.scheduler.fire(app.state.schedules.get(sched.id))
    assert gone.status == "error" and "was deleted" in gone.error


def test_a_command_gets_what_is_ticked_and_gathering_facts_gets_none(app, c, secrets):
    assert f'name="env_secret" value="{secrets["TEST_TOKEN"]}" >' in c.get("/runs/command").text
    r = c.post("/runs/command", data={"target": "box1", "module": "ansible.builtin.ping", "inventory": "0",
                                      "expected": ["box1"], "env_secret": secrets["OTHER_TOKEN"]},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert envs(app.state.store.get(int(r.headers["location"].rsplit("/", 1)[1]))) == ["OTHER_TOKEN"]
    r = c.post("/facts/gather", data={"target": "box1", "inventory": "0"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert envs(app.state.store.recent(1)[0]) == []


@needs_ansible
def test_a_run_stops_when_a_secret_picked_for_it_is_gone(app, c, secrets):
    tok = int(secrets["TEST_TOKEN"])
    run = app.state.store.create(playbook="sees.yml", target="box1", tags="", skip_tags="", check_mode=True,
                                 diff=False, verbosity=0, expected_hosts=["box1"], trigger="t",
                                 credentials={"env_secret": [{"id": tok, "name": "tok", "env": "TEST_TOKEN"}]})
    app.state.creds.delete(tok)
    app.state.executor.submit(run.id)
    done = wait(app, run.id)
    assert done.status == "error" and "deleted" in done.error


def test_templates_and_schedules_from_before_keep_the_secrets_they_got(tmp_path, project, app, c, secrets):
    """Until now every run got every environment secret. A saved template or schedule keeps getting the ones
    that exist at the upgrade, so nothing unattended loses a token it relied on."""
    c.post("/runs/templates", data={"name": "old", "playbook": "sees.yml", "target": "box1", "mode": "check",
                                    "diff": "1", "verbosity": "0", "purpose": "template"})
    c.post("/schedules", data={"name": "old", "cron": "0 3 * * *", "playbook": "sees.yml", "target": "box1",
                               "mode": "check", "diff": "1", "verbosity": "0", "purpose": "schedule"})
    app.state.scheduler.stop()
    db = sqlite3.connect(tmp_path / "data" / "ansible-gui.db")
    for table in ("templates", "schedules"):  # as an earlier version wrote them: no list at all
        (stored,) = db.execute(f"select credentials from {table}").fetchone()
        assert json.loads(stored)["env_secret"] == []
        db.execute(f"update {table} set credentials='{{}}'")
    db.commit()
    db.close()
    later = create_app(Settings(project_dir=project, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                                secret_key="s" * 40))
    everything = sorted(int(i) for i in secrets.values())
    assert later.state.templates.all()[0].credentials["env_secret"] == everything
    assert later.state.schedules.all()[0].credentials["env_secret"] == everything
    later.state.scheduler.stop()


# --- names ----------------------------------------------------------------------------------------------------------
def test_a_credential_name_may_have_spaces(tmp_path, app, c):
    store = CredentialStore(tmp_path, Keys.load("k" * 40, tmp_path).fernet)
    saved = store.save(kind="env_secret", name="  Enroll   token lab ",
                       values={"env": "ENROLL_TOKEN", "value": "v"}, is_default=False, user="")
    assert saved.name == "Enroll token lab"  # the spaces typed around and between are tidied
    for bad in (" ", "-starts with a dash", "semi;colon", "x" * 65):
        with pytest.raises(Invalid):
            store.save(kind="env_secret", name=bad, values={"env": "A_B", "value": "v"}, is_default=False, user="")
    r = c.post("/credentials", data={"kind": "env_secret", "name": "Enroll token", "env": "ENROLL_TOKEN", "value": "v"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "Enroll token" in c.get("/credentials/secrets").text and "Enroll token" in c.get("/runs/new").text


# --- the log --------------------------------------------------------------------------------------------------------
def test_redactor_hides_each_secret_however_ansible_writes_it():
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nQyNTUxOQAAACDx7m\n-----END OPENSSH PRIVATE KEY-----\n"
    redact = Redactor(["hunter2", 'say "hi"\\now', "hunter2-and-more", key, "ab", ""])
    assert redact("password is hunter2.") == f"password is {MASK}."
    assert redact("hunter2-and-more") == MASK  # the longer secret, not the shorter one inside it
    assert redact(json.dumps({"cmd": 'say "hi"\\now'})) == '{"cmd": "' + MASK + '"}'  # as a JSON result prints it
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in redact("line: b3BlbnNzaC1rZXktdjEAAAAA")  # a key, line by line
    assert redact(json.dumps(key)).count("PRIVATE KEY") <= 2 and "QyNTUxOQAAACDx7m" not in redact(json.dumps(key))
    assert redact("-----BEGIN OPENSSH PRIVATE KEY-----") == "-----BEGIN OPENSSH PRIVATE KEY-----"  # not a secret
    assert redact("grab the tab") == "grab the tab"  # too short to hide without blanking the log
    assert Redactor([])("anything") == "anything"


@needs_ansible
def test_a_secret_the_playbook_prints_is_masked_in_the_log_the_page_and_the_download(app, c, secrets):
    run = start(app, c, "leak.yml", env_secret=secrets["TEST_TOKEN"])
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "failed" and "Call the API" in log
    assert TOKEN not in log and f"calling with {MASK}" in log
    assert TOKEN not in c.get(f"/runs/{run.id}").text and TOKEN not in c.get(f"/runs/{run.id}/log.txt").text
    data = app.state.store.data_dir
    assert [str(f) for f in data.rglob("*") if f.is_file() and TOKEN.encode() in f.read_bytes()] == []
