import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import create_app, present
from ansible_gui.inventory.model import VAULTED

FIXTURE = Path(__file__).parent / "fixtures" / "homelab"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-inventory") is None, reason="needs ansible-core")


def client(**kw) -> TestClient:
    settings = Settings(project_dir=FIXTURE, inventory=["inventory/hosts.ini"], data_dir=Path(tempfile.mkdtemp()), **kw)
    return TestClient(create_app(settings))


def test_present_never_renders_vaulted():
    assert present(VAULTED) == {"kind": "vaulted"}
    assert present(True)["text"] == "true"
    assert present(["a"])["kind"] == "block"
    assert present("{{ x }}")["type"] == "template"


@needs_ansible
def test_host_page_shows_source_and_overrides():
    c = client(edit_url="https://git.example/repo/src/branch/main/{path}#L{line}")
    r = c.get("/inventory/hosts/web1")
    assert r.status_code == 200
    html = r.text
    assert "inventory/host_vars/web1.yml:1" in html
    assert "https://git.example/repo/src/branch/main/inventory/host_vars/web1.yml#L1" in html
    assert "overrides 2" in html
    assert "hunter2" not in html and "ANSIBLE_VAULT" not in html
    assert "vaulted" in html


@needs_ansible
def test_group_page_and_missing():
    c = client()
    assert "Variables set on this group" in c.get("/inventory/groups/web").text
    r = c.get("/inventory/hosts/nope")
    assert r.status_code == 404
    assert "no host with this name" in r.text


@needs_ansible
def test_overview_reports_all_match():
    assert "all match" in client().get("/inventory").text


def test_load_error_page_shows_command_and_retry():
    r = client(inventory_binary="ansible-inventory-missing").get("/inventory")
    assert "Couldn’t load the inventory" in r.text
    assert "ansible-inventory-missing is not installed" in r.text
    assert "Try again" in r.text


@needs_ansible
def test_empty_inventory_explains_itself():
    settings = Settings(project_dir=FIXTURE, inventory=["does-not-exist.ini"], data_dir=Path(tempfile.mkdtemp()))
    r = TestClient(create_app(settings)).get("/inventory")
    assert "This inventory has no hosts" in r.text


@needs_ansible
def test_reload_redirects_with_flash():
    c = client()
    r = c.post("/inventory/reload", headers={"referer": "http://testserver/inventory/hosts/web1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/inventory/hosts/web1?reloaded=")
    r = c.post("/inventory/reload", headers={"referer": "https://evil.example/phish"}, follow_redirects=False)
    assert r.status_code == 403  # cross-site write refused outright
    r = c.post("/inventory/reload", headers={"referer": "http://testserver/runs"}, follow_redirects=False)
    assert r.headers["location"].startswith("/inventory?reloaded=")


def test_rail_lists_hosts_and_groups_and_the_second_rail_shows_one_list():
    c = client()
    page = c.get("/inventory/groups/web").text
    assert 'href="/inventory/groups" aria-current="page"' in page
    assert 'data-list="groups"' in page and 'data-list="hosts"' not in page and "data-show" not in page
    page = c.get("/inventory/hosts").text
    assert 'href="/inventory/hosts" aria-current="page"' in page and 'data-list="groups"' not in page
    assert "data-rail-toggle" in page
