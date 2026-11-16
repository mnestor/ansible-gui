"""Git repositories: clone on save, pull before each run, cancel and email when a pull fails."""

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


def test_only_one_inventory_repo_and_paths_stay_inside(c, remotes):
    assert add(c, name="hosts", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml").status_code == 303
    r = add(c, name="hosts2", role="inventory", url=f"file://{remotes['inv']}", paths="inventory/hosts.yml")
    assert r.status_code == 400 and "already the inventory repository" in r.text
    r = add(c, name="bad", role="playbooks", url=f"file://{remotes['pb']}", paths="../etc")
    assert r.status_code == 400 and "inside the repository" in r.text


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


def http_git_server(root: Path, user: str, token: str):
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
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_private_https_repo_pulls_with_a_token_kept_out_of_the_clone(app, c, remotes, tmp_path):
    srv, seen = http_git_server(tmp_path, "mike", "s3cret-token")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/pb.git"
        # without a token: refused, and the reason is shown
        assert add(c, name="lab", role="playbooks", url=url).status_code == 303
        lab = app.state.repos.all()[0]
        assert lab.last_error.startswith("The git server wants a sign-in") and not lab.last_commit
        # a token in the URL is refused
        r = add(c, name="x", role="playbooks", url=url.replace("http://", "http://mike:tok@"))
        assert r.status_code == 400 and "Leave the user name and token out" in r.text
        # an SSH key for an HTTPS URL is refused
        c.post("/credentials", data={"kind": "git_token", "name": "forge", "username": "mike", "token": "s3cret-token"})
        tok = app.state.creds.by_kind("git_token")[0]
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
