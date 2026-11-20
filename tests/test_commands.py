"""Ad hoc commands: one module on the hosts a pattern matches, confirmed against the preview, with no dry run."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import create_app

needs_ansible = pytest.mark.skipif(shutil.which("ansible") is None, reason="needs ansible-core")


@pytest.fixture
def client(tmp_path):
    """box1 and box2 connect locally only through group_vars next to the playbooks."""
    p = tmp_path / "proj"
    (p / "inventory").mkdir(parents=True)
    (p / "inventory" / "hosts.yml").write_text("all:\n  children:\n    lab:\n      hosts:\n        box1:\n        box2:\n")
    (p / "playbooks" / "group_vars").mkdir(parents=True)
    (p / "playbooks" / "group_vars" / "all.yml").write_text(
        "ansible_connection: local\nansible_python_interpreter: \"{{ ansible_playbook_python }}\"\n")
    (p / "playbooks" / "site.yml").write_text("- hosts: lab\n  tasks: []\n")
    app = create_app(Settings(project_dir=p, inventory=["inventory/hosts.yml"], playbook_dir="playbooks",
                              data_dir=tmp_path / "data", secret_key="s" * 40))
    return app, TestClient(app)


def finish(app, response):
    assert response.status_code == 303, response.text
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    for _ in range(300):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


@needs_ansible
def test_preview_counts_hosts_and_asks_twice_unless_the_module_only_reads(client):
    _, c = client
    page = c.get("/runs/command?target=lab").text
    assert "Run on 2 hosts" in page and 'data-confirm-label="Click again to run on 2 hosts"' in page
    assert 'value="ansible.builtin.shell"' in page and 'data-confirm-twice' in page
    ping = c.get("/runs/command/preview", params={"target": "box1", "module": "ansible.builtin.ping"}).text
    assert "Run on 1 host" in ping and "data-confirm-label" not in ping and "Reads only" in ping
    assert "No hosts match" in c.get("/runs/command/preview", params={"target": "nope"}).text
    assert "Type the targets" in c.get("/runs/command/preview").text


@needs_ansible
def test_a_shell_command_runs_on_each_host_and_can_be_run_again(client):
    app, c = client
    run = finish(app, c.post("/runs/command", data={"target": "lab", "module": "ansible.builtin.shell",
                                                    "args": "echo hello from $((40 + 2))",
                                                    "expected": ["box1", "box2"]}, follow_redirects=False))
    log = app.state.store.log_path(run.id).read_text()
    assert run.kind == "command" and run.status == "successful", log
    assert log.startswith("$ ansible lab -m ansible.builtin.shell -a 'echo hello from $((40 + 2))' -i ")
    assert "--playbook-dir" in log and "--become" not in log and log.count("hello from 42") == 2
    assert not run.check_mode and run.mode == "Command"
    page = c.get(f"/runs/{run.id}").text
    assert "shell: echo hello from $((40 + 2))" in page and "Run again…" in page and "Schedule this" not in page
    assert "/runs/command?target=lab&amp;module=ansible.builtin.shell&amp;args=echo" in page
    assert "shell: echo hello" in c.get("/runs").text


@needs_ansible
def test_ping_with_become_is_logged_with_become(client):
    app, c = client
    run = finish(app, c.post("/runs/command", data={"target": "box1", "module": "ping", "become": "1",
                                                    "expected": ["box1"]}, follow_redirects=False))
    log = app.state.store.log_path(run.id).read_text()
    assert run.become and log.startswith("$ ansible box1 -m ping -i ") and " --become" in log


@needs_ansible
def test_a_command_is_refused_when_the_hosts_changed_since_the_preview(client):
    app, c = client
    r = c.post("/runs/command", data={"target": "lab", "module": "shell", "args": "true", "expected": ["box1"]})
    assert r.status_code == 409 and "now match 2 host(s)" in r.text
    r = c.post("/runs/command", data={"target": "lab", "module": "shell", "args": "true"})
    assert r.status_code == 409
    assert app.state.store.recent() == []


def test_a_command_needs_a_module_name_and_targets(client):
    app, c = client
    assert c.post("/runs/command", data={"target": "lab", "module": "-m evil"}).status_code == 400
    assert c.post("/runs/command", data={"target": "lab", "module": "a.b.c.d"}).status_code == 400
    assert c.post("/runs/command", data={"target": "", "module": "ping"}).status_code == 400
    assert c.post("/runs/command", data={"target": "--help", "module": "ping"}).status_code == 400
    assert app.state.store.recent() == []


@needs_ansible
def test_connect_as_beats_ansible_user_and_ansible_ssh_user_from_the_inventory(client, tmp_path):
    """The bootstrap case: the inventory names the everyday user, one run connects as another."""
    app, c = client
    hv = tmp_path / "proj" / "playbooks" / "host_vars"
    hv.mkdir()
    (hv / "box1.yml").write_text("ansible_user: daily\nansible_ssh_user: daily-ssh\n")
    say = {"target": "box1", "module": "ansible.builtin.debug",
           "args": "msg=user={{ ansible_user }}/{{ ansible_ssh_user }}", "expected": ["box1"]}

    run = finish(app, c.post("/runs/command", data=say, follow_redirects=False))
    assert "user=daily/daily-ssh" in app.state.store.log_path(run.id).read_text()

    run = finish(app, c.post("/runs/command", data={**say, "remote_user": " root "}, follow_redirects=False))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful" and run.remote_user == "root", log
    assert "user=root/root" in log and '"ansible_user": "root"' in log.splitlines()[0]
    page = c.get(f"/runs/{run.id}").text
    assert "Connected as" in page and "remote_user=root" in page  # shown, and kept by Run again


def test_connect_as_refuses_anything_but_a_user_name(client):
    app, c = client
    for bad in ("{{ lookup('pipe', 'id') }}", "root -e x=1", "-u", "a" * 65):
        r = c.post("/runs/command", data={"target": "box1", "module": "ping", "remote_user": bad})
        assert r.status_code == 400 and "Connect as takes a user name" in r.text, bad
        r = c.post("/runs", data={"playbook": "0:playbooks/site.yml", "target": "box1", "remote_user": bad})
        assert r.status_code == 400 and "Connect as takes a user name" in r.text, bad
    assert app.state.store.recent() == []
