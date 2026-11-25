import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import create_app, present
from ansible_gui.inventory.model import VAULTED, Layer, ResolvedVar, by_source

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
    assert "inventory/host_vars/web1.yml" in html
    assert "https://git.example/repo/src/branch/main/inventory/host_vars/web1.yml#L1" in html
    assert "overrides 2" in html
    assert "hunter2" not in html and "ANSIBLE_VAULT" not in html
    assert "vaulted" in html


def test_by_source_lists_each_source_once_the_one_that_wins_over_the_others_first():
    """The By file view: a variable sits under the source that set it, and the sources come in Ansible's
    precedence order, highest first, so what you would edit to change a host is at the top."""
    def var(name, layer, line=None):
        return ResolvedVar(name, 1, Layer(layer.kind, layer.path, layer.group, layer.host, layer.scope, line))

    inv = "/srv/repo/inventory"
    in_all = Layer("group_vars", f"{inv}/group_vars/all.yml", group="all")
    web_10 = Layer("group_vars", f"{inv}/group_vars/web/10-main.yml", group="web")
    web_20 = Layer("group_vars", f"{inv}/group_vars/web/20-extra.yml", group="web")
    prod = Layer("group_vars", f"{inv}/group_vars/prod.yml", group="prod")
    web_play = Layer("group_vars", "/srv/repo/group_vars/web.yml", group="web", scope="playbook")
    group_line = Layer("inventory_group", f"{inv}/hosts.ini", group="web")
    host_line = Layer("inventory_host", f"{inv}/hosts.ini", host="web1")
    host_vars = Layer("host_vars", f"{inv}/host_vars/web1.yml", host="web1")
    found = by_source(
        [var("a", in_all, 2), var("b", web_10, 2), var("c", web_20, 1), var("d", prod, 3), var("e", web_play, 1),
         var("f", group_line, 14), var("g", host_line, 2), var("h", host_vars, 1), var("i", host_vars, 2),
         var("j", Layer("unknown", None))],
        ["all", "prod", "web"])
    assert [(g.label, [v.name for v in g.vars]) for g in found] == [
        ("inventory/host_vars/web1.yml", ["h", "i"]),
        ("inventory/hosts.ini · host line", ["g"]),
        ("group_vars/web.yml", ["e"]),           # next to the playbooks: over every inventory group_vars file
        ("inventory/group_vars/web/20-extra.yml", ["c"]),  # a later file of the same group wins
        ("inventory/group_vars/web/10-main.yml", ["b"]),
        ("inventory/group_vars/prod.yml", ["d"]),          # web is the later group, so it outranks prod
        ("inventory/group_vars/all.yml", ["a"]),
        ("inventory/hosts.ini · [web]", ["f"]),
        ("outside the repo", ["j"]),             # nothing here sets it: last, whatever it would rank
    ]
    assert [g.key for g in found] == [f"s{n}" for n in range(1, 10)]
    assert found[0].layer.path == f"{inv}/host_vars/web1.yml" and found[0].layer.line is None  # the header: whole path
    assert by_source([], ["all"]) == []


@needs_ansible
def test_host_variables_are_grouped_under_the_file_that_set_them():
    c = client(edit_url="https://git.example/repo/src/branch/main/{path}#L{line}")
    html = c.get("/inventory/hosts/web1").text
    table = html.split('<table class="vars vars-by-file">')[1].split("</table>")[0]
    heads = [h.split("</tr>")[0] for h in table.split('<tr class="vars-group">')[1:]]
    named = [h.split('class="source source-win mono"')[1].split(">")[1].split("<")[0] for h in heads]
    assert named == ["inventory/host_vars/web1.yml", "inventory/hosts.ini", "group_vars/web.yml",
                     "inventory/group_vars/web/20-extra.yml", "inventory/group_vars/web/10-main.yml",
                     "inventory/group_vars/prod.yml", "inventory/group_vars/all.yml"]
    assert "host_vars" in heads[0] and "2 variables" in heads[0]
    assert "https://git.example/repo/src/branch/main/inventory/host_vars/web1.yml#L1" in heads[0]  # the file itself
    # A row says its line, not the file again; the file is the heading it sits under.
    first = table.split('<tbody data-group="s1">')[1].split("</tbody>")[0]
    port = first.split('<tr data-var="http_port"')[1].split("</tr>")[0]
    assert 'data-source="s1"' in port and 'data-label="Line"' in port and "web1.yml#L1" in port
    assert "web1.yml" not in port.split('data-label="Line"')[1].split("<ol")[0]
    assert "overrides 2" in port  # what it overrode still unfolds from the row
    # The filter reads the value and the file as well as the name.
    assert 'data-search="http_port 9090 inventory/host_vars/web1.yml:1 host_vars"' in port
    assert 'placeholder="Filter by name, value or file"' in html
    # One chip per source, all shown to begin with, and the way to the other order.
    chips = html.split('class="source-chips"')[1].split("</div>")[0]
    assert chips.count("data-source-chip=") == 7 and 'data-source-all aria-pressed="true"' in chips
    assert ">inventory/group_vars/prod.yml <span class=\"chip-count\">3</span>" in chips
    assert '<a href="?by=file#vars-1" aria-current="page">By file</a>' in html and '<a href="?by=name#vars-1">A–Z</a>' in html
    assert "hunter2" not in html and "ANSIBLE_VAULT" not in html  # a vaulted value is in no attribute either


@needs_ansible
def test_host_variables_a_to_z_keep_the_file_on_every_row():
    html = client().get("/inventory/hosts/web1", params={"by": "name"}).text
    table = html.split('<table class="vars">')[1].split("</table>")[0]
    assert "vars-group" not in table and '<a href="?by=name#vars-1" aria-current="page">A–Z</a>' in html
    names = [r.split('"')[0] for r in table.split('<tr data-var="')[1:]]
    assert names == sorted(names) and len(names) == 10
    port = table.split('<tr data-var="http_port"')[1].split("</tr>")[0]
    assert "inventory/host_vars/web1.yml:1" in port and 'data-source="s1"' in port  # the chips still narrow it
    assert html.split('class="source-chips"')[1].split("</div>")[0].count("data-source-chip=") == 7


@needs_ansible
def test_a_group_whose_variables_come_from_one_file_has_nothing_to_pick():
    html = client().get("/inventory/groups/prod").text
    table = html.split('<table class="vars vars-by-file">')[1].split("</table>")[0]
    assert table.count('<tr class="vars-group">') == 1 and "inventory/group_vars/prod.yml" in table
    assert "source-chips" not in html and "?by=name" not in html  # one source: no chips, no other order
    web = client().get("/inventory/groups/web").text
    assert web.split('class="source-chips"')[1].split("</div>")[0].count("data-source-chip=") == 4


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


def test_hosts_and_groups_are_full_width_lists_and_detail_pages_have_breadcrumbs():
    c = client()
    page = c.get("/inventory/groups").text
    assert 'href="/inventory/groups" aria-current="page"' in page
    assert 'data-list="groups"' in page and 'data-list="hosts"' not in page and 'class="sidebar"' not in page
    page = c.get("/inventory/hosts").text
    assert 'href="/inventory/hosts" aria-current="page"' in page and 'data-list="hosts"' in page
    assert '<a class="mono" href="/inventory/hosts/web1">web1</a>' in page
    detail = c.get("/inventory/hosts/web1").text
    assert '<nav class="crumbs" aria-label="Breadcrumb">' in detail and 'href="/inventory/hosts">Hosts</a>' in detail
    assert 'data-list=' not in detail and 'class="sidebar"' not in detail
    assert 'rel="next"' in detail or 'rel="prev"' in detail
    group = c.get("/inventory/groups/web").text
    assert 'href="/inventory/groups">Groups</a>' in group and '<span class="mono" aria-current="page">web</span>' in group
    assert "data-rail-toggle" in page


@needs_ansible
def test_whole_file_vaulted_values_never_reach_a_page(tmp_path):
    from test_attribution import vaulted_file_project
    project = vaulted_file_project(tmp_path)
    c = TestClient(create_app(Settings(project_dir=project, inventory=["inventory/hosts.ini"], data_dir=tmp_path / "data",
                                       vault_password_file=project / ".vault-pass")))
    page = c.get("/inventory/hosts/db1").text
    assert "S3CRET" not in page and "so its value isn’t shown" in page
    assert page.count('<span class="badge badge-neutral">vaulted</span>') == 2
    # The launcher notes what the inventory already sets on the targeted hosts: not these either.
    assert "S3CRET" not in c.get("/runs/new", params={"playbook": "site.yml", "target": "db1"}).text
    assert c.get("/openapi.json").status_code == 404

