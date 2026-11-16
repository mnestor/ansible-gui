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


class InventoryService:
    def __init__(self, opts: CliOptions):
        self.opts = opts
        self.state = LoadState()
        self._lock = threading.Lock()

    def reload(self) -> LoadState:
        with self._lock:
            try:
                inv = load(self.opts)
            except InventoryError as e:
                self.state = LoadState(self.state.inventory, e, datetime.now(timezone.utc))
            else:
                self.state = LoadState(inv)
            return self.state

    def current(self) -> LoadState:
        if self.state.inventory is None and self.state.error is None:
            return self.reload()
        return self.state
