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
