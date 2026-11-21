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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..credentials.store import CredentialStore
from ..facts import GATHER_ACTIONS
from ..scratch import Scratch
from ..worker import SCRATCH, VAULT, Cancel, vault_files
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


def command_line(pid: int) -> str:
    """The command a process was started with, or "" if there is no such process."""
    if Path("/proc/self").exists():  # Linux: no ps needed, and none in the image
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:  # gone
            return ""
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def clear_leftovers(scratch: Scratch, data_dir: Path) -> int:
    """Finish what a server that was killed couldn't: stop each noted playbook and ssh-agent, wipe each temp
    directory.

    A pid may belong to something else by now (always, after a container restart), so a process is only
    stopped if its command line still says it is the one noted: every run's Ansible is started with
    `-F <temp dir>/ssh_config`, and its agent with `-a <temp dir>/agent.sock`."""
    left = scratch.leftovers()
    for path, pids in left:
        play, agent = pids.get("play"), pids.get("agent")
        if play and play != os.getpid() and str(path / "ssh_config") in command_line(play):
            kill_tree(play)
        if agent and str(path / "agent.sock") in command_line(agent):
            try:
                os.kill(agent, signal.SIGTERM)
            except ProcessLookupError:
                pass
        scratch.drop(path)
        log.warning("wiped %s, left by a run or pull the server didn't get to finish", path)
    return len(left)


class CredentialMissing(Exception):
    pass


@dataclass
class Prepared:
    """What a run takes from the credential store, as the run job carries it: file contents, not files.
    The runner writes the files into its own temp directory; SCRATCH in cmdline and env stands for it."""
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    cmdline: list[str] = field(default_factory=list)
    passwords: dict[str, str] = field(default_factory=dict)
    key: str | None = None  # the file in files to load into the run's ssh-agent
    user: str = ""  # the account's login, if the run signs in with one


class Executor:
    def __init__(self, store: RunStore, workspace, creds: CredentialStore | None = None,
                 data_dir: Path | None = None, on_finish=None, facts=None, run_timeout: float = 0):
        self.on_finish = on_finish or (lambda run_id: None)
        self.run_timeout = run_timeout  # seconds Ansible may run before it is stopped; 0 for no limit
        self.facts = facts  # FactStore: every gather_facts/setup result is saved there
        self.store = store
        self.workspace = workspace
        self.creds = creds
        self.data_dir = data_dir or store.data_dir
        self.scratch = Scratch(self.data_dir)  # where a worker in this process notes its temp directories
        # What a server that was killed mid-run left, before this one makes its own.
        clear_leftovers(self.scratch, self.data_dir)
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._cancel: set[int] = set()
        self._live: dict[int, dict[str, dict[str, int]]] = {}
        self._jobs: dict[int, Cancel] = {}  # each run on a worker, to cancel it
        self._stopping = threading.Event()
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
            if job := self._jobs.get(run_id):
                job.cancel()

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

    def shutdown(self, timeout: float = 5.0) -> None:
        """The server is stopping: end the run in progress (its playbook, its ssh-agent, its temp directory)
        and start no other. Otherwise the playbook would carry on against the hosts with nothing recording
        it, and the run's key and passwords would stay on disk.

        The run stays "running" in the store, like any run a restart cuts short: the next start marks it
        interrupted and sends its email."""
        self._stopping.set()
        self._queue.put(None)  # wakes the worker if it is waiting for a run
        for job in list(self._jobs.values()):
            job.cancel()  # the worker kills Ansible's process tree now and wipes the run's temp directory
        self._thread.join(timeout)

    # --- worker ----------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            run_id = self._queue.get()
            if run_id is None or self._stopping.is_set():
                # Runs still queued stay queued: the next start marks them interrupted.
                self._queue.task_done()
                return
            try:
                self._execute(run_id)
            except Exception as e:  # never let one run kill the worker
                log.exception("run %s crashed", run_id)
                self.store.update(run_id, status="error", finished_at=now(), error=f"Internal error: {e}")
            finally:
                self._live.pop(run_id, None)
                self._cancel.discard(run_id)
                self.workspace.release_run(run_id)
                try:
                    run = self.store.get(run_id)
                    if run and run.finished:
                        self.on_finish(run_id)
                finally:
                    self._queue.task_done()

    def _execute(self, run_id: int) -> None:
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
        failed, commits = self._commits(run, repo, inv_repo, lambda line: log_file.write(line + "\n"))
        if failed:
            return stop(failed)
        # From here on the run reads only these commits' directories, which nothing changes until it ends.
        repo = repo.at(commits[repo.id]) if repo.id in commits else repo
        inv_repo = inv_repo.at(commits[inv_repo.id]) if inv_repo.id in commits else inv_repo
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
                          commit_sha=repo.last_commit if not repo.local else git_commit(repo.path),
                          inventory_commit=inv_repo.last_commit if not inv_repo.local else None,
                          collections=self.workspace.collections_digest(repo))
        run = self.store.get(run_id)
        if run_id in self._cancel:
            log_file.write("[ansible-gui] Canceled before Ansible started.\n")
            log_file.close()
            self.store.update(run_id, status="canceled", finished_at=now(), error="Canceled before Ansible started.")
            return
        if self._stopping.is_set():  # shutdown(): the run stays "running" for the next start to mark interrupted
            log_file.write("[ansible-gui] The server stopped before Ansible started.\n")
            log_file.close()
            return
        try:
            prepared = self.prepare(run)
        except CredentialMissing as e:
            return stop(str(e))

        cmdline = []
        if run.check_mode:
            cmdline.append("--check")
        if run.diff:
            cmdline.append("--diff")
        cmdline += prepared.cmdline
        files = dict(prepared.files)
        # As files, not arguments: secret answers stay out of ps and the log. Free-form first, so a prompt
        # answer wins over it, and Connect as over both.
        if run.extra_vars.strip():
            files["extra-vars.yml"] = run.extra_vars
            cmdline += ["-e", f"@{SCRATCH}/extra-vars.yml"]
        if run.prompt_vars:
            try:
                plain = variables.unseal(run.prompt_vars, self.creds.fernet)
            except ValueError as e:
                return stop(f"Didn’t start: the secret {e} can’t be decrypted: the secret key changed since it was "
                            "saved. Enter it again and run again.")
            files["prompt-vars.yml"] = variables.vars_file_text(plain)
            cmdline += ["-e", f"@{SCRATCH}/prompt-vars.yml"]
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
            shown = f"$ ansible {shlex.join(self._args(run, cmdline, inv_repo))}\n\n"
        else:
            what = dict(playbook=playbook, limit=run.target or None, tags=run.tags or None,
                        skip_tags=run.skip_tags or None)
            shown = f"$ ansible-playbook {shlex.join(self._args(run, cmdline, inv_repo))}\n\n"
        job = {"project_dir": str(project), "inventory": opts.sources, "what": what, "verbosity": run.verbosity,
               "cmdline": cmdline, "files": files, "key": prepared.key, "passwords": prepared.passwords,
               "env": {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0", **opts.env, **prepared.env},
               "ssh": self.workspace.ssh_job()}

        def on_message(msg: dict) -> None:
            if "scratch" in msg:  # the runner's temp directory is ready: the command line can name its files
                log_file.write(shown.replace(SCRATCH, str(msg["scratch"])))
            elif isinstance(msg.get("event"), dict):
                on_event(msg["event"])

        def on_event(ev: dict) -> None:
            data = ev.get("event_data") or {}
            host = data.get("host")
            kind = ev.get("event", "")
            gathered = (data.get("resolved_action") or data.get("task_action")) in GATHER_ACTIONS
            if adhoc and gathered and kind == "runner_on_ok":
                # Ad hoc setup prints every fact, ansible_env included: the log keeps a count instead.
                n = len((data.get("res") or {}).get("ansible_facts") or {})
                log_file.write(f"{host} | SUCCESS | {n} facts gathered and saved\n")
            elif ev.get("stdout"):
                log_file.write(str(ev["stdout"]) + "\n")
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

        cancel = Cancel()
        self._jobs[run_id] = cancel
        timed_out = threading.Event()
        timer = None
        if self.run_timeout:
            timer = threading.Timer(self.run_timeout, lambda: (timed_out.set(), cancel.cancel()))
            timer.daemon = True
            timer.start()
        if run_id in self._cancel or self._stopping.is_set():  # asked for while the job was being put together
            cancel.cancel()
        try:
            result = self.workspace.runner.call("run", job, on_message, cancel)
            self.workspace.learn_host_keys(result)
            status = result.get("status") or "error"
            # shutdown() killed it, unless it had just finished by itself.
            stopped = self._stopping.is_set() and status != "successful"
            if stopped:
                log_file.write("\n[ansible-gui] The server stopped: this run was interrupted.\n")
            elif "error" in result:
                log_file.write(f"\n[ansible-gui] The run couldn’t start: {result['error']}\n")
            elif timed_out.is_set() and run_id not in self._cancel:
                log_file.write(f"\n[ansible-gui] {self._timeout_message()}\n")
            elif status == "canceled":
                log_file.write("\n[ansible-gui] Run canceled from the web UI.\n")
        finally:
            if timer:
                timer.cancel()
            self._jobs.pop(run_id, None)
            log_file.close()
        if stopped:
            return  # still "running" in the store: the next start marks it interrupted and sends its email

        stats = final or live
        if "error" in result:
            self.store.update(run_id, status="error", finished_at=now(), host_stats=stats,
                              error=f"Didn’t start: {result['error']}")
            return
        if timed_out.is_set() and run_id not in self._cancel:
            self.store.update(run_id, status="failed", finished_at=now(), rc=result.get("rc"), host_stats=stats,
                              error=self._timeout_message())
            return
        if status == "timeout":
            status = "failed"
        if status == "successful" and any(host_result(s) in ("failed", "unreachable") for s in stats.values()):
            status = "failed"
        self.store.update(run_id, status=status, finished_at=now(), rc=result.get("rc"), host_stats=stats)

    def _commits(self, run: Run, repo, inv_repo, log_line) -> tuple[str | None, dict[int, str]]:
        """The commits the run executes, kept for it until it ends: what a fresh pull lands on, or, for a run of
        an earlier run's commits, those. (None, {repo id: commit}), or (why it can't start, {})."""
        ws = self.workspace
        if not (run.same_as and run.commit_sha) or repo.local:
            return ws.sync_for_run(repo, inv_repo, log_line, run.id)
        log_line(f"[ansible-gui] Running the commits run #{run.same_as} used, not the latest")
        wanted = {repo.id: run.commit_sha}
        if run.inventory_commit and not inv_repo.local:
            wanted[inv_repo.id] = run.inventory_commit
        elif not inv_repo.local:  # another inventory than the earlier run's: its latest
            failed, _ = ws.sync_for_run(inv_repo, None, log_line, run.id)
            if failed:
                return failed, {}
        for r in {r.id: r for r in (inv_repo, repo) if r.id in wanted}.values():
            commit = wanted[r.id]
            error, reinstalled = ws.ensure_commit(r, commit, pin_for=run.id, on_wait=lambda r=r: log_line(
                f"[ansible-gui] Waiting for a pull of {r.name} started from the web UI to finish"))
            if error:
                log_line(f"[ansible-gui] {error}")
                return f"Didn’t start: {error} Run again to use the latest commit instead.", {}
            log_line(f"[ansible-gui] {r.name} at {commit[:10]}, as in run #{run.same_as}")
            if reinstalled:
                note = (f"The collections run #{run.same_as} used had been removed since, so they were installed "
                        "again from the same requirements file. Unless it pins exact versions, they may be newer.")
                log_line(f"[ansible-gui] {note}")
                self.store.update(run.id, note=note)
        return None, ws.pinned(run.id)

    def _timeout_message(self) -> str:
        minutes = self.run_timeout / 60
        limit = f"{minutes:g} minute{'s' if minutes != 1 else ''}" if minutes >= 1 else f"{self.run_timeout:g} s"
        return (f"Stopped after {limit}: the longest a run may take (ANSIBLE_GUI_RUN_TIMEOUT_MINUTES). "
                "Ansible was stopped, with every SSH connection it had open.")

    def prepare(self, run: Run) -> Prepared:
        """What the run job carries from the credential store: the account's key and certificate, the become
        and vault passwords, and the environment secrets, for this run only.

        The run never sees the server user's ssh-agent, ~/.ssh/config or known_hosts: the runner starts an
        agent holding only this key, and SSH reads only the ssh_config the job carries (which includes
        ANSIBLE_GUI_SSH_CONFIG).
        """
        p = Prepared(cmdline=["--ssh-common-args", f"-F {SCRATCH}/ssh_config"])
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

        if "ssh_key" in loaded:
            cred, secret = loaded["ssh_key"]
            p.files["id"] = secret["private_key"]
            if cred.meta.get("certificate"):  # ssh-add picks up id-cert.pub beside id by itself
                p.files["id-cert.pub"] = cred.meta["certificate"].strip() + "\n"
            p.key = "id"
            p.env["ANSIBLE_PRIVATE_KEY_FILE"] = f"{SCRATCH}/id"
        if "vault_password" in loaded:
            p.files["vault-pass"] = loaded["vault_password"][1]["password"]
            p.cmdline += ["--vault-password-file", VAULT]
        else:
            files, args = vault_files(self.workspace.settings.vault_password_file)
            p.files.update(files)
            p.cmdline += args
        if "become_password" in loaded:
            p.cmdline.append("--ask-become-pass")
            p.passwords[r"^BECOME password.*:\s*?$"] = loaded["become_password"][1]["password"]
        for ref in chosen.get("env_secret", []):
            cred = self.creds.get(ref["id"]) if self.creds else None
            if cred and cred.readable:
                name, value = cred.meta["env"], self.creds.secret(cred)["value"]
                p.env[name] = value
                p.files[f"env-{name}"] = value
                p.env[f"{name}_FILE"] = f"{SCRATCH}/env-{name}"
        return p

    def _args(self, run, cmdline: list[str], inv_repo) -> list[str]:
        """The command line shown at the top of the log."""
        args = []
        for s in self.workspace.inventory_sources(inv_repo):
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
