"""The latest Ansible facts of each host, saved whenever a run gathers them.

They come from the run's own events (the gather_facts or setup result), so nothing in the
repository's ansible.cfg changes: no fact cache is configured, and plays gather exactly as before.
Only the latest facts are kept, per inventory host name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .db import Database
from .runs.store import now

SCHEMA = """
create table if not exists facts (
  host text primary key,
  facts text not null,
  gathered_at text not null,
  run_id integer
);
"""

GATHER_ACTIONS = {"ansible.builtin.gather_facts", "ansible.builtin.setup", "gather_facts", "setup"}
# The remote user's environment: on some hosts it holds tokens, and every signed-in user sees facts.
DROPPED = ("ansible_env",)


@dataclass
class Facts:
    host: str
    facts: dict
    gathered_at: datetime
    run_id: int | None

    def summary(self) -> list[tuple[str, str]]:
        """The handful worth reading first, as (label, value)."""
        f = self.facts

        def g(name):
            v = f.get(f"ansible_{name}")
            return None if v in (None, "", "NA", "N/A") else v

        rows = []
        if g("distribution"):
            release = g("distribution_release")
            version = " ".join(str(x) for x in (g("distribution"), g("distribution_version")) if x)
            rows.append(("OS", f"{version} ({release})" if release and str(release) not in version else version))
        if g("kernel"):
            rows.append(("Kernel", " ".join(str(x) for x in (g("kernel"), g("architecture")) if x)))
        if g("virtualization_type"):
            rows.append(("Virtualization", f"{g('virtualization_type')} {g('virtualization_role') or ''}".strip()))
        hardware = []
        if g("processor_vcpus"):
            hardware.append(f"{g('processor_vcpus')} vCPU")
        if isinstance(g("memtotal_mb"), int):
            hardware.append(f"{g('memtotal_mb') / 1024:.1f} GiB RAM")
        if hardware:
            rows.append(("Hardware", ", ".join(hardware)))
        # The host reports its own facts, so a value not shaped the way Ansible documents it is skipped.
        ipv4, all_ipv4 = g("default_ipv4"), g("all_ipv4_addresses")
        default = str(ipv4["address"]) if isinstance(ipv4, dict) and ipv4.get("address") else None
        ips = [str(ip) for ip in all_ipv4 if ip] if isinstance(all_ipv4, list) else []
        if default or ips:
            rest = [ip for ip in ips if ip != default]
            rows.append(("IPv4", ", ".join([x for x in [default] if x] + rest)))
        if g("fqdn"):
            rows.append(("FQDN", str(g("fqdn"))))
        if g("python_version"):
            rows.append(("Python", str(g("python_version"))))
        if isinstance(g("uptime_seconds"), int):
            days = g("uptime_seconds") // 86400
            rows.append(("Uptime when gathered", f"{days} day{'s' if days != 1 else ''}" if days else "under a day"))
        return rows


class FactStore:
    def __init__(self, data_dir: Path):
        self._db = Database(data_dir / "ansible-gui.db")
        self._db.executescript(SCHEMA)

    def record(self, host: str, facts: dict, run_id: int | None) -> None:
        """Save what a run gathered. A full gather replaces the host's facts; a partial one (gather_subset
        other than all, or a filter) updates only the facts it returned."""
        facts = {k: v for k, v in facts.items() if k not in DROPPED}
        with self._db.lock:
            if "all" not in (facts.get("gather_subset") or []):
                old = self.get(host)
                if old:
                    facts = {**old.facts, **facts}
            self._db.execute(
                "insert into facts (host, facts, gathered_at, run_id) values (?,?,?,?)"
                " on conflict(host) do update set facts=excluded.facts, gathered_at=excluded.gathered_at,"
                " run_id=excluded.run_id",
                (host, json.dumps(facts, sort_keys=True, default=str), now(), run_id))
            self._db.commit()

    def get(self, host: str) -> Facts | None:
        r = self._db.execute("select * from facts where host=?", (host,)).fetchone()
        if not r:
            return None
        return Facts(host=r["host"], facts=json.loads(r["facts"]), gathered_at=datetime.fromisoformat(r["gathered_at"]),
                     run_id=r["run_id"])
