"""Loads the inventory and keeps the last good result when a reload fails."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from . import attribution, cli
from .cli import CliOptions, InventoryError
from .model import Inventory


@dataclass
class LoadState:
    inventory: Inventory | None = None
    error: InventoryError | None = None
    error_at: datetime | None = None

    @property
    def stale(self) -> bool:
        """A reload failed but an older inventory is still being shown."""
        return self.inventory is not None and self.error is not None


def load(opts: CliOptions) -> Inventory:
    truth = cli.resolved(opts)
    bare = cli.sources_only(opts)
    return attribution.build(opts.project_dir, opts.sources, opts.playbook_dir, truth, bare)


NO_INVENTORY = ("No inventory is set up. Add an inventory repository on the Repositories page, "
                "or set ANSIBLE_GUI_PROJECT_DIR and ANSIBLE_GUI_INVENTORY.")


class InventoryService:
    def __init__(self, opts):
        """opts: CliOptions, or a callable returning CliOptions (or None when nothing is set up)."""
        self._opts = opts
        self.state = LoadState()
        self._lock = threading.Lock()

    def reload(self) -> LoadState:
        with self._lock:
            opts = self._opts() if callable(self._opts) else self._opts
            if opts is None:
                self.state = LoadState(None, InventoryError(NO_INVENTORY, []), datetime.now(timezone.utc))
                return self.state
            try:
                inv = load(opts)
            except InventoryError as e:
                self.state = LoadState(self.state.inventory, e, datetime.now(timezone.utc))
            else:
                self.state = LoadState(inv)
            return self.state

    def current(self) -> LoadState:
        if self.state.inventory is None and self.state.error is None:
            return self.reload()
        return self.state


class Inventories:
    """One InventoryService per inventory repository, kept in step with the workspace."""

    def __init__(self, workspace):
        self.ws = workspace
        self._services: dict[int, InventoryService] = {}
        self._lock = threading.Lock()

    def _service(self, inv) -> InventoryService:
        with self._lock:
            svc = self._services.get(inv.id)
            if svc is None:
                svc = InventoryService(lambda inv_id=inv.id: self._opts(inv_id))
                self._services[inv.id] = svc
            return svc

    def _opts(self, inv_id: int):
        inv = self.ws.inventory_repo(inv_id)
        return self.ws.inventory_opts(inv) if inv else None

    def entries(self) -> list:
        """[(repo, LoadState)] for every inventory, loading any not loaded yet."""
        invs = self.ws.inventory_repos()
        with self._lock:
            for gone in set(self._services) - {i.id for i in invs}:
                del self._services[gone]
        return [(inv, self._service(inv).current()) for inv in invs]

    def get(self, inv_id: int):
        inv = self.ws.inventory_repo(inv_id)
        return (inv, self._service(inv).current()) if inv else (None, None)

    def reload(self, inv_id: int | None = None) -> list:
        for inv in self.ws.inventory_repos():
            if inv_id is None or inv.id == inv_id:
                self._service(inv).reload()
        return self.entries()

    # --- the merged list ----------------------------------------------------------
    def loaded(self) -> list:
        return [(inv, st.inventory) for inv, st in self.entries() if st.inventory]

    def hosts(self) -> list:
        """[(name, [(repo, Host)])] by name, every inventory that has it."""
        found: dict[str, list] = {}
        for inv, data in self.loaded():
            for name, h in data.hosts.items():
                found.setdefault(name, []).append((inv, h))
        return sorted(found.items())

    def groups(self) -> list:
        found: dict[str, list] = {}
        for inv, data in self.loaded():
            for name, g in data.groups.items():
                found.setdefault(name, []).append((inv, g))
        return sorted(found.items())

    def find_host(self, name: str) -> list:
        return [(inv, data.hosts[name]) for inv, data in self.loaded() if name in data.hosts]

    def find_group(self, name: str) -> list:
        return [(inv, data.groups[name]) for inv, data in self.loaded() if name in data.groups]

    def duplicate_hosts(self) -> list:
        return [(name, entries) for name, entries in self.hosts() if len(entries) > 1]
