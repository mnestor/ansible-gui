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


def alias_bomb(levels: int) -> str:
    """Each line a list of ten references to the one before: ten times the values per 50 characters."""
    lines = ["a0: &a0 [" + ",".join(["x"] * 10) + "]"]
    lines += [f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 10) + "]" for i in range(1, levels)]
    return "\n".join(lines)


def test_aliases_cannot_multiply_typed_yaml_into_billions_of_values():
    """Eight levels took the server five seconds and a gigabyte to show, with every other request waiting;
    twelve never finish. The size is worked out from the aliases, without writing anything out."""
    bomb = alias_bomb(12)
    assert len(bomb) < 600
    free, problem = variables.check_freeform(bomb)
    assert free == {} and "20,000 values once the YAML aliases" in problem
    assert variables.split_yaml(bomb, [])[0] == {}
    assert "aliases" in variables.check_freeform("a: &a [*a]\n")[1]  # a list inside itself
    # Aliases used as they are meant to be are fine, and so is all the plain YAML that fits in the box.
    free, problem = variables.check_freeform("base: &base {port: 22, user: deploy}\nweb: *base\ndb: *base\n")
    assert problem is None and free["db"] == {"port": 22, "user": "deploy"}
    plain = "a: [" + ",".join(["1"] * 9_990) + "]"
    assert len(plain) < variables.MAX_FREEFORM and variables.check_freeform(plain)[1] is None
    # A YAML answer to a role's dict option goes the same way.
    option = variables.Prompt(name="build_opts", role="builder", kind="dict")
    assert "build_opts holds more than 20,000 values" in variables._convert(option, bomb)[1]
    assert variables._convert(option, "a: &a [1, 2]\nb: *a\n") == ({"a": [1, 2], "b": [1, 2]}, None)


def test_written_out_counts_shared_values_each_time_and_never_recurses():
    shared = [1, 2, 3]
    assert variables.written_out({"a": shared, "b": shared, "c": {"d": shared}}) == 2 + 3 * 4
    assert variables.written_out("x") == 1 and variables.written_out([]) == 1
    deep = []
    for _ in range(50_000):
        deep = [deep]
    assert variables.written_out(deep) == 50_001
    loop = {}
    loop["me"] = [loop]
    assert variables.written_out(loop) == float("inf")


def test_a_run_with_an_alias_bomb_is_refused(app, c):
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "check", "expected": ["box1"],
                              "extra_vars": alias_bomb(12)})
    assert r.status_code == 400 and "YAML aliases" in r.text and app.state.store.recent() == []
    # The launcher and its preview only read the form, so a link can carry one: they answer, and say why.
    assert c.get("/runs/preview", params={"playbook": "site.yml", "extra_vars": alias_bomb(12)}).status_code == 200
    assert c.get("/runs/new", params={"extra_vars": alias_bomb(12)}).status_code == 200


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
    c.post("/credentials", data={"kind": "account", "name": "dep", "username": "deploy"})
    acct = c.app.state.creds.by_kind("account")[0]
    r = c.post("/runs", data={**base, "account": str(acct.id), "extra_vars": "ansible_user: x"})
    assert r.status_code == 400 and "so does the account dep" in r.text
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
    # A field the inventory doesn't set has no note: the Variables card says it once, for all of them.
    assert page.split('id="inv-build_size">')[1].startswith("</p>")
    summary = page.split('id="inv-summary">')[1].split("</p>")[0]
    assert "The inventory sets 1 of these 8 variables for this host" in summary
    assert "cloud-user" in page.split('id="inv-ansible_user">')[1].split("</p>")[0]  # under Connect as
    # The preview redraws the notes when the targets change.
    preview = c.get("/runs/preview", params={"playbook": "0:build.yml", "target": "lab"}).text
    count = preview.split('id="inv-build_count" hx-swap-oob="true">')[1].split("</p>")[0]
    assert "on 1 of 2 hosts" in count
    assert "for these 2 hosts" in preview.split('id="inv-summary" hx-swap-oob="true">')[1].split("</p>")[0]


def side_of(page: str) -> str:
    return page.split('class="launcher-side"')[1] if 'class="launcher-side"' in page else page


def block(side: str, name: str) -> str:
    """One overridden variable's block in the preview."""
    return side.split(f'<li class="override" data-var="{name}">')[1].split("</li><!-- /override -->")[0]


@needs_ansible
def test_the_preview_lists_each_overridden_variable_once_with_what_it_replaces(c):
    c.post("/credentials", data={"kind": "account", "name": "dep", "username": "deploy"})
    acct = c.app.state.creds.by_kind("account")[0]
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "lab", "var.build_count": "5",
                                      "account": str(acct.id)}).text
    side = side_of(page)
    assert "Replaces inventory values" in side
    count = block(side, "build_count")
    assert '<span class="visually-hidden">now</span><span class="mono override-now">5</span>' in count
    assert '<span class="visually-hidden">replaces</span><s class="mono">3</s>' in count
    assert "host_vars/box1.yml" in count and "box1" in count
    assert "cloud-user" in block(side, "ansible_user") and "deploy" in block(side, "ansible_user")
    # The account sets ansible_ssh_user too, but only as the login's twin: it isn't news.
    assert "ansible_ssh_user" not in side
    # The old per-host rows are gone; the hosts are one list.
    assert "target-host" not in side and "keeps its inventory values" not in side
    hosts = side.split('class="preview-sub">Hosts')[1]
    assert "box1" in hosts and "box2" in hosts
    # The button's help line says it, above the details.
    actions = side.split('class="actions"')[1].split("</div>")[0]
    assert "Replaces inventory values for 2 variables, listed below." in actions
    assert "Replaces inventory values · 2 variables" in side


@needs_ansible
def test_hosts_past_the_fifth_are_checked_and_many_hosts_fold(c, project):
    hosts = "".join(f"        box{i}:\n          ansible_connection: local\n" for i in range(1, 8))
    (project / "inventory" / "hosts.yml").write_text(f"all:\n  children:\n    lab:\n      hosts:\n{hosts}")
    for i in (6, 7):
        (project / "inventory" / "host_vars" / f"box{i}.yml").write_text("build_count: 9\n")
    c.post("/inventory/reload")
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "lab", "var.build_count": "5"}).text
    count = block(side_of(page), "build_count")
    nine = count.split('<s class="mono">9</s>')[1].split("</details>")[0]
    assert "2 files" in nine  # one line for the value, though each host has its own host_vars file
    assert "<summary" in nine and "2 hosts" in nine and "box6" in nine and "box7" in nine


@needs_ansible
def test_the_preview_says_so_when_the_run_changes_no_inventory_value(c):
    nothing = side_of(c.get("/runs/new", params={"playbook": "build.yml", "target": "lab"}).text)
    assert "Replaces inventory values" not in nothing and "change any inventory value" not in nothing
    new_only = side_of(c.get("/runs/new", params={"playbook": "build.yml", "target": "lab",
                                                  "var.build_size": "large"}).text)
    assert "Replaces inventory values" not in new_only
    assert ('The run sets <span class="mono">build_size</span> without changing any inventory value on these hosts.'
            in new_only)
    same = c.get("/runs/new", params={"playbook": "build.yml", "target": "box1", "var.build_count": "3"}).text
    assert "Replaces inventory values" not in side_of(same)
    note = same.split('id="inv-build_count">')[1].split("</p>")[0]
    assert "Same as the inventory’s value" in note


@needs_ansible
def test_a_field_with_a_value_says_it_replaces_the_inventorys(c):
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "box1", "var.build_count": "5"}).text
    note = page.split('id="inv-build_count">')[1].split("</p>")[0]
    assert "Replaces</span> the inventory’s value <s class=\"mono inv-value\">3</s>" in note and "host_vars/box1.yml" in note
    c.post("/credentials", data={"kind": "account", "name": "dep", "username": "deploy"})
    acct = c.app.state.creds.by_kind("account")[0]
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "box1", "account": str(acct.id)}).text
    user = page.split('id="inv-ansible_user">')[1].split("</p>")[0]
    assert "Replaces" in user and "cloud-user" in user


@needs_ansible
def test_the_preview_cant_say_nothing_changes_when_the_inventory_didnt_load(tmp_path, project):
    app = create_app(Settings(project_dir=project, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                              secret_key="s" * 40, inventory_binary="ansible-inventory-missing"))
    side = side_of(TestClient(app).get("/runs/new", params={"playbook": "build.yml", "target": "lab",
                                                            "var.build_count": "5"}).text)
    assert "Can’t check for overrides: the inventory didn’t load." in side
    assert "without changing any inventory value" not in side
    page = TestClient(app).get("/runs/new", params={"playbook": "build.yml", "target": "lab"}).text
    assert "Can’t check what the inventory sets: it didn’t load." in page.split('id="inv-summary">')[1].split("</p>")[0]


def test_the_default_shown_is_the_one_defaults_main_sets(project):
    by = {p.name: p for p in variables.discover(project / "build.yml", project)[0]}
    assert by["build_count"].default_text == "4" and by["build_size"].default_text == "small"


def test_the_playbook_list_says_what_each_does_and_offers_its_actions(c):
    page = c.get("/runs/playbooks").text
    row = page.split('href="/runs/new?playbook=0%3Acommented.yml"')[1].split("</tr>")[0]
    # A short comment is one line and all there is: its two lines read as the sentence they are, nothing to unfold.
    assert '<div class="playbook-about">Patches every host. Reboots only if needed.</div>' in row
    assert "Builds a lab box from the cloud image" in page.split("0%3Abuild.yml")[1].split("</tr>")[0]  # its role
    assert "Lab check" in page.split("0%3Asite.yml")[1].split("</tr>")[0]  # no comment, no roles: play names
    assert 'href="/runs/templates/new?playbook=0%3Asite.yml"' in page and 'href="/schedules/new?playbook=0%3Asite.yml"' in page
    assert '<a class="rail-item rail-sub" href="/runs/playbooks" aria-current="page">' in page
    launcher = c.get("/runs/new", params={"playbook": "commented.yml"}).text
    assert '<a class="rail-item rail-sub" href="/runs/playbooks" aria-current="page">' in launcher
    current = launcher.split("data-picker-current>")[1].split('<span class="picker-change">')[0]
    assert "commented.yml" in current and "Patches every host." in current and 'class="repo-prefix"' in current
    assert "<details class=\"picker\" data-picker >" in launcher or "data-picker >" in launcher  # closed once chosen
    assert "Choose a playbook" in c.get("/runs/new").text.split("data-picker-current>")[1][:120]


def test_template_and_schedule_rows_have_run_actions(app, c):
    c.post("/runs/templates", data={"name": "t1", "playbook": "site.yml", "target": "box1", "mode": "check",
                                    "diff": "1", "purpose": "template"})
    t = app.state.templates.all()[0]
    listing = c.get("/runs/templates").text
    assert f'href="/runs/new?template={t.id}" aria-label="Run t1…"' in listing
    assert f'href="/schedules/new?template={t.id}" aria-label="Schedule t1…"' in listing
    assert '<span class="tip" aria-hidden="true">Run t1…</span>' in listing
    c.post("/schedules", data={"name": "n1", "cron": "0 3 * * *", "playbook": "site.yml", "target": "box1",
                               "mode": "apply", "diff": "1", "purpose": "schedule"})
    s = app.state.schedules.all()[0]
    sched = c.get("/schedules").text
    assert f'action="/schedules/{s.id}/run"' in sched and 'aria-label="Apply n1 now"' in sched and "data-confirm-twice" in sched


def test_the_yaml_view_carries_values_both_ways_and_stores_like_the_fields(app, c, project):
    base = {"playbook": "0:build.yml", "target": "box1"}
    to_yaml = c.get("/runs/vars", params={**base, "vars_mode": "yaml", "var.build_host": "web3", "var.build_size": "large",
                                          "var.build_token": "", "extra_vars": "free_var: x"}).text
    assert 'name="var.build_host"' not in to_yaml and 'type="password" name="var.build_token"' in to_yaml
    # The secret sits below the YAML, under its own heading saying why.
    assert to_yaml.index('name="extra_vars"') < to_yaml.index("Secrets</span>") < to_yaml.index('name="var.build_token"')
    assert "marked <span class=\"mono\">no_log</span>" in to_yaml
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
    assert 'data-var="build_count"' in page


def test_a_playbooks_top_comment_keeps_its_lines_and_indentation(tmp_path):
    """The comment was joined into one line, so a list in it, or a command to copy, ran together."""
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "hosts.yml").write_text("all:\n  hosts:\n    box1:\n")
    (tmp_path / "site.yml").write_text(
        "---\n"
        "# Sets up the lab:\n"
        "#   - installs the packages\n"
        "#   - writes the motd\n"
        "#\n"
        "#\n"
        "# Try it first:  ansible-playbook site.yml --check <b>\n"
        "# vim: ft=yaml\n"
        "#\n"
        "- name: Lab\n  hosts: all\n  tasks: []\n")
    written = "Sets up the lab:\n  - installs the packages\n  - writes the motd\n\nTry it first:  ansible-playbook site.yml --check <b>"
    assert variables.summary(variables.describe(tmp_path / "site.yml", tmp_path)) == written
    app = create_app(Settings(project_dir=tmp_path, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                              secret_key="s" * 40))
    c = TestClient(app)
    lead, rest = variables.lead_and_rest(written)
    assert lead == "Sets up the lab:"  # the list under it starts the rest, with its indentation
    page = c.get("/runs/playbooks").text
    assert ('<summary><span class="about-lead">Sets up the lab:</span> <span class="about-toggle">'
            '<span class="about-closed">Show 3 more lines</span><span class="about-open">Show less</span></span></summary>') in page
    assert rest == "  - installs the packages\n  - writes the motd\n\nTry it first:  ansible-playbook site.yml --check <b>"
    folded = f'<div class="about-rest">{html.escape(rest, quote=False)}</div>'
    assert folded in c.get("/runs/playbooks").text  # the catalogue: unfolded, the comment is as it was written
    launcher = c.get("/runs/new").text
    assert '<span class="picker-about">Sets up the lab:</span>' in launcher  # the picker: one line
    assert folded in launcher  # and the rest under the field, for reading before the run
    css = (Path(variables.__file__).parent.parent / "static" / "app.css").read_text()
    rule = ".about-rest {"  # the browser keeps the breaks only if told to
    assert "white-space: pre-wrap" in css[css.index(rule):css.index("}", css.index(rule))]


def test_the_line_a_list_shows_is_the_comments_opening_paragraph():
    """A long comment shows as one line with the rest folded. Where its author wrapped it says nothing, so the
    line is the opening paragraph read across the wraps, never the first line cut mid-sentence."""
    wrapped = ("Install/verify borgmatic on every LXC client and manage\n/etc/borgmatic/config.yaml.\n\n"
               "  ansible-playbook borgmatic.yml --check\n\nHosts already backing up keep their repository.")
    assert variables.lead_and_rest(wrapped) == (
        "Install/verify borgmatic on every LXC client and manage /etc/borgmatic/config.yaml.",
        "  ansible-playbook borgmatic.yml --check\n\nHosts already backing up keep their repository.")
    assert variables.lead_and_rest("Hardens the DMZ. See the wiki.\nRuns on dmz_hosts only.") == (
        "Hardens the DMZ. See the wiki. Runs on dmz_hosts only.", "")
    assert variables.lead_and_rest("One line.") == ("One line.", "")
    assert variables.lead_and_rest("") == ("", "")
    # A command to copy or a list is never read into the line: it starts the rest, as written.
    assert variables.lead_and_rest("Run it like this:\nansible-playbook x.yml") == ("Run it like this:", "ansible-playbook x.yml")
    assert variables.lead_and_rest("Sets up\n- one\n- two") == ("Sets up", "- one\n- two")
    assert variables.lead_and_rest("First\n  indented") == ("First", "  indented")
    # One long paragraph and nothing after it still folds: a row is one line until it is opened.
    long = variables.Folded(*variables.lead_and_rest("word " * 40))
    assert long.folds and not long.rest and not variables.Folded("Short.", "").folds


def test_the_playbook_list_can_be_searched_by_name_and_by_what_the_comment_says(tmp_path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "hosts.yml").write_text("all:\n  hosts:\n    box1:\n")
    (tmp_path / "backup.yml").write_text("# Backs up every host.\n#\n# Needs the Borg repository in host_vars.\n"
                                         "- hosts: all\n  tasks: []\n")
    (tmp_path / "patch.yml").write_text("# Patches every host.\n- hosts: all\n  tasks: []\n")
    app = create_app(Settings(project_dir=tmp_path, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                              secret_key="s" * 40))
    page = TestClient(app).get("/runs/playbooks").text
    assert 'type="search"' in page and "data-rowfilter" in page and "2 playbooks" in page
    # One repository: its heading, and nothing to pick between.
    assert page.count('<tr class="list-group">') == 1 and "source-chips" not in page and 'class="repo-prefix"' not in page
    backup = page.split(' data-search="')[1].split("</tr>")[0]
    # Found by what is folded away too; data-head says what is on screen, so a match elsewhere unfolds the row.
    assert "backup.yml" in backup and "needs the borg repository in host_vars." in backup.split('"')[0]
    assert 'data-head="' in backup and "borg repository" not in backup.split('data-head="')[1].split('"')[0]
    assert "No playbooks match" in page
    patch = page.split(' data-search="')[2].split("</tr>")[0]
    assert "<details" not in patch and "Patches every host." in patch  # nothing to unfold: no toggle


@needs_ansible
def test_only_the_count_is_announced_when_the_preview_redraws(c):
    page = c.get("/runs/new", params={"playbook": "build.yml", "target": "lab"}).text
    assert 'id="preview" class="preview">' in page  # not a live region: it holds the button and long lists
    assert '<p id="preview-status" class="visually-hidden" role="status"></p>' in page
    preview = c.get("/runs/preview", params={"playbook": "0:build.yml", "target": "lab", "var.build_count": "5"}).text
    status = preview.split('<p id="preview-status" hx-swap-oob="innerHTML">')[1].split("</p>")[0]
    assert status == "2 hosts. Replaces inventory values for 1 variable, listed below."


def test_the_launcher_copy_says_what_it_means(c):
    page = c.get("/runs/new", params={"playbook": "build.yml"}).text
    assert "a value here wins over the inventory and the role’s defaults" in page
    summary = page.split("optional variables")[1].split("</summary>")[0]
    assert ",..." not in summary and " more</span>" in summary
    empty = c.get("/runs/new").text
    assert 'aria-describedby="run-why"' in empty and 'id="run-why"' in empty
