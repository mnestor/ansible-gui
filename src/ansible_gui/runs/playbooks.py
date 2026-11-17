"""Finds playbooks and asks ansible-playbook which hosts and tags a run would touch."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..inventory.cli import CliOptions, InventoryError


class _AnyTagLoader(yaml.SafeLoader):
    pass


_AnyTagLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _is_playbook(path: Path) -> bool:
    try:
        data = yaml.load(path.read_text(), Loader=_AnyTagLoader)  # noqa: S506 - SafeLoader subclass
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return False
    if not isinstance(data, list) or not data:
        return False
    return all(
        isinstance(play, dict) and ("hosts" in play or "import_playbook" in play or "ansible.builtin.import_playbook" in play)
        for play in data
    )


def discover(project_dir: Path, playbook_dir: str = ".") -> list[str]:
    """Playbooks directly in the playbook directory, relative to project_dir."""
    base = (project_dir / playbook_dir).resolve()
    found = []
    for p in sorted(base.iterdir()):
        if p.is_file() and p.suffix in (".yml", ".yaml") and _is_playbook(p):
            found.append(str(p.relative_to(project_dir.resolve())))
    return found


@dataclass
class Preview:
    hosts: list[str] = field(default_factory=list)
    plays: int = 0
    tags: list[str] = field(default_factory=list)
    error: str | None = None


def _ansible_playbook(opts: CliOptions, playbook: str, args: list[str]) -> str:
    return _cli(opts, ["ansible-playbook"], [*args, playbook])


def _cli(opts: CliOptions, head: list[str], args: list[str]) -> str:
    cmd = list(head)
    for src in opts.sources:
        cmd += ["-i", src]
    if opts.vault_password_file:
        cmd += ["--vault-password-file", str(opts.vault_password_file)]
    cmd += args
    env = {**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0", **opts.env}
    try:
        proc = subprocess.run(cmd, cwd=opts.project_dir, env=env, capture_output=True, text=True, timeout=opts.timeout)
    except FileNotFoundError:
        raise InventoryError(f"{head[0]} is not installed or not on PATH.", cmd) from None
    except subprocess.TimeoutExpired:
        raise InventoryError(f"{head[0]} took longer than {opts.timeout}s.", cmd) from None
    if proc.returncode != 0:
        raise InventoryError(f"{head[0]} exited with code {proc.returncode}.", cmd, proc.stderr or proc.stdout)
    return proc.stdout


def list_hosts(opts: CliOptions, pattern: str) -> Preview:
    """Hosts an ad-hoc command on pattern reaches, as `ansible PATTERN --list-hosts` resolves them,
    with the playbook directory's group_vars and host_vars loaded like a run's."""
    if not pattern.strip() or pattern.lstrip().startswith("-"):
        return Preview(error="Name the hosts or group to gather facts from.")
    try:
        out = _cli(opts, ["ansible", pattern.strip()], ["--list-hosts", "--playbook-dir", opts.playbook_dir])
    except InventoryError as e:
        detail = [line for line in e.stderr.strip().splitlines() if line.strip()]
        return Preview(error=(detail[-1] if detail else str(e)))
    hosts = [line.strip() for line in out.splitlines()[1:] if line.strip()] if _HOSTS_HEADER.match(
        out.splitlines()[0] if out.strip() else "") else []
    return Preview(hosts=hosts, plays=1)


_HOSTS_HEADER = re.compile(r"^\s*hosts \((\d+)\):\s*$")
_TASK_TAGS = re.compile(r"TASK TAGS:\s*\[(.*)\]")


def preview(opts: CliOptions, playbook: str, limit: str) -> Preview:
    """Hosts the run would target (every play, after --limit) and the tags the
    playbook defines. Uses ansible-playbook itself, so patterns like
    'web:!web2' or 'db*' resolve exactly as a real run would."""
    args = ["--list-hosts", "--list-tags"]
    if limit.strip():
        args += ["--limit", limit.strip()]
    try:
        out = _ansible_playbook(opts, playbook, args)
    except InventoryError as e:
        if "no hosts to target" in e.stderr:
            return Preview()
        detail = [l for l in e.stderr.strip().splitlines() if l.strip()]
        return Preview(error=(detail[-1] if detail else str(e)))

    hosts: list[str] = []
    tags: set[str] = set()
    plays = 0
    in_hosts = False
    hosts_indent = 0
    for line in out.splitlines():
        m = _TASK_TAGS.search(line)
        if m:
            tags.update(t.strip() for t in m.group(1).split(",") if t.strip())
            in_hosts = False
            continue
        if _HOSTS_HEADER.match(line):
            plays += 1
            in_hosts = True
            hosts_indent = len(line) - len(line.lstrip())
            continue
        if in_hosts:
            indent = len(line) - len(line.lstrip())
            if line.strip() and indent > hosts_indent:
                h = line.strip()
                if h not in hosts:
                    hosts.append(h)
                continue
            in_hosts = False
    return Preview(hosts=hosts, plays=plays, tags=sorted(tags))
