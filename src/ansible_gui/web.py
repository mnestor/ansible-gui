"""HTTP routes. Server-rendered pages; htmx boosts navigation and polls live runs."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import Settings, is_web_url, scrub_environment
from .credentials import ssh as sshtools
from .facts import FactStore
from . import ansible_config
from .credentials.store import KINDS, PER_RUN, CredentialStore, Invalid
from .inventory.attribution import edit_url
from .repos import forge
from .keys import Keys
from . import overview as ov
from .notify import NOTIFY_MODES, Notifier
from .inventory.model import Layer, Vaulted
from .inventory.service import Inventories
from .repos import git as gitsync
from .repos.store import Invalid as RepoInvalid
from .repos.store import RepoStore
from .repos.workspace import Workspace
from .runs import playbooks, variables
from .runs.executor import Executor
from .runs.schedules import Invalid as ScheduleInvalid
from .runs.schedules import Scheduler, ScheduleStore, check_cron, local_now, next_times
from .runs.templates import FIELDS as TEMPLATE_FIELDS, Invalid as TemplateInvalid, TemplateStore, changed_fields
from .runs.store import REMOTE_USER, Retention, Run, RunStore, host_result
from .headers import SecurityHeaders
from .sessions import SessionStore

HERE = Path(__file__).parent
_static_versions: dict[str, tuple[int, int, str]] = {}


def static_url(name: str) -> str:
    """A /static URL that changes whenever the file does, so a browser never runs a cached app.js against
    newer pages. The hash is redone only when the file's mtime or size changes."""
    path = HERE / "static" / name
    st = path.stat()
    seen = _static_versions.get(name)
    if not seen or seen[:2] != (st.st_mtime_ns, st.st_size):
        seen = (st.st_mtime_ns, st.st_size, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
        _static_versions[name] = seen
    return f"/static/{name}?v={seen[2]}"

STATUS = {
    "queued": ("neutral", "Queued"),
    "running": ("neutral", "Running"),
    "successful": ("ok", "Succeeded"),
    "failed": ("failed", "Failed"),
    "canceled": ("neutral", "Canceled"),
    "error": ("failed", "Couldn’t start"),
    "interrupted": ("changed", "Interrupted"),
    "skipped": ("neutral", "Skipped"),
}


def present(value: Any) -> dict:
    """How a variable's value is drawn: a scalar inline, a list or mapping as YAML."""
    if isinstance(value, Vaulted):
        return {"kind": "vaulted"}
    if isinstance(value, (dict, list)):
        if not value:
            return {"kind": "scalar", "text": "{}" if isinstance(value, dict) else "[]", "type": "empty"}
        text = yaml.safe_dump(value, default_flow_style=False, sort_keys=False, allow_unicode=True).rstrip()
        return {"kind": "block", "text": text, "lines": text.count("\n") + 1}
    if value is None:
        return {"kind": "scalar", "text": "null", "type": "null"}
    if isinstance(value, bool):
        return {"kind": "scalar", "text": "true" if value else "false", "type": "bool"}
    if isinstance(value, (int, float)):
        return {"kind": "scalar", "text": str(value), "type": "number"}
    text = str(value)
    if text == "":
        return {"kind": "scalar", "text": '""', "type": "empty"}
    return {"kind": "scalar", "text": text, "type": "template" if "{{" in text or "{%" in text else "string"}


def seconds(value: float | None) -> str:
    if value is None:
        return "—"
    s = int(round(value))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def when(dt: datetime | None) -> str:
    if not dt:
        return "—"
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return f"Today {local:%H:%M}"
    if (local.date() - today).days == 1:
        return f"Tomorrow {local:%H:%M}"
    if (local.date() - today).days == -1:
        return f"Yesterday {local:%H:%M}"
    return f"{local:%b %d %H:%M}"


def found_changes(run: Run) -> bool:
    return run.status == "successful" and run.check_mode and any(st.get("changed") for st in run.host_stats.values())


def run_tone(run: Run) -> str:
    return "changed" if found_changes(run) else STATUS.get(run.status, ("neutral", run.status))[0]


def status_label(run: Run) -> str:
    if found_changes(run):
        return "Dry run: would change"
    if run.status == "successful" and run.check_mode:
        return "Dry run passed"
    return STATUS.get(run.status, ("neutral", run.status))[1]


BAD_REMOTE_USER = ("Connect as takes a user name: letters, digits, dots, dashes, underscores or @, up to 64. "
                   "Leave it empty to use the inventory’s.")
MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*){0,2}$")
COMMON_MODULES = ["ansible.builtin.shell", "ansible.builtin.command", "ansible.builtin.ping", "ansible.builtin.setup",
                  "ansible.builtin.package", "ansible.builtin.service", "ansible.builtin.systemd_service",
                  "ansible.builtin.reboot"]


def command_url(run: Run) -> str:
    q = {"target": run.target, "module": run.module, "args": run.module_args, "become": "1" if run.become else "",
         "remote_user": run.remote_user, "inventory": "" if run.inventory_id is None else str(run.inventory_id)}
    for kind in PER_RUN:
        q[kind] = str((run.credentials.get(kind) or {}).get("id", ""))
    return "/runs/command?" + urlencode({k: v for k, v in q.items() if v})


def rerun_url(run: Run, mode: str) -> str:
    q = {"playbook": f"{run.repo_id}:{run.playbook}", "target": run.target, "mode": mode,
         "inventory": "" if run.inventory_id is None else str(run.inventory_id),
         "tags": run.tags, "skip_tags": run.skip_tags, "diff": "1" if run.diff else "0",
         "verbosity": str(run.verbosity), "remote_user": run.remote_user,
         "template": "" if run.template_id is None else str(run.template_id)}
    for kind in PER_RUN:
        q[kind] = str((run.credentials.get(kind) or {}).get("id", ""))
    # Secret answers never go in a link: a template keeps its own, anything else is typed again.
    plain = {k: v for k, v in run.prompt_vars.items() if not (isinstance(v, dict) and variables.SECRET in v)}
    if plain:
        q["vars"] = json.dumps(plain, separators=(",", ":"))
    if run.extra_vars.strip():
        q["extra_vars"] = run.extra_vars
    if any(isinstance(v, dict) and variables.SECRET in v for v in run.prompt_vars.values()):
        q["rerun"] = str(run.id)  # its secrets stay on the server; an empty secret field reuses them
    return "/runs/new?" + urlencode({k: v for k, v in q.items() if v is not None})


def rerun_hosts(run: Run, mode: str, hosts: list[str]) -> str:
    """The launcher, filled in from `run` but limited to `hosts` (all of which that run targeted)."""
    url = urlsplit(rerun_url(run, mode))
    q = [(k, ",".join(hosts) if k == "target" else v) for k, v in parse_qsl(url.query, keep_blank_values=True)]
    return f"{url.path}?{urlencode(q)}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    lock_down(settings.data_dir)
    store = RunStore(settings.data_dir)
    keys = Keys.load(settings.secret_key, settings.data_dir)
    creds = CredentialStore(settings.data_dir, keys.fernet)
    repos = RepoStore(settings.data_dir)
    ws = Workspace(settings, repos, creds)
    invs = Inventories(ws)
    ws.on_inventory_changed = invs.reload
    facts = FactStore(settings.data_dir)
    executor = Executor(store, ws, creds, settings.data_dir, settings.ssh_config, facts=facts)
    sessions = SessionStore(settings.data_dir)
    if settings.run_retention_days:
        Retention(store, settings.run_retention_days)

    https = bool(settings.public_url and settings.public_url.startswith("https://"))
    app = FastAPI(title="ansible-gui", docs_url=None, redoc_url=None)
    # Starlette runs the last-added middleware first: the session must exist before the gate looks at it.
    app.add_middleware(auth.Gate, settings=settings, sessions=sessions)
    app.add_middleware(SessionMiddleware, secret_key=keys.session_secret, session_cookie="ansible_gui",
                       max_age=12 * 3600, same_site="lax", https_only=https)
    app.add_middleware(SecurityHeaders, hsts=https)
    app.state.creds = creds
    schedules = ScheduleStore(settings.data_dir)
    run_templates = TemplateStore(settings.data_dir)
    app.state.templates = run_templates
    notifier = Notifier(settings.smtp, store, schedules, settings.public_url)
    executor.on_finish = notifier.run_finished
    scheduler = Scheduler(schedules, store, executor, creds, on_finish=notifier.run_finished)
    for run_id in store.interrupted_at_start:
        notifier.run_finished(run_id)
    app.state.notifier = notifier
    app.state.workspace = ws
    app.state.repos = repos
    app.state.schedules = schedules
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.executor = executor
    app.state.store = store
    app.state.facts = facts
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env

    def run_label(x) -> str:
        """What a run or schedule did, in a few words: the playbook, Gather facts, or the command."""
        kind = getattr(x, "kind", "playbook")
        if kind == "facts":
            return "Gather facts"
        if kind == "command":
            what = x.module.removeprefix("ansible.builtin.")
            text = f"{what}: {x.module_args}" if x.module_args else what
            return text if len(text) <= 60 else text[:59] + "…"
        return ws.label(x.repo_id, x.playbook)
    env.globals.update(
        cred_list=lambda kind: "/credentials" if kind == "account" else "/credentials/secrets",
        present=present,
        edit_url=lambda layer, inv=None: edit_url((inv.link_template if inv else None) or settings.edit_url, layer),
        commit_link=lambda repo, sha: repo.commit_url(sha) if repo else None,
        playbook_repo=lambda rid: ws.playbook_repo(rid),
        inventory_repo=lambda iid: ws.inventory_repo(iid),
        forges=forge.FORGES,
        detected_forge=lambda url: (forge.KNOWN.get((forge.web_base(url) or ("",))[0]) or ""),
        inv_label=lambda inv_id: (ws.inventory_repo(inv_id).name if ws.inventory_repo(inv_id) else "removed inventory"),
        host_result=host_result,
        status_tone=lambda s: STATUS.get(s, ("neutral", s))[0],
        run_tone=run_tone,
        status_label=status_label,
        rerun_url=rerun_url,
        rerun_hosts=rerun_hosts,
        pb_label=run_label,
        command_url=command_url,
        static_url=static_url,
        shown_vars=variables.shown,
    )
    env.filters["extra_names"] = lambda text: [str(k) for k in variables.check_freeform(text or "")[0]]
    env.filters["clock"] = lambda dt: dt.astimezone().strftime("%H:%M:%S") if dt else ""
    env.filters["stamp"] = lambda dt: dt.astimezone().isoformat(timespec="seconds") if dt else ""
    env.filters["seconds"] = seconds
    env.filters["when"] = when
    # "Vault password" -> "vault password", but "SSH key" stays "SSH key".
    env.filters["lcfirst"] = lambda t: t if len(t) > 1 and t[1].isupper() else t[:1].lower() + t[1:]

    def layer_text(layer: Layer) -> str:
        if not layer.path:
            return "set outside the repo"
        return f"{layer.path}:{layer.line}" if layer.line else layer.path

    env.filters["where"] = layer_text

    def render(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        active = sum(1 for r in store.recent(20) if not r.finished)
        return templates.TemplateResponse(
            request, template,
            {"settings": settings, "active_runs": active, "user": auth.current_user(request), **ctx},
            status_code=status,
        )

    auth.install(app, settings, templates, sessions)

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    # --- inventory -------------------------------------------------------------
    def last_result(results: dict, inv, host: str):
        hit = results.get((inv.id, host))
        if hit is None and len(ws.inventory_repos()) == 1:
            hit = results.get((None, host))  # runs from before there could be several inventories
        return hit

    def inventory_page(request: Request, template: str, status: int = 200, listing: str = "hosts",
                       **ctx: Any) -> HTMLResponse:
        entries = invs.entries()
        results = store.last_status_by_host()
        return render(
            request, template, status,
            section=listing, listing=listing,
            entries=entries,
            loaded=[(i, st.inventory) for i, st in entries if st.inventory],
            multi_inv=len(entries) > 1,
            merged_hosts=invs.hosts(), merged_groups=invs.groups(), duplicates=invs.duplicate_hosts(),
            reloaded=request.query_params.get("reloaded"),
            last_result=lambda inv, host: last_result(results, inv, host),
            **ctx,
        )

    def overview_context() -> dict:
        entries = invs.entries()
        known = {name for name, _ in invs.hosts()} if any(st.inventory for _, st in entries) else None
        recent = store.recent(500)
        active = sorted((r for r in recent if not r.finished), key=lambda r: r.id)
        soon = datetime.now(timezone.utc) + timedelta(hours=24)
        upcoming = sorted((sc for sc in schedules.all() if sc.enabled and sc.next_run_at and sc.next_run_at <= soon),
                          key=lambda sc: sc.next_run_at)
        return {
            "host_issues": ov.host_issues(recent, known),
            "schedule_issues": ov.schedule_issues(schedules.all(), store),
            "setup_issues": ov.setup_issues(settings, entries, creds, repos.all()),
            "run_issues": ov.run_issues(recent),
            "email_issue": ov.email_issue(store) if settings.smtp else None,
            "active": active, "live": {r.id: executor.live_stats(r.id) or {} for r in active},
            "positions": {r.id: executor.queue_position(r.id) for r in active},
            "upcoming": upcoming, "recent": recent[:10], "week": ov.week_counts(recent),
            "has_schedules": bool(schedules.all()), "has_runs": bool(recent), "tz": local_now().tzname(),
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return render(request, "home.html", section="home", **overview_context())

    @app.get("/home/active", response_class=HTMLResponse)
    def home_active(request: Request, ids: str = ""):
        ctx = overview_context()
        resp = render(request, "_home_active.html", **ctx)
        # When a run finishes, the issues and recent lists change too: reload the page once.
        if ids != ",".join(str(r.id) for r in ctx["active"]):
            resp.headers["HX-Refresh"] = "true"
        return resp

    @app.get("/inventory", response_class=HTMLResponse)
    @app.get("/inventory/hosts", response_class=HTMLResponse)
    def overview(request: Request):
        return inventory_page(request, "overview.html", selected=None)

    @app.get("/inventory/groups", response_class=HTMLResponse)
    def groups_overview(request: Request):
        return inventory_page(request, "overview.html", listing="groups", selected=None)

    @app.get("/inventory/hosts/{name}", response_class=HTMLResponse)
    def host(request: Request, name: str):
        found = invs.find_host(name)
        if not found and invs.loaded():
            return inventory_page(request, "missing.html", status=404, kind="host", name=name, selected=None)
        return inventory_page(request, "host.html", name=name, found=found, selected=("host", name),
                              facts=facts.get(name), can_gather=bool(ws.playbook_repos()))

    @app.get("/inventory/groups/{name}", response_class=HTMLResponse)
    def group(request: Request, name: str):
        found = invs.find_group(name)
        if not found and invs.loaded():
            return inventory_page(request, "missing.html", status=404, listing="groups", kind="group", name=name,
                                  selected=None)
        return inventory_page(request, "group.html", listing="groups", name=name, found=found,
                              selected=("group", name))

    @app.post("/facts/gather")
    async def gather_facts(request: Request):
        """A read-only run: ansible TARGET -m setup, with the default credentials. Its facts are saved like
        any run's. It uses the first playbook repository's ansible.cfg and group_vars."""
        data = await request.form()
        target = str(data.get("target") or "").strip()
        inv_repo = chosen_inventory({"inventory": str(data.get("inventory") or "")})
        books = ws.playbook_repos()
        if not target or target.startswith("-") or inv_repo is None or not books:
            return PlainTextResponse("Gather facts needs a host or group, an inventory and a playbook repository.",
                                     status_code=400)
        chosen: dict = {}
        for kind in PER_RUN:
            cred = creds.default(kind)
            if cred:
                chosen[kind] = {"id": cred.id, "name": cred.name}
        chosen["env_secret"] = [{"id": c.id, "name": c.name, "env": c.meta.get("env")}
                                for c in creds.by_kind("env_secret")]
        run = store.create(kind="facts", playbook="", repo_id=books[0].id, target=target, tags="", skip_tags="",
                           check_mode=False, diff=False, verbosity=0, expected_hosts=[], credentials=chosen,
                           trigger=auth.display_name(auth.current_user(request)), inventory_id=inv_repo.id)
        executor.submit(run.id)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.post("/inventory/reload")
    def reload(request: Request) -> RedirectResponse:
        failed = any(st.error for _, st in invs.reload())
        # Only ever return to a page of this app, whatever the Referer says.
        back = urlsplit(request.headers.get("referer", "")).path
        if not back.startswith("/inventory"):
            back = "/inventory"
        flag = "failed" if failed else datetime.now().strftime("%H:%M:%S")
        return RedirectResponse(f"{back}?reloaded={flag}", status_code=303)

    # --- runs ------------------------------------------------------------------
    def known_playbooks() -> list[str]:
        return [p.key for p in ws.discover()]

    def chosen_inventory(form: dict):
        """The form's inventory: the one asked for, or the only one there is."""
        inv = ws.inventory_repo(int(form["inventory"])) if form.get("inventory", "").lstrip("-").isdigit() else None
        if inv is None and len(ws.inventory_repos()) == 1:
            inv = ws.inventory_repos()[0]
        form["inventory"] = str(inv.id) if inv else ""
        return inv

    def preview_for(key: str, target: str, inv) -> playbooks.Preview:
        pb = ws.find(key) if key else None
        if not pb:
            return playbooks.Preview()
        if inv is None:
            return playbooks.Preview(error="Choose the inventory to run against.")
        return playbooks.preview(ws.run_opts(pb.repo, inv), str((pb.repo.path / pb.path).resolve()), target)

    def read_form(src) -> dict:
        try:
            verbosity = max(0, min(4, int(src.get("verbosity") or 0)))
        except ValueError:
            verbosity = 0
        return {
            "playbook": src.get("playbook") or "",
            "target": (src.get("target") or "").strip(),
            "mode": "apply" if src.get("mode") == "apply" else "check",
            "diff": src.get("diff", "1") == "1",
            "tags": (src.get("tags") or "").strip(),
            "skip_tags": (src.get("skip_tags") or "").strip(),
            "verbosity": verbosity,
            "remote_user": (src.get("remote_user") or "").strip(),
            "template": str(src.get("template") or "") if str(src.get("template") or "").isdigit() else "",
            "inventory": str(src.get("inventory") or ""),
            **{kind: str(src.get(kind) or "") for kind in PER_RUN},
            "extra_vars": "\n".join(line.rstrip() for line in str(src.get("extra_vars") or "").splitlines()).strip("\n"),
            "prompt_vars": stored_vars(src),
            "var_raw": variables.raw_fields(src),
            "rerun": str(src.get("rerun") or "") if str(src.get("rerun") or "").isdigit() else "",
            "vars_mode": "yaml" if src.get("vars_mode") == "yaml" else "fields",
        }

    def effective(form: dict, prompts: list) -> dict:
        """The form as the fields view sees it. In the YAML view the declared variables are typed in the YAML:
        take them out into their fields (sent as plain text, like any field), and leave the rest as YAML."""
        if form.get("vars_mode") != "yaml" or not prompts:
            return form
        if any(p.name in (form.get("var_raw") or {}) for p in prompts if p.kind != "secret"):
            return form  # still drawn as fields: the switch was just clicked and the redraw hasn't landed
        answers, rest = variables.split_yaml(form.get("extra_vars", ""), prompts)
        secret = {p.name for p in prompts if p.kind == "secret"}
        raw = {k: v for k, v in (form.get("var_raw") or {}).items() if k.split(".")[0] in secret}
        raw.update({k: v if isinstance(v, list) else [v] for k, v in answers.items()})
        raw["__yaml__"] = [""]  # never empty: an empty one would fall back to the template's values
        return {**form, "var_raw": raw, "extra_vars": rest, "vars_mode": "fields"}

    def switch_view(form: dict, prompts: list) -> None:
        """Redraw for the view asked for, carrying the values over from the other one."""
        secret = {p.name for p in prompts if p.kind == "secret"}
        raw = form.get("var_raw") or {}
        has_fields = any(k in raw for k in (p.name for p in prompts if p.kind != "secret"))
        if form["vars_mode"] == "yaml" and has_fields:
            vals, _ = variables.values(variables.read_answers(raw, prompts), prompts, creds.fernet)
            plain = {k: v for k, v in vals.items() if k not in secret}
            form["extra_vars"] = variables.join_yaml(plain, form.get("extra_vars", ""))
            form["var_raw"] = {k: v for k, v in raw.items() if k.split(".")[0] in secret} or {"__yaml__": [""]}
        elif form["vars_mode"] == "fields" and not has_fields and form.get("extra_vars"):
            form.update(effective({**form, "vars_mode": "yaml"}, prompts))

    def secrets_of(source, form: dict) -> dict:
        """The encrypted answers of a run or template that an empty secret field may reuse: only for the same
        playbook, so they can't be handed to another one that prints a variable of that name."""
        if source is None or ws.canonical(f"{source.repo_id}:{source.playbook}") != ws.canonical(form["playbook"]):
            return {}
        return {k: v for k, v in source.prompt_vars.items() if isinstance(v, dict) and variables.SECRET in v}

    def kept_secrets(form: dict, base=None) -> dict:
        """What empty secret fields keep: `base` (the template or schedule being edited, or copied), else the
        template the form started from; then the run being re-run, which used what it used."""
        if base is None and form.get("template"):
            base = run_templates.get(int(form["template"]))
        kept = secrets_of(base, form)
        rerun = store.get(int(form["rerun"])) if form.get("rerun") else None
        return {**kept, **secrets_of(rerun, form)}

    def stored_vars(src) -> dict:
        """Prompt values a link carries as JSON (Run again, Save as template…). Never secrets."""
        try:
            data = json.loads(src.get("vars") or "{}")
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if variables.VAR_NAME.match(str(k))
                and not (isinstance(v, dict) and variables.SECRET in v)}

    def prompts_for(key: str) -> tuple[list, list]:
        """The playbook's prompts, from its roles' argument specs, and the roles that declare none."""
        pb = ws.find(key) if key else None
        if not pb:
            return [], []
        return variables.discover(pb.repo.path / pb.path, pb.repo.path)

    def inventory_notes(prompts: list, hosts: list[str], inv_repo) -> tuple[list[str], dict]:
        """For each prompt, and for ansible_user (Connect as): what the inventory already sets on the targeted
        hosts. Returns (every name, so a redraw can clear a note; notes by name)."""
        names = [p.name for p in prompts if p.name != "ansible_user"] + ["ansible_user"]
        inv = invs.get(inv_repo.id)[1].inventory if inv_repo else None
        if not inv or not hosts:
            return names, {}

        def short(v) -> str:
            text = "vaulted" if isinstance(v, Vaulted) else variables.show_value(v)
            return text if len(text) <= 40 else text[:39] + "…"

        notes = {}
        for name in names:
            found = [(h, inv.hosts[h].var(name)) for h in hosts if h in inv.hosts]
            found = sorted((h, v) for h, v in found if v is not None)
            if not found:
                notes[name] = {"state": "none", "total": len(hosts)}
                continue
            values = {short(v.value) for _, v in found}
            if len(values) == 1:
                where = sorted({v.source.path or v.source.label for _, v in found})
                notes[name] = {"state": "same", "value": values.pop(), "count": len(found), "total": len(hosts),
                               "host": found[0][0], "where": where[0] if len(where) == 1 else f"{len(where)} files"}
            else:
                notes[name] = {"state": "mixed", "items": [(h, short(v.value)) for h, v in found[:4]],
                               "more": max(0, len(found) - 4)}
        return names, notes

    def run_overrides(form: dict, prompts: list, hosts: list[str], inv_repo, first: int = 5) -> tuple[list, bool]:
        """For the first targeted hosts: each inventory value the run's variables override there,
        {name, was, now, where}. Second: whether the run sets any variable at all."""
        form = effective(form, prompts)
        sets: dict[str, str] = {}
        vals, _ = variables.values(form_answers(form, prompts), prompts, creds.fernet, kept_secrets(form))
        for k, v in vals.items():
            sets[k] = "secret" if isinstance(v, dict) and variables.SECRET in v else variables.show_value(v)
        free, _ = variables.check_freeform(form.get("extra_vars", ""))
        for k, v in free.items():
            sets.setdefault(str(k), variables.show_value(v))
        login, _ = login_for(form)
        if login:
            sets["ansible_user"] = sets["ansible_ssh_user"] = login
        inv = invs.get(inv_repo.id)[1].inventory if inv_repo else None
        out = []
        for h in hosts[:first]:
            host = inv.hosts.get(h) if inv else None
            found = []
            for name, now_text in sets.items():
                v = host.var(name) if host else None
                if v is not None:
                    was = "vaulted" if v.vaulted else variables.show_value(v.value)
                    found.append({"name": name, "was": was[:40], "now": now_text[:40],
                                  "where": v.source.path or v.source.label})
            out.append((h, found))
        return out, bool(sets)

    def form_answers(form: dict, prompts: list) -> dict:
        """What the prompt fields show: the fields as sent, or else the stored values (template, schedule, link)."""
        if form.get("var_raw"):
            return variables.read_answers(form["var_raw"], prompts)
        return variables.answers_from_values(prompts, form.get("prompt_vars") or {})

    def vars_for(form: dict, kept: dict) -> tuple[dict, str | None]:
        """The prompt values to store, or what is wrong with the variables. `kept` holds the secrets an empty
        secret field keeps."""
        prompts, _ = prompts_for(form["playbook"])
        form = effective(form, prompts)
        vals, errors = variables.values(form_answers(form, prompts), prompts, creds.fernet, kept)
        if errors:
            return {}, next(iter(errors.values()))
        free, problem = variables.check_freeform(form["extra_vars"])
        if problem:
            return {}, problem
        both = sorted(set(free) & set(vals))
        if both:
            return {}, f"{both[0]} is set in its own field and in Extra variables. Keep one."
        login, by = login_for(form)
        if login and {"ansible_user", "ansible_ssh_user"} & set(free):
            return {}, f"Extra variables set ansible_user, and so does {by}. Keep one."
        return vals, None

    def login_for(form: dict) -> tuple[str, str]:
        """The user the run signs in as, and what sets it: Connect as, else the account's user."""
        if form.get("remote_user"):
            return form["remote_user"], "Connect as"
        acct = creds.get(int(form["account"])) if str(form.get("account") or "").isdigit() else None
        if acct and acct.kind == "account" and acct.meta.get("username"):
            return acct.meta["username"], f"the account {acct.name}"
        return "", ""

    def shown(key: str, value) -> str:
        """A launcher field's value, as the template changes list shows it."""
        if key == "playbook":
            return str(value).split(":", 1)[-1] or "none"
        if key == "inventory":
            return ws.inventory_repo(int(value)).name if str(value).isdigit() and ws.inventory_repo(int(value)) else "none"
        if key == "mode":
            return "Apply" if value == "apply" else "Dry run"
        if key == "diff":
            return "on" if value else "off"
        if key == "verbosity":
            return "-" + "v" * int(value) if value else "default"
        if key in PER_RUN:
            cred = creds.get(int(value)) if str(value).isdigit() else None
            return cred.name if cred else "none"
        if key == "target":
            return value or "playbook’s hosts"
        if key == "remote_user":
            return value or "inventory’s user"
        if key == "extra_vars":
            text = " ".join(str(value or "").split())
            return (text[:40] + "…" if len(text) > 41 else text) or "none"
        return value or "none"

    def template_changes(form: dict) -> tuple[Any, list[dict]]:
        """The template the form started from, and each field changed from it: {label, was, now}."""
        t = run_templates.get(int(form["template"])) if form.get("template") else None
        if not t:
            return None, []
        form = effective(form, prompts_for(form["playbook"])[0])
        base = t.as_form()
        base["playbook"] = ws.canonical(base["playbook"])
        labels = dict(TEMPLATE_FIELDS)
        out = [{"label": labels[k] or KINDS[k], "was": shown(k, base.get(k)), "now": shown(k, form.get(k))}
               for k in changed_fields(form, base)]
        prompts, _ = prompts_for(form["playbook"])
        now_vals, _ = variables.values(form_answers(form, prompts), prompts, creds.fernet, kept_secrets(form, t))
        return t, out + variables.changes(now_vals, t.prompt_vars, prompts)

    def form_context(form: dict) -> dict:
        """What the launcher and the schedule form both need: playbooks, live host preview, credentials."""
        books = ws.discover()
        keys = [b.key for b in books]
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in keys:
            form["playbook"] = keys[0] if len(keys) == 1 else ""
        inv_repo = chosen_inventory(form)
        pv = preview_for(form["playbook"], form["target"], inv_repo)
        inv = invs.get(inv_repo.id)[1].inventory if inv_repo else None
        template, changes = template_changes(form)
        prompts, bare_roles = prompts_for(form["playbook"])
        form["answers"] = form_answers(form, prompts)
        kept = form["kept"] if form.get("kept") is not None else kept_secrets(form)
        note_names, notes = inventory_notes(prompts, pv.hosts, inv_repo)
        overrides, run_sets = run_overrides(form, prompts, pv.hosts, inv_repo)
        return dict(
            note_names=note_names, notes=notes, overrides=overrides, run_sets=run_sets,
            from_template=template, changes=changes, prompts=prompts, bare_roles=bare_roles,
            saved_secrets={k for k, v in kept.items() if isinstance(v, dict) and variables.SECRET in v},
            playbooks=books, form=form, preview=pv, multi_repo=len(ws.playbook_repos()) > 1,
            playbook_about=playbook_summaries(books),
            playbook_repos=ws.playbook_repos(), inventory_repo=inv_repo, inventories=ws.inventory_repos(),
            cred_choices={kind: creds.by_kind(kind) for kind in PER_RUN}, kinds=KINDS,
            env_secrets=creds.by_kind("env_secret"),
            hosts=sorted(inv.hosts) if inv else [], groups=sorted(g for g in inv.groups if g != "all") if inv else [],
        )

    def default_credentials(form: dict, given) -> None:
        for kind in PER_RUN:
            if kind not in given:
                default = creds.default(kind)
                form[kind] = str(default.id) if default else ""

    def pick_credentials(form: dict) -> tuple[dict, str | None]:
        """The credentials a form picked, as a run stores them, or the reason one can't be used."""
        chosen: dict = {}
        for kind in PER_RUN:
            if not form[kind]:
                continue
            cred = creds.get(int(form[kind])) if form[kind].isdigit() else None
            if not cred or cred.kind != kind:
                return {}, f"That {KINDS[kind].lower()} no longer exists. Choose another."
            chosen[kind] = {"id": cred.id, "name": cred.name}
        chosen["env_secret"] = [{"id": c.id, "name": c.name, "env": c.meta.get("env")}
                                for c in creds.by_kind("env_secret")]
        return chosen, None

    def launcher(request: Request, form: dict, form_error: str | None = None, status: int = 200) -> HTMLResponse:
        return render(request, "run_new.html", status, section="runs", form_error=form_error, purpose="run",
                      **form_context(form))

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return render(request, "runs.html", section="runs", runs=store.recent())

    def playbook_summaries(books) -> dict[str, str]:
        return {b.key: variables.summary(variables.describe(b.repo.path / b.path, b.repo.path)) for b in books}

    @app.get("/runs/playbooks", response_class=HTMLResponse)
    def playbook_list(request: Request):
        """Every playbook, with what it is for: its top comment, else its roles' descriptions, else its plays."""
        books = ws.discover()
        about = playbook_summaries(books)
        by_book: dict[str, list] = {}
        for t in run_templates.all():
            by_book.setdefault(ws.canonical(f"{t.repo_id}:{t.playbook}"), []).append(t)
        return render(request, "playbooks.html", section="runs", playbooks=books, about=about, templates_by=by_book)

    @app.get("/runs/new", response_class=HTMLResponse)
    def new_run(request: Request):
        q = request.query_params
        form = read_form(q)
        t = run_templates.get(int(form["template"])) if form["template"] else None
        if t:
            given = {k for k in q if k != "template"}
            asked = form
            form = {**t.as_form(), **{k: v for k, v in asked.items() if k in given}, "template": str(t.id),
                    "var_raw": asked["var_raw"]}
            if "vars" in given:
                form["prompt_vars"] = asked["prompt_vars"]
            if "diff" in given:
                form["diff"] = q.get("diff") == "1"
            return launcher(request, form)
        form["template"] = ""
        if "diff" not in q:
            form["diff"] = True
        default_credentials(form, q)
        return launcher(request, form)

    @app.get("/runs/preview", response_class=HTMLResponse)
    def run_preview(request: Request):
        form = read_form(request.query_params)
        form["diff"] = request.query_params.get("diff") == "1"
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            form["playbook"] = ""
        inv_repo = chosen_inventory(form)
        pv = preview_for(form["playbook"], form["target"], inv_repo)
        template, changes = template_changes(form)
        prompts = prompts_for(form["playbook"])[0]
        note_names, notes = inventory_notes(prompts, pv.hosts, inv_repo)
        overrides, run_sets = run_overrides(form, prompts, pv.hosts, inv_repo)
        return render(request, "_run_preview.html", section="runs", form=form, preview=pv, form_error=None,
                      note_names=note_names, notes=notes, oob=True, overrides=overrides, run_sets=run_sets,
                      from_template=template, changes=changes,
                      purpose=request.query_params.get("purpose", "run"),
                      editing=request.query_params.get("editing") == "1")

    @app.get("/runs/vars", response_class=HTMLResponse)
    def run_vars(request: Request):
        """The prompt fields, redrawn when the playbook changes. Answers to names both playbooks share stay."""
        q = request.query_params
        form = read_form(q)
        form["playbook"] = ws.canonical(form["playbook"])
        prompts, bare_roles = prompts_for(form["playbook"])
        switch_view(form, prompts)
        form["answers"] = form_answers(form, prompts)
        kind, _, ref = str(q.get("kept_from") or "").partition(":")
        source = (run_templates.get(int(ref)) if kind == "template" else schedules.get(int(ref)) if kind == "schedule"
                  else None) if ref.isdigit() else None
        kept = kept_secrets(form, source)
        inv_repo = chosen_inventory(form)
        note_names, notes = inventory_notes(prompts, preview_for(form["playbook"], form["target"], inv_repo).hosts,
                                            inv_repo)
        return render(request, "_run_vars.html", form=form, prompts=prompts, bare_roles=bare_roles, notes=notes,
                      kept_from=q.get("kept_from", ""),
                      saved_secrets={k for k, v in kept.items() if isinstance(v, dict) and variables.SECRET in v})

    @app.post("/runs")
    async def start_run(request: Request):
        data = await request.form()
        form = read_form(data)
        form["diff"] = data.get("diff") == "1"
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            return launcher(request, form, "Choose one of the playbooks listed.", status=400)
        if form["remote_user"] and not REMOTE_USER.match(form["remote_user"]):
            return launcher(request, form, BAD_REMOTE_USER, status=400)
        inv_repo = chosen_inventory(form)
        if inv_repo is None:
            return launcher(request, form, "Choose the inventory to run against.", status=400)
        pv = preview_for(form["playbook"], form["target"], inv_repo)
        if pv.error or not pv.hosts:
            return launcher(request, form, "Nothing to run: the targets match no hosts.", status=400)
        expected = sorted(data.getlist("expected"))
        if expected and expected != sorted(pv.hosts):
            return launcher(
                request, form,
                f"The targets changed since the preview: they now match {len(pv.hosts)} host(s). Check the list below, then run again.",
                status=409,
            )
        chosen, problem = pick_credentials(form)
        if problem:
            return launcher(request, form, problem, 400)
        template, changes = template_changes(form)
        form.update(effective(form, prompts_for(form["playbook"])[0]))  # the YAML view stores like the fields
        prompt_vals, problem = vars_for(form, kept_secrets(form))
        if problem:
            return launcher(request, form, problem, 400)
        run = store.create(
            playbook=ws.find(form["playbook"]).path, repo_id=ws.find(form["playbook"]).repo.id,
            target=form["target"], tags=form["tags"], skip_tags=form["skip_tags"],
            check_mode=form["mode"] != "apply", diff=form["diff"], verbosity=form["verbosity"],
            expected_hosts=pv.hosts, trigger=auth.display_name(auth.current_user(request)), credentials=chosen,
            inventory_id=inv_repo.id, remote_user=form["remote_user"],
            template_id=template.id if template else None, template_name=template.name if template else "",
            template_changes=[f"{c['label']}: {c['was']} → {c['now']}" for c in changes],
            prompt_vars=prompt_vals, extra_vars=form["extra_vars"],
        )
        executor.submit(run.id)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    # --- ad hoc commands ------------------------------------------------------------
    def read_command(src) -> dict:
        return {"target": (src.get("target") or "").strip(),
                "module": (src.get("module") or "").strip() or "ansible.builtin.shell",
                "args": (src.get("args") or "").strip(), "become": src.get("become") == "1",
                "remote_user": (src.get("remote_user") or "").strip(),
                "inventory": str(src.get("inventory") or ""), **{kind: str(src.get(kind) or "") for kind in PER_RUN}}

    def command_hosts(form: dict) -> playbooks.Preview:
        """Which hosts the command would reach. It runs with the first playbook repository's ansible.cfg and the
        group_vars next to its playbooks, as Gather facts does."""
        inv = chosen_inventory(form)
        books = ws.playbook_repos()
        if not form["target"]:
            return playbooks.Preview()
        if inv is None:
            return playbooks.Preview(error="Choose the inventory to run against.")
        if not books:
            return playbooks.Preview(error="Add a playbook repository first: commands use its ansible.cfg.")
        opts = dataclasses.replace(ws.run_opts(books[0], inv),
                                   playbook_dir=str((books[0].path / books[0].playbook_dir).resolve()))
        return playbooks.list_hosts(opts, form["target"])

    def command_page(request: Request, form: dict, form_error: str | None = None, status: int = 200) -> HTMLResponse:
        pv = command_hosts(form)
        inv_repo = ws.inventory_repo(int(form["inventory"])) if form["inventory"].isdigit() else None
        inv = invs.get(inv_repo.id)[1].inventory if inv_repo else None
        return render(request, "run_command.html", status, section="runs", form=form, form_error=form_error,
                      preview=pv, inventories=ws.inventory_repos(), modules=COMMON_MODULES,
                      cred_choices={kind: creds.by_kind(kind) for kind in PER_RUN}, kinds=KINDS,
                      env_secrets=creds.by_kind("env_secret"),
                      hosts=sorted(inv.hosts) if inv else [], groups=sorted(g for g in inv.groups if g != "all") if inv else [])

    @app.get("/runs/command", response_class=HTMLResponse)
    def new_command(request: Request):
        form = read_command(request.query_params)
        default_credentials(form, request.query_params)
        return command_page(request, form)

    @app.get("/runs/command/preview", response_class=HTMLResponse)
    def command_preview(request: Request):
        form = read_command(request.query_params)
        return render(request, "_command_preview.html", section="runs", form=form, form_error=None,
                      preview=command_hosts(form))

    @app.post("/runs/command")
    async def start_command(request: Request):
        data = await request.form()
        form = read_command(data)
        if not MODULE_NAME.match(form["module"]):
            return command_page(request, form, "Name a module, like ansible.builtin.shell or ansible.builtin.ping.", 400)
        if form["remote_user"] and not REMOTE_USER.match(form["remote_user"]):
            return command_page(request, form, BAD_REMOTE_USER, 400)
        if len(form["args"]) > 4000:
            return command_page(request, form, "The arguments are over 4000 characters. Put that in a playbook.", 400)
        pv = command_hosts(form)
        if pv.error or not pv.hosts:
            return command_page(request, form, pv.error or "Nothing to run: the targets match no hosts.", 400)
        if sorted(data.getlist("expected")) != sorted(pv.hosts):
            return command_page(request, form, f"The targets now match {len(pv.hosts)} host(s), not the ones listed "
                                "when you clicked. Check the list below, then run again.", 409)
        chosen, problem = pick_credentials(form)
        if problem:
            return command_page(request, form, problem, 400)
        run = store.create(kind="command", playbook="", module=form["module"], module_args=form["args"],
                           become=form["become"], repo_id=ws.playbook_repos()[0].id, target=form["target"], tags="",
                           skip_tags="", check_mode=False, diff=False, verbosity=0, expected_hosts=pv.hosts,
                           trigger=auth.display_name(auth.current_user(request)), credentials=chosen,
                           inventory_id=int(form["inventory"]), remote_user=form["remote_user"])
        executor.submit(run.id)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    # --- run templates ---------------------------------------------------------
    def template_page(request: Request, form: dict, tmpl=None, errors: dict | None = None,
                      form_error: str | None = None, flash: str | None = None, status: int = 200) -> HTMLResponse:
        form["template"] = ""  # the form edits the template; nothing to compare it with
        return render(request, "template_form.html", status, section="runs", tmpl=tmpl, errors=errors or {},
                      form_error=form_error, flash=flash, purpose="template", editing=bool(tmpl),
                      **form_context(form))

    @app.get("/runs/templates", response_class=HTMLResponse)
    def template_list(request: Request):
        return render(request, "templates.html", section="runs", templates_=run_templates.all(),
                      deleted=request.query_params.get("deleted"))

    @app.get("/runs/templates/new", response_class=HTMLResponse)
    def new_template(request: Request):
        q = request.query_params
        form = read_form(q)
        if "diff" not in q:
            form["diff"] = True
        default_credentials(form, q)  # only the kinds the link leaves out: Save as template… sends all, None included
        form["name"], form["note"] = q.get("name", ""), q.get("note", "")
        return template_page(request, form)

    @app.get("/runs/templates/{tid}", response_class=HTMLResponse)
    def show_template(request: Request, tid: int):
        t = run_templates.get(tid)
        if not t:
            return RedirectResponse("/runs/templates", status_code=303)
        form = {**t.as_form(), "name": t.name, "note": t.note, "kept": t.prompt_vars}
        return template_page(request, form, t, flash="Saved." if request.query_params.get("saved") else None)

    async def save_template(request: Request, tid: int | None):
        data = await request.form()
        form = read_form(data)
        form["diff"] = data.get("diff") == "1"
        form["name"], form["note"] = str(data.get("name") or ""), str(data.get("note") or "")
        tmpl = run_templates.get(tid) if tid else None
        if tid and not tmpl:
            return RedirectResponse("/runs/templates", status_code=303)
        form["kept"] = None
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            return template_page(request, form, tmpl, form_error="Choose one of the playbooks listed.", status=400)
        if form["remote_user"] and not REMOTE_USER.match(form["remote_user"]):
            return template_page(request, form, tmpl, form_error=BAD_REMOTE_USER, status=400)
        if problem := pick_credentials(form)[1]:
            return template_page(request, form, tmpl, form_error=problem, status=400)
        inv_repo = chosen_inventory(form)
        if inv_repo is None:
            return template_page(request, form, tmpl, form_error="Choose the inventory to run against.", status=400)
        form["kept"] = kept_secrets(form, tmpl)
        form.update(effective(form, prompts_for(form["playbook"])[0]))  # the YAML view stores like the fields
        prompt_vals, problem = vars_for(form, form["kept"])
        if problem:
            return template_page(request, form, tmpl, form_error=problem, status=400)
        pb = ws.find(form["playbook"])
        form.update(playbook_path=pb.path, repo_id=pb.repo.id, inventory_id=inv_repo.id, prompt_vars=prompt_vals)
        try:
            saved = run_templates.save(form, auth.display_name(auth.current_user(request)), tid)
        except TemplateInvalid as e:
            return template_page(request, form, tmpl, errors=e.errors, status=400)
        return RedirectResponse(f"/runs/templates/{saved.id}?saved=1", status_code=303)

    @app.post("/runs/templates")
    async def create_template(request: Request):
        return await save_template(request, None)

    @app.post("/runs/templates/{tid}")
    async def update_template(request: Request, tid: int):
        return await save_template(request, tid)

    @app.post("/runs/templates/{tid}/delete")
    def delete_template(tid: int):
        t = run_templates.get(tid)
        if t:
            run_templates.delete(tid)
        return RedirectResponse(f"/runs/templates?{urlencode({'deleted': t.name if t else ''})}", status_code=303)

    def run_context(run: Run, offset: int) -> dict:
        live = executor.live_stats(run.id)
        stats = run.host_stats if run.finished else (live or {h: {} for h in run.expected_hosts})
        stats = {h: {k: s.get(k, 0) for k in ("ok", "changed", "failed", "unreachable", "skipped")} for h, s in stats.items()}
        return {"run": run, "stats": stats, "queue_position": executor.queue_position(run.id), "log_offset": offset}

    def read_log(run_id: int, offset: int = 0) -> tuple[str, int]:
        path = store.log_path(run_id)
        if not path.exists():
            return "", offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        # Only hand out whole lines, so a half-written line is not split in two.
        cut = data.rfind(b"\n") + 1
        return data[:cut].decode("utf-8", "replace"), offset + cut

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = store.get(run_id)
        if not run:
            return render(request, "run_missing.html", status=404, section="runs", run_id=run_id)
        text, offset = read_log(run_id)
        return render(request, "run.html", section="runs", log_text=text, **run_context(run, offset))

    @app.get("/runs/{run_id}/live", response_class=HTMLResponse)
    def run_live(request: Request, run_id: int, offset: int = 0):
        run = store.get(run_id)
        if not run:
            return HTMLResponse("", status_code=404)
        chunk, offset = read_log(run_id, offset)
        resp = render(request, "_run_live.html", section="runs", chunk=chunk, **run_context(run, offset))
        if run.finished:
            resp.headers["HX-Refresh"] = "true"  # reload once for the final actions and full log
        return resp

    @app.get("/runs/{run_id}/log.txt")
    def run_log(run_id: int):
        path = store.log_path(run_id)
        if not store.get(run_id) or not path.exists():
            return PlainTextResponse("No output for this run.", status_code=404)
        return FileResponse(path, media_type="text/plain", filename=f"run-{run_id}.log")

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: int) -> RedirectResponse:
        executor.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    # --- credentials -----------------------------------------------------------
    def ssh_config_panel() -> dict:
        path = settings.ssh_config
        panel: dict = {"path": path, "exists": bool(path and path.is_file()), "text": None, "edit": None}
        if panel["exists"]:
            try:
                panel["text"] = path.read_text(errors="replace")[:20000]
            except OSError as e:
                panel["error"] = str(e)
            try:
                if not settings.project_dir:
                    raise ValueError
                rel = path.relative_to(settings.project_dir)
                panel["rel"] = str(rel)
                panel["edit"] = edit_url(settings.edit_url, Layer(kind="unknown", path=str(rel)))
            except ValueError:
                pass
        panel["overridden_on"] = sorted({
            h.name for _, data in invs.loaded() for h in data.hosts.values()
            if any(v.name in ("ansible_ssh_common_args", "ansible_ssh_args") for v in h.vars)
        })
        return panel

    def credentials_page(request: Request, view: str = "accounts") -> HTMLResponse:
        q = request.query_params
        everything = creds.all()
        if view == "accounts":
            items = [c for c in everything if c.kind == "account"]
        else:
            order = list(KINDS)
            items = sorted((c for c in everything if c.kind != "account"),
                           key=lambda c: (order.index(c.kind), c.name.lower()))
        used_by_repo: dict[int, list] = {}
        for r in repos.all():
            if r.ssh_key_id:
                used_by_repo.setdefault(r.ssh_key_id, []).append(r)
        return render(request, "credentials.html", section="credentials", view=view, items=items, kinds=KINDS,
                      token_repos=used_by_repo, saved=q.get("saved"), deleted=q.get("deleted"),
                      key_file=keys.generated_at,
                      legacy=any(c.kind in ("ssh_key", "become_password") for c in everything))

    def ssh_page(request: Request, hosts_text: str | None = None, hosts_error: str | None = None,
                 status: int = 200) -> HTMLResponse:
        q = request.query_params
        host_keys = {"mode": settings.ssh_host_key_checking, "count": sshtools.known_hosts_count(settings.data_dir),
                     "path": sshtools.known_hosts_path(settings.data_dir), "error": hosts_error,
                     "text": sshtools.read_known_hosts(settings.data_dir) if hosts_text is None else hosts_text,
                     "saved": q.get("hosts") if q.get("hosts", "").isdigit() else None}
        return render(request, "credentials_ssh.html", status, section="credentials",
                      ssh_panel=ssh_config_panel(), host_keys=host_keys)

    @app.get("/credentials", response_class=HTMLResponse)
    def credentials(request: Request):
        return credentials_page(request)

    @app.get("/credentials/secrets", response_class=HTMLResponse)
    def credential_secrets(request: Request):
        return credentials_page(request, "secrets")

    @app.get("/credentials/ssh", response_class=HTMLResponse)
    def credential_ssh(request: Request):
        return ssh_page(request)
    @app.post("/credentials/known-hosts")
    async def save_known_hosts(request: Request):
        text = str((await request.form()).get("known_hosts", ""))
        try:
            count = sshtools.save_known_hosts(settings.data_dir, text)
        except sshtools.InvalidKey as e:
            return ssh_page(request, hosts_text=text, hosts_error=str(e), status=400)
        return RedirectResponse(f"/credentials/ssh?hosts={count}#known-hosts", status_code=303)

    def cred_form(request: Request, kind: str, cred=None, values: dict | None = None,
                  errors: dict | None = None, status: int = 200) -> HTMLResponse:
        used_by = [sc for sc in schedules.all() if cred and cred.id in sc.credentials.values()] if cred else []
        used_by_repos = [r for r in repos.all() if cred and r.ssh_key_id == cred.id] if cred else []
        return render(request, "credential_form.html", status, section="credentials", kind=kind, kinds=KINDS,
                      cred=cred, values=values or {}, errors=errors or {}, used_by=used_by,
                      used_by_repos=used_by_repos,
                      other_creds={k: creds.by_kind(k) for k in ("ssh_key", "become_password")})

    @app.get("/credentials/new", response_class=HTMLResponse)
    def new_credential(request: Request, kind: str = "ssh_key"):
        return cred_form(request, kind if kind in KINDS else "ssh_key",
                         values={"is_default": not creds.by_kind(kind)})

    @app.get("/credentials/{cred_id}", response_class=HTMLResponse)
    def edit_credential(request: Request, cred_id: int):
        cred = creds.get(cred_id)
        if not cred:
            return RedirectResponse("/credentials", status_code=303)
        return cred_form(request, cred.kind, cred, values={"name": cred.name, "is_default": cred.is_default,
                                                           "env": cred.meta.get("env", ""),
                                                           "username": cred.meta.get("username", "")})

    def list_for(kind: str) -> str:
        return "/credentials" if kind == "account" else "/credentials/secrets"

    def copy_into_account(values: dict) -> None:
        """An account can start from a saved SSH key and become password: their values, decrypted here, unless
        something was typed in their place."""
        for field, kind in (("copy_key_from", "ssh_key"), ("copy_become_from", "become_password")):
            src = creds.get(int(values[field])) if str(values[field]).isdigit() else None
            if not src or src.kind != kind or not src.readable:
                continue
            secret = creds.secret(src)
            if kind == "ssh_key" and not values["private_key"].strip():
                values["private_key"] = secret["private_key"]
                values["certificate"] = values["certificate"] or src.meta.get("certificate", "")
            elif kind == "become_password" and not values["become_password"]:
                values["become_password"] = secret["password"]

    async def save_credential(request: Request, cred_id: int | None):
        data = await request.form()
        kind = data.get("kind", "")
        values = {k: data.get(k, "") for k in ("private_key", "certificate", "password", "env", "value", "username", "token",
                                                "become_password", "copy_key_from", "copy_become_from")}
        for flag in ("remove_certificate", "remove_key", "remove_become"):
            values[flag] = data.get(flag) == "1"
        if kind == "account":
            copy_into_account(values)
        is_default = data.get("is_default") == "1"
        try:
            cred = creds.save(kind=kind, name=data.get("name", ""), values=values, is_default=is_default,
                              user=auth.display_name(auth.current_user(request)), cred_id=cred_id)
        except Invalid as e:
            # Secrets are never echoed back into the page; the form asks for them again.
            keep = {"name": data.get("name", ""), "env": values["env"], "username": values["username"], "is_default": is_default,
                    "certificate": values["certificate"] if not values["copy_key_from"] else "",
                    "copy_key_from": values["copy_key_from"], "copy_become_from": values["copy_become_from"]}
            return cred_form(request, kind if kind in KINDS else "ssh_key", creds.get(cred_id) if cred_id else None,
                             keep, e.errors, status=400)
        return RedirectResponse(f"{list_for(cred.kind)}?saved={cred.id}", status_code=303)

    @app.post("/credentials")
    async def create_credential(request: Request):
        return await save_credential(request, None)

    @app.post("/credentials/{cred_id}")
    async def update_credential(request: Request, cred_id: int):
        return await save_credential(request, cred_id)

    @app.post("/credentials/{cred_id}/delete")
    def delete_credential(cred_id: int):
        cred = creds.get(cred_id)
        if cred:
            creds.delete(cred_id)
        return RedirectResponse(f"{list_for(cred.kind if cred else 'account')}?deleted={cred.name if cred else ''}",
                                status_code=303)


    # --- repositories ------------------------------------------------------------
    def repo_users(repo) -> list:
        if repo.role != "playbooks":
            return []
        return [sc for sc in schedules.all() if sc.repo_id == repo.id]

    @app.get("/repos", response_class=HTMLResponse)
    def repos_page(request: Request):
        q = request.query_params
        return render(request, "repos.html", section="repos", repos=repos.all(),
                      inventories=ws.inventory_repos(), playbook_repos=ws.playbook_repos(),
                      ssh_keys={c.id: c for c in creds.all()},
                      saved=q.get("saved"), pulled=q.get("pulled"), deleted=q.get("deleted"))

    def repo_form(request: Request, repo=None, values: dict | None = None, errors: dict | None = None,
                  status: int = 200) -> HTMLResponse:
        return render(request, "repo_form.html", status, section="repos", repo=repo, values=values or {},
                      errors=errors or {}, ssh_keys=creds.by_kind("ssh_key"), git_tokens=creds.by_kind("git_token"),
                      used_by=repo_users(repo) if repo else [])

    @app.get("/repos/new", response_class=HTMLResponse)
    def new_repo(request: Request, role: str = ""):
        has_inv = any(r.role == "inventory" for r in repos.all())
        role = role if role in ("inventory", "playbooks") else ("playbooks" if has_inv else "inventory")
        return repo_form(request, values={"role": role, "branch": "main",
                                          "paths": "inventory/hosts.yml" if role == "inventory" else ""})

    @app.get("/repos/{rid}/ansible", response_class=HTMLResponse)
    def repo_ansible(request: Request, rid: int):
        repo = ws.playbook_repo(rid)
        if not repo:
            return RedirectResponse("/repos", status_code=303)
        found = ansible_config.report(repo.run_dir, repo.path, ws.ansible_env(repo)) if repo.run_dir.is_dir() else None
        return render(request, "ansible_config.html", section="repos", repo=repo, report=found)

    @app.get("/repos/{rid}", response_class=HTMLResponse)
    def edit_repo(request: Request, rid: int):
        repo = repos.get(rid)
        if not repo:
            return RedirectResponse("/repos", status_code=303)
        return repo_form(request, repo, values=vars(repo))

    async def save_repo(request: Request, rid: int | None):
        data = await request.form()
        form = {k: data.get(k, "") for k in ("name", "role", "url", "branch", "ssh_key_id", "paths", "edit_url", "forge",
                                             "requirements")}
        if form["ssh_key_id"]:
            c = creds.get(int(form["ssh_key_id"])) if form["ssh_key_id"].isdigit() else None
            want = "git_token" if gitsync.is_https(form["url"].strip()) else "ssh_key"
            if not c:
                problem = "That credential no longer exists. Choose another."
            elif c.kind != want:
                problem = ("An HTTPS URL signs in with a git token." if want == "git_token"
                           else "An SSH URL signs in with an SSH key.") + " Pick one of those, or change the URL."
            elif want == "git_token" and not form["url"].strip().startswith("https://"):
                problem = "A git token is only sent over https://, never unencrypted. Use the https:// URL."
            else:
                problem = None
            if problem:
                return repo_form(request, repos.get(rid) if rid else None, form, {"ssh_key_id": problem}, status=400)
        try:
            repo = repos.save(form, auth.display_name(auth.current_user(request)), rid)
        except RepoInvalid as e:
            return repo_form(request, repos.get(rid) if rid else None, form, e.errors, status=400)
        ws.sync(repo)  # a first pull right away, so a wrong URL or key shows up now, not at 03:00
        if repo.role == "inventory":
            invs.reload(repo.id)
        return RedirectResponse(f"/repos?saved={repo.id}", status_code=303)

    @app.post("/repos")
    async def create_repo(request: Request):
        return await save_repo(request, None)

    @app.post("/repos/{rid}")
    async def update_repo(request: Request, rid: int):
        return await save_repo(request, rid)

    @app.post("/repos/{rid}/pull")
    def pull_repo(rid: int):
        repo = repos.get(rid)
        if repo:
            ws.sync(repo)
            if repo.role == "inventory":
                invs.reload(repo.id)
        return RedirectResponse(f"/repos?pulled={rid}", status_code=303)

    @app.post("/repos/{rid}/delete")
    def delete_repo(rid: int):
        repo = repos.get(rid)
        if repo:
            repos.delete(rid)
            gitsync.remove_clone(repo)
            if repo.role == "inventory":
                invs.reload(repo.id)
        return RedirectResponse(f"/repos?deleted={repo.name if repo else ''}", status_code=303)

    # --- schedules -------------------------------------------------------------
    tz_name = local_now().tzname()

    def when_context(cron: str) -> dict:
        problem = check_cron(cron) if cron.strip() else None
        return {"when_error": problem, "when_next": [] if problem or not cron.strip() else next_times(cron, 3)}

    def schedule_page(request: Request, form: dict, sched=None, errors: dict | None = None,
                      form_error: str | None = None, flash: str | None = None, status: int = 200) -> HTMLResponse:
        return render(request, "schedule_form.html", status, section="schedules", sched=sched, errors=errors or {},
                      notify_modes=NOTIFY_MODES, smtp=settings.smtp,
                      form_error=form_error, flash=flash, purpose="schedule", editing=bool(sched), tz=tz_name,
                      recent=store.for_schedule(sched.id) if sched else [], **when_context(form["cron"]),
                      **form_context(form))

    def schedule_form(sched) -> dict:
        form = {"name": sched.name, "cron": sched.cron, "playbook": f"{sched.repo_id}:{sched.playbook}",
                "target": sched.target,
                "mode": "check" if sched.check_mode else "apply", "diff": sched.diff, "tags": sched.tags,
                "skip_tags": sched.skip_tags, "verbosity": sched.verbosity, "notify": sched.notify,
                "remote_user": sched.remote_user, "template_name": sched.template_name,
                "inventory": "" if sched.inventory_id is None else str(sched.inventory_id),
                "prompt_vars": sched.prompt_vars, "extra_vars": sched.extra_vars, "kept": sched.prompt_vars}
        form.update({k: str(sched.credentials.get(k, "")) for k in PER_RUN})
        return form

    @app.get("/schedules", response_class=HTMLResponse)
    def schedule_list(request: Request):
        items = schedules.all()
        for sc in items:
            recent = store.for_schedule(sc.id, 1)
            sc.last_run = recent[0] if recent else None
        return render(request, "schedules.html", section="schedules", schedules=items, tz=tz_name,
                      deleted=request.query_params.get("deleted"), smtp=settings.smtp,
                      last_notified=store.last_notified(),
                      mailed=request.query_params.get("mailed"), mail_error=request.session.pop("mail_error", None))

    @app.post("/schedules/test-email")
    def test_email(request: Request):
        error = notifier.send_test(auth.display_name(auth.current_user(request)))
        if error:
            request.session["mail_error"] = error
            return RedirectResponse("/schedules#email", status_code=303)
        return RedirectResponse("/schedules?mailed=1#email", status_code=303)

    @app.get("/schedules/new", response_class=HTMLResponse)
    def new_schedule(request: Request):
        q = request.query_params
        form = read_form(q)
        form["diff"] = q.get("diff", "1") == "1"
        t = run_templates.get(int(form["template"])) if form["template"] else None
        if t:  # the template, then whatever the link says (Schedule this… on a run that changed it)
            given = {k for k in q if k != "template"}
            asked = form
            form = {**t.as_form(), **{k: v for k, v in asked.items() if k in given}, "var_raw": asked["var_raw"]}
            if "vars" in given:
                form["prompt_vars"] = asked["prompt_vars"]
            form["diff"] = q.get("diff") == "1" if "diff" in given else t.diff
            form["copy_from"] = str(t.id)  # its secrets come along
            form["kept"] = kept_secrets(form, t)
        else:
            default_credentials(form, q)
        form["template"] = ""
        form["template_name"] = t.name if t else ""
        form["name"] = q.get("name", "")
        form["cron"] = q.get("cron", "0 3 * * *")
        form["notify"] = "problems"
        return schedule_page(request, form)

    @app.get("/schedules/when", response_class=HTMLResponse)
    def schedule_when(request: Request, cron: str = ""):
        return render(request, "_schedule_when.html", **when_context(cron))

    async def save_schedule(request: Request, sid: int | None):
        data = await request.form()
        form = read_form(data)
        form["diff"] = data.get("diff") == "1"
        form["name"] = data.get("name", "")
        form["cron"] = data.get("cron", "")
        form["notify"] = data.get("notify", "problems")
        form["template_name"] = str(data.get("template_name") or "")[:64]
        sched = schedules.get(sid) if sid else None
        if sid and not sched:
            return RedirectResponse("/schedules", status_code=303)
        copy_from = str(data.get("copy_from") or "")
        source = sched or (run_templates.get(int(copy_from)) if copy_from.isdigit() else None)
        form["copy_from"] = copy_from if not sched else ""
        form["playbook"] = ws.canonical(form["playbook"])
        form["kept"] = kept_secrets(form, source)
        if form["playbook"] not in known_playbooks():
            return schedule_page(request, form, sched, form_error="Choose one of the playbooks listed.", status=400)
        if form["remote_user"] and not REMOTE_USER.match(form["remote_user"]):
            return schedule_page(request, form, sched, form_error=BAD_REMOTE_USER, status=400)
        for kind in PER_RUN:
            if form[kind] and not (form[kind].isdigit() and (c := creds.get(int(form[kind]))) and c.kind == kind):
                return schedule_page(request, form, sched, status=400,
                                     form_error=f"That {KINDS[kind].lower()} no longer exists. Choose another.")
        inv_repo = chosen_inventory(form)
        if inv_repo is None:
            return schedule_page(request, form, sched, status=400, form_error="Choose the inventory to run against.")
        pv = preview_for(form["playbook"], form["target"], inv_repo)
        if pv.error or not pv.hosts:
            return schedule_page(request, form, sched, status=400,
                                 form_error="The targets match no hosts, so every run would fail. Change them first.")
        form.update(effective(form, prompts_for(form["playbook"])[0]))  # the YAML view stores like the fields
        prompt_vals, problem = vars_for(form, form["kept"])
        if problem:
            return schedule_page(request, form, sched, status=400, form_error=problem)
        form["prompt_vars"] = prompt_vals
        form["inventory_id"] = inv_repo.id
        form["enabled"] = sched.enabled if sched else True
        chosen_pb = ws.find(form["playbook"])
        form["playbook_path"], form["repo_id"] = chosen_pb.path, chosen_pb.repo.id
        try:
            saved = schedules.save(form, auth.display_name(auth.current_user(request)), sid)
        except ScheduleInvalid as e:
            return schedule_page(request, form, sched, errors=e.errors, status=400)
        return RedirectResponse(f"/schedules/{saved.id}?saved=1", status_code=303)

    @app.post("/schedules")
    async def create_schedule(request: Request):
        return await save_schedule(request, None)

    @app.get("/schedules/{sid}", response_class=HTMLResponse)
    def show_schedule(request: Request, sid: int):
        sched = schedules.get(sid)
        if not sched:
            return RedirectResponse("/schedules", status_code=303)
        flash = None
        if request.query_params.get("saved"):
            flash = (f"Saved. Next run: {when(sched.next_run_at)}." if sched.enabled else "Saved. It’s paused, so nothing runs.")
        elif request.query_params.get("paused"):
            flash = "Paused. Nothing runs until you resume it."
        elif request.query_params.get("resumed"):
            flash = f"Resumed. Next run: {when(sched.next_run_at)}."
        return schedule_page(request, schedule_form(sched), sched, flash=flash)

    @app.post("/schedules/{sid}")
    async def update_schedule(request: Request, sid: int):
        return await save_schedule(request, sid)

    @app.post("/schedules/{sid}/run")
    def run_schedule_now(request: Request, sid: int):
        sched = schedules.get(sid)
        if not sched:
            return RedirectResponse("/schedules", status_code=303)
        run = scheduler.fire(sched, by=auth.display_name(auth.current_user(request)))
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.post("/schedules/{sid}/pause")
    def pause_schedule(request: Request, sid: int):
        schedules.set_enabled(sid, False, auth.display_name(auth.current_user(request)))
        return RedirectResponse(f"/schedules/{sid}?paused=1", status_code=303)

    @app.post("/schedules/{sid}/resume")
    def resume_schedule(request: Request, sid: int):
        schedules.set_enabled(sid, True, auth.display_name(auth.current_user(request)))
        return RedirectResponse(f"/schedules/{sid}?resumed=1", status_code=303)

    @app.post("/schedules/{sid}/delete")
    def delete_schedule(sid: int):
        sched = schedules.get(sid)
        if sched:
            schedules.delete(sid)
        return RedirectResponse(f"/schedules?deleted={sched.name if sched else ''}", status_code=303)

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    host = os.environ.get("ANSIBLE_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("ANSIBLE_GUI_PORT", "8080"))
    proxies = os.environ.get("ANSIBLE_GUI_TRUSTED_PROXIES", "127.0.0.1")
    check_bind(settings, host, os.environ.get("ANSIBLE_GUI_INSECURE_NO_AUTH") == "1")
    # Everything is read: from here on no child process can see the secrets in the environment.
    scrub_environment()
    uvicorn.run(create_app(settings), host=host, port=port, proxy_headers=True, forwarded_allow_ips=proxies)


def lock_down(data_dir: Path) -> None:
    """Only the server's own user can read the data: credentials, run logs, the git clones.

    The directory goes to 0700, which also closes everything already inside it, and the
    umask makes every new file and directory private, including those git and Ansible write."""
    os.umask(0o077)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
        for name in ("ansible-gui.db", "ansible-gui.db-journal", "ansible-gui.db-wal", "ansible-gui.db-shm",
                     "secret.key"):
            f = data_dir / name
            if f.exists():
                os.chmod(f, 0o600)
    except PermissionError as e:
        raise SystemExit(
            f"{e.filename} isn’t owned by the user ansible-gui runs as (uid {os.getuid()}), so it can’t be made "
            f"private. On the host, give it to that user: sudo chown -R {os.getuid()} <the directory mounted at "
            f"{data_dir}>") from None


def check_bind(settings: Settings, host: str, insecure_ok: bool) -> None:
    """Without sign-in, anyone who reaches the port can run playbooks. Only allow that on loopback."""
    if settings.auth_enabled or insecure_ok or host in ("127.0.0.1", "::1", "localhost"):
        return
    raise SystemExit(
        f"Refusing to listen on {host} without sign-in: anyone who can reach it could run playbooks. "
        "Set ANSIBLE_GUI_OIDC_ISSUER (and its client settings), or ANSIBLE_GUI_INSECURE_NO_AUTH=1 if "
        "something in front of ansible-gui already does the sign-in."
    )

