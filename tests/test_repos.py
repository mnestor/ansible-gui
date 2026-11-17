"""Git repositories: clone on save, pull before each run, cancel and email when a pull fails."""

import os
import shutil
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
    assert newer != first and run.commit_sha == newer[:12] and run.inventory_commit
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
        for f in lab.path.joinpath(".git").rglob("*"):
            if f.is_file():
                assert b"s3cret-token" not in f.read_bytes(), f
        assert "s3cret-token" not in c.get("/repos").text and "s3cret-token" not in c.get(f"/repos/{lab.id}").text
    finally:
        srv.shutdown()


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


def test_broken_collections_file_cancels_the_run(app, c, remotes, tmp_path):
    bad, _ = make_remote(tmp_path, "bad", {"site.yml": FIXTURE / "site.yml",
                                            "requirements.yml": "collections:\n  - name: /nonexistent.tar.gz\n    type: file\n"})
    add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml")
    add(c, name="bad", role="playbooks", url=f"file://{bad}")
    repo = next(r for r in app.state.repos.all() if r.name == "bad")
    assert repo.last_error and "Installing its collections failed" in repo.last_error
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
