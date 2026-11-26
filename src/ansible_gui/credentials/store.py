"""Credentials, encrypted at rest with the master key. Values are write-only in the UI."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..runs.store import now
from . import ssh
from ..db import Database

log = logging.getLogger(__name__)

KINDS = {
    "account": "Account",
    "ssh_key": "SSH key",
    "vault_password": "Vault password",
    "env_secret": "Environment secret",
    "git_token": "Git token",
}
# Kinds a run picks one of. It also picks any number of environment secrets, and gets no other.
PER_RUN = ("account", "vault_password")
# An account is who a run signs in as: a user, one of the saved SSH keys, a become password. An SSH key holds
# what goes with the key: its certificate, or the ssoossh enrollment that fetches one. It signs in for the
# accounts that pick it and for the repositories pulled over SSH with it.
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$", re.I)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # a template's or a schedule's name
# A credential's name is only ever shown (the launcher, the run history), never part of a file name or a
# command, so it may have spaces. An environment secret's variable name is another field, held to ENV_RE.
CRED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
# What `ssoossh --server` takes: a host (https assumed), with a port and a path if need be.
SSOOSSH_RE = re.compile(r"^(https://)?[A-Za-z0-9]([A-Za-z0-9.-]{0,252})(:\d{1,5})?(/[\w./~-]*)?$", re.I)
RESERVED_ENV = {"PATH", "HOME", "USER", "SHELL", "PWD", "TMPDIR", "LANG", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
                "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"}

SCHEMA = """
create table if not exists credentials (
  id integer primary key autoincrement,
  name text not null unique,
  kind text not null,
  is_default integer not null default 0,
  meta text not null default '{}',
  secret blob not null,
  created_at text not null,
  updated_at text not null,
  updated_by text not null default ''
);
"""


class Invalid(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def picked_ids(credentials: dict) -> set[int]:
    """Every credential a template or schedule picked, from its `credentials`:
    {"account": 3, "vault_password": 5, "env_secret": [4, 7]}."""
    out: set[int] = set()
    for value in (credentials or {}).values():
        out.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, int))
    return out


def with_earlier_env_secrets(stored: str, ids: list[int]) -> str | None:
    """A template's or schedule's stored credentials, given the environment secrets it got before they were
    picked per run: every one there is (`ids`). None when it already lists its own, even an empty list.

    Until then every run got them all, so one saved earlier has no list. Giving it none would take a token
    away from a schedule that runs unattended; it keeps what it had, to be unticked where it isn't needed."""
    picked = json.loads(stored or "{}")
    if "env_secret" in picked:
        return None
    return json.dumps({**picked, "env_secret": sorted(ids)})


def env_secret_ids(form: dict) -> list[int]:
    """The environment secrets a form ticked, as a template or schedule stores them."""
    return sorted({int(v) for v in form.get("env_secret") or [] if str(v).isdigit()})


@dataclass
class Credential:
    id: int
    name: str
    kind: str
    is_default: bool
    meta: dict
    created_at: datetime
    updated_at: datetime
    updated_by: str
    readable: bool = True  # False when the master key changed since it was stored
    _secret: bytes = field(default=b"", repr=False)

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]


class CredentialStore:
    def __init__(self, data_dir: Path, fernet: Fernet):
        self._fernet = fernet
        self._db = Database(data_dir / "ansible-gui.db")
        self._lock = self._db.lock
        self._db.executescript(SCHEMA)
        self._keys_out_of_accounts()

    def _keys_out_of_accounts(self) -> None:
        """Accounts once held a key of their own, with its certificate or ssoossh enrollment. Each such key
        becomes an SSH key the account picks: the saved one that is the same key with the same certificate
        arrangement, else a new one named after the account. A saved key with another arrangement is left
        as it is: a repository's deploy key must not start asking ssoossh for certificates because an
        account that copied it did. An account that can't be decrypted is left alone.

        A key was also once offered "Pick this by default in the launcher", which never picked one: that
        mark goes too."""
        with self._lock:
            self._db.execute("update credentials set is_default=0 where kind='ssh_key' and is_default=1")
            self._db.commit()

        def arrangement(meta: dict, secret: dict):
            if meta.get("ssoossh"):
                return "ssoossh", meta["ssoossh"].get("server"), secret.get("ssoossh_code")
            return ("certificate", meta["certificate"].strip()) if meta.get("certificate") else None

        def same_key(key: Credential, acct: Credential, secret: dict) -> bool:
            if key.meta.get("fingerprint") and acct.meta.get("fingerprint"):
                return key.meta["fingerprint"] == acct.meta["fingerprint"]  # the key itself, however it was pasted
            return self.secret(key).get("private_key") == secret["private_key"]

        for acct in self.by_kind("account"):
            if not acct.readable:
                continue
            secret = self.secret(acct)
            if "private_key" not in secret:
                continue
            same = next((k for k in self.by_kind("ssh_key")
                         if k.readable and same_key(k, acct, secret)
                         and arrangement(k.meta, self.secret(k)) == arrangement(acct.meta, secret)), None)
            key_meta = {k: acct.meta[k] for k in ("type", "fingerprint", "comment", "public_key", "certificate",
                                                  "cert", "ssoossh") if k in acct.meta}
            key_secret = {k: secret[k] for k in ("private_key", "ssoossh_code") if k in secret}
            rest = {k: secret[k] for k in ("become_password",) if k in secret}
            with self._lock:  # the new key and the account that picks it, or neither
                if same:
                    key_id, key_name = same.id, same.name
                else:
                    key_name = self._free_name(f"{acct.name[:60]} key")
                    key_id = self._db.execute(
                        "insert into credentials (name, kind, is_default, meta, secret, created_at, updated_at,"
                        " updated_by) values (?, 'ssh_key', 0, ?, ?, ?, ?, ?)",
                        (key_name, json.dumps(key_meta), self._fernet.encrypt(json.dumps(key_secret).encode()),
                         now(), now(), "upgrade")).lastrowid
                meta = {"username": acct.meta.get("username", ""), "ssh_key_id": key_id,
                        "has_become": "become_password" in rest}
                self._db.execute("update credentials set meta=?, secret=? where id=?",
                                 (json.dumps(meta), self._fernet.encrypt(json.dumps(rest).encode()), acct.id))
                self._db.commit()
            log.warning("The account %s held its own SSH key: it is now the SSH key %s, which the account picks.",
                        acct.name, key_name)

    def _free_name(self, wanted: str) -> str:
        name, n = wanted, 1
        while self._db.execute("select 1 from credentials where lower(name)=lower(?)", (name,)).fetchone():
            n += 1
            name = f"{wanted[:64 - len(str(n)) - 1]} {n}"
        return name

    def _row(self, r: sqlite3.Row) -> Credential:
        c = Credential(id=r["id"], name=r["name"], kind=r["kind"], is_default=bool(r["is_default"]),
                       meta=json.loads(r["meta"]), created_at=datetime.fromisoformat(r["created_at"]),
                       updated_at=datetime.fromisoformat(r["updated_at"]), updated_by=r["updated_by"],
                       _secret=r["secret"])
        try:
            self._fernet.decrypt(c._secret)
        except InvalidToken:
            c.readable = False
        return c

    def all(self) -> list[Credential]:
        return [self._row(r) for r in self._db.execute("select * from credentials order by kind, lower(name)")]

    def get(self, cred_id: int) -> Credential | None:
        r = self._db.execute("select * from credentials where id=?", (cred_id,)).fetchone()
        return self._row(r) if r else None

    def by_kind(self, kind: str) -> list[Credential]:
        return [c for c in self.all() if c.kind == kind]

    def default(self, kind: str) -> Credential | None:
        return next((c for c in self.by_kind(kind) if c.is_default), None)

    def key_of(self, account: Credential) -> Credential | None:
        """The SSH key an account signs in with: None if it picked none, or the one it picked is gone."""
        key_id = account.meta.get("ssh_key_id")
        key = self.get(key_id) if isinstance(key_id, int) else None
        return key if key and key.kind == "ssh_key" else None

    def accounts_using(self, key_id: int) -> list[Credential]:
        return [a for a in self.by_kind("account") if a.meta.get("ssh_key_id") == key_id]

    @property
    def fernet(self) -> Fernet:
        """The cipher credentials are stored with; run variables marked secret use it too."""
        return self._fernet

    def secret(self, cred: Credential) -> dict:
        """The decrypted value. Only the executor calls this, right before a run."""
        return json.loads(self._fernet.decrypt(cred._secret))

    # --- writes ------------------------------------------------------------------
    def save(self, *, kind: str, name: str, values: dict, is_default: bool, user: str,
             cred_id: int | None = None) -> Credential:
        """Validate and store. Empty secret fields on an edit keep the stored value."""
        if kind not in KINDS:
            raise Invalid({"kind": "Choose what kind of credential this is."})
        existing = self.get(cred_id) if cred_id else None
        if cred_id and (not existing or existing.kind != kind):
            raise Invalid({"name": "That credential no longer exists."})
        errors: dict[str, str] = {}
        name = " ".join(name.split())  # spaces typed around it or doubled inside it aren't part of the name
        if not CRED_NAME_RE.match(name):
            errors["name"] = ("Use 1–64 letters, digits, spaces, dots, dashes or underscores, starting with a "
                              "letter or digit.")
        else:
            clash = self._db.execute("select id from credentials where lower(name)=lower(?)", (name,)).fetchone()
            if clash and clash["id"] != cred_id:
                errors["name"] = f"There is already a credential called {name}."

        old = self.secret(existing) if existing and existing.readable else {}
        meta = dict(existing.meta) if existing else {}
        secret: dict = {}

        if kind == "ssh_key":
            self._key(values, old, meta, secret, errors, existing)
            self._ssoossh(values, old, meta, secret, errors)
            is_default = False  # picked by an account or a repository, never by the launcher
        elif kind == "account":
            login = values.get("username", "").strip()
            if login and not USER_RE.match(login):
                errors["username"] = "Use a login name like ansible or deploy: letters, digits, dot, dash, underscore."
            meta = {"username": login}  # nothing of a key: the account names a saved one
            picked = str(values.get("ssh_key_id") or "").strip()
            if picked:
                key = self.get(int(picked)) if picked.isdigit() else None
                if key and key.kind == "ssh_key":
                    meta["ssh_key_id"] = key.id
                else:
                    errors["ssh_key_id"] = "That SSH key no longer exists. Choose another."
            pw = values.get("become_password", "")
            if values.get("remove_become"):
                pass
            elif pw:
                secret["become_password"] = pw
            elif old.get("become_password"):
                secret["become_password"] = old["become_password"]
            meta["has_become"] = "become_password" in secret
            if not (login or "ssh_key_id" in meta or meta["has_become"]) and not errors:
                errors["username"] = "Give the account at least a user, an SSH key or a become password."
        elif kind == "vault_password":
            pw = values.get("password", "")
            if pw:
                secret["password"] = pw
            elif old.get("password"):
                secret["password"] = old["password"]
            else:
                errors["password"] = "Enter the password."
        elif kind == "env_secret":
            env = values.get("env", "").strip().upper()  # the form capitalises as you type; this catches the rest
            if not ENV_RE.match(env):
                errors["env"] = "Use capital letters, digits and underscores, like ENROLL_TOKEN."
            elif env in RESERVED_ENV or env.startswith(("ANSIBLE_", "RUNNER_")):
                errors["env"] = f"{env} is used by ansible-gui or Ansible itself. Pick another name."
            meta["env"] = env
            value = values.get("value", "")
            if value:
                secret["value"] = value
            elif old.get("value"):
                secret["value"] = old["value"]
            else:
                errors["value"] = "Enter the value."
            is_default = False

        elif kind == "git_token":
            git_user = values.get("username", "").strip()  # not `user`: that is who is saving this
            if not re.match(r"^[\w.@+-]{1,128}$", git_user):
                errors["username"] = "Enter the user name the git server expects with this token."
            meta["username"] = git_user
            token = values.get("token", "").strip()
            if token:
                secret["token"] = token
            elif old.get("token"):
                secret["token"] = old["token"]
            else:
                errors["token"] = "Paste the token."
            is_default = False

        if errors:
            raise Invalid(errors)

        blob = self._fernet.encrypt(json.dumps(secret).encode())
        with self._lock:
            if is_default:
                self._db.execute("update credentials set is_default=0 where kind=?", (kind,))
            if existing:
                self._db.execute(
                    "update credentials set name=?, is_default=?, meta=?, secret=?, updated_at=?, updated_by=? where id=?",
                    (name, int(is_default), json.dumps(meta), blob, now(), user, existing.id))
                new_id = existing.id
            else:
                cur = self._db.execute(
                    "insert into credentials (name, kind, is_default, meta, secret, created_at, updated_at, updated_by)"
                    " values (?,?,?,?,?,?,?,?)",
                    (name, kind, int(is_default), json.dumps(meta), blob, now(), now(), user))
                new_id = cur.lastrowid
            self._db.commit()
        return self.get(new_id)

    @staticmethod
    def _key(values, old, meta, secret, errors, existing) -> None:
        """An SSH key's private key and its optional certificate."""
        key_text = ssh.normalize(values.get("private_key", ""))
        if key_text:
            try:
                info = ssh.inspect_key(key_text)
                meta.update(type=info.type, fingerprint=info.fingerprint, comment=info.comment,
                            public_key=info.public_key)
                secret["private_key"] = key_text
            except ssh.InvalidKey as e:
                errors["private_key"] = str(e)
        elif old.get("private_key"):
            secret["private_key"] = old["private_key"]
        else:
            errors["private_key"] = "Paste the private key."
        cert_text = values.get("certificate", "").strip()
        if values.get("remove_certificate"):
            meta.pop("certificate", None)
            meta.pop("cert", None)
        elif cert_text and "private_key" not in secret:
            if "private_key" not in errors:
                errors["certificate"] = "Add the private key this certificate is for."
        elif cert_text and "private_key" not in errors:
            try:
                key = ssh.KeyInfo(public_key=meta.get("public_key", ""), fingerprint=meta.get("fingerprint", ""),
                                  type=meta.get("type", ""))
                cert = ssh.inspect_cert(cert_text, key)
                meta["certificate"] = cert_text
                meta["cert"] = {"key_id": cert.key_id, "principals": cert.principals, "valid": cert.valid}
            except ssh.InvalidKey as e:
                errors["certificate"] = str(e)
        elif cert_text == "" and existing and "private_key" in secret and secret["private_key"] != old.get("private_key"):
            # A new key makes the old certificate useless.
            meta.pop("certificate", None)
            meta.pop("cert", None)

    @staticmethod
    def _ssoossh(values, old, meta, secret, errors) -> None:
        """An SSH key's certificate fetched from ssoossh whenever the key signs in (a run, a pull), with the
        enrollment code `ssoossh service enroll` printed for this key. It takes the place of a pasted
        certificate. An empty server stops it."""
        server = values.get("ssoossh_server", "").strip().rstrip("/")
        code = values.get("ssoossh_code", "").strip()
        if not (server or code):
            meta.pop("ssoossh", None)
            return
        key_changed = secret.get("private_key") != old.get("private_key")
        kept = old.get("ssoossh_code") if not key_changed else None  # an enrollment is for one key
        if not server:
            errors["ssoossh_server"] = "Enter the ssoossh server the enrollment code is from."
        elif server.lower().startswith("http://"):
            errors["ssoossh_server"] = "Use https: the enrollment code is sent to this server."
        elif not SSOOSSH_RE.match(server):
            errors["ssoossh_server"] = "Enter the server’s address, like https://ssoossh.example.com."
        if not (code or kept):
            errors["ssoossh_code"] = ("Paste the enrollment code again: it was for the key saved here before."
                                      if old.get("ssoossh_code") else "Paste the enrollment code.")
        if values.get("certificate", "").strip() and "certificate" not in errors:
            errors["certificate"] = "Paste a certificate or fetch one from ssoossh, not both."
        if not errors:
            meta["ssoossh"] = {"server": server}
            secret["ssoossh_code"] = code or kept
            meta.pop("certificate", None)  # ssoossh's replaces it at every run
            meta.pop("cert", None)

    def delete(self, cred_id: int) -> None:
        with self._lock:
            self._db.execute("delete from credentials where id=?", (cred_id,))
            self._db.commit()
