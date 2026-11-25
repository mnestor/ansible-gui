"""Keeps the secrets a run was handed out of its log.

Ansible prints what a task did: the command it ran, a failed module's arguments, a debug message. A token that
went in through the environment comes back out that way, and the log is kept in plain text, shown on the run's
page and quoted in the email. So every line is searched for the values this run was given before it is written.

It finds a value as it was typed and as JSON writes it inside a string (how Ansible prints a result). It does
not find one a playbook has encoded or split: base64, a URL, half on each line. And a value shorter than
MIN_LENGTH is left alone, since hiding every "yes" or "1" would blank the log and hide nothing.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

MASK = "••••••"
MIN_LENGTH = 4
MIN_LINE = 16  # of a secret of several lines: a key's last line can be four letters that spell a word


def _written(value: str) -> set[str]:
    """The value as it is, and as JSON writes it inside a string, with and without \\u escapes."""
    return {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}


class Redactor:
    def __init__(self, secrets: Iterable[str]):
        needles: set[str] = set()
        for secret in secrets:
            secret = str(secret or "")
            # A value of several lines (a private key) is also looked for line by line: a task that prints
            # part of it, or a diff of the file it was written to, shows lines, not the whole.
            whole = secret.strip()
            if len(whole) >= MIN_LENGTH:
                needles |= _written(whole)
            if "\n" in whole:
                for line in (line.strip() for line in whole.splitlines()):
                    if len(line) >= MIN_LINE and not line.startswith("-----"):  # a PEM header says nothing
                        needles |= _written(line)
        # Longest first: of two secrets where one starts with the other, the whole of the longer one goes.
        self._pattern = re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True))) \
            if needles else None

    def __call__(self, text: str) -> str:
        return self._pattern.sub(MASK, text) if self._pattern else text
