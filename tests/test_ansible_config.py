"""The Ansible settings page: what ansible-config reports for a repository, with credentials hidden."""

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from ansible_gui import ansible_config
from ansible_gui.config import Settings
from ansible_gui.web import create_app

needs_ansible = pytest.mark.skipif(shutil.which("ansible-config") is None, reason="needs ansible-core")

CFG = """[defaults]
forks = 7

[galaxy]
server_list = mine

[galaxy_server.mine]
url = https://galaxy.example.test/
token = sekrit-token-123
"""


def test_secret_names():
    assert ansible_config.is_secret("token") and ansible_config.is_secret("GALAXY_API_KEY")
    assert ansible_config.is_secret("password")
    for name in ("GALAXY_TOKEN_PATH", "DEFAULT_VAULT_PASSWORD_FILE", "DEFAULT_PRIVATE_KEY_FILE", "DEFAULT_FORKS",
                 "DEFAULT_VAULT_IDENTITY_LIST"):
        assert not ansible_config.is_secret(name), name


@needs_ansible
def test_report_names_where_each_setting_comes_from(tmp_path, monkeypatch):
    (tmp_path / "ansible.cfg").write_text(CFG)
    monkeypatch.setenv("ANSIBLE_TIMEOUT", "33")
    rep = ansible_config.report(tmp_path, tmp_path, {})
    assert rep.error is None and rep.config_file == "ansible.cfg"
    found = {s.name: s for s in rep.settings}
    assert found["DEFAULT_FORKS"].value == "7" and found["DEFAULT_FORKS"].origin == "ansible.cfg"
    assert found["DEFAULT_TIMEOUT"].from_env and found["DEFAULT_TIMEOUT"].origin == "env: ANSIBLE_TIMEOUT"
    token = next(s for s in rep.galaxy["mine"] if s.name == "token")
    assert token.hidden and token.value == ""
    assert "sekrit" not in repr(rep)


def test_collections_in_search_order_mark_the_shadowed_copy(tmp_path, monkeypatch):
    listing = {str(tmp_path / "mine"): {"community.general": {"version": "13.5.1"}},
               "/usr/lib/ansible_collections": {"community.general": {"version": "9.0.0"},
                                                "ansible.posix": {"version": "2.2.2"}},
               "/internal": {"ansible._protomatter": {"version": "*"}}}

    def fake(cmd, cwd, env):
        out = "[]" if cmd[0] == "ansible-config" else json.dumps(listing)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(ansible_config, "_run", fake)
    rep = ansible_config.report(tmp_path, tmp_path, {})
    rows = [(c.name, c.version, c.path, c.used) for c in rep.collections]
    assert rows == [("community.general", "13.5.1", "mine", True),
                    ("ansible.posix", "2.2.2", "/usr/lib/ansible_collections", True),
                    ("community.general", "9.0.0", "/usr/lib/ansible_collections", False)]
    assert rep.collections[2].shadowed_by == "mine"


def test_a_failing_cli_is_reported_not_raised(tmp_path, monkeypatch):
    def fake(cmd, cwd, env):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(ansible_config, "_run", fake)
    rep = ansible_config.report(tmp_path, tmp_path, {})
    assert rep.error == "ansible-config isn’t installed on this server."
    assert rep.collections_error == "ansible-galaxy isn’t installed on this server."


@needs_ansible
def test_page_shows_settings_and_hides_the_galaxy_token(tmp_path):
    p = tmp_path / "proj"
    (p / "inventory").mkdir(parents=True)
    (p / "inventory" / "hosts.yml").write_text("all:\n  hosts:\n    box1:\n")
    (p / "playbooks").mkdir()
    (p / "playbooks" / "site.yml").write_text("- hosts: all\n  tasks: []\n")
    (p / "ansible.cfg").write_text(CFG)
    app = create_app(Settings(project_dir=p, inventory=["inventory/hosts.yml"], playbook_dir="playbooks",
                              data_dir=tmp_path / "data", secret_key="s" * 40))
    c = TestClient(app)
    assert 'href="/repos/0/ansible"' in c.get("/repos").text
    page = c.get("/repos/0/ansible").text
    assert "DEFAULT_FORKS" in page and "Reads <span class=\"mono\">ansible.cfg</span>" in page
    assert "set, not shown" in page and "sekrit" not in page and "galaxy.example.test" in page
    assert c.get("/repos/99/ansible", follow_redirects=False).status_code == 303
