"""Settings, all from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .inventory.cli import CliOptions


@dataclass
class Settings:
    project_dir: Path
    inventory: list[str]
    playbook_dir: str = "."
    vault_password_file: Path | None = None
    edit_url: str | None = None  # e.g. https://git.example.com/me/ansible/src/branch/main/{path}#L{line}
    inventory_binary: str = "ansible-inventory"

    @classmethod
    def from_env(cls) -> "Settings":
        project = os.environ.get("ANSIBLE_GUI_PROJECT_DIR")
        if not project:
            raise SystemExit(
                "Set ANSIBLE_GUI_PROJECT_DIR to the directory you run ansible-playbook from "
                "(the one holding ansible.cfg and your playbooks)."
            )
        inventory = [s.strip() for s in os.environ.get("ANSIBLE_GUI_INVENTORY", "inventory").split(",") if s.strip()]
        vault = os.environ.get("ANSIBLE_GUI_VAULT_PASSWORD_FILE")
        return cls(
            project_dir=Path(project).expanduser().resolve(),
            inventory=inventory,
            playbook_dir=os.environ.get("ANSIBLE_GUI_PLAYBOOK_DIR", "."),
            vault_password_file=Path(vault).expanduser() if vault else None,
            edit_url=os.environ.get("ANSIBLE_GUI_EDIT_URL") or None,
        )

    def cli_options(self) -> CliOptions:
        return CliOptions(
            project_dir=self.project_dir,
            sources=self.inventory,
            playbook_dir=self.playbook_dir,
            vault_password_file=self.vault_password_file,
            binary=self.inventory_binary,
        )
