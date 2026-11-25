"""Data shapes for a resolved inventory with per-variable source attribution."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
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
class SourceGroup:
    """The variables one source set and won: one file, or one kind of line in the inventory source."""
    key: str  # "s1", "s2"…: what a page calls it, the same in either order of the table
    layer: Layer  # the source, without a line
    label: str  # its short name: the path from where the sources part ways, and which line for the inventory's own
    vars: list[ResolvedVar]


def _rank(layer: Layer, groups: list[str]) -> tuple[int, int, str]:
    """Where a source falls in Ansible's default VARIABLE_PRECEDENCE, lowest first: the order attribution.py
    merges its layers in. `groups` is the host's groups in that order (all first)."""
    group = groups.index(layer.group) if layer.group in groups else len(groups)
    play = layer.scope == "playbook"
    if layer.kind == "inventory_group":
        tier = 0
    elif layer.kind == "group_vars":
        tier = (2 if play else 1) if layer.group == "all" else (4 if play else 3)
    elif layer.kind == "inventory_host":
        tier = 5
    elif layer.kind == "host_vars":
        tier = 7 if play else 6
    else:  # nothing in the repo sets it: it has no place in the order, and is listed last
        tier = -1
    return tier, group, layer.path or ""


def by_source(vars: list[ResolvedVar], groups: list[str]) -> list[SourceGroup]:
    """`vars` under the source that set each, the source that wins over the others first.

    A page of forty variables from seven files names a file forty times when every row carries it. Grouped,
    each file is named once, with what it sets under it, and the top of the page is what you would edit to
    change this host. Within a source the variables keep their order (A to Z)."""
    found: dict[Layer, list[ResolvedVar]] = {}
    for v in vars:
        s = v.source
        found.setdefault(Layer(s.kind, s.path, s.group, s.host, s.scope), []).append(v)
    ordered = sorted(found, key=lambda layer: _rank(layer, groups), reverse=True)
    # Paths are shown from where they part ways: next to each other, "/srv/repos/3/abc…/" on every chip says nothing.
    dirs = {str(PurePosixPath(layer.path).parent) for layer in ordered if layer.path and layer.path.startswith("/")}
    shared = (posixpath.commonpath(dirs) if dirs else "").rstrip("/")
    shared = str(PurePosixPath(shared).parent).rstrip("/") if shared and len(dirs) == 1 else shared

    def label(layer: Layer) -> str:
        if not layer.path:
            return "outside the repo"
        path = layer.path[len(shared) + 1:] if shared and layer.path.startswith(shared + "/") else layer.path
        if layer.kind == "inventory_group":
            return f"{path} · [{layer.group}]"
        return f"{path} · host line" if layer.kind == "inventory_host" else path

    return [SourceGroup(f"s{n}", layer, label(layer), found[layer]) for n, layer in enumerate(ordered, 1)]


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
