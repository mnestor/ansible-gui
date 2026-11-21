"""Works out which file set each host variable, and what it overrode.

Ansible merges inventory variables and drops their origin. This module
rebuilds the merge layer by layer in Ansible's default precedence order
(VARIABLE_PRECEDENCE), then checks every final value against what
ansible-inventory itself reports. Any disagreement is flagged, never hidden:
the value shown is always Ansible's.

One kind of value is never shown: with a vault password file, ansible-inventory
decrypts whole-file vaulted vars files, so what it reports for their variables
is the secret itself. A value that probably comes from such a file is shown as
vaulted, like an inline !vault value, with a note naming the file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any

from .. import repofiles
from . import varfiles
from .model import VAULTED, Group, Host, Inventory, Layer, ResolvedVar, Setting

NOT_FROM_REPO = (
    "Ansible sets this, but no inventory source or group_vars/host_vars file in the "
    "repo does. It may come from a vars plugin or a source outside the repo."
)
NOT_IN_ANSIBLE = "This file sets it, but Ansible does not report it for this host."
VALUE_DIFFERS = (
    "Ansible resolves a different value than the source shown. The value shown is "
    "Ansible's; the source may be wrong."
)
IN_ENCRYPTED_FILE = "Probably set in an encrypted file ({files}), so its value isn’t shown."
UNDER_ENCRYPTED_FILE = (
    "Ansible resolves a different value than the source shown, probably from an encrypted "
    "file that outranks it ({files}), so the value isn’t shown."
)


def _is_vault_blob(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"__ansible_vault"}


def _clean(value: Any) -> Any:
    """Undo ansible-inventory's JSON wrappers: vault blobs become VAULTED and
    {"__ansible_unsafe": x} (a !unsafe value) becomes x."""
    if isinstance(value, dict):
        if _is_vault_blob(value):
            return VAULTED
        if set(value) == {"__ansible_unsafe"}:
            return _clean(value["__ansible_unsafe"])
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _devault(values: dict[str, Any]) -> dict[str, Any]:
    return {k: _clean(v) for k, v in values.items()}


def _norm(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _same(ours: Any, truth: Any) -> bool:
    if ours is VAULTED or truth is VAULTED:
        return True  # can't compare ciphertext with plaintext; both mean "vaulted"
    return _norm(ours) == _norm(truth)


class _SourceText:
    """Finds line numbers inside inventory source files, for links."""

    def __init__(self, path: Path):
        try:
            self.lines = repofiles.read_text(path).splitlines()
        except OSError:
            self.lines = []

    def _find(self, pattern: str, start: int = 0) -> int | None:
        rx = re.compile(pattern)
        for i in range(start, len(self.lines)):
            if rx.search(self.lines[i]):
                return i
        return None

    def group_var_line(self, group: str, key: str) -> int | None:
        g = re.escape(group)
        anchor = self._find(rf"^\s*\[{g}:vars\]") if self.lines else None
        if anchor is None:
            anchor = self._find(rf"^\s*{g}:\s*$")
        if anchor is None:
            return None
        hit = self._find(rf"^\s*{re.escape(key)}\s*[=:]", anchor + 1)
        return (hit if hit is not None else anchor) + 1

    def host_var_line(self, host: str, key: str) -> int | None:
        h = re.escape(host)
        anchor = self._find(rf"^\s*{h}(\s|:|$)")
        if anchor is None:
            return None
        line = self.lines[anchor]
        if re.search(rf"\b{re.escape(key)}\s*=", line):
            return anchor + 1
        hit = self._find(rf"^\s*{re.escape(key)}\s*:", anchor + 1)
        return (hit if hit is not None else anchor) + 1


def build(
    project_dir: Path,
    sources: list[str],
    playbook_dir: str,
    truth: dict,
    bare: dict,
) -> Inventory:
    project_dir = project_dir.resolve()
    warnings: list[str] = []

    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(project_dir))
        except ValueError:
            return str(p)

    # --- structure ---------------------------------------------------------
    group_names = [k for k in truth if k != "_meta"]
    children = {g: list(truth[g].get("children", [])) for g in group_names}
    direct_hosts = {g: list(truth[g].get("hosts", [])) for g in group_names}
    for g in list(children):
        for c in children[g]:
            children.setdefault(c, [])
            direct_hosts.setdefault(c, [])
    parents: dict[str, list[str]] = {g: [] for g in children}
    for g, cs in children.items():
        for c in cs:
            parents[c].append(g)

    @cache
    def depth(g: str) -> int:
        return 0 if not parents[g] else max(depth(p) + 1 for p in parents[g])

    bare_groups = {g: _devault(bare.get(g, {}).get("vars", {}) or {}) for g in children}
    priority = {}
    for g in children:
        try:
            priority[g] = int(bare_groups[g].get("ansible_group_priority", 1))
        except (TypeError, ValueError):
            priority[g] = 1

    @cache
    def ancestors(g: str) -> frozenset[str]:
        out = set(parents[g])
        for p in parents[g]:
            out |= ancestors(p)
        return frozenset(out)

    @cache
    def all_members(g: str) -> tuple[str, ...]:
        hs = set(direct_hosts[g])
        for c in children[g]:
            hs |= set(all_members(c))
        return tuple(sorted(hs))

    host_names: set[str] = set(truth.get("_meta", {}).get("hostvars", {}))
    for hs in direct_hosts.values():
        host_names.update(hs)

    def sort_key(g: str) -> tuple[int, int, str]:
        return (depth(g), priority[g], g)

    # --- where files live --------------------------------------------------
    inv_basedirs: list[Path] = []
    source_files: dict[str, Path] = {}
    for s in sources:
        p = (project_dir / s).resolve()
        base = p if p.is_dir() else p.parent
        if base not in inv_basedirs:
            inv_basedirs.append(base)
        if p.is_file():
            source_files[s] = p
    play_base = (project_dir / playbook_dir).resolve()
    play_basedirs = [] if play_base in inv_basedirs else [play_base]

    single_source = next(iter(source_files.values())) if len(source_files) == 1 else None
    source_text = _SourceText(single_source) if single_source else None
    source_rel = rel(single_source) if single_source else None

    @cache
    def load(path: Path) -> varfiles.VarFile:
        vf = varfiles.load(path)
        if vf.error:
            warnings.append(f"{rel(path)}: {vf.error}")
        return vf

    def file_layers(subdir: str, entity: str, kind: str, scope: str, base: Path, **who) -> list[tuple[Layer, dict, dict]]:
        out = []
        for path in varfiles.find(base, subdir, entity):
            vf = load(path)
            if vf.encrypted:
                out.append((Layer(kind, rel(path), scope=scope, **who), {}, {"__encrypted__": True}))
                continue
            out.append((Layer(kind, rel(path), scope=scope, **who), vf.values, vf.lines))
        return out

    def group_layers(g: str) -> list[tuple[str, list[tuple[Layer, dict, dict]]]]:
        """(stage, layers) for one group's own settings."""
        inv_layer = Layer("inventory_group", source_rel, group=g)
        stages = [("inventory", [(inv_layer, bare_groups.get(g, {}), {})])]
        stages.append(("inventory_files", [l for b in inv_basedirs for l in file_layers("group_vars", g, "group_vars", "inventory", b, group=g)]))
        stages.append(("playbook_files", [l for b in play_basedirs for l in file_layers("group_vars", g, "group_vars", "playbook", b, group=g)]))
        return stages

    def with_line(layer: Layer, key: str, lines: dict) -> Layer:
        line = lines.get(key)
        if line is None and source_text and layer.path == source_rel:
            if layer.kind == "inventory_group":
                line = source_text.group_var_line(layer.group or "", key)
            elif layer.kind == "inventory_host":
                line = source_text.host_var_line(layer.host or "", key)
        return Layer(layer.kind, layer.path, layer.group, layer.host, layer.scope, line)

    def merge(ordered: list[tuple[Layer, dict, dict]]) -> tuple[dict[str, list[Setting]], list[Layer], dict[str, int]]:
        """Every setting of each variable, lowest precedence first; the encrypted files, whose variables
        can't be read; and for each variable how many of those files rank below its winning setting
        (the others outrank it, and may be what Ansible actually uses)."""
        track: dict[str, list[Setting]] = {}
        encrypted: list[Layer] = []
        below: dict[str, int] = {}
        for layer, values, lines in ordered:
            if lines.get("__encrypted__"):
                encrypted.append(layer)
                continue
            for k, v in values.items():
                track.setdefault(k, []).append(Setting(with_line(layer, k, lines), v))
                below[k] = len(encrypted)
        return track, encrypted, below

    def files(layers: list[Layer]) -> str:
        return ", ".join(l.path or "" for l in layers)

    # --- hosts -------------------------------------------------------------
    truth_vars = truth.get("_meta", {}).get("hostvars", {})
    bare_hostvars = bare.get("_meta", {}).get("hostvars", {})
    hosts: dict[str, Host] = {}
    for h in sorted(host_names):
        direct = sorted(g for g in children if h in direct_hosts[g])
        member_of = set(direct)
        for g in direct:
            member_of |= ancestors(g)
        member_of.add("all")
        ordered_groups = sorted((g for g in member_of if g != "all"), key=sort_key)

        layers: list[tuple[Layer, dict, dict]] = []
        # VARIABLE_PRECEDENCE default: all_inventory, groups_inventory,
        # all_plugins_inventory, all_plugins_play, groups_plugins_inventory,
        # groups_plugins_play; then host vars, host_vars (inventory), host_vars (play).
        for g in ["all", *ordered_groups]:
            # Ansible consumes ansible_group_priority from inventory sources to
            # order groups; it never reaches the host's variables.
            own = {k: v for k, v in bare_groups.get(g, {}).items() if k != "ansible_group_priority"}
            layers.append((Layer("inventory_group", source_rel, group=g), own, {}))
        for b in inv_basedirs:
            layers += file_layers("group_vars", "all", "group_vars", "inventory", b, group="all")
        for b in play_basedirs:
            layers += file_layers("group_vars", "all", "group_vars", "playbook", b, group="all")
        for b in inv_basedirs:
            for g in ordered_groups:
                layers += file_layers("group_vars", g, "group_vars", "inventory", b, group=g)
        for b in play_basedirs:
            for g in ordered_groups:
                layers += file_layers("group_vars", g, "group_vars", "playbook", b, group=g)
        layers.append((Layer("inventory_host", source_rel, host=h), _devault(bare_hostvars.get(h, {})), {}))
        for b in inv_basedirs:
            layers += file_layers("host_vars", h, "host_vars", "inventory", b, host=h)
        for b in play_basedirs:
            layers += file_layers("host_vars", h, "host_vars", "playbook", b, host=h)

        track, encrypted, below = merge(layers)
        actual = _devault(truth_vars.get(h, {}))
        resolved: list[ResolvedVar] = []
        for key in sorted(set(track) | set(actual)):
            settings = track.get(key, [])
            if not settings:
                value, note = actual[key], NOT_FROM_REPO
                if encrypted:  # ansible-inventory decrypted it: what it reports is the secret
                    value, note = VAULTED, IN_ENCRYPTED_FILE.format(files=files(encrypted))
                resolved.append(ResolvedVar(key, value, Layer("unknown", None), [], note))
                continue
            winner = settings[-1]
            if key not in actual:
                resolved.append(ResolvedVar(key, winner.value, winner.layer, settings[:-1], NOT_IN_ANSIBLE))
                continue
            truth_value = actual[key]
            value = VAULTED if winner.value is VAULTED else truth_value
            mismatch = None if _same(winner.value, truth_value) else VALUE_DIFFERS
            outranking = encrypted[below[key]:]
            if mismatch and outranking:  # a readable file sets it, and an encrypted one above it sets it again
                value, mismatch = VAULTED, UNDER_ENCRYPTED_FILE.format(files=files(outranking))
            resolved.append(ResolvedVar(key, value, winner.layer, settings[:-1], mismatch))
        hosts[h] = Host(h, direct, ["all", *ordered_groups], resolved)

    # --- groups ------------------------------------------------------------
    groups: dict[str, Group] = {}
    for g in sorted(children):
        ordered = [l for _, ls in group_layers(g) for l in ls]
        track, _, _ = merge(ordered)
        own = [
            ResolvedVar(k, s[-1].value, s[-1].layer, s[:-1])
            for k, s in sorted(track.items())
        ]
        groups[g] = Group(
            name=g,
            depth=depth(g),
            priority=priority[g],
            parents=sorted(parents[g]),
            children=sorted(children[g]),
            hosts=sorted(direct_hosts[g]),
            all_hosts=list(all_members(g)),
            vars=own,
        )

    if len(source_files) > 1:
        warnings.append(
            "Several inventory sources are configured, so variables set inside them "
            "are attributed to the sources as a whole, not to one file."
        )

    return Inventory(
        sources=list(sources),
        playbook_dir=playbook_dir,
        hosts=hosts,
        groups=groups,
        loaded_at=datetime.now(timezone.utc),
        warnings=warnings,
    )


def edit_url(template: str | None, layer: Layer) -> str | None:
    """Link to the file in the git web UI. template uses {path} and {line}."""
    if not template or not layer.path or not template.lower().startswith(("https://", "http://")):
        return None
    url = template.replace("{path}", layer.path)
    return url.replace("{line}", str(layer.line or 1))

