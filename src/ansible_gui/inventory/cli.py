"""Calls the ansible-inventory CLI.

ansible-core is GPLv3, so this app never imports it: Ansible is only ever run
as a subprocess, and its JSON output is the interface.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


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


def _run(opts: CliOptions, extra: list[str], env_extra: dict[str, str]) -> dict:
    cmd = [opts.binary]
    for src in opts.sources:
        cmd += ["-i", src]
    cmd += ["--playbook-dir", opts.playbook_dir]
    if opts.vault_password_file:
        cmd += ["--vault-password-file", str(opts.vault_password_file)]
    cmd += ["--list", *extra]

    env = {**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0", **opts.env, **env_extra}
    try:
        proc = subprocess.run(
            cmd,
            cwd=opts.project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=opts.timeout,
        )
    except FileNotFoundError:
        raise InventoryError(
            f"{opts.binary} is not installed or not on PATH.", cmd
        ) from None
    except subprocess.TimeoutExpired:
        raise InventoryError(
            f"{opts.binary} took longer than {opts.timeout}s. A dynamic inventory source may be unreachable.",
            cmd,
        ) from None

    if proc.returncode != 0:
        raise InventoryError(f"{opts.binary} exited with code {proc.returncode}.", cmd, proc.stderr)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise InventoryError(f"{opts.binary} did not return JSON.", cmd, proc.stderr) from None


def resolved(opts: CliOptions) -> dict:
    """Everything Ansible resolves for each host: the ground truth."""
    return _run(opts, [], {})


def sources_only(opts: CliOptions) -> dict:
    """Variables set inside the inventory sources themselves, per group and
    host, with group_vars/host_vars files switched off."""
    # A vars-plugin name that matches nothing disables host_group_vars.
    return _run(opts, ["--export"], {"ANSIBLE_VARS_ENABLED": "__ansible_gui_none__"})
