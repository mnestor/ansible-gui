"""Extra variables: prompts from the roles' argument specs, free-form YAML, and secrets stored encrypted."""

import html
import re
import shutil
import time
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.runs import variables
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")

SPEC = {"argument_specs": {
    "main": {"short_description": "Build a box", "options": {
        "build_host": {"type": "str", "required": True, "description": "Host name to give it. Short name, no domain."},
        "build_size": {"type": "str", "choices": ["small", "large"], "default": "small", "description": "Size"},
        "build_count": {"type": "int", "default": 1, "description": "How many"},
        "build_debug": {"type": "bool", "default": False, "description": "Debug"},
        "build_pkgs": {"type": "list", "elements": "str", "description": "Packages"},
        "build_features": {"type": "list", "choices": ["ssh", "nfs", "smb"], "description": "Features"},
        "build_token": {"type": "str", "no_log": True, "description": "API token"},
    }},
    "extra": {"options": {"extra_only": {"type": "str", "description": "From tasks_from"}}},
}}


def make_project(root: Path) -> Path:
    project = root / "project"
    shutil.copytree(FIXTURE, project)
    role = project / "roles" / "builder"
    (role / "meta").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "meta" / "argument_specs.yml").write_text(yaml.safe_dump(SPEC, sort_keys=False))
    (role / "tasks" / "main.yml").write_text(
        "- name: Show\n  ansible.builtin.debug:\n    msg: \"host={{ build_host }} size={{ build_size | default('small') }}"
        " token={{ build_token | default('none') | length }} free={{ free_var | default('-') }}\"\n")
    (role / "tasks" / "extra.yml").write_text("- ansible.builtin.debug:\n    msg: extra\n")
    (project / "roles" / "plain" / "tasks").mkdir(parents=True)
    (project / "roles" / "plain" / "tasks" / "main.yml").write_text("- ansible.builtin.debug:\n    msg: plain\n")
    (project / "build.yml").write_text(
        "- hosts: lab\n  gather_facts: false\n  roles:\n    - builder\n    - role: plain\n"
        "  tasks:\n    - ansible.builtin.include_role:\n        name: builder\n        tasks_from: extra\n"
        "    - ansible.builtin.include_role:\n        name: \"{{ dynamic }}\"\n      when: dynamic is defined\n")
    (project / "outer.yml").write_text("- import_playbook: build.yml\n")
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text("build_count: 4\n")  # what applies; the spec says 1
    (role / "meta" / "main.yml").write_text("galaxy_info:\n  description: Builds a lab box from the cloud image\n")
    (project / "commented.yml").write_text("---\n# Patches every host.\n# Reboots only if needed.\n"
                                          "- hosts: lab\n  gather_facts: false\n  tasks: []\n")
    (project / "inventory" / "host_vars").mkdir()
    (project / "inventory" / "host_vars" / "box1.yml").write_text("build_count: 3\nansible_user: cloud-user\n")
    return project


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


@pytest.fixture
def app(tmp_path, project):
    return create_app(Settings(project_dir=project, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                               secret_key="s" * 40))


@pytest.fixture
def c(app):
    return TestClient(app)


def wait(app, run_id):
    for _ in range(300):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


# --- discovery and conversion ----------------------------------------------------------------------------------
def test_discover_reads_specs_of_every_role_the_playbook_names(project):
    prompts, bare = variables.discover(project / "outer.yml", project)  # through import_playbook
    by = {p.name: p for p in prompts}
    assert list(by)[:2] == ["build_host", "build_size"] and "extra_only" in by  # tasks_from: extra
    assert (by["build_host"].label, by["build_host"].help, by["build_host"].required) == (
        "Host name to give it", "Short name, no domain.", True)
    assert [by[k].kind for k in ("build_size", "build_count", "build_debug", "build_pkgs", "build_features",
                                 "build_token")] == ["choice", "int", "bool", "list", "multichoice", "secret"]
    assert by["build_size"].default_text == "small" and not by["build_host"].has_default
    assert bare == ["plain"]  # the templated include_role is skipped, not guessed at


def test_spec_inside_meta_main_counts_too(tmp_path):
    role = tmp_path / "roles" / "r" / "meta"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(yaml.safe_dump({"galaxy_info": {}, "argument_specs": {"main": {"options": {
        "x": {"type": "str"}}}}}))
    (tmp_path / "p.yml").write_text("- hosts: all\n  roles: [r]\n")
    assert [p.name for p in variables.discover(tmp_path / "p.yml", tmp_path)[0]] == ["x"]


def test_a_role_outside_the_repository_is_never_read(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "p.yml").write_text("- hosts: all\n  roles: [../outside]\n")
    (tmp_path / "outside" / "meta").mkdir(parents=True)
    (tmp_path / "outside" / "meta" / "argument_specs.yml").write_text(
        yaml.safe_dump({"argument_specs": {"main": {"options": {"leak": {"type": "str"}}}}}))
    assert variables.discover(tmp_path / "repo" / "p.yml", tmp_path / "repo") == ([], ["../outside"])


def test_values_convert_keep_secrets_and_report_bad_answers(project):
    f = Fernet(Fernet.generate_key())
    prompts, _ = variables.discover(project / "build.yml", project)
    raw = {"build_host": ["web3"], "build_count": ["2"], "build_debug": ["true"], "build_pkgs": ["git\n\ncurl "],
           "build_features": ["", "nfs", "ssh"], "build_size": [""], "build_token": ["t0k"]}
    vals, errors = variables.values(variables.read_answers(raw, prompts), prompts, f)
    assert errors == {}
    assert {k: v for k, v in vals.items() if k != "build_token"} == {
        "build_host": "web3", "build_count": 2, "build_debug": True, "build_pkgs": ["git", "curl"],
        "build_features": ["ssh", "nfs"]}  # the spec's order; an empty size sends nothing
    assert variables.unseal(vals, f)["build_token"] == "t0k" and "t0k" not in str(vals)

    kept = {"build_token": vals["build_token"]}
    again, _ = variables.values(variables.read_answers({"build_token": [""]}, prompts), prompts, f, kept)
    assert again["build_token"] == kept["build_token"]  # empty keeps
    cleared, _ = variables.values(variables.read_answers({"build_token": [""], "build_token.clear": ["1"]}, prompts),
                                  prompts, f, kept)
    assert "build_token" not in cleared

    _, errors = variables.values({"build_count": "two", "build_size": "huge"}, prompts, f)
    assert errors == {"var.build_count": "build_count takes a whole number, like 3.",
                      "var.build_size": "build_size takes one of: small, large."}


def test_answers_are_never_templated_by_ansible():
    text = variables.vars_file_text({"a": "{{ lookup('pipe', 'id') }}", "n": 3, "l": ["{{ x }}"]})
    assert text.startswith("a: !unsafe") and "n: 3" in text and "- !unsafe" in text


def test_freeform_must_be_a_mapping_of_names():
    assert variables.check_freeform("a: 1\nb: [x]\nc: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\n")[1] is None
    assert "line 2" in variables.check_freeform("a: 1\n b: : 2\n")[1]
    assert "list" in variables.check_freeform("- a\n- b\n")[1]
    assert "isn’t a variable name" in variables.check_freeform("1bad: x\n")[1]


# --- the launcher ----------------------------------------------------------------------------------------------
def test_the_launcher_draws_a_field_per_option(c):
    page = c.get("/runs/new", params={"playbook": "build.yml"}).text
    assert 'name="var.build_host"' in page and "(required by the role)" in page
    assert 'type="password" name="var.build_token"' in page and 'value="nfs"' in page
    assert "Not set here" in page and 'name="extra_vars"' in page
    assert 'name="var.' not in c.get("/runs/new", params={"playbook": "site.yml"}).text
    # Redrawn only when the playbook changes (app.js), never on every change: that closed the optional ones.
    assert 'hx-trigger="playbook-changed"' in page
    part = c.get("/runs/vars", params={"playbook": "build.yml", "var.build_host": "keep-me"}).text
    assert 'value="keep-me"' in part and part.lstrip().startswith("{#") is False


@needs_ansible
def test_a_run_sends_answers_as_plain_text_and_secrets_stay_out_of_the_log(app, c):
    form = {"playbook": "build.yml", "target": "box1", "mode": "apply", "diff": "1", "expected": ["box1"],
            "var.build_host": "{{ 6 * 7 }}", "var.build_size": "large", "var.build_token": "sekrit",
            "var.build_features": "", "extra_vars": "free_var: \"{{ 40 + 2 }}\""}
    r = c.post("/runs", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "successful", app.state.store.log_path(run.id).read_text()
    log = app.state.store.log_path(run.id).read_text()
    assert "host={{ 6 * 7 }} size=large token=6 free=42" in log  # answers literal, free-form templated
    assert "sekrit" not in log and "sekrit" not in str(run.prompt_vars)
    page = c.get(f"/runs/{run.id}").text
    assert "build_size: large" in page and "(secret)" in page and "1 line of YAML" in page
    again = c.get(html.unescape(re.search(r'href="(/runs/new\?[^"]+)"', page).group(1))).text
    assert 'value="large" selected' in again and "free_var: &#34;{{ 40 + 2 }}&#34;" in again
    assert "sekrit" not in again  # Run again carries the answers and the free-form block; the secret is typed again


def test_bad_variables_are_refused_with_the_reason(c):
    base = {"playbook": "build.yml", "target": "box1", "mode": "check", "diff": "1", "expected": ["box1"]}
    r = c.post("/runs", data={**base, "var.build_count": "lots"})
    assert r.status_code == 400 and "build_count takes a whole number" in r.text
    r = c.post("/runs", data={**base, "var.build_host": "a", "extra_vars": "build_host: b"})
    assert r.status_code == 400 and "build_host is set in its own field and in Extra variables" in r.text
    r = c.post("/runs", data={**base, "remote_user": "deploy", "extra_vars": "ansible_user: x"})
    assert r.status_code == 400 and "Connect as" in r.text
    assert app_runs(c) == 0


def app_runs(c):
    return len(c.app.state.store.recent())


@needs_ansible
def test_template_keeps_its_secret_and_records_changed_answers(app, c):
    data = {"name": "web", "playbook": "build.yml", "target": "box1", "mode": "apply", "diff": "1",
            "purpose": "template", "var.build_host": "web1", "var.build_token": "tpl-secret"}
    assert c.post("/runs/templates", data=data, follow_redirects=False).status_code == 303
    t = app.state.templates.all()[0]
    assert t.prompt_vars["build_host"] == "web1" and "tpl-secret" not in str(t.prompt_vars)
    page = c.get(f"/runs/templates/{t.id}").text
    assert "Saved, encrypted. Leave empty to reuse it." in page and "tpl-secret" not in page

    launcher = c.get("/runs/new", params={"template": t.id}).text
    assert 'value="web1"' in launcher and "As template <strong>web</strong> sets it" in launcher
    form = {"playbook": "0:build.yml", "template": str(t.id), "target": "box1", "mode": "apply", "diff": "1",
            "expected": ["box1"], "var.build_host": "web3", "var.build_token": ""}
    run = wait(app, int(c.post("/runs", data=form, follow_redirects=False).headers["location"].rsplit("/", 1)[1]))
    assert run.template_changes == ["build_host: web1 → web3"]
    assert "token=10" in app.state.store.log_path(run.id).read_text()  # the template's secret, kept

    # Editing the template with the secret field empty keeps it; ticking Remove drops it.
    c.post(f"/runs/templates/{t.id}", data={**data, "var.build_token": ""})
    assert app.state.templates.get(t.id).prompt_vars["build_token"] == t.prompt_vars["build_token"]
    c.post(f"/runs/templates/{t.id}", data={**data, "var.build_token": "", "var.build_token.clear": "1"})
    assert "build_token" not in app.state.templates.get(t.id).prompt_vars


def test_a_schedule_from_a_template_copies_its_answers_and_secret(app, c):
    data = {"name": "web", "playbook": "build.yml", "target": "box1", "mode": "apply", "diff": "1",
            "purpose": "template", "var.build_host": "web1", "var.build_token": "tpl-secret", "extra_vars": "a: 1"}
    c.post("/runs/templates", data=data)
    t = app.state.templates.all()[0]
    page = c.get("/schedules/new", params={"template": t.id}).text
    assert f'name="copy_from" value="{t.id}"' in page and 'value="web1"' in page
    r = c.post("/schedules", data={**data, "purpose": "schedule", "name": "nightly", "cron": "0 3 * * *",
                                   "copy_from": str(t.id), "var.build_token": ""}, follow_redirects=False)
    assert r.status_code == 303, r.text
    s = app.state.schedules.all()[0]
    assert s.prompt_vars == t.prompt_vars and s.extra_vars == "a: 1"


@needs_ansible
def test_run_again_reuses_the_runs_secret_for_the_same_playbook_only(app, c, project):
    form = {"playbook": "build.yml", "target": "box1", "mode": "apply", "diff": "1", "expected": ["box1"],
            "var.build_host": "web1", "var.build_token": "typed-once"}
    first = wait(app, int(c.post("/runs", data=form, follow_redirects=False).headers["location"].rsplit("/", 1)[1]))
    page = c.get(f"/runs/{first.id}").text
    link = html.unescape(re.search(r'href="(/runs/new\?[^"]+)"', page).group(1))
    assert f"rerun={first.id}" in link and "typed-once" not in link
    launcher = c.get(link).text
    assert f'name="rerun" value="{first.id}"' in launcher and "Leave empty to reuse it." in launcher

    again = {**form, "var.build_token": "", "rerun": str(first.id), "target": "box2", "expected": ["box2"]}
    second = wait(app, int(c.post("/runs", data=again, follow_redirects=False).headers["location"].rsplit("/", 1)[1]))
    assert second.prompt_vars["build_token"] == first.prompt_vars["build_token"]
    assert "token=10" in app.state.store.log_path(second.id).read_text()

    # Another playbook with a prompt of the same name doesn't get it.
    (project / "build2.yml").write_text((project / "build.yml").read_text())
    other = {**again, "playbook": "build2.yml"}
    third = wait(app, int(c.post("/runs", data=other, follow_redirects=False).headers["location"].rsplit("/", 1)[1]))
    assert "build_token" not in third.prompt_vars

    # Save as template… from the run keeps the secret too.
    data = {**form, "name": "from-run", "purpose": "template", "var.build_token": "", "rerun": str(first.id)}
    assert c.post("/runs/templates", data=data, follow_redirects=False).status_code == 303
    assert app.state.templates.all()[0].prompt_vars["build_token"] == first.prompt_vars["build_token"]


def test_each_field_says_what_the_inventory_already_sets_on_the_targets(c):
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "box1"}).text
    note = page.split('id="inv-build_count">')[1].split("</p>")[0]
    assert "Inventory" in note and ">3<" in note and "on box1" in note and "host_vars/box1.yml" in note
    assert "Not in the inventory for this host." in page.split('id="inv-build_size">')[1].split("</p>")[0]
    assert "cloud-user" in page.split('id="inv-ansible_user">')[1].split("</p>")[0]  # under Connect as
    # The preview redraws the notes when the targets change.
    preview = c.get("/runs/preview", params={"playbook": "0:build.yml", "target": "lab"}).text
    count = preview.split('id="inv-build_count" hx-swap-oob="true">')[1].split("</p>")[0]
    assert "on 1 of 2 hosts" in count


def test_the_preview_lists_the_inventory_values_the_run_overrides(c):
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "lab", "var.build_count": "5",
                                      "remote_user": "deploy"}).text
    side = page.split('class="launcher-side"')[1]
    assert "Inventory values this run overrides" in side
    box1 = side.split('target-host">box1</span>')[1].split("</li>\n      \n")[0]
    assert "build_count</span> <s class=\"mono\">3</s>" in box1 and "deploy" in box1 and "host_vars/box1.yml" in box1
    assert "none: it keeps its inventory values" in side.split('target-host">box2</span>')[1]


def test_the_default_shown_is_the_one_defaults_main_sets(project):
    by = {p.name: p for p in variables.discover(project / "build.yml", project)[0]}
    assert by["build_count"].default_text == "4" and by["build_size"].default_text == "small"


def test_the_playbook_list_says_what_each_does_and_offers_its_actions(c):
    page = c.get("/runs/playbooks").text
    row = page.split('href="/runs/new?playbook=0%3Acommented.yml"')[1].split("</tr>")[0]
    assert "Patches every host. Reboots only if needed." in row
    assert "Builds a lab box from the cloud image" in page.split("0%3Abuild.yml")[1].split("</tr>")[0]  # its role
    assert "Lab check" in page.split("0%3Asite.yml")[1].split("</tr>")[0]  # no comment, no roles: play names
    assert 'href="/runs/templates/new?playbook=0%3Asite.yml"' in page and 'href="/schedules/new?playbook=0%3Asite.yml"' in page
    assert '<a class="rail-item rail-sub" href="/runs/playbooks" aria-current="page">' in page
    assert '<a class="rail-item rail-sub" href="/runs/playbooks" aria-current="page">' in c.get("/runs/new").text


def test_template_and_schedule_rows_have_run_actions(app, c):
    c.post("/runs/templates", data={"name": "t1", "playbook": "site.yml", "target": "box1", "mode": "check",
                                    "diff": "1", "purpose": "template"})
    t = app.state.templates.all()[0]
    listing = c.get("/runs/templates").text
    assert f'href="/runs/new?template={t.id}" aria-label="Run t1">Run…' in listing
    assert f'href="/schedules/new?template={t.id}" aria-label="Schedule t1"' in listing
    c.post("/schedules", data={"name": "n1", "cron": "0 3 * * *", "playbook": "site.yml", "target": "box1",
                               "mode": "apply", "diff": "1", "purpose": "schedule"})
    s = app.state.schedules.all()[0]
    sched = c.get("/schedules").text
    assert f'action="/schedules/{s.id}/run"' in sched and "Apply now…" in sched and "data-confirm-twice" in sched


def test_the_yaml_view_carries_values_both_ways_and_stores_like_the_fields(app, c, project):
    base = {"playbook": "0:build.yml", "target": "box1"}
    to_yaml = c.get("/runs/vars", params={**base, "vars_mode": "yaml", "var.build_host": "web3", "var.build_size": "large",
                                          "var.build_token": "", "extra_vars": "free_var: x"}).text
    assert 'name="var.build_host"' not in to_yaml and 'type="password" name="var.build_token"' in to_yaml
    text = html.unescape(to_yaml.split('name="extra_vars"')[1].split(">", 1)[1].split("</textarea>")[0])
    assert text == "build_host: web3\nbuild_size: large\nfree_var: x"
    back = c.get("/runs/vars", params={**base, "vars_mode": "fields", "extra_vars": text}).text
    assert 'value="web3"' in back and 'value="large" selected' in back and "Also sends <span class=\"mono\">free_var</span>" in back
    assert "<textarea name=\"extra_vars\" hidden>free_var: x</textarea>" in back

    form = {**base, "vars_mode": "yaml", "mode": "check", "diff": "1", "expected": ["box1"],
            "extra_vars": "build_host: \"{{ 6 * 7 }}\"\nfree_var: x"}
    r = c.post("/runs", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    run = app.state.store.get(int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.prompt_vars == {"build_host": "{{ 6 * 7 }}"} and run.extra_vars == "free_var: x"


def test_a_preview_sent_mid_switch_keeps_the_field_values(c):
    page = c.get("/runs/preview", params={"playbook": "0:build.yml", "target": "box1", "vars_mode": "yaml",
                                          "var.build_count": "5", "extra_vars": ""}).text
    assert "build_count" in page.split('class="override-list"')[1]
