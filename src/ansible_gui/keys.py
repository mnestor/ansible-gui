"""The master key: signs session cookies and encrypts stored credentials.

Taken from ANSIBLE_GUI_SECRET_KEY (or _FILE). Without it, one is generated in the
data directory, which means a copy of the data directory is enough to decrypt the
credentials. Set the variable to keep the key out of backups of the data.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)


class Keys:
    def __init__(self, master: bytes, generated_at: Path | None = None):
        self._master = master
        self.generated_at = generated_at  # set when the key lives next to the data

    @classmethod
    def load(cls, configured: str | None, data_dir: Path) -> "Keys":
        if configured:
            if len(configured) < 32:
                raise SystemExit("ANSIBLE_GUI_SECRET_KEY must be at least 32 characters.")
            return cls(configured.encode())
        path = data_dir / "secret.key"
        if not path.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48))
            log.warning("Generated %s. Anyone with a copy of the data directory can decrypt the stored "
                        "credentials; set ANSIBLE_GUI_SECRET_KEY to keep the key elsewhere.", path)
        return cls(path.read_text().strip().encode(), generated_at=path)

    def derive(self, purpose: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode()).derive(self._master)

    @property
    def session_secret(self) -> str:
        return base64.urlsafe_b64encode(self.derive("ansible-gui session")).decode()

    @property
    def fernet(self) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(self.derive("ansible-gui credentials")))
