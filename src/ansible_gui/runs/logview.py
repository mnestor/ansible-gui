"""The run log as HTML: lines that say what Ansible reported are wrapped in a span naming it, for the colour.

The log on disk stays plain text: it is what Download log and the notification email hand out, and Ansible's
own colours are off for it (ANSIBLE_NOCOLOR), since they are escape codes. So the colour is put back here, from
how the default output starts a line: "ok:", "changed:", "fatal:", "TASK [", a recap line, a diff. A line that
says nothing itself (a wrapped message, the rest of a result) keeps the kind of the line above it, up to the
next blank line.

The page asks for the log in pieces while a run is going. Each piece ends on a whole line, and render() returns
the state the next piece starts in, which the page hands back.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

# What a result line says, and so what the lines under it are part of.
RESULTS = ("ok", "changed", "failed", "unreachable", "skipped", "warn")

_STARTS = (
    ("ok:", "ok"), ("changed:", "changed"), ("skipping:", "skipped"), ("skipped:", "skipped"),
    ("included:", "skipped"), ("...ignoring", "skipped"),
    ("failed:", "failed"), ("fatal:", "failed"), ("[ERROR]", "failed"), ("ERROR!", "failed"),
    ("[WARNING]", "warn"), ("[DEPRECATION WARNING]", "warn"),
    ("PLAY [", "head"), ("TASK [", "head"), ("RUNNING HANDLER [", "head"), ("PLAY RECAP", "head"),
    ("[ansible-gui]", "note"), ("$ ansible", "note"),
)
# box1 : ok=3 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0
_RECAP = re.compile(r"^\S+\s*:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)")
# box1 | CHANGED | rc=0 >>      box1 | SUCCESS => {      box1 | UNREACHABLE! => {
_ADHOC = re.compile(r"^\S+ \| (SUCCESS|CHANGED|FAILED|UNREACHABLE|SKIPPED)\b")
_ADHOC_KIND = {"SUCCESS": "ok", "CHANGED": "changed", "FAILED": "failed", "UNREACHABLE": "unreachable",
               "SKIPPED": "skipped"}
# The only markup there is. Everything else that goes into the page is the log's text, escaped: the pieces
# are joined as Markup, which escapes whatever isn't Markup already.
_OPEN = Markup('<span class="ll ll-%s">')
_CLOSE = Markup("</span>")


def _said(line: str) -> str:
    """What this line says by itself: a kind, or "" for a line that is part of something else."""
    for start, kind in _STARTS:
        if line.startswith(start):
            return "unreachable" if kind == "failed" and "UNREACHABLE!" in line else kind
    if m := _ADHOC.match(line):
        return _ADHOC_KIND[m.group(1)]
    if m := _RECAP.match(line):
        _ok, changed, unreachable, failed = map(int, m.groups())
        return "unreachable" if unreachable else "failed" if failed else "changed" if changed else "ok"
    return ""


def _parse(state: str) -> tuple[str, bool]:
    kind, _, diff = state.partition("~")
    return (kind if kind in RESULTS else ""), diff == "d"


def render(text: str, state: str = "") -> tuple[Markup, str]:
    """text (whole lines) as HTML, and the state to render what follows it with. state is "" at the top of a
    log; otherwise only what an earlier call returned means anything."""
    under, in_diff = _parse(state)  # the result the next lines belong to; inside a --diff block
    out: list[Markup] = []
    open_kind = ""
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        kind = ""
        if in_diff and body[:1] in ("+", "-", " ", "@") and not body.startswith(("--- ", "+++ ")):
            kind = {"+": "add", "-": "del", "@": "note"}.get(body[:1], "")
        elif body.startswith(("--- ", "+++ ")):
            kind, in_diff, under = "note", True, ""
        else:
            in_diff = False
            said = _said(body)
            if said:
                kind = said
                under = said if said in RESULTS else ""
            elif not body.strip():
                under = ""
            else:
                kind = under
        if kind != open_kind:
            if open_kind:
                out.append(_CLOSE)
            if kind:
                out.append(_OPEN % kind)
            open_kind = kind
        out.append(escape(line))
    if open_kind:
        out.append(_CLOSE)
    return Markup("").join(out), f"{under}~d" if in_diff else under
