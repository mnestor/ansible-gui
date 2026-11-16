"""OpenSSH helpers: check a pasted key and certificate, write the run's ssh_config, run an agent.

Everything goes through ssh-keygen, ssh-agent and ssh-add, so the keys and
certificates accepted are exactly the ones OpenSSH accepts.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class InvalidKey(ValueError):
    """A key or certificate OpenSSH would not use. The message is shown to the user."""


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text + "\n" if text else ""


def _keygen(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh-keygen", *args], input=stdin, capture_output=True, text=True, timeout=15)


@dataclass
class KeyInfo:
    public_key: str
    fingerprint: str
    type: str
    comment: str = ""


@dataclass
class CertInfo:
    type: str  # "user" or "host"
    key_id: str = ""
    principals: list[str] = field(default_factory=list)
    valid: str = ""
    fingerprint: str = ""  # of the key the certificate certifies


def inspect_key(private_key: str) -> KeyInfo:
    """The public half and fingerprint of an unencrypted private key, or InvalidKey."""
    if "PRIVATE KEY" not in private_key:
        raise InvalidKey("That doesn’t look like a private key. Paste the whole file, including the "
                        "-----BEGIN … PRIVATE KEY----- line.")
    with tempfile.TemporaryDirectory(prefix="agui-") as d:
        path = Path(d) / "key"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key)
        pub = _keygen("-y", "-P", "", "-f", str(path))
        if pub.returncode != 0:
            if "passphrase" in pub.stderr.lower():
                raise InvalidKey("This key has a passphrase. Runs are unattended, so store a key without one: "
                                "ssh-keygen -p -N \"\" -f <copy of the key>.")
            raise InvalidKey(f"ssh-keygen couldn’t read the key: {pub.stderr.strip() or 'unknown error'}")
        public = pub.stdout.strip()
        (Path(d) / "key.pub").write_text(public + "\n")
        fp = _keygen("-l", "-f", str(Path(d) / "key.pub"))
    # "256 SHA256:abc… comment (ED25519)"
    m = re.match(r"\d+\s+(\S+)\s*(.*?)\s*\((\w+)\)\s*$", fp.stdout.strip())
    if not m:
        raise InvalidKey("ssh-keygen couldn’t fingerprint the key.")
    return KeyInfo(public_key=public, fingerprint=m.group(1), comment=m.group(2), type=m.group(3))


def inspect_cert(cert: str, key: KeyInfo | None = None) -> CertInfo:
    """Parse an OpenSSH certificate; with `key`, require that it certifies that key."""
    line = cert.strip()
    if "-cert-v01@openssh.com" not in line.split(" ", 1)[0]:
        raise InvalidKey("That isn’t an OpenSSH certificate. It’s the one-line file ending in -cert.pub.")
    with tempfile.TemporaryDirectory(prefix="agui-") as d:
        path = Path(d) / "key-cert.pub"
        path.write_text(line + "\n")
        out = _keygen("-L", "-f", str(path))
    if out.returncode != 0:
        raise InvalidKey(f"ssh-keygen couldn’t read the certificate: {out.stderr.strip()}")
    info = CertInfo(type="user")
    lines = out.stdout.splitlines()
    for i, raw in enumerate(lines):
        text = raw.strip()
        if text.startswith("Type:"):
            info.type = "host" if " host certificate" in text else "user"
        elif text.startswith("Public key:"):
            info.fingerprint = text.split()[-1]
        elif text.startswith("Key ID:"):
            info.key_id = text.split(":", 1)[1].strip().strip('"')
        elif text.startswith("Valid:"):
            info.valid = text.split(":", 1)[1].strip()
        elif text.startswith("Principals:"):
            for nxt in lines[i + 1:]:
                if not nxt.startswith("    ") or ":" in nxt.strip():
                    break
                if nxt.strip() != "(none)":
                    info.principals.append(nxt.strip())
    if info.type != "user":
        raise InvalidKey("This is a host certificate. Runs need a user certificate.")
    if key and info.fingerprint and info.fingerprint != key.fingerprint:
        raise InvalidKey(f"This certificate is for a different key ({info.fingerprint}), "
                        f"not this one ({key.fingerprint}).")
    return info


def write_ssh_config(data_dir: Path, user_config: Path | None) -> Path:
    """The ssh_config every run uses: the configured file first, then ansible-gui's fallbacks.

    ssh keeps the first value it reads for each option, so anything in the configured
    file wins; the fallbacks only fill gaps. Known hosts live in the data directory,
    never in the server user's ~/.ssh.
    """
    d = data_dir / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    lines = ["# Written by ansible-gui before every run. Edit the file it includes instead.", ""]
    if user_config:
        lines.append(f'Include "{user_config}"')
        lines.append("")
    lines += [
        "Host *",
        f'  UserKnownHostsFile "{d / "known_hosts"}"',
        "  StrictHostKeyChecking accept-new",
        "",
    ]
    path = d / "ssh_config"
    path.write_text("\n".join(lines))
    return path


def known_hosts_count(data_dir: Path) -> int:
    path = data_dir / "ssh" / "known_hosts"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))


class Agent:
    """A private ssh-agent for one run, so a run never sees the server user's agent."""

    def __init__(self, workdir: Path):
        self.socket = workdir / "agent.sock"
        out = subprocess.run(["ssh-agent", "-s", "-a", str(self.socket)], capture_output=True, text=True, timeout=10)
        m = re.search(r"SSH_AGENT_PID=(\d+)", out.stdout)
        if out.returncode != 0 or not m:
            raise RuntimeError(f"ssh-agent did not start: {out.stderr.strip()}")
        self.pid = int(m.group(1))

    def add(self, key_path: Path) -> None:
        # ssh-add picks up key_path + "-cert.pub" by itself.
        out = subprocess.run(["ssh-add", str(key_path)], env={**os.environ, "SSH_AUTH_SOCK": str(self.socket)},
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            raise RuntimeError(f"ssh-add failed: {out.stderr.strip()}")

    def stop(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
