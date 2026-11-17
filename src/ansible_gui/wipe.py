"""Remove a directory that held keys, passwords or secret environment variables."""

from __future__ import annotations

import shutil
from pathlib import Path


def wipe_tree(root: Path) -> None:
    """Overwrite every file under root with zeros, then delete root.

    The overwrite is best effort (a copy-on-write filesystem or an SSD may keep the old blocks); what
    it guarantees is that nothing is left behind for a backup or a copy of the data directory to pick up."""
    if not root.exists():
        return
    for f in root.rglob("*"):
        if f.is_file() and not f.is_symlink():
            try:
                f.write_bytes(b"\0" * f.stat().st_size)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)
