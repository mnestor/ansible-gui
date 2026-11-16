"""Data shapes for a resolved inventory with per-variable source attribution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class Vaulted:
    """A value that is vault-encrypted in its source file. Never rendered."""

    def __repr__(self) -> str:
        return "<vaulted>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Vaulted)

    def __hash__(self) -> int:
        return hash("vaulted")


VAULTED = Vaulted()


@dataclass(frozen=True)
class Layer:
    """One place a variable can be set, in Ansible's precedence order.

    kind is one of: inventory_group, group_vars, inventory_host, host_vars, unknown.
    scope says where the file sits: "inventory" (next to the inventory source)
    or "playbook" (next to the playbooks, loaded via --playbook-dir).
    """

    kind: str
    path: str | None  # repo-relative file path, None when not file-backed
    group: str | None = None
    host: str | None = None
    scope: str = "inventory"
    line: int | None = None

    @property
    def label(self) -> str:
        if self.kind == "inventory_group":
            return f"[{self.group}] in inventory"
        if self.kind == "group_vars":
            return f"group {self.group}"
        if self.kind == "inventory_host":
            return "host line in inventory"
        if self.kind == "host_vars":
            return "host_vars"
        return "unknown source"


@dataclass
class Setting:
    layer: Layer
    value: Any


@dataclass
class ResolvedVar:
    name: str
    value: Any  # what Ansible reports; VAULTED when the winning source is vaulted
    source: Layer
    overridden: list[Setting] = field(default_factory=list)  # lowest precedence first
    mismatch: str | None = None  # why attribution disagrees with Ansible, if it does

    @property
    def vaulted(self) -> bool:
        return isinstance(self.value, Vaulted)


@dataclass
class Host:
    name: str
    groups: list[str]  # direct groups, sorted
    all_groups: list[str]  # every group incl. ancestors, in Ansible precedence order
    vars: list[ResolvedVar]

    @property
    def mismatches(self) -> list[ResolvedVar]:
        return [v for v in self.vars if v.mismatch]

    def var(self, name: str) -> ResolvedVar | None:
        return next((v for v in self.vars if v.name == name), None)


@dataclass
class Group:
    name: str
    depth: int
    priority: int
    parents: list[str]
    children: list[str]
    hosts: list[str]  # direct members
    all_hosts: list[str]  # members incl. those of child groups
    vars: list[ResolvedVar]  # the group's own settings, merged across its layers


@dataclass
class Inventory:
    sources: list[str]
    playbook_dir: str
    hosts: dict[str, Host]
    groups: dict[str, Group]
    loaded_at: datetime
    warnings: list[str] = field(default_factory=list)

    @property
    def mismatch_count(self) -> int:
        return sum(len(h.mismatches) for h in self.hosts.values())

    def tree(self, root: str = "all") -> list[tuple[int, Group]]:
        """Groups depth-first from root, children sorted by name. A group with
        several parents appears under each of them."""
        out: list[tuple[int, Group]] = []

        def walk(name: str, level: int) -> None:
            g = self.groups[name]
            out.append((level, g))
            for child in sorted(g.children):
                walk(child, level + 1)

        if root in self.groups:
            walk(root, 0)
        return out
