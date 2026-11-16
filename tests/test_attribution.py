import shutil
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
