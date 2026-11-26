"""Git repositories: clone on save, pull before each run, cancel and email when a pull fails."""

import os
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings, SmtpSettings
from ansible_gui.web import create_app

from test_notify import FakeSMTP, fake_smtp  # noqa: F401 - fixture

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def make_remote(tmp_path: Path, name: str, files: dict[str, Path | str]) -> tuple[Path, Path]:
    bare, work = tmp_path / f"{name}.git", tmp_path / f"{name}-work"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("clone", "-q", str(bare), str(work), cwd=tmp_path)
    for rel, src in files.items():
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, Path):
            shutil.copy(src, dest)
        else:
            dest.write_text(src)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "first", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    return bare, work


@pytest.fixture
def remotes(tmp_path):
    inv, inv_work = make_remote(tmp_path, "inv", {"inventory/hosts.yml": FIXTURE / "inventory" / "hosts.yml"})
    pb, pb_work = make_remote(tmp_path, "pb", {"site.yml": FIXTURE / "site.yml"})
    other, _ = make_remote(tmp_path, "other", {"plays/site.yml": FIXTURE / "site.yml"})
    return {"inv": inv, "pb": pb, "pb_work": pb_work, "other": other, "inv_work": inv_work}


@pytest.fixture
def app(tmp_path):
    smtp = SmtpSettings(host="mail.test", to=["ops@example.test"], sender="agui@example.test")
    # No ANSIBLE_GUI_PROJECT_DIR: everything comes from git.
    return create_app(Settings(data_dir=tmp_path / "data", secret_key="s" * 40, smtp=smtp,
                               public_url="https://ansible.example.test"))


@pytest.fixture
def c(app):
    return TestClient(app, base_url="https://ansible.example.test")


def add(c, **form):
    return c.post("/repos", data={"branch": "main", **form}, follow_redirects=False)


def wait(app, run_id):
    for _ in range(300):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.1)
    raise AssertionError("run didn't finish")


def setup_repos(c, remotes):
    assert add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    assert add(c, name="lab", role="playbooks", url=f"file://{remotes['pb']}").status_code == 303
    assert add(c, name="extra", role="playbooks", url=f"file://{remotes['other']}", paths="plays").status_code == 303


def test_nothing_set_up_says_so(c):
    assert "No inventory is set up" in c.get("/inventory").text
    assert "None yet" in c.get("/repos").text


def test_paths_stay_inside(c, remotes):
    r = add(c, name="bad", role="playbooks", url=f"file://{remotes['pb']}", paths="../etc")
    assert r.status_code == 400 and "inside the repository" in r.text


SECOND_INV = """all:
  children:
    lab:
      hosts:
        box1:
          ansible_connection: local
          ansible_python_interpreter: "{{ ansible_playbook_python }}"
          marker: from-second
    vault_only:
      hosts:
        box9:
          ansible_connection: local
"""


def test_several_inventories_list_merged_and_run_against_the_one_picked(app, c, remotes, tmp_path):
    second, _ = make_remote(tmp_path, "inv2", {"hosts.yml": SECOND_INV})
    play, _ = make_remote(tmp_path, "mark", {"mark.yml": "- hosts: lab\n  gather_facts: false\n  tasks:\n"
                                                         "    - ansible.builtin.debug:\n"
                                                         "        msg: \"marker={{ marker | default('none') }}\"\n"})
    assert add(c, name="first", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    assert add(c, name="second", role="inventory", url=f"file://{second}", paths="hosts.yml").status_code == 303
    assert add(c, name="mark", role="playbooks", url=f"file://{play}").status_code == 303
    first, second_repo = sorted((r for r in app.state.repos.all() if r.role == "inventory"), key=lambda r: r.id)
    pb = next(r for r in app.state.repos.all() if r.name == "mark")

    hosts = c.get("/inventory").text
    # one merged list: box1 once (flagged as in 2), box2 from first, box9 from second
    assert hosts.count('href="/inventory/hosts/box1"') >= 1 and "in 2" in hosts
    assert "box2" in hosts and "box9" in hosts and "In more than one inventory" in hosts
    page = c.get("/inventory/hosts/box1").text
    assert "is in 2 inventories" in page and "From first" in page and "From second" in page
    assert "from-second" in page
    assert f"inventory={first.id}" in page and f"inventory={second_repo.id}" in page
    # a group in both is shown per inventory, without a warning
    group = c.get("/inventory/groups/lab").text
    assert "From first" in group and "From second" in group and "is in 2 inventories" not in group

    # with two inventories, a run must say which
    r = c.post("/runs", data={"playbook": f"{pb.id}:mark.yml", "target": "box1", "mode": "check",
                              "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
    assert r.status_code == 400 and "Choose the inventory" in r.text
    launcher = c.get(f"/runs/new?inventory={second_repo.id}").text
    assert 'name="inventory" value="%d" checked' % second_repo.id in launcher

    for inv, want in ((second_repo, "marker=from-second"), (first, "marker=none")):
        r = c.post("/runs", data={"playbook": f"{pb.id}:mark.yml", "target": "box1", "mode": "check", "verbosity": "0",
                                  "inventory": str(inv.id), "expected": ["box1"]}, follow_redirects=False)
        assert r.status_code == 303, r.text[-400:]
        run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
        log = app.state.store.log_path(run.id).read_text()
        assert run.status == "successful" and run.inventory_id == inv.id and want in log, log[-800:]
        assert f"inventory={inv.id}" in c.get(f"/runs/{run.id}").text  # Run again keeps the inventory
    # results are per inventory: the host page shows each inventory's own last run
    results = app.state.store.last_status_by_host()
    assert (first.id, "box1") in results and (second_repo.id, "box1") in results

    # schedules need an inventory too, and keep it
    data = {"name": "n", "cron": "0 3 * * *", "playbook": f"{pb.id}:mark.yml", "target": "box1", "mode": "check",
            "diff": "1", "verbosity": "0", "notify": "never"}
    r = c.post("/schedules", data=data, follow_redirects=False)
    assert r.status_code == 400 and "Choose the inventory" in r.text
    assert c.post("/schedules", data={**data, "inventory": str(second_repo.id)}, follow_redirects=False).status_code == 303
    sched = app.state.schedules.all()[-1]
    assert sched.inventory_id == second_repo.id
    run = wait(app, app.state.scheduler.fire(sched).id)
    assert run.inventory_id == second_repo.id and "marker=from-second" in app.state.store.log_path(run.id).read_text()


def test_save_clones_and_launcher_lists_playbooks_by_repo(app, c, remotes):
    setup_repos(c, remotes)
    page = c.get("/repos").text
    assert "not cloned yet" not in page and "Last pull failed" not in page
    assert "box1" in c.get("/inventory").text
    launcher = c.get("/runs/new").text
    assert "lab /" in launcher and "extra /" in launcher and "plays/site.yml" in launcher


def test_run_pulls_latest_commit_first(app, c, remotes):
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    first = lab.last_commit
    work = remotes["pb_work"]
    (work / "site.yml").write_text((work / "site.yml").read_text() + "\n# newer\n")
    git("commit", "-qam", "newer", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    key = f"{lab.id}:site.yml"
    r = c.post("/runs", data={"playbook": key, "target": "box1", "mode": "check", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    assert r.status_code == 303
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "successful", run.error
    newer = app.state.repos.get(lab.id).last_commit
    assert newer != first and run.commit_sha == newer and run.inventory_commit
    assert "Pulling lab" in app.state.store.log_path(run.id).read_text()


def test_failed_pull_cancels_the_run_and_emails(app, c, remotes):
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    shutil.rmtree(remotes["pb"])  # the git server is gone
    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "apply",
                              "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error" and "couldn’t update lab from git" in run.error and "Nothing ran" in run.error
    assert run.host_stats == {}
    app.state.executor.wait_idle()
    app.state.notifier.wait_idle()
    assert FakeSMTP.sent and "lab" in FakeSMTP.sent[-1].get_content()  # emailed though started by hand
    assert "Last pull failed" in c.get("/repos").text
    assert "Can’t pull lab from git" in c.get("/").text


def test_changed_hosts_after_pull_cancel_a_hand_started_run(app, c, remotes):
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    work = remotes["inv_work"]  # box2 leaves the inventory after the preview
    hosts = (work / "inventory" / "hosts.yml").read_text()
    (work / "inventory" / "hosts.yml").write_text(hosts.split("        box2:")[0])
    git("commit", "-qam", "drop box2", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "", "mode": "apply", "verbosity": "0",
                              "expected": ["box1", "box2"]}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error" and "not the 2 you previewed" in run.error and run.host_stats == {}


def test_delete_removes_clone(app, c, remotes):
    setup_repos(c, remotes)
    extra = next(r for r in app.state.repos.all() if r.name == "extra")
    assert extra.path.is_dir()
    c.post(f"/repos/{extra.id}/delete")
    assert not extra.path.exists() and "extra /" not in c.get("/runs/new").text



def test_a_new_address_that_cant_be_pulled_leaves_the_repository_on_its_last_commit(app, c, remotes, tmp_path):
    """Saving another clone URL pulls from it at once. Until a pull from it works the repository stays on the
    commit it had: a mistyped address, or a key the new server refuses, must not take the inventory away."""
    setup_repos(c, remotes)
    hosts = next(r for r in app.state.repos.all() if r.name == "hosts")
    form = {"name": "hosts", "role": "inventory", "branch": "main", "paths": "inventory/hosts.yml"}
    r = c.post(f"/repos/{hosts.id}", data={**form, "url": f"file://{tmp_path / 'moved-away.git'}"},
               follow_redirects=False)
    assert r.status_code == 303
    after = app.state.repos.get(hosts.id)
    assert after.url.endswith("moved-away.git") and after.last_error
    assert after.last_commit == hosts.last_commit and after.path.is_dir()
    assert "box1" in c.get("/inventory").text
    page = c.get("/repos").text
    assert "Last pull failed" in page and f"the clone stays at {hosts.short}" in page
    # An address that can be pulled takes over, with another repository's history, whole.
    moved, _ = make_remote(tmp_path, "moved", {"inventory/hosts.yml":
                                               "all:\n  hosts:\n    elsewhere:\n      ansible_connection: local\n"})
    assert c.post(f"/repos/{hosts.id}", data={**form, "url": f"file://{moved}"},
                  follow_redirects=False).status_code == 303
    now = app.state.repos.get(hosts.id)
    assert now.last_commit and now.last_commit != hosts.last_commit and not now.last_error
    inventory = c.get("/inventory").text
    assert "elsewhere" in inventory and "box1" not in inventory
    assert not hosts.path.exists()  # the old address's commit went with the pull that replaced it


def fake_ssh(tmp_path: Path, *says: str) -> dict:
    """git's environment with an ssh that prints these lines and fails, as ssh does when it can't sign in."""
    from ansible_gui.repos import git as gitsync
    ssh = tmp_path / "fake-ssh"
    ssh.write_text("#!/bin/sh\n" + "".join(f"echo {shlex.quote(line)} >&2\n" for line in says) + "exit 255\n")
    ssh.chmod(0o755)
    return {**gitsync._base_env(), "GIT_SSH_COMMAND": str(ssh)}


@pytest.mark.parametrize("says, shown", [
    # ssh could not sign in: the reason is the line before git's advice, and has a plain explanation.
    (["Warning: Permanently added 'git.example.test' (ED25519) to the list of known hosts.",
      "git@git.example.test: Permission denied (publickey)."],
     "The git server refused the SSH key. Add its public key as a deploy key. "
     "(git: git@git.example.test: Permission denied (publickey).)"),
    # the server let the key in and refused the repository: its own words
    (["ERROR: Repository not found."], "ERROR: Repository not found."),
    # nothing but the advice
    ([], "The git server closed the connection without saying why: it refused the sign-in, or the repository "
         "isn’t there. (git: Could not read from remote repository.)"),
])
def test_a_refused_ssh_pull_shows_the_reason_not_the_end_of_gits_advice(tmp_path, says, shown):
    """When the connection fails git ends with "Please make sure you have the correct access rights / and the
    repository exists." Its last line alone, "and the repository exists.", was all the Repositories page said."""
    from ansible_gui.repos import git as gitsync
    with pytest.raises(gitsync.GitError) as e:
        gitsync._git(["ls-remote", "git@git.example.test:me/inventory.git"], tmp_path, fake_ssh(tmp_path, *says))
    assert str(e.value) == shown


def start_slow_run(app, c, remotes, tmp_path):
    """A run of slow.yml from its own repository, waited on until its 20 s pause is under way."""
    slow, slow_work = make_remote(tmp_path, "slow", {"slow.yml": FIXTURE / "slow.yml"})
    assert add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    assert add(c, name="lab", role="playbooks", url=f"file://{slow}").status_code == 303
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    r = c.post("/runs", data={"playbook": f"{lab.id}:slow.yml", "target": "box1", "mode": "apply", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    log = app.state.store.log_path(run_id)
    end = time.time() + 30
    while "TASK [Wait]" not in (log.read_text() if log.exists() else ""):
        assert time.time() < end, "the pause task never started"
        time.sleep(0.2)
    return lab, slow_work, run_id


def push_change(work: Path, rel: str, line: str) -> None:
    (work / rel).write_text((work / rel).read_text() + f"\n{line}\n")
    git("commit", "-qam", line, cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)


def test_a_pull_during_a_run_leaves_the_runs_files_alone(app, c, remotes, tmp_path):
    """Each commit has its own directory: a pull lands on a new one at once, and the run keeps reading its own."""
    lab, slow_work, run_id = start_slow_run(app, c, remotes, tmp_path)
    ran_from = app.state.repos.get(lab.id)
    assert ran_from.path == lab.home / "commits" / ran_from.last_commit and not (ran_from.path / ".git").exists()
    push_change(slow_work, "slow.yml", "# newer")

    r = c.post(f"/repos/{lab.id}/pull", follow_redirects=False)
    assert r.headers["location"] == f"/repos?pulled={lab.id}"
    now = app.state.repos.get(lab.id)
    assert now.last_commit != ran_from.last_commit and "# newer" in (now.path / "slow.yml").read_text()
    assert "# newer" not in (ran_from.path / "slow.yml").read_text()  # still there, unchanged, under the run

    r = c.post(f"/repos/{lab.id}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/repos?kept={lab.id}&busy_run={run_id}"
    assert "Didn’t delete lab: run" in c.get(r.headers["location"]).text
    assert app.state.repos.get(lab.id) and ran_from.path.is_dir()

    c.post(f"/runs/{run_id}/cancel")
    assert wait(app, run_id).status == "canceled"
    app.state.executor.wait_idle()
    assert app.state.store.get(run_id).commit_sha == ran_from.last_commit
    # Once the run is over its commit goes; the current one stays.
    assert not ran_from.path.exists() and now.path.is_dir()
    assert sorted(p.name for p in (lab.home / "commits").iterdir()) == [now.last_commit]


def test_running_the_same_commits_again(app, c, remotes):
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    hosts = next(r for r in app.state.repos.all() if r.name == "hosts")
    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    first = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert first.status == "successful" and len(first.commit_sha) == 40 and len(first.inventory_commit) == 40
    app.state.executor.wait_idle()
    push_change(remotes["pb_work"], "site.yml", "# newer")
    c.post(f"/repos/{lab.id}/pull")
    latest = app.state.repos.get(lab.id).last_commit
    assert latest != first.commit_sha and not (lab.home / "commits" / first.commit_sha).exists()  # pruned

    # Run again: the latest, and the launcher says what moved, offering the earlier commits.
    page = c.get(f"/runs/new?playbook={lab.id}:site.yml&target=box1&inventory={hosts.id}&from={first.id}").text
    assert f"This runs the latest commits: lab is at {latest[:10]}, run #{first.id} used {first.commit_sha[:10]}" in page
    assert f"&amp;same={first.id}" in page

    # Run the same commits: written out again from the bare clone, and nothing pulled.
    page = c.get(f"/runs/new?playbook={lab.id}:site.yml&target=box1&inventory={hosts.id}&same={first.id}").text
    assert f"Runs the commits run #{first.id} used: lab at {first.commit_sha[:10]}" in page and "Use the latest" in page
    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check", "verbosity": "0",
                              "inventory": str(hosts.id), "same": str(first.id), "expected": ["box1"]},
               follow_redirects=False)
    again = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(again.id).read_text()
    assert again.status == "successful", log
    assert again.same_as == first.id and again.commit_sha == first.commit_sha
    assert again.inventory_commit == first.inventory_commit
    assert "Pulling" not in log and f"Running the commits run #{first.id} used" in log
    assert f"as in run #{first.id}" in c.get(f"/runs/{again.id}").text
    assert app.state.repos.get(lab.id).last_commit == latest  # the current commit didn't move


def test_running_a_commit_gone_upstream_again_says_so(app, c, remotes, tmp_path):
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    hosts = next(r for r in app.state.repos.all() if r.name == "hosts")
    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    first = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    app.state.executor.wait_idle()
    # The branch is rewritten and the old commit is gone from the server and from the bare clone.
    work = remotes["pb_work"]
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=work)
    git("push", "-q", "--force", "origin", "HEAD:main", cwd=work)
    c.post(f"/repos/{lab.id}/pull")
    subprocess.run(["git", "--git-dir", str(lab.home / "git"), "reflog", "expire", "--expire=now", "--all"], check=True)
    subprocess.run(["git", "--git-dir", str(lab.home / "git"), "gc", "-q", "--prune=now"], check=True)
    page = c.get(f"/runs/new?playbook={lab.id}:site.yml&target=box1&inventory={hosts.id}&same={first.id}").text
    assert f"Can’t run the commits run #{first.id} used" in page and "force-pushed" in page
    assert 'name="same" value=""' in page


def test_a_runs_pull_says_when_it_waits_for_one_from_the_web_ui(app, c, remotes):
    from ansible_gui.repos import git as gitsync
    setup_repos(c, remotes)
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    lock = gitsync._lock(lab.home)
    lock.acquire()  # as a Pull now pressed a moment before the run would
    try:
        r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check",
                                  "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
        run_id = int(r.headers["location"].rsplit("/", 1)[1])
        log = app.state.store.log_path(run_id)
        end = time.time() + 20
        while "Waiting for a pull of lab started from the web UI to finish" not in (log.read_text() if log.exists() else ""):
            assert time.time() < end, "the run never said it was waiting"
            time.sleep(0.1)
        assert app.state.store.get(run_id).status == "running"
    finally:
        lock.release()
    assert wait(app, run_id).status == "successful"


# --- HTTPS with a token: a real smart-HTTP git server (git http-backend) behind basic auth ---
import base64  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


def self_signed(tmp_path: Path) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 that is its own CA, so git can be told to trust it (GIT_SSL_CAINFO)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    crt, pem = tmp_path / "tls.crt", tmp_path / "tls.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return crt, pem


def http_git_server(root: Path, user: str, token: str, tls: tuple[Path, Path] | None = None):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self):
            auth = self.headers.get("Authorization", "")
            seen.append(auth)
            if auth != "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            env = {"GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
                   "QUERY_STRING": query, "REQUEST_METHOD": self.command, "REMOTE_USER": user,
                   "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                   "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
            out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            status = 200
            headers = []
            for line in head.split(b"\r\n"):
                k, _, v = line.decode().partition(":")
                if k.lower() == "status":
                    status = int(v.strip().split()[0])
                elif k:
                    headers.append((k, v.strip()))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _handle

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_private_https_repo_pulls_with_a_token_kept_out_of_the_clone(app, c, remotes, tmp_path, monkeypatch):
    tls = self_signed(tmp_path)
    monkeypatch.setenv("GIT_SSL_CAINFO", str(tls[0]))
    srv, seen = http_git_server(tmp_path, "mike", "s3cret-token", tls)
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}/pb.git"
        # without a token: refused, and the reason is shown
        assert add(c, name="lab", role="playbooks", url=url).status_code == 303
        lab = app.state.repos.all()[0]
        assert lab.last_error.startswith("The git server wants a sign-in") and not lab.last_commit
        # a token in the URL is refused
        r = add(c, name="x", role="playbooks", url=url.replace("https://", "https://mike:tok@"))
        assert r.status_code == 400 and "Leave the user name and token out" in r.text
        # an SSH key for an HTTPS URL is refused
        c.post("/credentials", data={"kind": "git_token", "name": "forge", "username": "mike", "token": "s3cret-token"})
        tok = app.state.creds.by_kind("git_token")[0]
        # ...and so is sending it over plain http://
        r = c.post(f"/repos/{lab.id}", data={"name": "lab", "role": "playbooks", "url": url.replace("https:", "http:"),
                                             "branch": "main", "ssh_key_id": str(tok.id)}, follow_redirects=False)
        assert r.status_code == 400 and "only sent over https://" in r.text
        r = c.post(f"/repos/{lab.id}", data={"name": "lab", "role": "playbooks", "url": url, "branch": "main",
                                             "ssh_key_id": str(tok.id)}, follow_redirects=False)
        assert r.status_code == 303
        lab = app.state.repos.get(lab.id)
        assert lab.last_commit and not lab.last_error, lab.last_error
        assert any(a.startswith("Basic ") for a in seen)
        # the token is nowhere in the clone
        for f in lab.home.rglob("*"):
            if f.is_file():
                assert b"s3cret-token" not in f.read_bytes(), f
        assert "s3cret-token" not in c.get("/repos").text and "s3cret-token" not in c.get(f"/repos/{lab.id}").text
    finally:
        srv.shutdown()


@pytest.mark.background_pulls
def test_a_pull_from_the_web_runs_in_the_background_and_the_page_follows_it(app, c, remotes, monkeypatch):
    """Adding a repository pulled it before answering: on a big clone the form hung, and the whole server with
    it, since that request ran on the server's one event loop."""
    ws = app.state.workspace
    real, gate, pulled = ws.sync, threading.Event(), []

    def slow(repo, *args, **kwargs):
        pulled.append(repo.name)
        gate.wait(20)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(ws, "sync", slow)
    try:
        r = add(c, name="lab", role="playbooks", url=f"file://{remotes['pb']}")
        assert r.status_code == 303  # answered while the pull is still going
        lab = app.state.repos.all()[0]
        assert ws.pulling() == {lab.id} and not lab.last_commit
        page = c.get(r.headers["location"]).text
        assert "Saved lab. Pulling it now" in page and 'badge badge-neutral">pulling</span>' in page
        assert "first clone" in page and "disabled>Pulling…" in page and "Pull now" not in page
        assert f'hx-get="/repos/pulling?ids={lab.id}"' in page
        going = c.get(f"/repos/pulling?ids={lab.id}")
        assert going.status_code == 204 and "hx-refresh" not in going.headers

        # Not deleted from under its pull, which would write the clone back.
        r = c.post(f"/repos/{lab.id}/delete", follow_redirects=False)
        assert r.headers["location"] == f"/repos?kept={lab.id}"
        assert "Didn’t delete lab: a pull of it was running" in c.get(r.headers["location"]).text
        assert app.state.repos.get(lab.id)

        # Saved again meanwhile: pulled once more afterwards, as it was saved, not beside the first.
        r = c.post(f"/repos/{lab.id}", data={"name": "lab2", "role": "playbooks", "url": f"file://{remotes['pb']}",
                                             "branch": "main"}, follow_redirects=False)
        assert r.status_code == 303 and pulled == ["lab"]
    finally:
        gate.set()
    for _ in range(200):
        if not ws.pulling():
            break
        time.sleep(0.05)
    assert not ws.pulling() and pulled == ["lab", "lab2"]
    assert c.get(f"/repos/pulling?ids={lab.id}").headers.get("hx-refresh") == "true"  # the page reloads once
    page = c.get(f"/repos?saved={lab.id}").text
    assert "Saved and pulled lab2: now at" in page and "repos-pulling" not in page and "Pull now" in page
    assert app.state.repos.get(lab.id).last_commit


def test_a_background_pull_that_breaks_says_so_on_the_repository(app, c, remotes, monkeypatch):
    assert add(c, name="lab", role="playbooks", url=f"file://{remotes['pb']}").status_code == 303
    lab = app.state.repos.all()[0]

    def gone(repo, *args, **kwargs):
        raise RuntimeError("the fetcher went away")

    monkeypatch.setattr(app.state.workspace, "sync", gone)
    c.post(f"/repos/{lab.id}/pull")
    assert app.state.repos.get(lab.id).last_error == "The pull stopped: the fetcher went away"
    assert not app.state.workspace.pulling()
    assert "Last pull failed: The pull stopped: the fetcher went away" in c.get("/repos").text


def test_commands_and_facts_run_with_only_an_inventory_repository(app, c, remotes):
    """Without a playbook repository a command was refused ("Add a playbook repository first"). It starts at
    the top of the inventory's repository instead; with a playbook repository it uses that one, as before."""
    assert add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    inv = app.state.repos.all()[0]
    where = {"target": "lab", "inventory": str(inv.id)}
    preview = c.get("/runs/command/preview", params=where).text
    assert "Add a playbook repository" not in preview and "box1" in preview and "box2" in preview
    assert "runs from the top of the inventory repository" in c.get("/runs/command", params=where).text

    def command(args):
        r = c.post("/runs/command", data={**where, "module": "ansible.builtin.shell", "args": args,
                                          "expected": ["box1", "box2"]}, follow_redirects=False)
        assert r.status_code == 303, r.text
        run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
        log = app.state.store.log_path(run.id).read_text()
        assert run.status == "successful", log
        return run, log

    run, log = command("echo hello")
    inv = app.state.repos.get(inv.id)
    assert run.repo_id == inv.id == run.inventory_id and run.commit_sha == run.inventory_commit == inv.last_commit
    shown = next(line for line in log.splitlines() if line.startswith("$ ansible "))
    assert f"--playbook-dir {(inv.home / 'commits' / run.commit_sha).resolve()}" in shown  # the inventory's top
    assert log.count("hello") >= 2
    status = c.get(f"/runs/{run.id}").text
    assert "Inventory commit" in status and "Playbook commit" not in status

    assert 'action="/facts/gather"' in c.get("/inventory/hosts/box1").text
    r = c.post("/facts/gather", data={"target": "box1", "inventory": str(inv.id)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    facts_run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert facts_run.status == "successful", app.state.store.log_path(facts_run.id).read_text()
    assert facts_run.repo_id == inv.id

    assert add(c, name="lab", role="playbooks", url=f"file://{remotes['pb']}").status_code == 303
    book = next(r for r in app.state.repos.all() if r.role == "playbooks")
    assert "first playbook repository" in c.get("/runs/command", params=where).text
    run, _ = command("echo again")
    assert run.repo_id == book.id and run.inventory_id == inv.id
    assert "Playbook commit" in c.get(f"/runs/{run.id}").text


def test_a_token_reaches_git_with_no_file_to_run_and_only_for_its_server(remotes, tmp_path, monkeypatch):
    """A worker's temp directory is a tmpfs mounted noexec: git can't run a GIT_ASKPASS script written there,
    and every HTTPS pull with a token then failed as if there were no token."""
    from ansible_gui.repos import git as gitsync

    env = {**gitsync._base_env(), **gitsync.token_env("https://git.example.test:8443/me/pb.git", ("mike", "s3cret"))}

    def asked(host):  # what git gets when a server at host answers 401
        return subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n", env=env,
                              cwd=tmp_path, capture_output=True, text=True)

    mine = asked("git.example.test:8443")
    assert "username=mike" in mine.stdout and "password=s3cret" in mine.stdout, mine.stderr
    for elsewhere in ("git.example.test", "git.example.test:9443", "evil.example.test"):  # a redirect, say
        other = asked(elsewhere)
        assert other.returncode != 0 and "s3cret" not in other.stdout + other.stderr
    with pytest.raises(gitsync.GitError, match="line break"):
        gitsync.token_env("https://git.example.test/pb.git", ("mike", "tok\nurl=https://evil.example.test"))

    # A real pull: nothing it leaves in its temp directory is a program.
    tls = self_signed(tmp_path)
    monkeypatch.setenv("GIT_SSL_CAINFO", str(tls[0]))
    srv, seen = http_git_server(tmp_path, "mike", "s3cret-token", tls)
    work = tmp_path / "work"
    work.mkdir()
    (work / "ssh_config").write_text("")
    try:
        got = gitsync.fetch(tmp_path / "home", f"https://127.0.0.1:{srv.server_address[1]}/pb.git", "main",
                            work / "ssh_config", token=("mike", "s3cret-token"), workdir=work)
    finally:
        srv.shutdown()
    assert got.commit and any(a.startswith("Basic ") for a in seen)
    assert [f.name for f in work.iterdir() if f.stat().st_mode & 0o111] == []


def test_edit_link_must_be_a_web_address(app, c, remotes):
    r = add(c, name="lab", role="playbooks", url=str(remotes["pb"]), edit_url="javascript:alert(document.cookie)")
    assert r.status_code == 400 and "Use a web address starting with https://" in r.text
    assert app.state.repos.all() == []


def test_repo_form_says_who_it_trusts(c):
    assert "Anyone who can push to it can run commands on this server" in c.get("/repos/new").text


def test_credential_kind_must_match_url(app, c, remotes):
    c.post("/credentials", data={"kind": "git_token", "name": "forge", "username": "mike", "token": "t"})
    tok = app.state.creds.by_kind("git_token")[0]
    r = add(c, name="lab", role="playbooks", url="git@git.example.com:me/a.git", ssh_key_id=str(tok.id))
    assert r.status_code == 400 and "An SSH URL signs in with an SSH key" in r.text


def build_collection(tmp_path: Path) -> Path:
    """An offline collection, testns.testcol, with one role that prints a marker."""
    subprocess.run(["ansible-galaxy", "collection", "init", "testns.testcol", "--init-path", str(tmp_path / "src")],
                   check=True, capture_output=True)
    root = tmp_path / "src" / "testns" / "testcol"
    tasks = root / "roles" / "hello" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- ansible.builtin.debug:\n    msg: hello-from-collection\n")
    subprocess.run(["ansible-galaxy", "collection", "build", str(root), "--output-path", str(tmp_path)],
                   check=True, capture_output=True)
    return next(tmp_path.glob("testns-testcol-*.tar.gz"))


def test_run_dir_config_and_collections_apply(app, c, remotes, tmp_path):
    tarball = build_collection(tmp_path)
    play = ("- hosts: lab\n  gather_facts: false\n  roles:\n    - local_role\n    - testns.testcol.hello\n")
    files = {
        "lxc/ansible.cfg": "[defaults]\nroles_path = myroles\n",
        "lxc/myroles/local_role/tasks/main.yml": "- ansible.builtin.debug:\n    msg: hello-from-run-dir-config\n",
        "lxc/site.yml": play,
        "lockdown/site.yml": FIXTURE / "site.yml",
        "collections/requirements.yml": f"collections:\n  - name: {tarball}\n    type: file\n",
    }
    mono, _ = make_remote(tmp_path, "mono", files)
    assert add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    assert add(c, name="lxc", role="playbooks", url=f"file://{mono}", paths="lxc").status_code == 303
    assert add(c, name="lockdown", role="playbooks", url=f"file://{mono}", paths="lockdown").status_code == 303
    lxc = next(r for r in app.state.repos.all() if r.name == "lxc")
    assert not lxc.last_error, lxc.last_error
    launcher = c.get("/runs/new").text
    # the same file name from two places stays two playbooks, named by their full path
    assert "lxc/site.yml" in launcher and "lockdown/site.yml" in launcher
    r = c.post("/runs", data={"playbook": f"{lxc.id}:lxc/site.yml", "target": "box1", "mode": "check",
                              "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
    assert r.status_code == 303, r.text[-400:]
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log[-1500:]
    assert "hello-from-run-dir-config" in log and "hello-from-collection" in log


def test_commit_and_collections_directories_cant_be_written_once_they_exist(app, c, remotes, tmp_path):
    """Never written again, in the file modes too: whatever the fetcher runs for one repository can't add a
    line to another's files. Pruning and deleting still remove them."""
    tarball = build_collection(tmp_path)
    sealed, work = make_remote(tmp_path, "sealed", {
        "site.yml": FIXTURE / "site.yml", "roles/r/tasks/main.yml": "[]\n",
        "requirements.yml": f"collections:\n  - name: {tarball}\n    type: file\n"})
    assert add(c, name="sealed", role="playbooks", url=f"file://{sealed}").status_code == 303
    repo = next(r for r in app.state.repos.all() if r.name == "sealed")
    assert not repo.last_error, repo.last_error
    collections = app.state.workspace.collections_dir(repo)
    for tree in (repo.path, collections):
        paths = [tree, *tree.rglob("*")]
        assert len(paths) > 3
        assert not [str(p) for p in paths if not p.is_symlink() and stat.S_IMODE(p.stat().st_mode) & 0o222]
    for attempt in (lambda: (repo.path / "planted.yml").write_text("x"),
                    lambda: (repo.path / "site.yml").open("a"),
                    lambda: (repo.path / "roles" / "r" / "tasks" / "main.yml").unlink(),
                    lambda: (collections / "ansible_collections" / "planted").mkdir()):
        with pytest.raises(PermissionError):
            attempt()

    old = repo.path
    push_change(work, "site.yml", "# changed")
    c.post(f"/repos/{repo.id}/pull")
    assert not old.exists() and app.state.repos.get(repo.id).path.is_dir()  # the earlier commit was pruned
    c.post(f"/repos/{repo.id}/delete")
    assert not repo.home.exists() and not collections.exists()


def test_broken_collections_file_cancels_the_run(app, c, remotes, tmp_path):
    bad, work = make_remote(tmp_path, "bad", {"site.yml": FIXTURE / "site.yml"})
    add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml")
    add(c, name="bad", role="playbooks", url=f"file://{bad}")
    repo = next(r for r in app.state.repos.all() if r.name == "bad")
    good = repo.last_commit
    (work / "requirements.yml").write_text("collections:\n  - name: /nonexistent.tar.gz\n    type: file\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "broken requirements", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    c.post(f"/repos/{repo.id}/pull")
    repo = app.state.repos.get(repo.id)
    assert repo.last_error and "Installing its collections failed" in repo.last_error
    assert repo.last_commit == good  # the commit that works stays the current one
    r = c.post("/runs", data={"playbook": f"{repo.id}:site.yml", "target": "box1", "mode": "check",
                              "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
    assert r.status_code == 303
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error" and "collections failed" in run.error and "Nothing ran" in run.error
    assert run.host_stats == {}


FAKE_SSH = '''#!{python}
"""Stands in for ssh: records how git called it and the key it was given, then serves the repo locally."""
import json, os, stat, sys
args = sys.argv[1:]
key = args[args.index("-i") + 1] if "-i" in args else None
info = {{"args": args}}
if key:
    info["key_mode"] = stat.S_IMODE(os.stat(key).st_mode)
    info["key_first_line"] = open(key).readline().strip()
with open({log!r}, "a") as f:
    f.write(json.dumps(info) + "\\n")
command = args[-1].replace("git-upload-pack", "git upload-pack", 1)
os.execvp("sh", ["sh", "-c", command])
'''


def test_ssh_deploy_key_is_used_and_kept_private(app, c, remotes, tmp_path, monkeypatch):
    import json
    import sys
    log = tmp_path / "ssh-calls.jsonl"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text(FAKE_SSH.format(python=sys.executable, log=str(log)))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    key = tmp_path / "deploy"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "deploy@lab", "-f", str(key)], check=True)
    assert c.post("/credentials", data={"kind": "ssh_key", "name": "deploy", "private_key": key.read_text()},
                  follow_redirects=False).status_code == 303
    cred = app.state.creds.by_kind("ssh_key")[0]

    url = f"ssh://git@git.lab.test{remotes['inv']}"
    r = add(c, name="hosts", role="inventory", url=url, paths="inventory/hosts.yml", ssh_key_id=str(cred.id))
    assert r.status_code == 303, r.text[-400:]
    repo = app.state.repos.all()[0]
    assert not repo.last_error, repo.last_error
    assert "box1" in c.get("/inventory").text

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls, "git never called ssh"
    for call in calls:
        a = call["args"]
        assert "BatchMode=yes" in a and "IdentitiesOnly=yes" in a and "IdentityAgent=none" in a
        assert call["key_mode"] == 0o600
        assert call["key_first_line"] == "-----BEGIN OPENSSH PRIVATE KEY-----"
        assert "git@git.lab.test" in a
    # the key only exists for the length of the pull
    key_path = calls[0]["args"][calls[0]["args"].index("-i") + 1]
    assert not os.path.exists(key_path)


def test_the_playbook_list_is_grouped_by_repository_with_a_chip_for_each(app, c, remotes):
    """Every row used to lead with its repository's name. The list says it once, as a heading over that
    repository's playbooks, and with more than one repository a chip each narrows the list to it."""
    setup_repos(c, remotes)
    page = c.get("/runs/playbooks").text
    table = page.split('<table class="history playbooks">')[1].split("</table>")[0]
    heads = [h.split("</tr>")[0] for h in table.split('<tr class="list-group">')[1:]]
    # In the repositories' own order, which is by name.
    assert len(heads) == 2 and ">extra</a>" in heads[0] and "1 playbook" in heads[0] and ">lab</a>" in heads[1]
    assert 'class="repo-prefix"' not in table  # a row is its path; the heading above it is the repository
    by_name = {r.name: r.id for r in app.state.repos.all()}
    lab, extra = by_name["lab"], by_name["extra"]
    assert f'<tbody data-rowgroup="r{lab}">' in table and f'data-source="r{extra}"' in table
    chips = page.split('class="source-chips"')[1].split("</div>")[0]
    assert 'data-rows-all aria-pressed="true">All <span class="chip-count">2</span>' in chips
    assert f'data-rows-chip="r{lab}" aria-pressed="false">lab <span class="chip-count">1</span>' in chips
    assert chips.count("data-rows-chip=") == 2


def test_pull_now_on_the_pages_that_use_the_repositories(app, c, remotes):
    setup_repos(c, remotes)
    work = remotes["pb_work"]
    (work / "patch.yml").write_text("# Patches the lab.\n- hosts: all\n  gather_facts: false\n  tasks: []\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "patch", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    page = c.get("/runs/playbooks").text
    assert "patch.yml" not in page and 'action="/runs/playbooks/pull"' in page
    r = c.post("/runs/playbooks/pull", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/playbooks?pulled=")
    page = c.get(r.headers["location"]).text
    assert "patch.yml" in page and "Patches the lab." in page and "Pulled at" in page

    inv = remotes["inv_work"]
    hosts = inv / "inventory" / "hosts.yml"
    hosts.write_text(hosts.read_text().replace("box2:", "box2:\n        box3:", 1))
    git("commit", "-qam", "box3", cwd=inv)
    git("push", "-q", "origin", "HEAD:main", cwd=inv)
    listing = c.get("/inventory/hosts").text
    assert "Pull and reload" in listing and "/inventory/hosts/box3" not in listing
    r = c.post("/inventory/reload", headers={"referer": "https://ansible.example.test/inventory/hosts"},
               follow_redirects=False)
    after = c.get(r.headers["location"]).text
    assert "/inventory/hosts/box3" in after and "Pulled and reloaded at" in after
