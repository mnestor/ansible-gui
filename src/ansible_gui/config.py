"""Settings, all from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

HOST_KEY_CHECKING = ("yes", "accept-new", "no")  # OpenSSH's StrictHostKeyChecking values
# The names this machine has for itself. A request addressed to any other name is answered only if
# ANSIBLE_GUI_PUBLIC_URL names it (Settings.allowed_hosts).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def host_name(header: str) -> str:
    """The host in a Host header, without its port: example.com:8080, 127.0.0.1, [::1]:8080."""
    host = header.strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0]


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
    # StrictHostKeyChecking for runs and git pulls when the ssh_config doesn't set it: yes only connects to hosts
    # whose key is already in known_hosts; accept-new trusts a new host's first answer; no trusts any answer.
    ssh_host_key_checking: str = "accept-new"
    run_retention_days: int = 90  # finished runs older than this are deleted; 0 keeps them forever
    run_timeout_minutes: int = 120  # a run still going after this long is stopped; 0 lets it run forever

    @property
    def auth_enabled(self) -> bool:
        return bool(self.oidc_issuer)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """The names a request may be addressed to (its Host header): this machine's own, and the public URL's.

        A web page anywhere can point a DNS name of its own at 127.0.0.1 or a LAN address and then talk to
        whatever listens there as if it were its own server (DNS rebinding): the browser sees one origin, so
        the same-origin check on writes passes and the page can read every answer. The Host header still
        carries the page's name, which is what gives it away."""
        public = urlsplit(self.public_url).hostname if self.public_url else None
        return LOOPBACK_HOSTS | {public} if public else LOOPBACK_HOSTS

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
        edit = os.environ.get("ANSIBLE_GUI_EDIT_URL") or None
        if edit and not is_web_url(edit):
            raise SystemExit("ANSIBLE_GUI_EDIT_URL must start with https:// or http://.")
        public = (os.environ.get("ANSIBLE_GUI_PUBLIC_URL") or "").strip().rstrip("/") or None
        if public and not (is_web_url(public) and urlsplit(public).hostname):
            raise SystemExit("ANSIBLE_GUI_PUBLIC_URL must be the address people use, starting with https:// or "
                             "http://, like https://ansible.example.com.")
        if issuer and not public:
            # Otherwise both would be worked out per request: the address the provider sends people back to,
            # and whether the session cookie is marked Secure (it is when this starts with https://).
            raise SystemExit("ANSIBLE_GUI_OIDC_ISSUER is set, so ANSIBLE_GUI_PUBLIC_URL is needed too: the address "
                             "people use, like https://ansible.example.com. The provider sends them back to "
                             "<that address>/auth/callback.")
        host_keys = os.environ.get("ANSIBLE_GUI_SSH_HOST_KEY_CHECKING", "accept-new").strip().lower()
        if host_keys not in HOST_KEY_CHECKING:
            raise SystemExit("ANSIBLE_GUI_SSH_HOST_KEY_CHECKING must be yes, accept-new or no.")
        retention = os.environ.get("ANSIBLE_GUI_RUN_RETENTION_DAYS", "90").strip()
        if not retention.isdigit():
            raise SystemExit("ANSIBLE_GUI_RUN_RETENTION_DAYS must be a whole number of days (0 keeps runs forever).")
        run_timeout = os.environ.get("ANSIBLE_GUI_RUN_TIMEOUT_MINUTES", "120").strip()
        if not run_timeout.isdigit():
            raise SystemExit("ANSIBLE_GUI_RUN_TIMEOUT_MINUTES must be a whole number of minutes (0 for no limit).")
        return cls(
            project_dir=project_dir,
            inventory=inventory,
            playbook_dir=os.environ.get("ANSIBLE_GUI_PLAYBOOK_DIR", "."),
            vault_password_file=Path(vault).expanduser() if vault else None,
            edit_url=edit,
            data_dir=Path(os.environ.get("ANSIBLE_GUI_DATA_DIR", "data")).expanduser().resolve(),
            ssh_config=((project_dir or Path("/")) / Path(ssh_config).expanduser()).resolve() if ssh_config else None,
            public_url=public,
            oidc_issuer=issuer.rstrip("/") if issuer else None,
            oidc_client_id=os.environ.get("ANSIBLE_GUI_OIDC_CLIENT_ID") or None,
            oidc_client_secret=_env_or_file("ANSIBLE_GUI_OIDC_CLIENT_SECRET"),
            oidc_groups=groups,
            secret_key=_env_or_file("ANSIBLE_GUI_SECRET_KEY"),
            smtp=SmtpSettings.from_env(),
            ssh_host_key_checking=host_keys,
            run_retention_days=int(retention),
            run_timeout_minutes=int(run_timeout),
        )


def is_web_url(url: str) -> bool:
    """Only http(s) links are ever rendered as links: a javascript: URL would run in whoever clicks it."""
    return url.lower().startswith(("https://", "http://"))


def scrub_environment() -> None:
    """Remove every ANSIBLE_GUI_* variable once the settings are read.

    Every program ansible-gui starts (ansible-playbook, ansible-inventory, ansible-galaxy, git, ssh)
    inherits this process's environment, and ansible-runner also writes it to disk. Without this, any
    playbook, inventory plugin or collection could read the OIDC client secret, the SMTP password or the
    master key with lookup('env', ...), and a debug task would put them in the run log and the email."""
    for name in [k for k in os.environ if k.startswith("ANSIBLE_GUI_")]:
        del os.environ[name]


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
