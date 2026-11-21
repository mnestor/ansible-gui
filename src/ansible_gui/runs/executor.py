"""Runs playbooks one at a time through ansible-runner, in a background thread."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..credentials import ssh
from ..credentials.store import CredentialStore
from ..facts import GATHER_ACTIONS
from ..wipe import wipe_tree
from . import playbooks, variables
from .store import Run, RunStore, host_result, now

log = logging.getLogger(__name__)

_COUNTERS = ("ok", "changed", "failed", "unreachable", "skipped")


def git_commit(project_dir: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=project_dir,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _parent_pairs():
    """(pid, parent pid) for every process. /proc on Linux (the image has no ps), ps elsewhere."""
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                stat = (d / "stat").read_text()
            except OSError:  # gone since the listing
                continue
            # The command (field 2) can hold spaces and parentheses: the parent pid is the second field after it.
            yield int(d.name), int(stat.rsplit(")", 1)[1].split()[1])
        return
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines():
        pid, ppid = line.split()
        yield int(pid), int(ppid)


def kill_tree(root: int) -> None:
    """SIGKILL `root` and every process under it.

    ansible-runner's cancel kills only the playbook's process group, but each Ansible worker calls setsid():
    the worker running the current task would carry on, on the hosts, after the run says it was canceled.
    The root is stopped first so it can't fork another worker between the listing and the kill."""
    try:
        os.kill(root, signal.SIGSTOP)
    except ProcessLookupError:
        return
    children: dict[int, list[int]] = {}
    try:
        for pid, ppid in _parent_pairs():
            children.setdefault(ppid, []).append(pid)
    except (OSError, subprocess.SubprocessError, ValueError):
        log.exception("listing processes to cancel %s", root)
    tree, stack = [], [root]
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children.get(pid, []))
    for pid in reversed(tree):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class CredentialMissing(Exception):
    pass


@dataclass
class Prepared:
    """What a run needs from the credential store, materialised for this run only.

    ansible-runner's own directory lives in here too (runner/): it writes the environment, the become
    password and every event to it, so it goes when the run ends, with the keys."""
    workdir: Path
    env: dict[str, str] = field(default_factory=dict)
    cmdline: list[str] = field(default_factory=list)
    passwords: dict[str, str] = field(default_factory=dict)
    agent: ssh.Agent | None = None
    user: str = ""  # the account's login, if the run signs in with one

    @property
    def runner_dir(self) -> Path:
        return self.workdir / "runner"

    def cleanup(self) -> None:
        if self.agent:
            self.agent.stop()
        wipe_tree(self.workdir)


def _write_secret(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class Executor:
    def __init__(self, store: RunStore, workspace, creds: CredentialStore | None = None,
                 data_dir: Path | None = None, ssh_config: Path | None = None, on_finish=None, facts=None):
        self.on_finish = on_finish or (lambda run_id: None)
        self.facts = facts  # FactStore: every gather_facts/setup result is saved there
        self.store = store
        self.workspace = workspace
        self.creds = creds
        self.data_dir = data_dir or store.data_dir
        self.ssh_config = ssh_config
        self._queue: queue.Queue[int] = queue.Queue()
        self._cancel: set[int] = set()
        self._live: dict[int, dict[str, dict[str, int]]] = {}
        self._thread = threading.Thread(target=self._loop, name="ansible-gui-runner", daemon=True)
        self._thread.start()

    # --- public ----------------------------------------------------------------
    def submit(self, run_id: int) -> None:
        self._queue.put(run_id)

    def cancel(self, run_id: int) -> None:
        run = self.store.get(run_id)
        if run and run.status == "queued":
            self.store.update(run_id, status="canceled", finished_at=now(), error="Canceled before it started.")
        else:
            self._cancel.add(run_id)

    def cancel_requested(self, run_id: int) -> bool:
        return run_id in self._cancel

    def live_stats(self, run_id: int) -> dict[str, dict[str, int]] | None:
        return self._live.get(run_id)

    def wait_idle(self, timeout: float = 120) -> bool:
        """Block until every submitted run has finished and its on_finish (the email) has been handed
        off. A run is marked finished before on_finish runs, so waiting for the status isn't enough."""
        end = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > end:
                return False
            time.sleep(0.05)
        return True

    def queue_position(self, run_id: int) -> int:
        with self._queue.mutex:
            items = list(self._queue.queue)
        return items.index(run_id) + 1 if run_id in items else 0

    # --- worker ----------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                self._execute(run_id)
            except Exception as e:  # never let one run kill the worker
                log.exception("run %s crashed", run_id)
                self.store.update(run_id, status="error", finished_at=now(), error=f"Internal error: {e}")
            finally:
                self._live.pop(run_id, None)
                self._cancel.discard(run_id)
                try:
                    run = self.store.get(run_id)
                    if run and run.finished:
                        self.on_finish(run_id)
                finally:
                    self._queue.task_done()

    def _execute(self, run_id: int) -> None:
        import ansible_runner  # imported lazily: slow, and only the worker needs it

        run = self.store.get(run_id)
        if run is None or run.status != "queued":
            return
        log_file = self.store.log_path(run_id).open("a", encoding="utf-8", buffering=1)
        self.store.update(run_id, status="running", started_at=now())

        def stop(message: str) -> None:
            log_file.write(f"[ansible-gui] {message}\n")
            log_file.close()
            self.store.update(run_id, status="error", finished_at=now(), error=message)

        repo = self.workspace.playbook_repo(run.repo_id)
        if repo is None:
            return stop("Didn’t start: the playbook’s repository was removed.")
        inv_repo = self.workspace.inventory_repo(run.inventory_id)
        if inv_repo is None:
            return stop("Didn’t start: its inventory repository was removed, or there are several and the run "
                        "doesn’t say which. Pick one and run again.")
        failed = self.workspace.sync_for_run(repo, inv_repo, lambda line: log_file.write(line + "\n"))
        if failed:
            return stop(failed)
        repo = self.workspace.playbook_repo(run.repo_id)
        inv_repo = self.workspace.inventory_repo(inv_repo.id)
        project = repo.run_dir
        opts = self.workspace.run_opts(repo, inv_repo)
        facts_run = run.kind == "facts"
        adhoc = run.kind in ("facts", "command")
        if adhoc:  # absolute: Ansible starts in run_dir, which may already be the playbook directory
            opts = dataclasses.replace(opts, playbook_dir=str((repo.path / repo.playbook_dir).resolve()))
        playbook = str((repo.path / run.playbook).resolve())
        if not adhoc and not (repo.path / run.playbook).is_file():
            return stop(f"Didn’t start: {run.playbook} isn’t in {repo.name} any more.")

        # The files may have changed since the preview: ask Ansible again which hosts this run hits.
        pv = playbooks.list_hosts(opts, run.target) if adhoc else playbooks.preview(opts, playbook, run.target)
        if pv.error or not pv.hosts:
            why = pv.error or f"the targets ({run.target or 'the playbook’s hosts'}) match no hosts"
            return stop(f"Didn’t start: {why}.")
        if not run.schedule_id and run.expected_hosts and sorted(pv.hosts) != sorted(run.expected_hosts):
            return stop(f"Canceled: after pulling from git, the targets match {len(pv.hosts)} host(s) "
                        f"({', '.join(pv.hosts[:10])}{'…' if len(pv.hosts) > 10 else ''}), not the "
                        f"{len(run.expected_hosts)} you previewed. Nothing ran. Check the preview and run again.")
        live: dict[str, dict[str, int]] = {h: dict.fromkeys(_COUNTERS, 0) for h in pv.hosts}
        self._live[run_id] = live
        final: dict[str, dict[str, int]] = {}
        self.store.update(run_id, expected_hosts=pv.hosts, inventory_id=inv_repo.id,
                          commit_sha=(repo.last_commit or "")[:12] if not repo.local else git_commit(repo.path),
                          inventory_commit=None if not inv_repo else
                          ((inv_repo.last_commit or "")[:12] if not inv_repo.local else None))
        run = self.store.get(run_id)
        if run_id in self._cancel:
            log_file.write("[ansible-gui] Canceled before Ansible started.\n")
            log_file.close()
            self.store.update(run_id, status="canceled", finished_at=now(), error="Canceled before Ansible started.")
            return
        try:
            prepared = self.prepare(run)
        except CredentialMissing as e:
            return stop(str(e))

        playbook_pid: list[int] = []  # the ansible process, from its first event

        def cancel_requested() -> bool:
            if run_id not in self._cancel:
                return False
            if playbook_pid:
                kill_tree(playbook_pid.pop())
            return True

        def on_event(ev: dict) -> bool:
            if not playbook_pid and isinstance(ev.get("pid"), int):
                playbook_pid.append(ev["pid"])
            data = ev.get("event_data") or {}
            host = data.get("host")
            kind = ev.get("event", "")
            gathered = (data.get("resolved_action") or data.get("task_action")) in GATHER_ACTIONS
            if adhoc and gathered and kind == "runner_on_ok":
                # Ad hoc setup prints every fact, ansible_env included: the log keeps a count instead.
                n = len((data.get("res") or {}).get("ansible_facts") or {})
                log_file.write(f"{host} | SUCCESS | {n} facts gathered and saved\n")
            elif ev.get("stdout"):
                log_file.write(ev["stdout"] + "\n")
            if host and kind.startswith("runner_on_"):
                c = live.setdefault(host, dict.fromkeys(_COUNTERS, 0))
                if kind == "runner_on_ok":
                    c["ok"] += 1
                    res = data.get("res") or {}
                    if res.get("changed"):
                        c["changed"] += 1
                    action = data.get("resolved_action") or data.get("task_action")
                    if self.facts and action in GATHER_ACTIONS and res.get("ansible_facts"):
                        try:
                            self.facts.record(host, res["ansible_facts"], run_id)
                        except Exception:  # facts are a bonus: never fail the run over them
                            log.exception("saving facts of %s from run %s", host, run_id)
                elif kind == "runner_on_failed" and not data.get("ignore_errors"):
                    c["failed"] += 1
                elif kind == "runner_on_unreachable":
                    c["unreachable"] += 1
                elif kind == "runner_on_skipped":
                    c["skipped"] += 1
            if kind == "playbook_on_stats":
                for key, field in (("ok", "ok"), ("changed", "changed"), ("failures", "failed"),
                                   ("dark", "unreachable"), ("skipped", "skipped")):
                    for h, n in (data.get(key) or {}).items():
                        final.setdefault(h, dict.fromkeys(_COUNTERS, 0))[field] = n
            return True

        cmdline = []
        if run.check_mode:
            cmdline.append("--check")
        if run.diff:
            cmdline.append("--diff")
        cmdline += prepared.cmdline
        # As files, not arguments: secret answers stay out of ps and the log. Free-form first, so a prompt
        # answer wins over it, and Connect as over both.
        if run.extra_vars.strip():
            cmdline += ["-e", "@" + str(_write_secret(prepared.workdir / "extra-vars.yml", run.extra_vars))]
        if run.prompt_vars:
            try:
                plain = variables.unseal(run.prompt_vars, self.creds.fernet)
            except ValueError as e:
                prepared.cleanup()
                return stop(f"Didn’t start: the secret {e} can’t be decrypted: the secret key changed since it was "
                            "saved. Enter it again and run again.")
            text = variables.vars_file_text(plain)
            cmdline += ["-e", "@" + str(_write_secret(prepared.workdir / "prompt-vars.yml", text))]
        user = prepared.user
        if user:
            # Extra vars beat the inventory and the play. Both names, because ansible_ssh_user beats ansible_user.
            cmdline += ["-e", json.dumps({"ansible_user": user, "ansible_ssh_user": user})]

        if adhoc:
            # Ad hoc rather than a playbook, so --playbook-dir loads the group_vars and host_vars next to
            # the playbooks, exactly as for the repository's own playbooks.
            cmdline = ["--playbook-dir", opts.playbook_dir, *(["--become"] if run.become else []), *cmdline]
            what = dict(module="ansible.builtin.setup", host_pattern=run.target) if facts_run else dict(
                module=run.module, module_args=run.module_args or None, host_pattern=run.target)
            log_file.write(f"$ ansible {shlex.join(self._args(run, cmdline))}\n\n")
        else:
            what = dict(playbook=playbook, limit=run.target or None, tags=run.tags or None,
                        skip_tags=run.skip_tags or None)
            log_file.write(f"$ ansible-playbook {shlex.join(self._args(run, cmdline))}\n\n")
        try:
            result = ansible_runner.run(
                private_data_dir=str(prepared.runner_dir),
                # Hand envvars, passwords and cmdline over in memory rather than as files in env/.
                suppress_env_files=True,
                project_dir=str(project),
                inventory=opts.sources,
                **what,
                verbosity=run.verbosity or None,
                cmdline=shlex.join(cmdline) if cmdline else None,
                envvars={"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0", **opts.env, **prepared.env},
                passwords=prepared.passwords or None,
                event_handler=on_event,
                cancel_callback=cancel_requested,
                quiet=True,
            )
            if result.status == "canceled":
                log_file.write("\n[ansible-gui] Run canceled from the web UI.\n")
        finally:
            prepared.cleanup()
            log_file.close()

        stats = final or live
        status = result.status
        if status == "timeout":
            status = "failed"
        if status == "successful" and any(host_result(s) in ("failed", "unreachable") for s in stats.values()):
            status = "failed"
        self.store.update(run_id, status=status, finished_at=now(), rc=result.rc, host_stats=stats)

    def prepare(self, run: Run) -> Prepared:
        """Write this run's keys and passwords to a private temp dir and start its ssh-agent.

        The run never sees the server user's ssh-agent, ~/.ssh/config or known_hosts:
        SSH reads only the ssh_config ansible-gui writes (which includes ANSIBLE_GUI_SSH_CONFIG),
        and talks only to an agent holding the chosen key.
        """
        # Short path: the agent socket must fit in a Unix socket address.
        p = Prepared(workdir=Path(tempfile.mkdtemp(prefix="agui-")))
        try:
            p.runner_dir.mkdir()
            chosen = run.credentials or {}
            loaded = {}
            for kind in ("account", "vault_password"):
                ref = chosen.get(kind)
                if not ref:
                    continue
                cred = self.creds.get(ref["id"]) if self.creds else None
                if not cred or cred.kind != kind:
                    raise CredentialMissing(f"The credential “{ref['name']}” was deleted before this run started.")
                if not cred.readable:
                    raise CredentialMissing(f"The credential “{cred.name}” can’t be decrypted: the secret key changed "
                                            "since it was saved. Enter its value again on the Credentials page.")
                loaded[kind] = (cred, self.creds.secret(cred))
            if "account" in loaded:
                acct, secret = loaded.pop("account")
                p.user = acct.meta.get("username") or ""
                if secret.get("private_key"):
                    loaded["ssh_key"] = (acct, secret)
                if secret.get("become_password"):
                    loaded["become_password"] = (acct, {"password": secret["become_password"]})
            envs = []
            for ref in chosen.get("env_secret", []):
                cred = self.creds.get(ref["id"]) if self.creds else None
                if cred and cred.readable:
                    envs.append((cred, self.creds.secret(cred)))

            config = ssh.write_ssh_config(self.data_dir, self.ssh_config,
                                          self.workspace.settings.ssh_host_key_checking)
            p.cmdline += ["--ssh-common-args", f"-F {shlex.quote(str(config))}"]
            p.agent = ssh.Agent(p.workdir)
            p.env["SSH_AUTH_SOCK"] = str(p.agent.socket)
            p.env["SSH_AGENT_PID"] = str(p.agent.pid)

            if "ssh_key" in loaded:
                cred, secret = loaded["ssh_key"]
                key = _write_secret(p.workdir / "id", secret["private_key"])
                if cred.meta.get("certificate"):
                    (p.workdir / "id-cert.pub").write_text(cred.meta["certificate"].strip() + "\n")
                p.agent.add(key)
                p.env["ANSIBLE_PRIVATE_KEY_FILE"] = str(key)
            if "vault_password" in loaded:
                path = _write_secret(p.workdir / "vault-pass", loaded["vault_password"][1]["password"])
                p.cmdline += ["--vault-password-file", str(path)]
            elif self.workspace.settings.vault_password_file:
                p.cmdline += ["--vault-password-file", str(self.workspace.settings.vault_password_file)]
            if "become_password" in loaded:
                p.cmdline.append("--ask-become-pass")
                p.passwords[r"^BECOME password.*:\s*?$"] = loaded["become_password"][1]["password"]
            for cred, secret in envs:
                name = cred.meta["env"]
                p.env[name] = secret["value"]
                p.env[f"{name}_FILE"] = str(_write_secret(p.workdir / f"env-{name}", secret["value"]))
            return p
        except BaseException:
            p.cleanup()
            raise

    def _args(self, run, cmdline: list[str]) -> list[str]:
        """The command line shown at the top of the log."""
        args = []
        for s in self.workspace.inventory_sources(self.workspace.inventory_repo(run.inventory_id)):
            args += ["-i", s]
        if run.kind == "facts":
            return [run.target, "-m", "ansible.builtin.setup", *args, *cmdline]
        if run.kind == "command":
            return [run.target, "-m", run.module, *(["-a", run.module_args] if run.module_args else []), *args, *cmdline]
        if run.target:
            args += ["--limit", run.target]
        if run.tags:
            args += ["--tags", run.tags]
        if run.skip_tags:
            args += ["--skip-tags", run.skip_tags]
        if run.verbosity:
            args.append("-" + "v" * run.verbosity)
        return [*args, *cmdline, run.playbook]
