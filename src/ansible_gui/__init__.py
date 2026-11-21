"""ansible-gui: see what Ansible resolves for each host, and where it came from."""

USAGE = """usage: ansible-gui                     start the server
       ansible-gui sign-out-everyone   end every session now (in the container:
                                       docker exec ansible-gui-app ansible-gui sign-out-everyone)
       ansible-gui worker --socket PATH --allow-uid UID [--jobs all|fetcher|runner]
                                       take git and Ansible jobs from the app on a Unix socket"""


def main() -> None:
    import sys

    args = sys.argv[1:]
    if not args:
        from .web import main as run

        return run()
    if args == ["sign-out-everyone"]:
        return sign_out_everyone()
    if args[:1] == ["worker"]:
        return run_worker(args[1:])
    raise SystemExit(USAGE)


def run_worker(args: list[str]) -> None:
    """A worker container's process: serve jobs to the app until stopped.

    It reads only ANSIBLE_GUI_REPOS_DIR (the paths a job may name), ANSIBLE_GUI_PROJECT_DIR (a local checkout,
    if one is used) and ANSIBLE_GUI_INVENTORY_BINARY. It holds no secret of its own: each job brings its own."""
    import argparse
    import logging
    import os
    import signal
    import tempfile
    import threading
    import time
    from pathlib import Path

    from .config import scrub_environment
    from .runs.executor import clear_leftovers
    from .scratch import Scratch
    from .worker import InlineWorker
    from .worker.jobs import FETCHER, RUNNER
    from .worker.socket import Server

    p = argparse.ArgumentParser(prog="ansible-gui worker")
    p.add_argument("--socket", required=True, type=Path)
    p.add_argument("--allow-uid", required=True, type=int, help="the app's uid: the only one that may connect")
    p.add_argument("--jobs", default="all", choices=["all", "fetcher", "runner"])
    a = p.parse_args(args)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    repos = os.environ.get("ANSIBLE_GUI_REPOS_DIR")
    if not repos:
        raise SystemExit("Set ANSIBLE_GUI_REPOS_DIR to the repositories volume, mounted where the app has it.")
    roots = [Path(repos).resolve()]
    if os.environ.get("ANSIBLE_GUI_PROJECT_DIR"):
        roots.append(Path(os.environ["ANSIBLE_GUI_PROJECT_DIR"]).resolve())
    tools = {"ansible", "ansible-playbook", "ansible-inventory", "ansible-config", "ansible-galaxy",
             os.environ.get("ANSIBLE_GUI_INVENTORY_BINARY", "ansible-inventory")}
    scrub_environment()
    state = Path(tempfile.gettempdir()) / "ansible-gui-worker"
    scratch = Scratch(state)
    clear_leftovers(scratch, state)  # this container's tmpfs normally went with it; a crashed process's didn't
    kinds = {"all": FETCHER | RUNNER, "fetcher": FETCHER, "runner": RUNNER}[a.jobs]
    server = Server(a.socket, kinds, a.allow_uid, InlineWorker(scratch, roots=roots, tools=tools))
    signal.signal(signal.SIGTERM, lambda *_: server.close())
    logging.getLogger("ansible_gui.worker").info("taking %s jobs on %s from uid %s", a.jobs, a.socket, a.allow_uid)
    server.serve_forever()
    deadline = time.monotonic() + 8  # the jobs it canceled wipe their temp directories as they end
    while threading.active_count() > 1 and time.monotonic() < deadline:
        time.sleep(0.1)


def sign_out_everyone() -> None:
    """For when someone must lose access before their session would end on its own."""
    import os
    from pathlib import Path

    from .sessions import SessionStore

    data_dir = Path(os.environ.get("ANSIBLE_GUI_DATA_DIR", "data")).expanduser().resolve()
    if not (data_dir / "ansible-gui.db").exists():
        raise SystemExit(f"No ansible-gui database in {data_dir}. Set ANSIBLE_GUI_DATA_DIR to the data directory.")
    n = SessionStore(data_dir).end_all()
    print(f"Ended {n} session{'s' if n != 1 else ''}. Everyone signs in again on their next click.")
