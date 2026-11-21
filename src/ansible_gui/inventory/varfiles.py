"""Reads group_vars/ and host_vars/ files the way Ansible's host_group_vars
vars plugin finds them, keeping the file and line each key came from."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import repofiles
from .model import VAULTED

EXTENSIONS = ["", ".yml", ".yaml", ".json"]


class _Loader(yaml.SafeLoader):
    pass


def _vault(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    return VAULTED


def _unsafe(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_constructor("!vault", _vault)
_Loader.add_constructor("!unsafe", _unsafe)


@dataclass
class VarFile:
    path: Path  # absolute
    values: dict[str, Any] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)
    encrypted: bool = False  # whole file is vault-encrypted; keys unknown
    error: str | None = None


def load(path: Path) -> VarFile:
    vf = VarFile(path=path)
    try:
        text = repofiles.read_text(path)
    except OSError as e:
        vf.error = f"could not read: {e.strerror}"
        return vf
    if text.lstrip().startswith("$ANSIBLE_VAULT"):
        vf.encrypted = True
        return vf
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass  # nosec B506
        node = yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError as e:
        vf.error = f"invalid YAML: {e}"
        return vf
    if data is None:
        return vf
    if not isinstance(data, dict):
        vf.error = "top level is not a mapping"
        return vf
    vf.values = data
    if isinstance(node, yaml.MappingNode):
        for key_node, _ in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                vf.lines[key_node.value] = key_node.start_mark.line + 1
    return vf


def _dir_files(d: Path) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(d.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            # A directory that is a link out of the repositories isn't listed: load() then says why.
            out.extend(_dir_files(entry) if repofiles.inside(entry) else [entry])
        elif entry.suffix in EXTENSIONS[1:] or entry.suffix == "":
            out.append(entry)
    return out


def find(basedir: Path, subdir: str, name: str) -> list[Path]:
    """Files for one entity, in load order. Like Ansible, the first existing
    candidate of name, name.yml, name.yaml, name.json wins; a directory means
    every file inside it, sorted."""
    base = basedir / subdir
    for ext in EXTENSIONS:
        candidate = base / f"{name}{ext}"
        if candidate.is_dir():
            return _dir_files(candidate) if repofiles.inside(candidate) else [candidate]
        if candidate.is_file():
            return [candidate]
    return []
