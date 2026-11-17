"""HTTP routes. Server-rendered pages; htmx boosts navigation and polls live runs."""

from __future__ import annotations

import os

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
from .credentials.store import KINDS, PER_RUN, CredentialStore, Invalid
from .inventory.attribution import edit_url
from .keys import Keys
from . import overview as ov
from .notify import NOTIFY_MODES, Notifier
from .inventory.model import Layer, Vaulted
from .inventory.service import Inventories
from .repos import git as gitsync
from .repos.store import Invalid as RepoInvalid
from .repos.store import RepoStore
from .repos.workspace import Workspace
from .runs import playbooks
from .runs.executor import Executor
from .runs.schedules import Invalid as ScheduleInvalid
from .runs.schedules import Scheduler, ScheduleStore, check_cron, local_now, next_times
from .runs.store import Retention, Run, RunStore, host_result
from .headers import SecurityHeaders
from .sessions import SessionStore

HERE = Path(__file__).parent

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


def rerun_url(run: Run, mode: str) -> str:
    q = {"playbook": f"{run.repo_id}:{run.playbook}", "target": run.target, "mode": mode,
         "inventory": "" if run.inventory_id is None else str(run.inventory_id),
         "tags": run.tags, "skip_tags": run.skip_tags, "diff": "1" if run.diff else "0",
         "verbosity": str(run.verbosity)}
    for kind in PER_RUN:
        q[kind] = str((run.credentials.get(kind) or {}).get("id", ""))
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

    env.globals.update(
        present=present,
        edit_url=lambda layer, inv=None: edit_url((inv.edit_url if inv else None) or settings.edit_url, layer),
        inv_label=lambda inv_id: (ws.inventory_repo(inv_id).name if ws.inventory_repo(inv_id) else "removed inventory"),
        host_result=host_result,
        status_tone=lambda s: STATUS.get(s, ("neutral", s))[0],
        run_tone=run_tone,
        status_label=status_label,
        rerun_url=rerun_url,
        rerun_hosts=rerun_hosts,
        pb_label=lambda x: "Gather facts" if getattr(x, "kind", "") == "facts" else ws.label(x.repo_id, x.playbook),
    )
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
            "inventory": str(src.get("inventory") or ""),
            **{kind: str(src.get(kind) or "") for kind in PER_RUN},
        }

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
        return dict(
            playbooks=books, form=form, preview=pv, multi_repo=len(ws.playbook_repos()) > 1,
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

    def launcher(request: Request, form: dict, form_error: str | None = None, status: int = 200) -> HTMLResponse:
        return render(request, "run_new.html", status, section="runs", form_error=form_error, purpose="run",
                      **form_context(form))

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return render(request, "runs.html", section="runs", runs=store.recent())

    @app.get("/runs/new", response_class=HTMLResponse)
    def new_run(request: Request):
        form = read_form(request.query_params)
        if "diff" not in request.query_params:
            form["diff"] = True
        default_credentials(form, request.query_params)
        return launcher(request, form)

    @app.get("/runs/preview", response_class=HTMLResponse)
    def run_preview(request: Request):
        form = read_form(request.query_params)
        form["diff"] = request.query_params.get("diff") == "1"
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            form["playbook"] = ""
        pv = preview_for(form["playbook"], form["target"], chosen_inventory(form))
        return render(request, "_run_preview.html", section="runs", form=form, preview=pv, form_error=None,
                      purpose=request.query_params.get("purpose", "run"),
                      editing=request.query_params.get("editing") == "1")

    @app.post("/runs")
    async def start_run(request: Request):
        data = await request.form()
        form = read_form(data)
        form["diff"] = data.get("diff") == "1"
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            return launcher(request, form, "Choose one of the playbooks listed.", status=400)
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
        chosen: dict = {}
        for kind in PER_RUN:
            if not form[kind]:
                continue
            cred = creds.get(int(form[kind])) if form[kind].isdigit() else None
            if not cred or cred.kind != kind:
                return launcher(request, form, f"That {KINDS[kind].lower()} no longer exists. Choose another.", 400)
            chosen[kind] = {"id": cred.id, "name": cred.name}
        chosen["env_secret"] = [{"id": c.id, "name": c.name, "env": c.meta.get("env")}
                                for c in creds.by_kind("env_secret")]
        run = store.create(
            playbook=ws.find(form["playbook"]).path, repo_id=ws.find(form["playbook"]).repo.id,
            target=form["target"], tags=form["tags"], skip_tags=form["skip_tags"],
            check_mode=form["mode"] != "apply", diff=form["diff"], verbosity=form["verbosity"],
            expected_hosts=pv.hosts, trigger=auth.display_name(auth.current_user(request)), credentials=chosen,
            inventory_id=inv_repo.id,
        )
        executor.submit(run.id)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

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

    def credentials_page(request: Request, hosts_text: str | None = None, hosts_error: str | None = None,
                         status: int = 200) -> HTMLResponse:
        q = request.query_params
        host_keys = {"mode": settings.ssh_host_key_checking, "count": sshtools.known_hosts_count(settings.data_dir),
                     "path": sshtools.known_hosts_path(settings.data_dir), "error": hosts_error,
                     "text": sshtools.read_known_hosts(settings.data_dir) if hosts_text is None else hosts_text,
                     "saved": q.get("hosts") if q.get("hosts", "").isdigit() else None}
        return render(request, "credentials.html", status, section="credentials",
                      groups={kind: creds.by_kind(kind) for kind in KINDS}, kinds=KINDS,
                      ssh_panel=ssh_config_panel(), host_keys=host_keys, saved=q.get("saved"),
                      deleted=q.get("deleted"), key_file=keys.generated_at)

    @app.get("/credentials", response_class=HTMLResponse)
    def credentials(request: Request):
        return credentials_page(request)

    @app.post("/credentials/known-hosts")
    async def save_known_hosts(request: Request):
        text = str((await request.form()).get("known_hosts", ""))
        try:
            count = sshtools.save_known_hosts(settings.data_dir, text)
        except sshtools.InvalidKey as e:
            return credentials_page(request, hosts_text=text, hosts_error=str(e), status=400)
        return RedirectResponse(f"/credentials?hosts={count}#known-hosts", status_code=303)

    def cred_form(request: Request, kind: str, cred=None, values: dict | None = None,
                  errors: dict | None = None, status: int = 200) -> HTMLResponse:
        used_by = [sc for sc in schedules.all() if cred and cred.id in sc.credentials.values()] if cred else []
        used_by_repos = [r for r in repos.all() if cred and r.ssh_key_id == cred.id] if cred else []
        return render(request, "credential_form.html", status, section="credentials", kind=kind, kinds=KINDS,
                      cred=cred, values=values or {}, errors=errors or {}, used_by=used_by,
                      used_by_repos=used_by_repos)

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

    async def save_credential(request: Request, cred_id: int | None):
        data = await request.form()
        kind = data.get("kind", "")
        values = {k: data.get(k, "") for k in ("private_key", "certificate", "password", "env", "value", "username", "token")}
        values["remove_certificate"] = data.get("remove_certificate") == "1"
        is_default = data.get("is_default") == "1"
        try:
            cred = creds.save(kind=kind, name=data.get("name", ""), values=values, is_default=is_default,
                              user=auth.display_name(auth.current_user(request)), cred_id=cred_id)
        except Invalid as e:
            # Secrets are never echoed back into the page; the form asks for them again.
            keep = {"name": data.get("name", ""), "env": values["env"], "username": values["username"], "is_default": is_default,
                    "certificate": values["certificate"]}
            return cred_form(request, kind if kind in KINDS else "ssh_key", creds.get(cred_id) if cred_id else None,
                             keep, e.errors, status=400)
        return RedirectResponse(f"/credentials?saved={cred.id}", status_code=303)

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
        return RedirectResponse(f"/credentials?deleted={cred.name if cred else ''}", status_code=303)


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

    @app.get("/repos/{rid}", response_class=HTMLResponse)
    def edit_repo(request: Request, rid: int):
        repo = repos.get(rid)
        if not repo:
            return RedirectResponse("/repos", status_code=303)
        return repo_form(request, repo, values=vars(repo))

    async def save_repo(request: Request, rid: int | None):
        data = await request.form()
        form = {k: data.get(k, "") for k in ("name", "role", "url", "branch", "ssh_key_id", "paths", "edit_url",
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
                "inventory": "" if sched.inventory_id is None else str(sched.inventory_id)}
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
        form["name"] = q.get("name", "")
        form["cron"] = q.get("cron", "0 3 * * *")
        form["notify"] = "problems"
        default_credentials(form, q)
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
        sched = schedules.get(sid) if sid else None
        if sid and not sched:
            return RedirectResponse("/schedules", status_code=303)
        form["playbook"] = ws.canonical(form["playbook"])
        if form["playbook"] not in known_playbooks():
            return schedule_page(request, form, sched, form_error="Choose one of the playbooks listed.", status=400)
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

