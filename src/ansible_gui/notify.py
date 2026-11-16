"""Email about scheduled runs that need attention, over SMTP.

Only runs started by a schedule send email: a run started by hand is being watched.
Per schedule, "problems" (the default) mails on failures, unreachable hosts, runs
that couldn't start, missed or skipped runs, interrupted runs, dry runs that found
changes (drift), and on the first good run after any of those. "always" mails every
run; "never" mails nothing. Each run records whether its email went out.
"""

from __future__ import annotations

import logging
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import make_msgid

from .config import SmtpSettings
from .repos.workspace import GIT_CANCELED
from .runs.store import Run, RunStore, host_result

log = logging.getLogger(__name__)

NOTIFY_MODES = {"problems": "Problems and recoveries", "always": "Every run", "never": "Never"}


def classify(run: Run) -> tuple[str, str]:
    """(kind, one-line summary). kind is ok, drift, failed, error, skipped, interrupted or canceled."""
    results = {h: host_result(s) for h, s in run.host_stats.items()}
    bad = sorted(h for h, r in results.items() if r in ("failed", "unreachable"))
    if run.status == "error":
        if (run.error or "").startswith(GIT_CANCELED):
            return "error", run.error.split(". Nothing ran")[0]
        return "error", run.error or "Couldn’t start."
    if run.status == "skipped":
        return "skipped", run.error or "Skipped."
    if run.status == "interrupted":
        return "interrupted", "Interrupted: the server stopped during the run."
    if run.status == "canceled":
        return "canceled", "Canceled."
    if run.status == "failed" and not bad:
        return "failed", "Failed before any host reported a result"
    if bad or run.status == "failed":
        unreachable = [h for h in bad if results[h] == "unreachable"]
        parts = []
        if len(bad) - len(unreachable):
            parts.append(f"{len(bad) - len(unreachable)} failed")
        if unreachable:
            parts.append(f"{len(unreachable)} unreachable")
        return "failed", f"{' and '.join(parts) or 'Failed'} of {len(results)} host{'s' if len(results) != 1 else ''}"
    changed = sorted(h for h, r in results.items() if r == "changed")
    if run.check_mode and changed:
        return "drift", f"Dry run found changes on {len(changed)} of {len(results)} host{'s' if len(results) != 1 else ''}"
    if run.check_mode:
        return "ok", "Dry run passed: no changes"
    return "ok", f"Succeeded on {len(results)} host{'s' if len(results) != 1 else ''}"


PROBLEMS = {"failed", "error", "skipped", "interrupted", "drift"}
SUBJECT = {"failed": "FAILED", "error": "DIDN’T START", "skipped": "SKIPPED", "interrupted": "INTERRUPTED",
           "drift": "WOULD CHANGE", "ok": "OK", "canceled": "CANCELED"}


class Notifier:
    def __init__(self, smtp: SmtpSettings | None, runs: RunStore, schedules, public_url: str | None):
        self.smtp = smtp
        self.runs = runs
        self.schedules = schedules
        self.public_url = public_url
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="ansible-gui-mail", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        return self.smtp is not None

    # Called by the executor and the scheduler whenever a run reaches a final state.
    def run_finished(self, run_id: int) -> None:
        self._queue.put(("run", run_id))

    def send_test(self, by: str) -> str | None:
        """Send a test email now. None on success, else the error."""
        if not self.smtp:
            return "Email isn’t set up."
        msg = self._message("[ansible-gui] Test email",
                            f"This is a test from ansible-gui, sent by {by}.\n\n"
                            "Scheduled runs that need attention will be mailed to this address.\n")
        return self._send(msg)

    def wait_idle(self, timeout: float = 10) -> None:
        """For tests: block until queued emails are handled."""
        done = threading.Event()
        self._queue.put(("flush", done))
        done.wait(timeout)

    # --- internals ---------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            kind, arg = self._queue.get()
            if kind == "flush":
                arg.set()
                continue
            try:
                self._handle(arg)
            except Exception:
                log.exception("notification for run %s failed", arg)

    def _handle(self, run_id: int) -> None:
        run = self.runs.get(run_id)
        if not run or not self.smtp:
            return
        git_failure = (run.error or "").startswith(GIT_CANCELED)
        if not run.schedule_id and not git_failure:
            return  # a run started by hand is being watched; a failed pull is mailed regardless
        sched = self.schedules.get(run.schedule_id) if run.schedule_id else None
        mode = sched.notify if sched else "problems"
        if git_failure and mode == "never":
            mode = "problems"  # a failed pull means the repos are out of reach: always worth a mail
        kind, summary = classify(run)
        if kind == "canceled" or mode == "never":
            return
        recovered = False
        if kind not in PROBLEMS:
            previous = next((r for r in self.runs.for_schedule(run.schedule_id, 10) if r.id < run.id and r.finished), None)
            recovered = bool(previous and classify(previous)[0] in PROBLEMS)
            if mode == "problems" and not recovered:
                return
        label = "RECOVERED" if recovered else SUBJECT[kind]
        name = sched.name if sched else run.trigger
        subject = f"[ansible-gui] {label} {name}: {summary}"
        msg = self._message(subject, self._body(run, summary, recovered))
        error = self._send(msg)
        note = f"Emailed {', '.join(self.smtp.to)}" if not error else f"Email failed: {error}"
        self.runs.update(run.id, notified=note)

    def _message(self, subject: str, body: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp.sender
        msg["To"] = ", ".join(self.smtp.to)
        msg["Message-ID"] = make_msgid(domain="ansible-gui")
        msg.set_content(body)
        return msg

    def _send(self, msg: EmailMessage) -> str | None:
        s = self.smtp
        try:
            if s.tls == "ssl":
                server = smtplib.SMTP_SSL(s.host, s.port, timeout=30, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(s.host, s.port, timeout=30)
            with server:
                if s.tls == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if s.username:
                    server.login(s.username, s.password or "")
                server.send_message(msg)
            return None
        except (OSError, smtplib.SMTPException) as e:
            log.warning("email to %s failed: %s", s.to, e)
            return str(e) or e.__class__.__name__

    def _body(self, run: Run, summary: str, recovered: bool) -> str:
        link = f"{self.public_url}/runs/{run.id}" if self.public_url else f"/runs/{run.id} on the ansible-gui server"
        lines = [
            ("Back to normal. " if recovered else "") + summary + ".",
            "",
            f"Run:       #{run.id}  {link}",
            f"Schedule:  {run.trigger}",
            f"Playbook:  {run.playbook}",
            f"Targets:   {run.target or 'the playbook’s hosts'}",
            f"Mode:      {run.mode}",
        ]
        if run.started_at:
            lines.append(f"Started:   {run.started_at.astimezone():%Y-%m-%d %H:%M %Z}")
        if run.duration is not None:
            lines.append(f"Took:      {int(run.duration)}s")
        if run.commit_sha:
            lines.append(f"Commit:    {run.commit_sha}")
        if run.host_stats:
            lines += ["", "Hosts:"]
            width = max(len(h) for h in run.host_stats)
            for h, st in sorted(run.host_stats.items()):
                lines.append(f"  {h.ljust(width)}  {host_result(st):<11} ok={st.get('ok', 0)} changed={st.get('changed', 0)}"
                             f" failed={st.get('failed', 0)} unreachable={st.get('unreachable', 0)}")
        excerpt = self._excerpt(run)
        if excerpt:
            lines += ["", "From the log:", *("  " + line for line in excerpt)]
        return "\n".join(lines) + "\n"

    def _excerpt(self, run: Run, limit: int = 30) -> list[str]:
        """The failing tasks' lines, not the whole log (diffs can be long and sensitive)."""
        path = self.runs.log_path(run.id)
        if not path.exists():
            return []
        out, task = [], None
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("TASK ["):
                task = line.split("]")[0] + "]"
            elif line.startswith(("fatal:", "failed:")) or "UNREACHABLE!" in line:
                if task and task not in out:
                    out.append(task)
                out.append(line[:300])
            if len(out) >= limit:
                break
        return out
