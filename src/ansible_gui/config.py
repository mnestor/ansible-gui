"""Settings, all from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    data_dir: Path = Path("data")
    ssh_config: Path | None = None  # the only ssh_config runs read (ssh -F); None means -F none
    public_url: str | None = None  # e.g. https://ansible.example.com, used for the OIDC redirect
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_groups: list[str] = field(default_factory=list)  # ["*"] allows anyone the provider lets in
    secret_key: str | None = None  # master key for sessions and stored credentials

    @property
    def auth_enabled(self) -> bool:
        return bool(self.oidc_issuer)

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
        project_dir = Path(project).expanduser().resolve()
        ssh_config = os.environ.get("ANSIBLE_GUI_SSH_CONFIG")
        issuer = os.environ.get("ANSIBLE_GUI_OIDC_ISSUER") or None
        if issuer and not (os.environ.get("ANSIBLE_GUI_OIDC_CLIENT_ID") and _env_or_file("ANSIBLE_GUI_OIDC_CLIENT_SECRET")):
            raise SystemExit("ANSIBLE_GUI_OIDC_ISSUER is set, so ANSIBLE_GUI_OIDC_CLIENT_ID and "
                             "ANSIBLE_GUI_OIDC_CLIENT_SECRET (or _FILE) are needed too.")
        groups = [g.strip() for g in os.environ.get("ANSIBLE_GUI_OIDC_GROUPS", "").split(",") if g.strip()]
        if issuer and not groups:
            raise SystemExit("Set ANSIBLE_GUI_OIDC_GROUPS to the groups allowed to use ansible-gui "
                             "(comma-separated), or to * to allow anyone your OIDC provider signs in.")
        return cls(
            project_dir=project_dir,
            inventory=inventory,
            playbook_dir=os.environ.get("ANSIBLE_GUI_PLAYBOOK_DIR", "."),
            vault_password_file=Path(vault).expanduser() if vault else None,
            edit_url=os.environ.get("ANSIBLE_GUI_EDIT_URL") or None,
            data_dir=Path(os.environ.get("ANSIBLE_GUI_DATA_DIR", "data")).expanduser().resolve(),
            ssh_config=(project_dir / Path(ssh_config).expanduser()).resolve() if ssh_config else None,
            public_url=(os.environ.get("ANSIBLE_GUI_PUBLIC_URL") or "").rstrip("/") or None,
            oidc_issuer=issuer.rstrip("/") if issuer else None,
            oidc_client_id=os.environ.get("ANSIBLE_GUI_OIDC_CLIENT_ID") or None,
            oidc_client_secret=_env_or_file("ANSIBLE_GUI_OIDC_CLIENT_SECRET"),
            oidc_groups=groups,
            secret_key=_env_or_file("ANSIBLE_GUI_SECRET_KEY"),
        )

    def cli_options(self) -> CliOptions:
        return CliOptions(
            project_dir=self.project_dir,
            sources=self.inventory,
            playbook_dir=self.playbook_dir,
            vault_password_file=self.vault_password_file,
            binary=self.inventory_binary,
        )


def _env_or_file(name: str) -> str | None:
    """NAME, or the contents of the file NAME_FILE points at (for Docker secrets)."""
    if os.environ.get(name):
        return os.environ[name]
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).expanduser().read_text().strip() or None
    return None
