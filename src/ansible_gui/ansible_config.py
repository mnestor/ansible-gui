"""Which Ansible settings a repository's runs get, and where each one comes from.

Asks Ansible itself, the way a run starts it (the repository's run directory, the same ANSIBLE_* environment):
`ansible-config dump --only-changed` names the ansible.cfg or environment variable behind every setting that
isn't a default, and `ansible-galaxy collection list` shows which copy of each collection is found first.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 60
# Values that are credentials, not configuration: ansible-config prints a Galaxy server's token as is.
SECRET = re.compile(r"token|password|passwd|secret|private_key|api_key", re.I)
NOT_SECRET = re.compile(r"_file$|_path$|^vault_identity", re.I)


def is_secret(name: str) -> bool:
    return bool(SECRET.search(name)) and not NOT_SECRET.search(name)


@dataclass
class Setting:
    name: str
    value: str
    origin: str  # "ansible.cfg" (relative to the repository when inside it), "env: ANSIBLE_X", or a path
    hidden: bool = False

    @property
    def from_env(self) -> bool:
        return self.origin.startswith("env:")


@dataclass
class Collection:
    name: str
    version: str
    path: str
    used: bool = True  # False when an earlier path has the same collection: Ansible loads that one
    shadowed_by: str = ""


@dataclass
class Report:
    config_file: str | None = None
    settings: list[Setting] = field(default_factory=list)
    galaxy: dict[str, list[Setting]] = field(default_factory=dict)
    collections: list[Collection] = field(default_factory=list)
    error: str | None = None
    collections_error: str | None = None


def _render(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return "" if value is None else str(value)


def _origin(origin: str, root: Path) -> str:
    if not origin or origin.startswith("env:"):
        return origin
    try:
        return str(Path(origin).resolve().relative_to(root.resolve()))
    except ValueError:
        return origin


def _run(cmd: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # No ANSIBLE_NOCOLOR here: the JSON has no colour, and it would show up as a setting the repository made.
    return subprocess.run(cmd, cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True, timeout=TIMEOUT)


def _failure(cmd: str, proc: subprocess.CompletedProcess | None, exc: Exception | None = None) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"{cmd} isn’t installed on this server."
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{cmd} took longer than {TIMEOUT}s."
    lines = [ln for ln in (proc.stderr if proc else "").splitlines() if ln.strip()]
    return f"{cmd} failed: {lines[-1] if lines else 'exit code ' + str(proc.returncode if proc else '?')}"


def report(run_dir: Path, repo_root: Path, env: dict) -> Report:
    out = Report()
    try:
        proc = _run(["ansible-config", "dump", "--only-changed", "--format", "json"], run_dir, env)
        if proc.returncode != 0:
            out.error = _failure("ansible-config", proc)
        else:
            for item in json.loads(proc.stdout):
                if "GALAXY_SERVERS" in item:
                    for server, entries in item["GALAXY_SERVERS"].items():
                        out.galaxy[server] = [
                            Setting(e["name"], "" if is_secret(e["name"]) else _render(e.get("value")),
                                    _origin(e.get("origin", ""), repo_root), hidden=is_secret(e["name"]))
                            for e in entries]
                elif item.get("name") == "CONFIG_FILE":
                    out.config_file = _origin(_render(item.get("value")), repo_root) or None
                elif "name" in item:
                    hidden = is_secret(item["name"])
                    out.settings.append(Setting(item["name"], "" if hidden else _render(item.get("value")),
                                                _origin(item.get("origin", ""), repo_root), hidden=hidden))
    except (OSError, subprocess.TimeoutExpired) as e:
        out.error = _failure("ansible-config", None, e)
    except (ValueError, TypeError, KeyError):
        out.error = "ansible-config didn’t return the JSON this page reads."

    try:
        proc = _run(["ansible-galaxy", "collection", "list", "--format", "json"], run_dir, env)
        if proc.returncode != 0:
            out.collections_error = _failure("ansible-galaxy", proc)
        else:
            seen: dict[str, str] = {}
            # Paths come in search order, so the first copy of a collection is the one Ansible loads.
            for path, found in json.loads(proc.stdout or "{}").items():
                for name, meta in sorted(found.items()):
                    if name.startswith("ansible._"):  # ansible-core's own internals
                        continue
                    c = Collection(name, str((meta or {}).get("version", "?")), _origin(path, repo_root))
                    if name in seen:
                        c.used, c.shadowed_by = False, seen[name]
                    else:
                        seen[name] = c.path
                    out.collections.append(c)
    except (OSError, subprocess.TimeoutExpired) as e:
        out.collections_error = _failure("ansible-galaxy", None, e)
    except (ValueError, TypeError, AttributeError):
        out.collections_error = "ansible-galaxy didn’t return the JSON this page reads."
    return out
