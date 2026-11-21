"""One handler per kind of job. Each takes the job (plain JSON) and a Context, and returns plain JSON.

Fetcher jobs write the repos volume: fetch, materialize, collections, prune, prune_collections, remove.
Runner jobs only read it: cli (ansible-inventory, ansible-playbook --list-hosts, ansible-config, ...) and run.
"""

from __future__ import annotations

import configparser
import io
import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from ..credentials import ssh
from ..repos import git
from ..scratch import Scratch
from . import SCRATCH, Cancel


class Refused(Exception):
    """The job asks for something this worker won't do. The message goes back as the job's error."""


class Context:
    def __init__(self, scratch: Scratch, send: Callable[[dict], None], cancel: Cancel,
                 roots: list[Path] | None = None, tools: set[str] | None = None):
        self.scratch = scratch
        self.send = send
        self.cancel = cancel
        self.roots = roots
        self.tools = tools

    def path(self, value: str) -> Path:
        """A path the job names, if this worker may touch it."""
        p = Path(value)
        if not p.is_absolute() or ".." in p.parts:
            raise Refused(f"{value} isn’t an absolute path without ..")
        if self.roots is not None and not any(p == r or r in p.parents for r in self.roots):
            raise Refused(f"{value} is outside the directories this worker serves")
        return p


def _files(d: Path, files: dict[str, str]) -> None:
    for name, text in (files or {}).items():
        if not name or "/" in name or name.startswith("."):
            raise Refused(f"not a file name: {name!r}")
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)


def _sub(value: str, d: Path) -> str:
    return value.replace(SCRATCH, str(d))


def _ssh(d: Path, job_ssh: dict) -> Path:
    """Write the job's ssh_config, the file it includes and its known_hosts into d. Returns the ssh_config."""
    known = d / "known_hosts"
    known.write_text(job_ssh.get("known_hosts") or "")
    include = job_ssh.get("include")
    if job_ssh.get("config") is not None:
        include = str(d / "user_config")
        (d / "user_config").write_text(job_ssh["config"])
    path = d / "ssh_config"
    path.write_text(ssh.ssh_config_text(Path(include) if include else None, job_ssh.get("checking", "accept-new"),
                                        known))
    return path


def _learned(d: Path, job_ssh: dict) -> list[str]:
    """Host keys ssh added to the job's known_hosts (StrictHostKeyChecking accept-new), for the app to keep."""
    before = set((job_ssh.get("known_hosts") or "").splitlines())
    try:
        after = (d / "known_hosts").read_text().splitlines()
    except OSError:
        return []
    return [line for line in after if line.strip() and line not in before]


# --- fetcher ------------------------------------------------------------------------------------------------

def fetch(job: dict, ctx: Context) -> dict:
    home = ctx.path(job["home"])
    d = ctx.scratch.make("git-")
    try:
        config = _ssh(d, job["ssh"])
        token = tuple(job["token"]) if job.get("token") else None
        try:
            got = git.fetch(home, job["url"], job["branch"], config, job.get("private_key"), token, workdir=d)
        except git.GitError as e:
            return {"error": str(e), "host_keys": _learned(d, job["ssh"])}
        return {"commit": got.commit, "subject": got.subject, "host_keys": _learned(d, job["ssh"])}
    finally:
        ctx.scratch.drop(d)


def materialize(job: dict, ctx: Context) -> dict:
    try:
        git.materialize(ctx.path(job["home"]), job["commit"])
    except git.CommitGone as e:
        return {"error": str(e), "gone": True}
    except (git.GitError, ValueError) as e:
        return {"error": str(e)}
    return {}


# What a repository's ansible.cfg may tell ansible-galaxy: which Galaxy servers to ask, and how to sign in to
# them. Nothing in these names a path.
GALAXY_SETTINGS = {"server", "server_list", "server_timeout", "ignore_certs", "disable_gpg_verify",
                   "ignore_signature_status_codes", "required_valid_signature_count"}
GALAXY_SERVER_SETTINGS = {"url", "username", "password", "token", "auth_url", "validate_certs", "client_id",
                          "client_secret", "timeout"}


def galaxy_config(run_dir: Path) -> str:
    """The ansible.cfg ansible-galaxy runs with: the Galaxy servers of the repository's own ansible.cfg, and
    nothing else of it.

    The fetcher can write every repository's directories, and ansible-galaxy does what its configuration says:
    with the repository's file as it is, `log_path` appended ansible-galaxy's log to any file in another
    repository (its ansible.cfg, a script), and `home`, `local_tmp` and `cache_dir` chose where it wrote too.
    So the settings it may have are listed, not the ones it may not."""
    theirs = configparser.ConfigParser(inline_comment_prefixes=(";",), interpolation=None)
    try:
        theirs.read_string((run_dir / "ansible.cfg").read_text())
    except (OSError, UnicodeDecodeError):  # no ansible.cfg: Galaxy's defaults
        pass
    except configparser.Error as e:
        raise Refused(f"its ansible.cfg can’t be read: {str(e).splitlines()[0]}") from None
    ours = configparser.ConfigParser(interpolation=None)
    for section in theirs.sections():
        allowed = (GALAXY_SETTINGS if section == "galaxy"
                   else GALAXY_SERVER_SETTINGS if section.startswith("galaxy_server.") else None)
        kept = {k: v for k, v in theirs.items(section, raw=True) if allowed and k in allowed and v is not None}
        if kept:
            ours[section] = kept
    out = io.StringIO()
    out.write("# Written by ansible-gui for this install: the Galaxy servers from the repository's ansible.cfg.\n")
    ours.write(out)
    return out.getvalue()


def collections(job: dict, ctx: Context) -> dict:
    """Install what a requirements file lists into target, unless target is there already. Written beside it
    under a temporary name and renamed into place, so a collections directory that exists is whole, and
    without write permission from then on."""
    req, run_dir, target = ctx.path(job["requirements"]), ctx.path(job["run_dir"]), ctx.path(job["target"])
    if target.is_dir():
        return {"installed": False}
    fresh = target.with_name(f".{target.name}.partial")
    git.remove_tree(fresh)
    d = ctx.scratch.make("galaxy-")
    try:
        (d / "ansible.cfg").write_text(galaxy_config(run_dir))
        fresh.mkdir(parents=True)
        cmd = ["ansible-galaxy", "collection", "install", "-r", str(req), "-p", str(fresh), "--force"]
        try:
            # In the repository's directory, so a requirements file can name a collection beside it, but with
            # ANSIBLE_CONFIG set: Ansible then doesn't read the ansible.cfg there.
            proc = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True, timeout=600,
                                  env={**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_CONFIG": str(d / "ansible.cfg")})
        except (OSError, subprocess.TimeoutExpired) as e:
            git.remove_tree(fresh)
            return {"error": str(e)}
        if proc.returncode != 0:
            git.remove_tree(fresh)
            lines = [l for l in (proc.stderr or proc.stdout).splitlines() if l.strip()]
            return {"error": lines[-1] if lines else f"ansible-galaxy exited with {proc.returncode}"}
        git.seal(fresh)
        fresh.rename(target)
        return {"installed": True}
    finally:
        ctx.scratch.drop(d)


def prune(job: dict, ctx: Context) -> dict:
    return {"removed": git.prune(ctx.path(job["home"]), set(job["keep"]))}


def prune_collections(job: dict, ctx: Context) -> dict:
    root, keep, removed = ctx.path(job["root"]), set(job["keep"]), []
    for d in root.iterdir() if root.is_dir() else []:
        if (len(d.name) == 64 or d.name.endswith(".partial")) and d.name not in keep:
            git.remove_tree(d)
            removed.append(d.name)
    return {"removed": removed}


def remove(job: dict, ctx: Context) -> dict:
    git.remove_tree(ctx.path(job["home"]))
    return {}


# --- runner -------------------------------------------------------------------------------------------------

def cli(job: dict, ctx: Context) -> dict:
    """One Ansible command, its output captured: the inventory, a host preview, the settings report."""
    argv = list(job["argv"])
    if ctx.tools is not None and os.path.basename(argv[0]) not in ctx.tools and argv[0] not in ctx.tools:
        raise Refused(f"this worker doesn’t run {argv[0]}")
    cwd = ctx.path(job["cwd"])
    d = ctx.scratch.make("cli-") if job.get("files") else None
    try:
        if d:
            _files(d, job["files"])
            argv = [_sub(a, d) for a in argv]
        env = {**os.environ, **{k: _sub(v, d) if d else v for k, v in (job.get("env") or {}).items()}}
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=job["timeout"])
        except FileNotFoundError:
            return {"error": "not_found"}
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
        return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    finally:
        if d:
            ctx.scratch.drop(d)


def run(job: dict, ctx: Context) -> dict:
    """A playbook, an ad hoc command or a fact gathering, through ansible-runner.

    The job's files (key, certificate, vault password, answers) go into a private temp directory, with its
    ssh_config and known_hosts; the key is loaded into an ssh-agent that serves this run only. Sends
    {"scratch": dir} once that is ready, then {"event": ...} for every event, and returns the status.
    Canceling kills Ansible's whole process tree at once."""
    import ansible_runner  # slow to import, and only runs need it

    from ..runs.executor import kill_tree

    d = ctx.scratch.make()
    agent = None
    play: list[int] = []
    killed: list[bool] = []

    def kill() -> None:
        if play and not killed:
            killed.append(True)
            kill_tree(play[0])

    try:
        (d / "runner").mkdir()
        _files(d, job.get("files") or {})
        _ssh(d, job["ssh"])
        agent = ssh.Agent(d)
        ctx.scratch.note(d, "agent", agent.pid)
        if job.get("key"):
            agent.add(d / job["key"])
        ctx.send({"scratch": str(d)})
        ctx.cancel.on_cancel(kill)
        project = ctx.path(job["project_dir"])
        what = dict(job["what"])
        if what.get("playbook"):
            what["playbook"] = str(ctx.path(what["playbook"]))

        def on_event(ev: dict) -> bool:
            if not play and isinstance(ev.get("pid"), int):
                play.append(ev["pid"])
                ctx.scratch.note(d, "play", ev["pid"])  # so a later start can stop it if this process is killed
                if ctx.cancel.requested:
                    kill()
            ctx.send({"event": ev})
            return True

        def cancel_requested() -> bool:
            if ctx.cancel.requested:
                kill()
                return True
            return False

        cmdline = [_sub(a, d) for a in job.get("cmdline") or []]
        result = ansible_runner.run(
            private_data_dir=str(d / "runner"),
            # Hand envvars, passwords and cmdline over in memory rather than as files in env/.
            suppress_env_files=True,
            project_dir=str(project),
            inventory=[str(ctx.path(s)) for s in job.get("inventory") or []],
            **what,
            verbosity=job.get("verbosity") or None,
            cmdline=shlex.join(cmdline) if cmdline else None,
            envvars={**{k: _sub(v, d) for k, v in (job.get("env") or {}).items()},
                     "SSH_AUTH_SOCK": str(agent.socket), "SSH_AGENT_PID": str(agent.pid)},
            passwords=job.get("passwords") or None,
            event_handler=on_event,
            cancel_callback=cancel_requested,
            quiet=True,
        )
        return {"status": result.status, "rc": result.rc, "host_keys": _learned(d, job["ssh"])}
    finally:
        if agent:
            agent.stop()
        ctx.scratch.drop(d)


HANDLERS = {"fetch": fetch, "materialize": materialize, "collections": collections, "prune": prune,
            "prune_collections": prune_collections, "remove": remove, "cli": cli, "run": run}
FETCHER = {"fetch", "materialize", "collections", "prune", "prune_collections", "remove"}
RUNNER = {"cli", "run"}
