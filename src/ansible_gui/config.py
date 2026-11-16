"""Settings, all from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path



@dataclass
class Settings:
    project_dir: Path | None = None  # a local checkout; git repositories configured in the UI replace it
    inventory: list[str] = field(default_factory=lambda: ["inventory"])
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
    smtp: "SmtpSettings | None" = None

    @property
    def auth_enabled(self) -> bool:
        return bool(self.oidc_issuer)

    @classmethod
    def from_env(cls) -> "Settings":
        project = os.environ.get("ANSIBLE_GUI_PROJECT_DIR")
        inventory = [s.strip() for s in os.environ.get("ANSIBLE_GUI_INVENTORY", "inventory").split(",") if s.strip()]
        vault = os.environ.get("ANSIBLE_GUI_VAULT_PASSWORD_FILE")
        project_dir = Path(project).expanduser().resolve() if project else None
        ssh_config = os.environ.get("ANSIBLE_GUI_SSH_CONFIG")
        if ssh_config and not project_dir and not Path(ssh_config).expanduser().is_absolute():
            raise SystemExit("ANSIBLE_GUI_SSH_CONFIG must be an absolute path when ANSIBLE_GUI_PROJECT_DIR isn’t set.")
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
            ssh_config=((project_dir or Path("/")) / Path(ssh_config).expanduser()).resolve() if ssh_config else None,
            public_url=(os.environ.get("ANSIBLE_GUI_PUBLIC_URL") or "").rstrip("/") or None,
            oidc_issuer=issuer.rstrip("/") if issuer else None,
            oidc_client_id=os.environ.get("ANSIBLE_GUI_OIDC_CLIENT_ID") or None,
            oidc_client_secret=_env_or_file("ANSIBLE_GUI_OIDC_CLIENT_SECRET"),
            oidc_groups=groups,
            secret_key=_env_or_file("ANSIBLE_GUI_SECRET_KEY"),
            smtp=SmtpSettings.from_env(),
        )


def _env_or_file(name: str) -> str | None:
    """NAME, or the contents of the file NAME_FILE points at (for Docker secrets)."""
    if os.environ.get(name):
        return os.environ[name]
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).expanduser().read_text().strip() or None
    return None


@dataclass
class SmtpSettings:
    host: str
    to: list[str]
    sender: str
    port: int = 587
    tls: str = "starttls"  # starttls | ssl | none
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> "SmtpSettings | None":
        host = os.environ.get("ANSIBLE_GUI_SMTP_HOST")
        if not host:
            return None
        to = [a.strip() for a in os.environ.get("ANSIBLE_GUI_NOTIFY_TO", "").split(",") if a.strip()]
        sender = os.environ.get("ANSIBLE_GUI_SMTP_FROM", "")
        tls = os.environ.get("ANSIBLE_GUI_SMTP_TLS", "starttls").lower()
        if not to or not sender:
            raise SystemExit("ANSIBLE_GUI_SMTP_HOST is set, so ANSIBLE_GUI_SMTP_FROM and ANSIBLE_GUI_NOTIFY_TO "
                             "(comma-separated addresses) are needed too.")
        if tls not in ("starttls", "ssl", "none"):
            raise SystemExit("ANSIBLE_GUI_SMTP_TLS must be starttls, ssl or none.")
        default_port = {"starttls": 587, "ssl": 465, "none": 25}[tls]
        return cls(host=host, to=to, sender=sender, tls=tls,
                   port=int(os.environ.get("ANSIBLE_GUI_SMTP_PORT") or default_port),
                   username=os.environ.get("ANSIBLE_GUI_SMTP_USERNAME") or None,
                   password=_env_or_file("ANSIBLE_GUI_SMTP_PASSWORD"))
