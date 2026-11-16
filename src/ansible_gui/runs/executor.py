"""Runs playbooks one at a time through ansible-runner, in a background thread."""

from __future__ import annotations

import logging
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..credentials import ssh
from ..credentials.store import CredentialStore
from . import playbooks
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


class CredentialMissing(Exception):
    pass


@dataclass
class Prepared:
    """What a run needs from the credential store, materialised for this run only."""
    workdir: Path
    env: dict[str, str] = field(default_factory=dict)
    cmdline: list[str] = field(default_factory=list)
    passwords: dict[str, str] = field(default_factory=dict)
    agent: ssh.Agent | None = None

    def cleanup(self) -> None:
        if self.agent:
            self.agent.stop()
        for f in self.workdir.glob("*"):
            if f.is_file():
                try:  # overwrite before unlinking: these are keys and passwords
                    f.write_bytes(b"\0" * f.stat().st_size)
                except OSError:
                    pass
        shutil.rmtree(self.workdir, ignore_errors=True)


def _write_secret(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class Executor:
    def __init__(self, store: RunStore, workspace, creds: CredentialStore | None = None,
                 data_dir: Path | None = None, ssh_config: Path | None = None, on_finish=None):
        self.on_finish = on_finish or (lambda run_id: None)
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
                run = self.store.get(run_id)
                if run and run.finished:
                    self.on_finish(run_id)

    def _execute(self, run_id: int) -> None:
        import ansible_runner  # imported lazily: slow, and only the worker needs it

        run = self.store.get(run_id)
        if run is None or run.status != "queued":
            return
        run_dir = self.store.run_dir(run_id)
        (run_dir / "runner").mkdir(exist_ok=True)
        log_file = self.store.log_path(run_id).open("a", encoding="utf-8", buffering=1)
        self.store.update(run_id, status="running", started_at=now())

        def stop(message: str) -> None:
            log_file.write(f"[ansible-gui] {message}\n")
            log_file.close()
            self.store.update(run_id, status="error", finished_at=now(), error=message)

        repo = self.workspace.playbook_repo(run.repo_id)
        if repo is None:
            return stop("Didn’t start: the playbook’s repository was removed.")
        failed = self.workspace.sync_for_run(repo, lambda line: log_file.write(line + "\n"))
        if failed:
            return stop(failed)
        repo = self.workspace.playbook_repo(run.repo_id)
        inv_repo = self.workspace.inventory_repo()
        project = repo.run_dir
        opts = self.workspace.run_opts(repo)
        playbook = str((repo.path / run.playbook).resolve())
        if not (repo.path / run.playbook).is_file():
            return stop(f"Didn’t start: {run.playbook} isn’t in {repo.name} any more.")

        # The files may have changed since the preview: ask Ansible again which hosts this run hits.
        pv = playbooks.preview(opts, playbook, run.target)
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
        self.store.update(run_id, expected_hosts=pv.hosts,
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

        def on_event(ev: dict) -> bool:
            if ev.get("stdout"):
                log_file.write(ev["stdout"] + "\n")
            data = ev.get("event_data") or {}
            host = data.get("host")
            kind = ev.get("event", "")
            if host and kind.startswith("runner_on_"):
                c = live.setdefault(host, dict.fromkeys(_COUNTERS, 0))
                if kind == "runner_on_ok":
                    c["ok"] += 1
                    if (data.get("res") or {}).get("changed"):
                        c["changed"] += 1
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

        log_file.write(f"$ ansible-playbook {shlex.join(self._args(run, cmdline))}\n\n")
        try:
            result = ansible_runner.run(
                private_data_dir=str(run_dir / "runner"),
                project_dir=str(project),
                playbook=playbook,
                inventory=opts.sources,
                limit=run.target or None,
                tags=run.tags or None,
                skip_tags=run.skip_tags or None,
                verbosity=run.verbosity or None,
                cmdline=shlex.join(cmdline) if cmdline else None,
                envvars={"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0", **opts.env, **prepared.env},
                passwords=prepared.passwords or None,
                event_handler=on_event,
                cancel_callback=lambda: run_id in self._cancel,
                quiet=True,
                rotate_artifacts=1,
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
            chosen = run.credentials or {}
            loaded = {}
            for kind in ("ssh_key", "vault_password", "become_password"):
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
            envs = []
            for ref in chosen.get("env_secret", []):
                cred = self.creds.get(ref["id"]) if self.creds else None
                if cred and cred.readable:
                    envs.append((cred, self.creds.secret(cred)))

            config = ssh.write_ssh_config(self.data_dir, self.ssh_config)
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
        args = []
        for s in self.workspace.inventory_sources():
            args += ["-i", s]
        if run.target:
            args += ["--limit", run.target]
        if run.tags:
            args += ["--tags", run.tags]
        if run.skip_tags:
            args += ["--skip-tags", run.skip_tags]
        if run.verbosity:
            args.append("-" + "v" * run.verbosity)
        return [*args, *cmdline, run.playbook]
