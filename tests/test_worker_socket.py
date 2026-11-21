"""The app with its workers on the other side of a Unix socket, as in the containers."""

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.scratch import Scratch
from ansible_gui.web import create_app
from ansible_gui.worker import Cancel, InlineWorker
from ansible_gui.worker.jobs import FETCHER, RUNNER
from ansible_gui.worker import socket as sockets
from ansible_gui.worker.socket import Server, SocketWorker

from test_repos import FIXTURE, add, git, make_remote, pytestmark, wait  # noqa: F401 - pytestmark: needs ansible-core


@pytest.fixture
def sockdir():
    # Short: a Unix socket's path must fit in about 100 bytes, and pytest's tmp_path on macOS doesn't.
    with tempfile.TemporaryDirectory(prefix="agui-t-", dir="/tmp") as d:
        yield Path(d)


def serve(path: Path, repos: Path, kinds=FETCHER | RUNNER, uid=None, state: Path | None = None) -> Server:
    worker = InlineWorker(Scratch(state or path.parent / "state"), roots=[repos],
                          tools={"ansible", "ansible-playbook", "ansible-inventory", "ansible-config", "ansible-galaxy"})
    server = Server(path, kinds, os.getuid() if uid is None else uid, worker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_worker_takes_jobs_only_from_the_apps_uid(sockdir, tmp_path):
    server = serve(sockdir / "w.sock", tmp_path, uid=os.getuid() + 1)
    try:
        res = SocketWorker(sockdir / "w.sock").call("cli", {"argv": ["ansible", "--version"], "cwd": str(tmp_path),
                                                            "env": {}, "timeout": 10, "files": {}})
        assert res == {"error": f"this worker takes jobs from uid {os.getuid() + 1} only, not {os.getuid()}"}
    finally:
        server.close()


def test_a_fetcher_takes_no_runner_jobs(sockdir, tmp_path):
    server = serve(sockdir / "f.sock", tmp_path, kinds=FETCHER)
    try:
        res = SocketWorker(sockdir / "f.sock", "fetcher").call("cli", {"argv": ["ansible", "--version"],
                                                                        "cwd": str(tmp_path), "env": {},
                                                                        "timeout": 10, "files": {}})
        assert res == {"error": "this worker doesn’t take cli jobs"}
    finally:
        server.close()
    assert "isn’t answering" in SocketWorker(sockdir / "f.sock", "fetcher").call("prune", {})["error"]


@pytest.fixture
def split(tmp_path, sockdir):
    """The app with one worker taking every job over a socket, and the repositories outside its data."""
    repos = tmp_path / "repos"
    repos.mkdir()
    server = serve(sockdir / "w.sock", repos)
    app = create_app(Settings(data_dir=tmp_path / "data", secret_key="s" * 40, public_url="https://ansible.example.test",
                              repos_dir=repos, fetcher_socket=sockdir / "w.sock", runner_socket=sockdir / "w.sock"))
    yield app, TestClient(app, base_url="https://ansible.example.test"), server, repos
    server.close()


def test_pulls_previews_runs_and_settings_go_through_the_socket(split, tmp_path):
    app, c, server, repos = split
    inv, _ = make_remote(tmp_path, "inv", {"inventory/hosts.yml": FIXTURE / "inventory" / "hosts.yml"})
    pb, _ = make_remote(tmp_path, "pb", {"site.yml": FIXTURE / "site.yml", "slow.yml": FIXTURE / "slow.yml"})
    assert add(c, name="hosts", role="inventory", url=f"file://{inv}", paths="inventory/hosts.yml").status_code == 303
    assert add(c, name="lab", role="playbooks", url=f"file://{pb}").status_code == 303
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    assert not lab.last_error and lab.path.parent.parent.parent == repos  # <repos>/<id>/commits/<sha>
    assert "box1" in c.get("/inventory").text
    assert "box1" in c.get(f"/runs/preview?playbook={lab.id}:site.yml&target=box1").text
    assert "Couldn’t" not in c.get(f"/repos/{lab.id}/ansible").text

    r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert "$ ansible-playbook" in log and "@@AGUI_SCRATCH@@" not in log

    # Cancel crosses the socket, and the worker stops Ansible's process tree.
    r = c.post("/runs", data={"playbook": f"{lab.id}:slow.yml", "target": "box1", "mode": "apply", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    log = app.state.store.log_path(run_id)
    end = time.time() + 30
    while "TASK [Wait]" not in (log.read_text() if log.exists() else ""):
        assert time.time() < end, "the pause task never started"
        time.sleep(0.2)
    started = time.time()
    c.post(f"/runs/{run_id}/cancel")
    assert wait(app, run_id).status == "canceled" and time.time() - started < 10


def test_a_worker_that_stops_mid_run_leaves_the_run_interrupted(split, tmp_path):
    app, c, server, _ = split
    inv, _ = make_remote(tmp_path, "inv", {"inventory/hosts.yml": FIXTURE / "inventory" / "hosts.yml"})
    pb, _ = make_remote(tmp_path, "pb", {"slow.yml": FIXTURE / "slow.yml"})
    add(c, name="hosts", role="inventory", url=f"file://{inv}", paths="inventory/hosts.yml")
    add(c, name="lab", role="playbooks", url=f"file://{pb}")
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    r = c.post("/runs", data={"playbook": f"{lab.id}:slow.yml", "target": "box1", "mode": "apply", "verbosity": "0",
                              "expected": ["box1"]}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    log = app.state.store.log_path(run_id)
    end = time.time() + 30
    while "TASK [Wait]" not in (log.read_text() if log.exists() else ""):
        assert time.time() < end, "the pause task never started"
        time.sleep(0.2)
    server.close()  # docker stop on the worker
    run = wait(app, run_id)
    assert run.status == "interrupted" and "the runner stopped" in run.error, run.error
    assert server.worker.scratch.leftovers() == []  # its key directory went with the run


def test_a_cancel_reaches_a_job_on_the_other_side(sockdir, tmp_path):
    """Cancel is a message on the job's connection; the worker's Cancel fires, whatever the job is."""
    seen = threading.Event()
    server = serve(sockdir / "c.sock", tmp_path)
    real = server.worker.call

    def call(kind, job, on_message=None, cancel=None):
        cancel.on_cancel(seen.set)
        on_message({"ready": True})
        seen.wait(10)
        return {"stopped": seen.is_set()}

    server.worker.call = call
    try:
        token = Cancel()
        res = SocketWorker(sockdir / "c.sock").call("prune", {}, on_message=lambda m: token.cancel(), cancel=token)
        assert res == {"stopped": True}
    finally:
        server.worker.call = real
        server.close()


def test_fetcher_and_runner_each_get_only_their_own_jobs(tmp_path, sockdir):
    """The compose layout: pulls to the fetcher, which alone writes the repositories; everything that runs
    them to the runner. Each socket refuses the other's kinds, so a misrouted job fails instead of running."""
    repos = tmp_path / "repos"
    repos.mkdir()
    fetcher = serve(sockdir / "f.sock", repos, kinds=FETCHER, state=sockdir / "fs")
    runner = serve(sockdir / "r.sock", repos, kinds=RUNNER, state=sockdir / "rs")
    try:
        app = create_app(Settings(data_dir=tmp_path / "data", secret_key="s" * 40,
                                  public_url="https://ansible.example.test", repos_dir=repos,
                                  fetcher_socket=sockdir / "f.sock", runner_socket=sockdir / "r.sock"))
        c = TestClient(app, base_url="https://ansible.example.test")
        inv, _ = make_remote(tmp_path, "inv", {"inventory/hosts.yml": FIXTURE / "inventory" / "hosts.yml"})
        pb, _ = make_remote(tmp_path, "pb", {"site.yml": FIXTURE / "site.yml"})
        assert add(c, name="hosts", role="inventory", url=f"file://{inv}", paths="inventory/hosts.yml").status_code == 303
        assert add(c, name="lab", role="playbooks", url=f"file://{pb}").status_code == 303
        lab = next(r for r in app.state.repos.all() if r.name == "lab")
        assert not lab.last_error, lab.last_error
        r = c.post("/runs", data={"playbook": f"{lab.id}:site.yml", "target": "box1", "mode": "check",
                                  "verbosity": "0", "expected": ["box1"]}, follow_redirects=False)
        run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
        assert run.status == "successful", app.state.store.log_path(run.id).read_text()
    finally:
        fetcher.close()
        runner.close()


def test_the_app_stops_waiting_for_a_worker_that_never_answers(sockdir, tmp_path):
    """A playbook runs as the runner's user and could answer on its socket, or just hang it. Every job but a
    run has a time by which it must have answered; a run that was told to stop has a little longer."""
    server = serve(sockdir / "h.sock", tmp_path)
    release = threading.Event()

    def never(kind, job, on_message=None, cancel=None):
        release.wait(30)
        return {"late": True}

    server.worker.call = never
    try:
        started = time.time()
        fetcher = SocketWorker(sockdir / "h.sock", "fetcher", deadlines={"prune": 0.3})
        assert fetcher.call("prune", {}) == {"error": "the fetcher didn’t answer in time", "lost": True}

        runner = SocketWorker(sockdir / "h.sock", "runner", cancel_grace=0.3)
        stop = Cancel()
        threading.Timer(0.2, stop.cancel).start()
        assert runner.call("run", {}, cancel=stop) == {"error": "the runner didn’t answer in time", "lost": True}
        assert time.time() - started < 10
    finally:
        release.set()
        server.close()
    runner = SocketWorker(sockdir / "h.sock", "runner")
    assert runner.deadline("run", {}) is None  # the run timeout stops a run, not the socket
    assert runner.deadline("cli", {"timeout": 60}) == 60 + sockets.CLI_MARGIN
    assert all(runner.deadline(kind, {}) for kind in FETCHER)


def test_a_reply_longer_than_the_app_reads_at_once_is_left_out_and_the_job_goes_on(sockdir, tmp_path, monkeypatch):
    monkeypatch.setattr(sockets, "MAX_REPLY", 4096)
    server = serve(sockdir / "o.sock", tmp_path)

    def call(kind, job, on_message=None, cancel=None):
        on_message({"event": {"stdout": "x" * 20000}})
        on_message({"event": {"stdout": "after"}})
        return {"status": "successful"}

    server.worker.call = call
    try:
        seen = []
        assert SocketWorker(sockdir / "o.sock").call("run", {}, on_message=seen.append) == {"status": "successful"}
        assert len(seen) == 2 and seen[0]["oversized"] > 20000 and seen[1] == {"event": {"stdout": "after"}}
    finally:
        server.close()


def test_with_workers_the_server_doesnt_read_through_a_repositorys_links_to_its_own_files(split, tmp_path):
    """Ansible follows a repository's links in the runner. The server, listing playbooks and showing where a
    variable is set, would follow them into its own data directory."""
    app, c, _, _ = split
    secret = tmp_path / "data" / "notes.yml"
    secret.write_text("- hosts: all\n  tasks: []\n")
    pb, work = make_remote(tmp_path, "pb", {"site.yml": FIXTURE / "site.yml"})
    (work / "linked.yml").symlink_to(secret)
    (work / "beside.yml").symlink_to("site.yml")
    (work / "requirements.yml").symlink_to(tmp_path / "data" / "ansible-gui.db")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "links", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    add(c, name="lab", role="playbooks", url=f"file://{pb}")
    lab = next(r for r in app.state.repos.all() if r.name == "lab")
    assert "can’t be read: it is a link to somewhere outside the repositories" in lab.last_error

    (work / "requirements.yml").unlink()
    git("commit", "-qam", "no requirements", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    c.post(f"/repos/{lab.id}/pull")
    lab = app.state.repos.get(lab.id)
    assert not lab.last_error and (lab.path / "linked.yml").is_symlink()
    assert [p.path for p in app.state.workspace.discover()] == ["beside.yml", "site.yml"]
