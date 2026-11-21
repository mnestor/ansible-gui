"""With workers, the server reads a repository's files only where the repositories are."""

import pytest

from ansible_gui import repofiles
from ansible_gui.inventory import varfiles
from ansible_gui.runs import playbooks

PLAY = "- hosts: all\n  tasks: []\n"


@pytest.fixture
def repo(tmp_path):
    """A commit directory with links in it: one to a file beside it, the rest to the server's own files."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "secret.yml").write_text("smtp_password: hunter2\n")
    (data / "play.yml").write_text(PLAY)
    repo = tmp_path / "repos" / "1" / "commits" / ("a" * 40)
    (repo / "group_vars").mkdir(parents=True)
    (repo / "site.yml").write_text(PLAY)
    (repo / "inside.yml").symlink_to("site.yml")
    (repo / "linked.yml").symlink_to(data / "play.yml")
    (repo / "group_vars" / "all.yml").symlink_to(data / "secret.yml")
    (repo / "host_vars").symlink_to(data)
    (repo / "loop.yml").symlink_to("loop.yml")
    return repo


def test_without_workers_links_are_followed_as_ansible_follows_them(repo):
    """The repository's code runs as the server then, so there is nothing a link reaches that it can't."""
    assert playbooks.discover(repo) == ["inside.yml", "linked.yml", "site.yml"]
    assert varfiles.load(repo / "group_vars" / "all.yml").values == {"smtp_password": "hunter2"}


def test_with_workers_a_link_out_of_the_repositories_is_not_read(repo, tmp_path):
    repofiles.confine([tmp_path / "repos"])
    assert playbooks.discover(repo) == ["inside.yml", "site.yml"]
    assert playbooks.discover(repo, "host_vars") == []  # the playbook directory itself is a link out
    all_vars = varfiles.load(repo / "group_vars" / "all.yml")
    assert all_vars.values == {} and all_vars.error == "could not read: it is a link to somewhere outside the repositories"
    # A directory that is a link out isn't listed either: the one entry says why.
    found = varfiles.find(repo, "host_vars", "secret") + varfiles.find(repo, ".", "host_vars")
    assert found and all(varfiles.load(f).error and not varfiles.load(f).values for f in found)
    with pytest.raises(PermissionError):
        repofiles.read_bytes(repo / "linked.yml")
    with pytest.raises(PermissionError):
        repofiles.read_text(repo / "loop.yml")
    assert repofiles.read_text(repo / "inside.yml") == PLAY
