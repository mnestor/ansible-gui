"""The home page: what needs resolving, what is running, what runs next, what just ran."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .notify import PROBLEMS, classify
from .runs.store import Run, host_result


@dataclass
class HostIssue:
    """Hosts whose latest result came from the same run and is a failure or drift."""
    run: Run
    kind: str  # failed | unreachable | drift
    hosts: list[str] = field(default_factory=list)


@dataclass
class ScheduleIssue:
    schedule: object
    run: Run
    kind: str
    summary: str


@dataclass
class SetupIssue:
    title: str
    detail: str
    href: str
    action: str


def host_issues(runs: list[Run], known_hosts: set[str] | None) -> list[HostIssue]:
    """Each host's most recent real result; keep the bad ones, grouped by the run that produced them.

    A dry run that would change a host is drift. A later good run (dry or not) resolves it.
    """
    latest: dict[str, tuple[str, Run]] = {}
    for run in runs:  # newest first
        if not run.finished:
            continue
        for host, stats in run.host_stats.items():
            if host in latest:
                continue
            result = host_result(stats)
            if result == "pending":
                continue
            if result == "changed" and run.check_mode:
                result = "drift"
            latest[host] = (result, run)
    groups: dict[tuple[int, str], HostIssue] = {}
    for host, (result, run) in latest.items():
        if result not in ("failed", "unreachable", "drift"):
            continue
        if known_hosts is not None and host not in known_hosts:
            continue  # gone from the inventory
        groups.setdefault((run.id, result), HostIssue(run=run, kind=result)).hosts.append(host)
    order = {"unreachable": 0, "failed": 1, "drift": 2}
    out = sorted(groups.values(), key=lambda g: (order[g.kind], -g.run.id))
    for g in out:
        g.hosts.sort()
    return out


def schedule_issues(schedules, store) -> list[ScheduleIssue]:
    out = []
    for s in schedules:
        if not s.enabled:
            continue
        last = next((r for r in store.for_schedule(s.id, 5) if r.finished), None)
        if not last:
            continue
        kind, summary = classify(last)
        # Host failures and drift already show per host; here only what hosts can't show.
        if kind in PROBLEMS and (kind not in ("failed", "drift") or no_host_failed(last)):
            out.append(ScheduleIssue(schedule=s, run=last, kind=kind, summary=summary))
    return out


def no_host_failed(run: Run) -> bool:
    return run.status == "failed" and not any(host_result(st) in ("failed", "unreachable")
                                              for st in run.host_stats.values())


def run_issues(runs: list[Run]) -> list[Run]:
    """Hand-started runs that failed before any host reported, unless a later run of the same playbook worked."""
    out, seen = [], set()
    for run in runs:  # newest first
        if run.schedule_id or not run.finished or run.status in ("skipped", "canceled"):
            continue
        key = run.playbook
        if key in seen:
            continue
        seen.add(key)
        if no_host_failed(run) or run.status in ("error", "interrupted"):
            out.append(run)
    return out


def email_issue(store) -> Run | None:
    """The latest emailed run, if its email failed."""
    last = store.last_notified()
    return last if last and last.notified and last.notified.startswith("Email failed") else None


def setup_issues(settings, entries, creds, repos=()) -> list[SetupIssue]:
    """entries: [(inventory repo, LoadState)]."""
    out = []
    for r in repos:
        if r.last_error:
            out.append(SetupIssue(f"Can’t pull {r.name} from git",
                                  f"{r.last_error} Runs that need it are canceled until a pull works.",
                                  "/repos", "Open repositories"))
    named = len(entries) > 1
    for inv, state in entries:
        if state.error:
            what = f"Inventory {inv.name}" if named else "Inventory"
            out.append(SetupIssue(
                f"{what} didn’t load" if not state.inventory else f"{what} reload failed",
                "Runs and previews use the inventory as Ansible reads it now; the error is on the inventory page.",
                "/inventory", "See the error"))
    if settings.ssh_config and not settings.ssh_config.is_file():
        out.append(SetupIssue("SSH config is missing", f"{settings.ssh_config} doesn’t exist, so every run fails to connect.",
                              "/credentials", "Open credentials"))
    unreadable = [c.name for c in creds.all() if not c.readable]
    if unreadable:
        out.append(SetupIssue("Credentials can’t be decrypted",
                              f"{', '.join(unreadable)}: the secret key changed since they were saved. Enter them again.",
                              "/credentials", "Open credentials"))
    return out


def week_counts(runs: list[Run]) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    week = [r for r in runs if r.created_at >= since and r.status not in ("skipped",)]
    kinds = [classify(r)[0] for r in week if r.finished]
    return {"runs": len(week), "failed": sum(k in ("failed", "error", "interrupted") for k in kinds),
            "drift": kinds.count("drift"),
            "ok": kinds.count("ok")}
