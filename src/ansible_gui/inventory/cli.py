"""Calls the ansible-inventory CLI.

ansible-core is GPLv3, so this app never imports it: Ansible is only ever run
as a subprocess, and its JSON output is the interface.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import worker as workers


class InventoryError(Exception):
    def __init__(self, message: str, command: list[str], stderr: str = ""):
        super().__init__(message)
        self.command = command
        self.stderr = stderr.strip()

    @property
    def command_line(self) -> str:
        return " ".join(self.command)


@dataclass
class CliOptions:
    project_dir: Path
    sources: list[str]  # relative to project_dir
    playbook_dir: str = "."
    vault_password_file: Path | None = None
    binary: str = "ansible-inventory"
    timeout: int = 120
    env: dict = field(default_factory=dict)  # extra environment, e.g. ANSIBLE_COLLECTIONS_PATH
    worker: Any = None  # where the command runs: a worker.Worker, or None for this process

    def vault(self) -> tuple[dict[str, str], list[str]]:
        return workers.vault_files(self.vault_password_file)

    def shown(self, cmd: list[str]) -> list[str]:
        """The command as the error page shows it: the configured vault password file, not the job's copy."""
        return [str(self.vault_password_file) if a == workers.VAULT else a for a in cmd]


def run_cli(opts: CliOptions, head: list[str], args: list[str], env: dict[str, str]) -> str:
    """head + the inventory sources + the vault password + args, on the options' worker. Returns stdout.
    Raises InventoryError."""
    cmd = list(head)
    for src in opts.sources:
        cmd += ["-i", src]
    files, vault_args = opts.vault()
    cmd += [*vault_args, *args]
    try:
        proc = workers.cli(opts.worker, cmd, opts.project_dir, env, opts.timeout, files)
    except FileNotFoundError:
        raise InventoryError(f"{head[0]} is not installed or not on PATH.", opts.shown(cmd)) from None
    except subprocess.TimeoutExpired:
        raise InventoryError(f"{head[0]} took longer than {opts.timeout}s.", opts.shown(cmd)) from None
    except OSError as e:
        raise InventoryError(f"{head[0]} couldn’t be started: {e}", opts.shown(cmd)) from None
    if proc.returncode != 0:
        raise InventoryError(f"{head[0]} exited with code {proc.returncode}.", opts.shown(cmd),
                             proc.stderr or proc.stdout)
    return proc.stdout


def _run(opts: CliOptions, extra: list[str], env_extra: dict[str, str]) -> dict:
    env = {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0", **opts.env, **env_extra}
    try:
        out = run_cli(opts, [opts.binary], ["--playbook-dir", opts.playbook_dir, "--list", *extra], env)
    except InventoryError as e:
        if "took longer" in str(e):
            e.args = (f"{e} A dynamic inventory source may be unreachable.",)
        raise
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise InventoryError(f"{opts.binary} did not return JSON.", [opts.binary]) from None


def resolved(opts: CliOptions) -> dict:
    """Everything Ansible resolves for each host: the ground truth."""
    return _run(opts, [], {})


def sources_only(opts: CliOptions) -> dict:
    """Variables set inside the inventory sources themselves, per group and
    host, with group_vars/host_vars files switched off."""
    # A vars-plugin name that matches nothing disables host_group_vars.
    return _run(opts, ["--export"], {"ANSIBLE_VARS_ENABLED": "__ansible_gui_none__"})
