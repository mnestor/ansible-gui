import shutil
import subprocess
from pathlib import Path

import pytest

from ansible_gui.inventory import service
from ansible_gui.inventory.cli import CliOptions, InventoryError
from ansible_gui.inventory.model import VAULTED

FIXTURE = Path(__file__).parent / "fixtures" / "homelab"

pytestmark = pytest.mark.skipif(shutil.which("ansible-inventory") is None, reason="needs ansible-core")


@pytest.fixture(scope="module")
def inv():
    return service.load(CliOptions(project_dir=FIXTURE, sources=["inventory/hosts.ini"]))


def src(inv, host, var):
    v = inv.hosts[host].var(var)
    assert v is not None, f"{host} has no {var}"
    return v


def test_no_mismatches_against_ansible(inv):
    bad = {h.name: [(v.name, v.mismatch) for v in h.mismatches] for h in inv.hosts.values() if h.mismatches}
    assert bad == {}


def test_group_vars_file_overrides_inventory_all_vars(inv):
    v = src(inv, "db1", "ntp_server")
    assert v.value == "ntp.home.arpa"
    assert v.source.path == "inventory/group_vars/all.yml"
    assert v.source.line == 2
    assert [s.layer.kind for s in v.overridden] == ["inventory_group"]
    assert v.overridden[0].value == "pool.ntp.org"


def test_child_group_beats_parent(inv):
    v = src(inv, "web1", "timezone")
    assert v.value == "America/New_York"
    assert v.source.path == "inventory/group_vars/prod.yml"


def test_group_vars_directory_files_load_in_order(inv):
    v = src(inv, "web1", "tier")
    assert v.value == "web-extra"
    assert v.source.path == "inventory/group_vars/web/20-extra.yml"
    assert [s.value for s in v.overridden] == ["frontend", "web"]


def test_playbook_group_vars_beat_inventory_group_vars(inv):
    v = src(inv, "web2", "packages")
    assert v.value == ["nginx", "certbot"]
    assert v.source.path == "group_vars/web.yml"
    assert v.source.scope == "playbook"


def test_group_priority_from_inventory_source_breaks_ties(inv):
    v = src(inv, "web2", "shared")
    assert v.value == "from-db"
    assert v.source.path == "inventory/group_vars/db.yml"


def test_host_vars_file_beats_host_line(inv):
    v = src(inv, "web1", "http_port")
    assert v.value == 9090
    assert v.source.path == "inventory/host_vars/web1.yml"
    assert [s.value for s in v.overridden] == [80, 8080]
    host_line = v.overridden[-1].layer
    assert host_line.kind == "inventory_host" and host_line.line == 2


def test_vaulted_value_is_never_exposed(inv):
    v = src(inv, "web1", "db_password")
    assert v.value is VAULTED
    assert v.source.path == "inventory/host_vars/web1.yml"


def test_playbook_host_vars(inv):
    v = src(inv, "db1", "role")
    assert v.source.path == "host_vars/db1.yml"


def test_groups_and_tree(inv):
    prod = inv.groups["prod"]
    assert prod.children == ["db", "web"]
    assert prod.all_hosts == ["db1", "web1", "web2"]
    assert inv.groups["db"].priority == 10
    assert inv.hosts["web2"].groups == ["db", "web"]
    assert [g.name for _, g in inv.tree()][:3] == ["all", "prod", "db"]


def test_group_own_vars(inv):
    web = {v.name: v for v in inv.groups["web"].vars}
    assert web["tier"].value == "web-extra"
    assert web["packages"].source.scope == "playbook"


def test_cli_error_is_reported():
    with pytest.raises(InventoryError) as e:
        service.load(CliOptions(project_dir=FIXTURE, sources=["inventory/hosts.ini"], binary="ansible-inventory-missing"))
    assert "not installed" in str(e.value)


def test_unsafe_value_is_unwrapped(inv):
    v = src(inv, "db1", "deploy_hook")
    assert v.value == "echo {{ not_a_template }}"
    assert v.mismatch is None


def test_edit_links_are_web_links_only():
    from ansible_gui.inventory.attribution import edit_url
    from ansible_gui.inventory.model import Layer
    layer = Layer(kind="unknown", path="host_vars/a.yml", line=3)
    assert edit_url("https://git.lab/x/{path}#L{line}", layer) == "https://git.lab/x/host_vars/a.yml#L3"
    assert edit_url("javascript:alert(1)//{path}", layer) is None
    assert edit_url("JavaScript:alert(1)", layer) is None


def vaulted_file_project(root: Path) -> Path:
    """The fixture, plus a whole-file vaulted inventory/host_vars/db1.yml: one variable nothing else sets, one
    that overrides a group_vars file, and one that a playbook host_vars file overrides in turn."""
    project = root / "homelab"
    shutil.copytree(FIXTURE, project)
    secret = project / "inventory" / "host_vars" / "db1.yml"
    secret.write_text('api_token: "S3CRET-ONLY-IN-VAULT"\nntp_server: "S3CRET-OVER-GROUP-VARS"\nrole: replica\n')
    subprocess.run(["ansible-vault", "encrypt", "--vault-password-file", str(project / ".vault-pass"), str(secret)],
                   check=True, capture_output=True)
    assert secret.read_text().startswith("$ANSIBLE_VAULT")
    return project


def test_values_from_a_whole_file_vault_are_never_exposed(tmp_path):
    """With a vault password file, ansible-inventory decrypts whole-file vaulted vars files: what it reports for
    their variables is the secret. Those values are shown as vaulted, like an inline !vault one."""
    project = vaulted_file_project(tmp_path)
    inv = service.load(CliOptions(project_dir=project, sources=["inventory/hosts.ini"],
                                  vault_password_file=project / ".vault-pass"))
    db1 = inv.hosts["db1"]
    token, ntp, role = db1.var("api_token"), db1.var("ntp_server"), db1.var("role")
    assert token.value is VAULTED and token.source.kind == "unknown"
    assert token.mismatch == "Probably set in an encrypted file (inventory/host_vars/db1.yml), so its value isn’t shown."
    # A readable file sets it too, but the encrypted one outranks that file: Ansible's value is the secret.
    assert ntp.value is VAULTED and ntp.source.path == "inventory/group_vars/all.yml"
    assert "an encrypted file that outranks it (inventory/host_vars/db1.yml)" in ntp.mismatch
    # The playbook's host_vars outrank the encrypted file, and Ansible agrees with them: nothing to hide.
    assert role.value == "primary" and role.mismatch is None and role.source.path == "host_vars/db1.yml"
    assert "S3CRET" not in repr(inv.hosts)
    assert inv.hosts["web1"].mismatches == []

