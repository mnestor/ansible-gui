"""The worker boundary: what a job carries, and what a worker refuses."""

import subprocess
from pathlib import Path

from ansible_gui import worker as workers
from ansible_gui.credentials import ssh
from ansible_gui.inventory import cli as invcli
from ansible_gui.scratch import Scratch
from ansible_gui.worker import InlineWorker


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


def test_host_keys_a_job_learned_are_added_once_and_checked(tmp_path):
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    line = "newhost " + " ".join(key.with_suffix(".pub").read_text().split()[:2])
    assert ssh.learn_host_keys(tmp_path, [line, line, "not a key line", "evil ssh-ed25519 AAAA\nsecond"]) == 1
    assert ssh.learn_host_keys(tmp_path, [line]) == 0
    assert ssh.read_known_hosts(tmp_path) == line + "\n"


def test_a_jobs_ssh_config_includes_the_configured_file_as_text(tmp_path):
    mine = tmp_path / "config"
    mine.write_text("Host jump\n  User me\n")
    job = ssh.for_job(tmp_path, mine, "yes")
    assert job == {"config": "Host jump\n  User me\n", "include": None, "known_hosts": "", "checking": "yes"}
    missing = ssh.for_job(tmp_path, tmp_path / "nope", "accept-new")
    assert missing["config"] is None and missing["include"] == str(tmp_path / "nope")
