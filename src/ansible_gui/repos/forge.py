"""Web links into a repository's git host, derived from its clone address.

The well-known hosts are recognised by name. A self-hosted Gitea, Forgejo or GitLab looks the same from its
address, so the repository says which it is (``forge``). A hand-written ``edit_url`` template still wins.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

FORGES = {
    "": "Detect from the address",
    "gitea": "Gitea or Forgejo",
    "gitlab": "GitLab",
    "github": "GitHub",
    "bitbucket": "Bitbucket",
    "none": "No links",
}
KNOWN = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea", "bitbucket.org": "bitbucket"}

# {base}, {ref}; then the file path and line are filled in per link.
_FILE = {
    "github": "{base}/blob/{ref}/{path}#L{line}",
    "gitlab": "{base}/-/blob/{ref}/{path}#L{line}",
    "gitea": "{base}/src/{kind}/{ref}/{path}#L{line}",
    "bitbucket": "{base}/src/{ref}/{path}#lines-{line}",
}
_COMMIT = {
    "github": "{base}/commit/{sha}",
    "gitlab": "{base}/-/commit/{sha}",
    "gitea": "{base}/commit/{sha}",
    "bitbucket": "{base}/commits/{sha}",
}
_SCP = re.compile(r"^[\w.-]+@(?P<host>[\w.-]+):(?P<path>.+)$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def web_base(url: str) -> tuple[str, str] | None:
    """(host, https://host/owner/repo) for a clone address, or None for a local path or file:// URL."""
    url = (url or "").strip()
    m = _SCP.match(url)
    if m:
        host, path = m["host"], m["path"]
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https", "ssh", "git") or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip("/").removesuffix(".git").strip("/")
    if not path:
        return None
    return host.lower(), f"https://{host.lower()}/{path}"


def kind_of(url: str, forge: str = "") -> tuple[str, str] | None:
    """(forge kind, web base), or None when no link can be made."""
    if forge == "none":
        return None
    base = web_base(url)
    if not base:
        return None
    host, web = base
    kind = forge or KNOWN.get(host)
    return (kind, web) if kind in _FILE else None


def file_template(url: str, forge: str, branch: str, commit: str | None = None) -> str | None:
    """A {path}/{line} template like the hand-written ones. At the commit when known: that is what's checked out."""
    k = kind_of(url, forge)
    if not k:
        return None
    kind, base = k
    ref, ref_kind = (commit, "commit") if commit and _SHA.match(commit) else (branch or "main", "branch")
    return _FILE[kind].format(base=base, ref=ref, kind=ref_kind, path="{path}", line="{line}")


def commit_url(url: str, forge: str, sha: str | None) -> str | None:
    k = kind_of(url, forge)
    if not k or not sha or not _SHA.match(sha):
        return None
    return _COMMIT[k[0]].format(base=k[1], sha=sha)
