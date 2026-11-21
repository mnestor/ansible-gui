"""The worker boundary: what a job carries, and what a worker refuses."""

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

from ansible_gui import worker as workers
from ansible_gui.credentials import ssh
from ansible_gui.inventory import cli as invcli
from ansible_gui.scratch import Scratch
from ansible_gui.worker import InlineWorker, jobs


class Recorder:
    """A worker that keeps the jobs it is sent and answers like a command that printed {}."""

    def __init__(self):
        self.jobs = []

    def call(self, kind, job, on_message=None, cancel=None):
        self.jobs.append((kind, job))
        return {"rc": 0, "stdout": "{}", "stderr": ""}


def test_a_job_carries_only_its_own_environment_and_the_vault_password_as_text(tmp_path, monkeypatch):
    """The app's environment holds its secrets (SMTP, OIDC): a job must never copy it to a worker."""
    monkeypatch.setenv("ANSIBLE_GUI_SMTP_PASSWORD", "do-not-send")
    vault = tmp_path / "vault"
    vault.write_text("vault-pw\n")
    rec = Recorder()
    opts = invcli.CliOptions(project_dir=tmp_path, sources=["hosts.yml"], vault_password_file=vault,
                             env={"ANSIBLE_COLLECTIONS_PATH": "/c"}, worker=rec)
    invcli.resolved(opts)
    [(kind, job)] = rec.jobs
    assert kind == "cli" and "ANSIBLE_GUI_SMTP_PASSWORD" not in job["env"] and "PATH" not in job["env"]
    assert job["env"]["ANSIBLE_COLLECTIONS_PATH"] == "/c"
    assert job["files"] == {"vault-pass": "vault-pw\n"}
    assert job["argv"][job["argv"].index("--vault-password-file") + 1] == workers.VAULT
    assert str(vault) not in " ".join(job["argv"])


def test_an_executable_vault_password_file_is_run_like_ansible_runs_it(tmp_path):
    script = tmp_path / "vault.sh"
    script.write_text("#!/bin/sh\necho from-script\n")
    script.chmod(0o700)
    assert workers.vault_files(script) == ({"vault-pass": "from-script\n"}, ["--vault-password-file", workers.VAULT])


def test_a_cli_job_gets_its_files_in_a_temp_directory_wiped_afterwards(tmp_path):
    w = InlineWorker(Scratch(tmp_path / "data"))
    res = w.call("cli", {"argv": ["sh", "-c", 'cat "$1"; echo "$2"', "sh", f"{workers.SCRATCH}/f", workers.SCRATCH],
                         "cwd": str(tmp_path), "env": {}, "timeout": 10, "files": {"f": "hello\n"}})
    assert res["rc"] == 0 and res["stdout"].startswith("hello\n")
    d = Path(res["stdout"].splitlines()[1])
    assert d.name.startswith("agui-cli-") and not d.exists()
    assert Scratch(tmp_path / "data").leftovers() == []


def test_a_worker_with_roots_refuses_paths_outside_them(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    w = InlineWorker(Scratch(tmp_path / "data"), roots=[repos], tools={"ansible-inventory"})
    job = {"argv": ["ansible-inventory", "--list"], "env": {}, "timeout": 10, "files": {}}
    assert "outside the directories" in w.call("cli", {**job, "cwd": str(tmp_path)})["error"]
    assert "without .." in w.call("cli", {**job, "cwd": str(repos / ".." / "data")})["error"]
    assert "doesn’t run sh" in w.call("cli", {**job, "argv": ["sh", "-c", "id"], "cwd": str(repos)})["error"]
    assert "outside the directories" in w.call("remove", {"home": "/etc"})["error"]
    assert "outside the directories" in w.call("fetch", {"home": str(tmp_path / "x"), "url": "", "branch": "",
                                                          "ssh": {}})["error"]
    assert w.call("nonsense", {})["error"] == "this worker doesn’t take nonsense jobs"


def test_a_file_name_in_a_job_cant_leave_the_temp_directory(tmp_path):
    w = InlineWorker(Scratch(tmp_path / "data"))
    res = w.call("cli", {"argv": ["true"], "cwd": str(tmp_path), "env": {}, "timeout": 10,
                         "files": {"../escape": "x"}})
    assert "not a file name" in res["error"] and not (tmp_path / "escape").exists()


def public_key(tmp_path: Path, name: str) -> str:
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return " ".join(key.with_suffix(".pub").read_text().split()[:2])


def test_host_keys_a_job_learned_are_added_once_and_checked(tmp_path):
    line = "newhost " + public_key(tmp_path, "k")
    assert ssh.learn_host_keys(tmp_path, [line, line, "not a key line", "evil ssh-ed25519 AAAA\nsecond"]) == 1
    assert ssh.learn_host_keys(tmp_path, [line]) == 0
    assert ssh.read_known_hosts(tmp_path) == line + "\n"


def test_learned_host_keys_are_held_to_what_ssh_adds_for_a_host_it_just_met(tmp_path):
    """The lines come from a directory the playbook can write. A @cert-authority line there would have every
    later run and pull trust whoever holds that key, for any host; a second key for a known host, likewise for
    that host."""
    mine, theirs = public_key(tmp_path, "mine"), public_key(tmp_path, "theirs")
    ssh.save_known_hosts(tmp_path, f"git.example.test {mine}\n@cert-authority *.lan {mine}\n")
    before = ssh.read_known_hosts(tmp_path)
    planted = [f"@cert-authority * {theirs}", f"@revoked git.example.test {mine}", f"* {theirs}",
               f"host?.example.test {theirs}", f"!a.example.test,b.example.test {theirs}",
               f"git.example.test {theirs}", f"new.example.test,git.example.test {theirs}",
               f"|1|c2FsdHNhbHRzYWx0c2FsdHNhbHQ=|aGFzaGhhc2hoYXNoaGFzaGhhc2g= {theirs}",
               f"-oProxyCommand=id {theirs}", "a b c", f"only.example.test {theirs.split()[0]}"]
    assert ssh.learn_host_keys(tmp_path, planted) == 0
    assert ssh.read_known_hosts(tmp_path) == before

    # What ssh writes: a name or [name]:port, and one key. A host covered only by a certificate authority has
    # no key of its own yet, so its key is kept. A comment after the key isn't.
    met = [f"nas.lan {theirs}", f"[10.0.0.5]:2222 {theirs} planted comment", f"nas.lan {mine}"]
    assert ssh.learn_host_keys(tmp_path, met) == 2
    assert ssh.read_known_hosts(tmp_path) == before + f"nas.lan {theirs}\n[10.0.0.5]:2222 {theirs}\n"

    # StrictHostKeyChecking yes: ssh adds nothing itself, so nothing is taken from a job.
    assert ssh.learn_host_keys(tmp_path, [f"another.example.test {theirs}"], "yes") == 0
    assert "another.example.test" not in ssh.read_known_hosts(tmp_path)


def test_ansible_galaxy_gets_the_galaxy_servers_of_a_repositorys_ansible_cfg_and_nothing_else(tmp_path):
    (tmp_path / "ansible.cfg").write_text(
        "[defaults]\nlog_path = /repos/1/commits/x/ansible.cfg\nhome = /repos/1\nlocal_tmp = /repos/2\n\n"
        "[galaxy]\nserver_list = hub\ncache_dir = /repos/3\ntoken_path = /repos/4\nignore_certs = true\n\n"
        "[galaxy_server.hub]\nurl = https://hub.example.test/api/\ntoken = abc\nlog_path = /repos/5\n")
    ours = configparser.ConfigParser()
    ours.read_string(jobs.galaxy_config(tmp_path))
    assert {name: dict(ours[name]) for name in ours.sections()} == {
        "galaxy": {"server_list": "hub", "ignore_certs": "true"},
        "galaxy_server.hub": {"url": "https://hub.example.test/api/", "token": "abc"}}

    # A value that runs on over indented lines stays one value: it can't start a section of its own.
    (tmp_path / "ansible.cfg").write_text("[galaxy]\nserver = https://a.example.test\n  [defaults]\n  log_path = /x\n")
    ours = configparser.ConfigParser()
    ours.read_string(jobs.galaxy_config(tmp_path))
    assert ours.sections() == ["galaxy"] and "log_path" not in ours["galaxy"]

    none = configparser.ConfigParser()
    none.read_string(jobs.galaxy_config(tmp_path / "no-such-directory"))
    assert none.sections() == []
    (tmp_path / "ansible.cfg").write_text("not an ini file\n")
    with pytest.raises(jobs.Refused, match="ansible.cfg can’t be read"):
        jobs.galaxy_config(tmp_path)


@pytest.mark.skipif(shutil.which("ansible-galaxy") is None, reason="needs ansible-core")
def test_a_repositorys_ansible_cfg_cant_make_the_fetcher_write_into_another_repository(tmp_path):
    """The fetcher writes every repository as one user. With the repository's ansible.cfg as it is, log_path
    there appended ansible-galaxy's log, with a line of the requirement's own choosing in it, to another
    repository's ansible.cfg: a setting Ansible then obeyed in that repository's runs."""
    repos = tmp_path / "repos"
    other = repos / "1" / "commits" / ("a" * 40)
    other.mkdir(parents=True)
    (other / "ansible.cfg").write_text("[defaults]\nroles_path = roles\n\n[ssh_connection]\npipelining = True\n")
    before = (other / "ansible.cfg").read_text()
    hostile = repos / "2" / "commits" / ("b" * 40)
    hostile.mkdir(parents=True)
    (hostile / "ansible.cfg").write_text(
        f"[defaults]\nlog_path = {other}/ansible.cfg\nhome = {repos}/planted-home\n"
        f"local_tmp = {repos}/planted-tmp\n\n[galaxy]\ncache_dir = {repos}/planted-cache\n")
    (hostile / "requirements.yml").write_text(
        'collections:\n  - name: "a = b\\nssh_executable = %s/pwn.sh\\n#"\n' % hostile)
    w = InlineWorker(Scratch(tmp_path / "data"), roots=[repos])
    res = w.call("collections", {"requirements": str(hostile / "requirements.yml"), "run_dir": str(hostile),
                                 "target": str(repos / "collections" / ("c" * 64))})
    assert "error" in res  # the requirement names no collection: the install fails, as it did before
    assert (other / "ansible.cfg").read_text() == before
    assert sorted(p.name for p in repos.iterdir()) == ["1", "2", "collections"]
    assert not list((repos / "collections").iterdir()) and Scratch(tmp_path / "data").leftovers() == []


def test_a_jobs_ssh_config_includes_the_configured_file_as_text(tmp_path):
    mine = tmp_path / "config"
    mine.write_text("Host jump\n  User me\n")
    job = ssh.for_job(tmp_path, mine, "yes")
    assert job == {"config": "Host jump\n  User me\n", "include": None, "known_hosts": "", "checking": "yes"}
    missing = ssh.for_job(tmp_path, tmp_path / "nope", "accept-new")
    assert missing["config"] is None and missing["include"] == str(tmp_path / "nope")
