"""Run templates: a saved way to run a playbook that fills in the launcher, where anything can still change."""

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.runs.templates import changed_fields
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


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


def account(c, name="cloud", user="cloud-user") -> str:
    creds = c.app.state.creds
    if not any(a.name == name for a in creds.by_kind("account")):
        c.post("/credentials", data={"kind": "account", "name": name, "username": user})
    return str(next(a.id for a in creds.by_kind("account") if a.name == name))


def make(c, **over):
    data = {"name": "hello-only", "note": "Just the greeting", "playbook": "site.yml", "target": "box1",
            "mode": "apply", "diff": "1", "verbosity": "0", "tags": "hello", "account": account(c),
            "purpose": "template", **over}
    return c.post("/runs/templates", data=data, follow_redirects=False)


def test_changed_fields_ignore_tag_order_and_spacing():
    base = {"playbook": "0:site.yml", "tags": "a,b", "skip_tags": "", "diff": True, "verbosity": 0, "mode": "check"}
    assert changed_fields({**base, "tags": "b, a"}, base) == []
    assert changed_fields({**base, "skip_tags": "x", "diff": False}, base) == ["diff", "skip_tags"]


def test_empty_list_says_how_to_make_one(c):
    page = c.get("/runs/templates").text
    assert "No templates yet" in page and "/runs/templates/new" in page
    assert "/runs/templates" in c.get("/runs").text


@needs_ansible
def test_save_list_and_edit(app, c):
    r = make(c)
    assert r.status_code == 303, r.text
    t = app.state.templates.all()[0]
    assert (t.name, t.tags, str(t.credentials["account"]), t.check_mode, t.note) == (
        "hello-only", "hello", account(c), False, "Just the greeting")
    listing = c.get("/runs/templates").text
    assert "hello-only" in listing and "Signs in as" in listing and ">cloud</td>" in listing and f"/runs/new?template={t.id}" in listing
    page = c.get(r.headers["location"]).text
    assert "Saved." in page and 'value="hello-only"' in page and "Run from it…" in page
    assert make(c).status_code == 400  # the name is taken
    assert make(c, name="bad name").status_code == 400
    assert len(app.state.templates.all()) == 1


@needs_ansible
def test_a_run_from_a_template_records_what_was_changed(app, c):
    make(c)
    t = app.state.templates.all()[0]
    page = c.get(f"/runs/new", params={"template": t.id}).text
    assert "From template" in page and "Just the greeting" in page
    assert f'value="{account(c)}" selected' in page and 'value="hello"' in page and f'name="template" value="{t.id}"' in page
    assert "As template <strong>hello-only</strong> sets it" in page

    preview = c.get("/runs/preview", params={**t.as_form(), "template": t.id, "skip_tags": "change",
                                             "account": "", "diff": "1"}).text
    assert "Changed from hello-only" in preview and "Skip tags" in preview and "Account" in preview

    form = {**t.as_form(), "template": str(t.id), "diff": "1", "skip_tags": "change", "mode": "check",
            "expected": ["box1"]}
    run = wait(app, int(c.post("/runs", data=form, follow_redirects=False).headers["location"].rsplit("/", 1)[1]))
    assert run.template_id == t.id and run.template_name == "hello-only"
    assert run.template_changes == ["Mode: Apply → Dry run", "Skip tags: none → change"]
    page = c.get(f"/runs/{run.id}").text
    assert "Skip tags: none → change" in page and f"template={t.id}" in page  # Run again keeps the template

    # Deleting the template leaves the run's record alone.
    c.post(f"/runs/templates/{t.id}/delete")
    assert app.state.templates.all() == [] and app.state.store.get(run.id).template_name == "hello-only"
    assert "hello-only" in c.get(f"/runs/{run.id}").text


@needs_ansible
def test_a_schedule_copies_the_template_and_editing_it_later_changes_nothing(app, c):
    make(c)
    t = app.state.templates.all()[0]
    page = c.get("/schedules/new", params={"template": t.id}).text
    assert "copied from template hello-only" in page and f'value="{account(c)}" selected' in page
    data = {**t.as_form(), "name": "nightly", "cron": "0 3 * * *", "diff": "1", "template_name": "hello-only",
            "purpose": "schedule"}
    assert c.post("/schedules", data=data, follow_redirects=False).status_code == 303
    c.post(f"/runs/templates/{t.id}", data={**t.as_form(), "name": "hello-only", "diff": "1", "tags": "change",
                                             "purpose": "template"})
    assert app.state.templates.get(t.id).tags == "change"
    sched = app.state.schedules.all()[0]
    assert (sched.tags, str(sched.credentials["account"]), sched.template_name) == ("hello", account(c), "hello-only")


@needs_ansible
def test_save_as_template_from_the_launcher_keeps_the_picks(c):
    page = c.get("/runs/templates/new", params={"playbook": "site.yml", "target": "box2", "tags": "change",
                                                "account": account(c, "dep", "deploy"), "mode": "apply"}).text
    assert 'value="box2"' in page and 'value="change"' in page and f'value="{account(c, "dep")}" selected' in page
    assert "Save template" in page


def test_the_rail_lists_runs_pages_and_marks_the_one_open(c):
    page = c.get("/runs/templates").text
    assert 'href="/runs/playbooks"' in page and 'href="/runs/command"' in page
    assert '<a class="rail-item rail-sub" href="/runs/templates" aria-current="page">' in page
    assert '<a class="rail-item" href="/runs" aria-current="true">' in page
    runs = c.get("/runs").text
    assert '<a class="rail-item" href="/runs" aria-current="page">' in runs and 'rail-sub" href="/runs/playbooks" aria-current' not in runs


def test_schedules_sit_under_runs_in_the_rail(c):
    page = c.get("/schedules").text
    assert '<a class="rail-item rail-sub" href="/schedules" aria-current="page">' in page
    assert '<a class="rail-item" href="/runs" aria-current="true">' in page
    assert '<a class="rail-item" href="/schedules"' not in page  # no longer a section of its own
    assert 'href="/schedules">Schedules</a>' in c.get("/runs").text  # reachable when the rail is icons only
