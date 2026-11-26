"""Reading a repository's files in the server, when the repository's code runs somewhere else.

With workers, nothing from a repository runs in the server, but the server still reads its files: to list the
playbooks, to show where a variable is set, to find the requirements file. A repository can hold a symbolic
link to anywhere. Ansible follows it in the runner, where there is nothing of the server's to find; followed
here, a link to /proc/1/environ or into the data directory would have the server read its own secrets into a
page.

So once confine() has named the directories the repositories are in, read_text() and read_bytes() read a
file only if, links followed, it really is in one of them. Without workers the repository's code runs as the
server's own user anyway, and nothing is confined.

One server per process sets this at start. The roots are the process's, not an app object's, so that the
functions that read (which take paths, not settings) need no extra argument.
"""

from __future__ import annotations

import errno
from pathlib import Path

_roots: tuple[Path, ...] | None = None


def confine(roots: list[Path] | None) -> None:
    """From now on, read repository files only inside these directories. None: anywhere."""
    global _roots
    _roots = tuple(Path(r).resolve() for r in roots) if roots else None


def inside(path: Path) -> bool:
    """Whether the path, links followed, is in a directory the repositories are in."""
    if _roots is None:
        return True
    try:
        real = Path(path).resolve(strict=True)
    except FileNotFoundError:
        real = Path(path).resolve()  # nothing there: where it would be, links followed as far as they go
    except (OSError, RuntimeError):
        # A link that leads back to itself. Resolving one raised RuntimeError until Python 3.13. Since then
        # only a strict resolve says so: a lenient one hands the path back as it was, which looks inside.
        return False
    return any(real == root or root in real.parents for root in _roots)


def _checked(path: Path) -> Path:
    if not inside(path):
        raise PermissionError(errno.EACCES, "it is a link to somewhere outside the repositories", str(path))
    return path


def read_text(path: Path) -> str:
    return _checked(path).read_text()


def read_bytes(path: Path) -> bytes:
    return _checked(path).read_bytes()
