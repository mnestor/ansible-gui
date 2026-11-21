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
from ansible_gui.worker.socket import Server, SocketWorker

from test_repos import FIXTURE, add, make_remote, pytestmark, wait  # noqa: F401 - pytestmark: needs ansible-core


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
