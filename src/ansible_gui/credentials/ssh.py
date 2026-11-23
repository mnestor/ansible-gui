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
import threading
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


def ssh_config_text(user_config: Path | None, host_key_checking: str, known_hosts: Path) -> str:
    """The ssh_config every run and git pull uses: the configured file first, then ansible-gui's fallbacks.

    ssh keeps the first value it reads for each option, so anything in the configured
    file wins; the fallbacks only fill gaps. Known hosts are ansible-gui's own, never the
    server user's ~/.ssh. host_key_checking is ANSIBLE_GUI_SSH_HOST_KEY_CHECKING.
    """
    if host_key_checking not in ("yes", "accept-new", "no"):
        raise ValueError(f"StrictHostKeyChecking {host_key_checking!r}")
    lines = ["# Written by ansible-gui before every run. Edit the file it includes instead.", ""]
    if user_config:
        lines.append(f'Include "{user_config}"')
        lines.append("")
    lines += [
        "Host *",
        f'  UserKnownHostsFile "{known_hosts}"',
        f"  StrictHostKeyChecking {host_key_checking}",
        "",
    ]
    return "\n".join(lines)


def write_ssh_config(data_dir: Path, user_config: Path | None, host_key_checking: str = "accept-new") -> Path:
    """ssh_config_text() written to data/ssh/ssh_config, with data/ssh/known_hosts."""
    d = data_dir / "ssh"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / "ssh_config"
    path.write_text(ssh_config_text(user_config, host_key_checking, d / "known_hosts"))
    return path


def config_problem(user_config: Path | None) -> str | None:
    """Why the configured ssh_config can't be used, as "<path> doesn't exist" or "<path> can't be read (why)";
    None when it can, or none is set.

    Nothing may connect while this says something. The job would carry no text, only an Include of a path the
    runner's container doesn't have, and ssh skips an Include that matches no file without a word: every host
    would be reached as if the file were empty, with none of its jump hosts, names or host key rules."""
    if not user_config:
        return None
    try:
        user_config.read_text()
    except FileNotFoundError:
        return f"{user_config} doesn’t exist"
    except OSError as e:
        return f"{user_config} can’t be read ({e.strerror or e})"
    return None


def for_job(data_dir: Path, user_config: Path | None, host_key_checking: str) -> dict:
    """What a job needs to write its own ssh_config: the configured file's text (a worker in another
    container can't read it), the known hosts, and StrictHostKeyChecking."""
    text = None
    if user_config:
        try:
            text = user_config.read_text()
        except OSError:  # config_problem() says why, and keeps runs and SSH pulls from starting
            pass
    return {"config": text, "include": str(user_config) if user_config and text is None else None,
            "known_hosts": read_known_hosts(data_dir), "checking": host_key_checking}


_learn_lock = threading.Lock()
MAX_LEARNED = 1000  # host keys taken from one job; a run that meets more hosts than this learns the rest next time
# A name as ssh writes it for a host it just met: a name or an address, or [name]:port.
_LEARNED_NAME = re.compile(r"^(?:\[[A-Za-z0-9._:%-]+\]:\d{1,5}|[A-Za-z0-9._:%][A-Za-z0-9._:%-]*)$")


def _has_key(known_hosts: Path, name: str) -> bool:
    """Whether known_hosts already lists a key for the name. ssh-keygen does the matching, so hashed names and
    patterns on file count. A @cert-authority line doesn't: a host without a certificate still gets its key."""
    found = _keygen("-F", name, "-f", str(known_hosts))
    return found.returncode == 0 and any(
        line.strip() and not line.startswith(("#", "@")) for line in found.stdout.splitlines())


def learn_host_keys(data_dir: Path, lines: list[str], host_key_checking: str = "accept-new") -> int:
    """Add host keys a job's ssh accepted on first contact to known_hosts. Returns how many were added.

    The lines come back from a worker's temp directory, where the playbook could write as well, so they are
    held to what ssh itself adds for a host it has just met: one key, for names that have none yet. Dropped:
    a marker (@cert-authority would trust a key to vouch for any host), a pattern, a hashed name (it can't be
    told which host it is for), and a key for a host that already has one (ssh would then accept either).
    With StrictHostKeyChecking yes ssh adds nothing, so nothing is taken."""
    if host_key_checking == "yes":
        return 0
    path = known_hosts_path(data_dir)
    new: list[str] = []
    taken: set[str] = set()
    with _learn_lock:
        for line in lines[:MAX_LEARNED]:
            fields = line.split() if isinstance(line, str) and len(line) < 8192 else []
            if len(fields) < 3:
                continue
            names = fields[0].split(",")
            if not all(_LEARNED_NAME.match(n) for n in names) or taken.intersection(names):
                continue
            if _keygen("-l", "-f", "-", stdin=f"{fields[1]} {fields[2]}\n").returncode != 0:
                continue
            if path.exists() and any(_has_key(path, n) for n in names):
                continue
            new.append(" ".join(fields[:3]))
            taken.update(names)
        if new:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            with path.open("a") as f:
                f.write("".join(l + "\n" for l in new))
    return len(new)


def known_hosts_path(data_dir: Path) -> Path:
    return data_dir / "ssh" / "known_hosts"


def known_hosts_count(data_dir: Path) -> int:
    path = known_hosts_path(data_dir)
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip() and not line.startswith("#"))


def read_known_hosts(data_dir: Path) -> str:
    path = known_hosts_path(data_dir)
    return path.read_text(errors="replace") if path.exists() else ""


def check_known_hosts(text: str) -> tuple[str, int]:
    """Normalise pasted known_hosts lines and count the entries, or raise InvalidKey naming the first bad line.

    Takes what ssh-keyscan prints, lines from a ~/.ssh/known_hosts (hashed names too), and
    @cert-authority or @revoked lines. Each key is checked by ssh-keygen."""
    text = normalize(text)
    entries = 0
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0].startswith("@"):
            if fields[0] not in ("@cert-authority", "@revoked"):
                raise InvalidKey(f"Line {n}: {fields[0]} isn’t a known_hosts marker. Use @cert-authority or @revoked.")
            fields = fields[1:]
        if len(fields) < 3:
            raise InvalidKey(f"Line {n} needs the host names, the key type and the key, like the lines "
                             "ssh-keyscan prints: git.example.com ssh-ed25519 AAAA…")
        check = _keygen("-l", "-f", "-", stdin=f"{fields[1]} {fields[2]}\n")
        if check.returncode != 0:
            raise InvalidKey(f"Line {n}: ssh-keygen can’t read the key for {fields[0]}. Paste the whole line.")
        entries += 1
    return text, entries


def save_known_hosts(data_dir: Path, text: str) -> int:
    """Replace the known_hosts runs and pulls check against. Returns the number of entries."""
    text, entries = check_known_hosts(text)
    path = known_hosts_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".new")
    tmp.write_text(text)
    os.replace(tmp, path)
    return entries


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
