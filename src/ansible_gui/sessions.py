"""Sign-ins recorded on the server, so a session can be ended for good.

The session cookie is signed, and Starlette re-signs it on every response, so on its own a copied
cookie would work for as long as it kept being used, and signing out would only clear the copy in
that browser. Here every sign-in gets a row: the cookie carries a random id, the table its SHA-256.
A session is valid only while its row exists and is younger than LIFETIME, so:

- signing out deletes the row, and the cookie is dead wherever it was copied to;
- every session ends LIFETIME after sign-in, so group membership is checked again at least that often;
- `ansible-gui sign-out-everyone` deletes every row, for when someone must lose access now.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database

LIFETIME = timedelta(hours=12)

SCHEMA = """
create table if not exists sessions (
  id text primary key,      -- sha256 of the id in the cookie
  who text not null,
  created_at text not null
);
"""


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    def __init__(self, data_dir: Path):
        self._db = Database(data_dir / "ansible-gui.db")
        self._db.executescript(SCHEMA)

    def start(self, who: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self._db.lock:
            self._db.execute("delete from sessions where created_at < ?", ((_now() - LIFETIME).isoformat(),))
            self._db.execute("insert into sessions (id, who, created_at) values (?,?,?)",
                             (_hash(sid), who, _now().isoformat()))
            self._db.commit()
        return sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        row = self._db.execute("select created_at from sessions where id=?", (_hash(sid),)).fetchone()
        return bool(row) and datetime.fromisoformat(row["created_at"]) > _now() - LIFETIME

    def end(self, sid: str | None) -> None:
        if sid:
            with self._db.lock:
                self._db.execute("delete from sessions where id=?", (_hash(sid),))
                self._db.commit()

    def end_all(self) -> int:
        with self._db.lock:
            n = self._db.execute("delete from sessions").rowcount
            self._db.commit()
        return n
