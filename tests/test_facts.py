"""Facts: the latest ones each run gathers are kept per host, and Gather facts reads them without changing anything."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.facts import FactStore
from ansible_gui.web import create_app

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")

FULL = {"gather_subset": ["all"], "ansible_distribution": "Debian", "ansible_distribution_version": "12.7",
        "ansible_distribution_release": "bookworm", "ansible_kernel": "6.8.12-4-pve", "ansible_architecture": "x86_64",
        "ansible_virtualization_type": "lxc", "ansible_virtualization_role": "guest", "ansible_processor_vcpus": 2,
        "ansible_memtotal_mb": 2048, "ansible_default_ipv4": {"address": "172.16.5.4"},
        "ansible_all_ipv4_addresses": ["10.0.0.4", "172.16.5.4"], "ansible_python_version": "3.11.2",
        "ansible_uptime_seconds": 3 * 86400 + 5, "ansible_env": {"API_TOKEN": "leak-me"},
        "ansible_mounts": [{"mount": "/"}]}


def test_full_gather_replaces_partial_gather_updates(tmp_path):
    store = FactStore(tmp_path)
    store.record("emby", {**FULL, "ansible_lsb": {"id": "Debian"}}, run_id=3)
    store.record("emby", {**FULL, "ansible_kernel": "6.8.12-5-pve"}, run_id=4)
    f = store.get("emby")
    assert f.run_id == 4 and f.facts["ansible_kernel"] == "6.8.12-5-pve" and "ansible_lsb" not in f.facts
    store.record("emby", {"gather_subset": ["min"], "ansible_python_version": "3.11.9"}, run_id=5)
    f = store.get("emby")
    assert f.run_id == 5 and f.facts["ansible_python_version"] == "3.11.9" and f.facts["ansible_mounts"] == [{"mount": "/"}]
    assert "ansible_env" not in f.facts and "leak-me" not in (tmp_path / "ansible-gui.db").read_bytes().decode("latin-1")
    assert store.get("nobody") is None


def test_summary_reads_like_a_spec_sheet(tmp_path):
    store = FactStore(tmp_path)
    store.record("emby", FULL, run_id=1)
    assert dict(store.get("emby").summary()) == {
        "OS": "Debian 12.7 (bookworm)", "Kernel": "6.8.12-4-pve x86_64", "Virtualization": "lxc guest",
        "Hardware": "2 vCPU, 2.0 GiB RAM", "IPv4": "172.16.5.4, 10.0.0.4", "Python": "3.11.2",
        "Uptime when gathered": "3 days"}
    store.record("bare", {"gather_subset": ["all"], "ansible_virtualization_type": "NA"}, run_id=1)
    assert store.get("bare").summary() == []


@pytest.fixture
def project(tmp_path):
    """box1 connects locally only through group_vars next to the playbooks: a facts run that skipped
    them would try SSH to box1 and fail."""
    p = tmp_path / "proj"
    (p / "inventory").mkdir(parents=True)
    (p / "inventory" / "hosts.yml").write_text("all:\n  children:\n    lab:\n      hosts:\n        box1:\n        box2:\n")
    (p / "playbooks" / "group_vars").mkdir(parents=True)
    (p / "playbooks" / "group_vars" / "all.yml").write_text(
        "ansible_connection: local\nansible_python_interpreter: \"{{ ansible_playbook_python }}\"\n")
    (p / "playbooks" / "site.yml").write_text(
        "- hosts: lab\n  gather_facts: true\n  tasks:\n    - ansible.builtin.debug:\n        msg: hi\n")
    (p / "playbooks" / "quiet.yml").write_text(
        "- hosts: lab\n  gather_facts: false\n  tasks:\n    - ansible.builtin.debug:\n        msg: hi\n")
    return p


@pytest.fixture
def client(tmp_path, project):
    app = create_app(Settings(project_dir=project, inventory=["inventory/hosts.yml"], playbook_dir="playbooks",
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
def test_a_playbook_run_saves_the_facts_it_gathers(client):
    app, c = client
    assert "No facts yet." in c.get("/inventory/hosts/box1").text
    run = finish(app, c.post("/runs", data={"playbook": "0:playbooks/site.yml", "target": "box1", "mode": "check"},
                             follow_redirects=False))
    assert run.status == "successful", app.state.store.log_path(run.id).read_text()
    f = app.state.facts.get("box1")
    assert f.run_id == run.id and f.facts["ansible_distribution"] and "ansible_env" not in f.facts
    assert app.state.facts.get("box2") is None  # not targeted
    page = c.get("/inventory/hosts/box1").text
    assert f"by <a href=\"/runs/{run.id}\">run #{run.id}</a>" in page and "All " in page and "data-fact=" in page
    # A play that doesn't gather leaves the facts as they were.
    quiet = finish(app, c.post("/runs", data={"playbook": "0:playbooks/quiet.yml", "target": "box1", "mode": "apply"},
                               follow_redirects=False))
    assert quiet.status == "successful" and app.state.facts.get("box1").run_id == run.id


@needs_ansible
def test_gather_facts_on_a_group_changes_nothing_and_saves_each_host(client):
    app, c = client
    run = finish(app, c.post("/facts/gather", data={"target": "lab"}, follow_redirects=False))
    assert run.kind == "facts" and run.status == "successful", app.state.store.log_path(run.id).read_text()
    assert sorted(run.expected_hosts) == ["box1", "box2"]
    assert all(app.state.facts.get(h).run_id == run.id for h in ("box1", "box2"))
    assert all(s["changed"] == 0 for s in run.host_stats.values())
    log = app.state.store.log_path(run.id).read_text()
    assert log.startswith("$ ansible lab -m ansible.builtin.setup -i ") and "--playbook-dir" in log
    assert "box1 | SUCCESS | " in log and "ansible_env" not in log and "ansible_distribution" not in log
    page = c.get(f"/runs/{run.id}").text
    assert "Gather facts" in page and "Read only" in page and "Gather again" in page and "Schedule this" not in page
    assert "Gather facts" in c.get("/runs").text
    assert 'action="/facts/gather"' in c.get("/inventory/groups/lab").text


@needs_ansible
def test_gather_facts_on_an_unknown_target_stops_before_running(client):
    app, c = client
    run = finish(app, c.post("/facts/gather", data={"target": "nope"}, follow_redirects=False))
    assert run.status == "error" and "match no hosts" in run.error


def test_gather_facts_refuses_a_missing_or_option_like_target(client):
    _, c = client
    assert c.post("/facts/gather", data={"target": ""}).status_code == 400
    assert c.post("/facts/gather", data={"target": "--help"}).status_code == 400
