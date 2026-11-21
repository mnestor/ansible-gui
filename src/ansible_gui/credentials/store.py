"""Credentials, encrypted at rest with the master key. Values are write-only in the UI."""

from __future__ import annotations

import json
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

KINDS = {
    "account": "Account",
    "ssh_key": "SSH key",
    "vault_password": "Vault password",
    "env_secret": "Environment secret",
    "git_token": "Git token",
}
# Kinds a run picks one of. Environment secrets go to every run.
PER_RUN = ("account", "vault_password")
# An account is who a run signs in as: a user, an SSH key (and certificate), a become password. A separate
# SSH key is a repository's deploy key; an account can copy one in.
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$", re.I)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
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
        name = name.strip()
        if not NAME_RE.match(name):
            errors["name"] = "Use 1–64 letters, digits, dots, dashes or underscores, starting with a letter or digit."
        else:
            clash = self._db.execute("select id from credentials where lower(name)=lower(?)", (name,)).fetchone()
            if clash and clash["id"] != cred_id:
                errors["name"] = f"There is already a credential called {name}."

        old = self.secret(existing) if existing and existing.readable else {}
        meta = dict(existing.meta) if existing else {}
        secret: dict = {}

        if kind == "ssh_key":
            self._key(values, old, meta, secret, errors, existing, required=True)
        elif kind == "account":
            login = values.get("username", "").strip()
            if login and not USER_RE.match(login):
                errors["username"] = "Use a login name like ansible or deploy: letters, digits, dot, dash, underscore."
            meta["username"] = login
            if values.get("remove_key"):
                for k in ("type", "fingerprint", "comment", "public_key", "certificate", "cert"):
                    meta.pop(k, None)
                old = {k: v for k, v in old.items() if k != "private_key"}
            self._key(values, old, meta, secret, errors, existing, required=False)
            pw = values.get("become_password", "")
            if values.get("remove_become"):
                pass
            elif pw:
                secret["become_password"] = pw
            elif old.get("become_password"):
                secret["become_password"] = old["become_password"]
            meta["has_key"] = "private_key" in secret
            meta["has_become"] = "become_password" in secret
            if not (login or meta["has_key"] or meta["has_become"]) and not errors:
                errors["username"] = "Give the account at least a user, a key or a become password."
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
            user = values.get("username", "").strip()
            if not re.match(r"^[\w.@+-]{1,128}$", user):
                errors["username"] = "Enter the user name the git server expects with this token."
            meta["username"] = user
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
    def _key(values, old, meta, secret, errors, existing, required: bool) -> None:
        """An SSH private key and its optional certificate, for an SSH key or an account."""
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
        elif required:
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

    def delete(self, cred_id: int) -> None:
        with self._lock:
            self._db.execute("delete from credentials where id=?", (cred_id,))
            self._db.commit()
