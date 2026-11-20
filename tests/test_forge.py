from ansible_gui.repos import forge

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_known_hosts_are_derived_from_any_clone_address():
    for url in ("git@github.com:me/ansible.git", "https://github.com/me/ansible", "ssh://git@github.com/me/ansible.git"):
        assert forge.file_template(url, "", "main") == "https://github.com/me/ansible/blob/main/{path}#L{line}"
    assert forge.file_template("https://gitlab.com/g/sub/repo.git", "", "dev", SHA) == \
        f"https://gitlab.com/g/sub/repo/-/blob/{SHA}/{{path}}#L{{line}}"
    assert forge.file_template("git@codeberg.org:me/r.git", "", "main", SHA) == \
        f"https://codeberg.org/me/r/src/commit/{SHA}/{{path}}#L{{line}}"
    assert forge.commit_url("git@bitbucket.org:me/r.git", "", SHA) == f"https://bitbucket.org/me/r/commits/{SHA}"


def test_a_self_hosted_server_needs_its_kind_and_local_paths_get_no_link():
    url = "ssh://git@git.lab.example:2222/me/ansible.git"
    assert forge.file_template(url, "", "main") is None
    assert forge.file_template(url, "gitea", "main") == "https://git.lab.example/me/ansible/src/branch/main/{path}#L{line}"
    assert forge.file_template(url, "none", "main") is None
    assert forge.file_template("/srv/ansible", "github", "main") is None
    assert forge.commit_url("https://github.com/me/r", "", "not-a-sha") is None
