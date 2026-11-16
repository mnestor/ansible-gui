"""HTTP routes. Server-rendered pages; htmx boosts navigation and polls live runs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .inventory.attribution import edit_url
from .inventory.model import Layer, Vaulted
from .inventory.service import InventoryService
from .runs import playbooks
from .runs.executor import Executor
from .runs.store import Run, RunStore, host_result

HERE = Path(__file__).parent

STATUS = {
    "queued": ("neutral", "Queued"),
    "running": ("accent", "Running"),
    "successful": ("ok", "Succeeded"),
    "failed": ("failed", "Failed"),
    "canceled": ("neutral", "Canceled"),
    "error": ("failed", "Couldn’t start"),
    "interrupted": ("changed", "Interrupted"),
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
    return f"{local:%b %d %H:%M}"


def status_label(run: Run) -> str:
    if run.status == "successful" and run.check_mode:
        return "Dry run passed"
    return STATUS.get(run.status, ("neutral", run.status))[1]


def rerun_url(run: Run, mode: str) -> str:
    q = {"playbook": run.playbook, "target": run.target, "mode": mode,
         "tags": run.tags, "skip_tags": run.skip_tags, "diff": "1" if run.diff else "0",
         "verbosity": str(run.verbosity)}
    return "/runs/new?" + urlencode({k: v for k, v in q.items() if v not in ("", None)})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    opts = settings.cli_options()
    service = InventoryService(opts)
    store = RunStore(settings.data_dir)
    executor = Executor(store, opts)

    app = FastAPI(title="ansible-gui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.executor = executor
    app.state.store = store
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(
        present=present,
        edit_url=lambda layer: edit_url(settings.edit_url, layer),
        host_result=host_result,
        status_tone=lambda s: STATUS.get(s, ("neutral", s))[0],
        status_label=status_label,
        rerun_url=rerun_url,
    )
    env.filters["clock"] = lambda dt: dt.astimezone().strftime("%H:%M:%S") if dt else ""
    env.filters["stamp"] = lambda dt: dt.astimezone().isoformat(timespec="seconds") if dt else ""
    env.filters["seconds"] = seconds
    env.filters["when"] = when

    def layer_text(layer: Layer) -> str:
        if not layer.path:
            return "set outside the repo"
        return f"{layer.path}:{layer.line}" if layer.line else layer.path

    env.filters["where"] = layer_text

    def render(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        active = sum(1 for r in store.recent(20) if not r.finished)
        return templates.TemplateResponse(
            request, template, {"settings": settings, "active_runs": active, **ctx}, status_code=status
        )

    # --- inventory -------------------------------------------------------------
    def inventory_page(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        state = service.current()
        return render(
            request, template, status,
            section="inventory",
            state=state,
            inv=state.inventory,
            reloaded=request.query_params.get("reloaded"),
            last_results=store.last_status_by_host(),
            **ctx,
        )

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/inventory", status_code=303)

    @app.get("/inventory", response_class=HTMLResponse)
    def overview(request: Request):
        return inventory_page(request, "overview.html", selected=None)

    @app.get("/inventory/hosts/{name}", response_class=HTMLResponse)
    def host(request: Request, name: str):
        inv = service.current().inventory
        h = inv.hosts.get(name) if inv else None
        if inv and not h:
            return inventory_page(request, "missing.html", status=404, kind="host", name=name, selected=None)
        return inventory_page(request, "host.html", host=h, selected=("host", name))

    @app.get("/inventory/groups/{name}", response_class=HTMLResponse)
    def group(request: Request, name: str):
        inv = service.current().inventory
        g = inv.groups.get(name) if inv else None
        if inv and not g:
            return inventory_page(request, "missing.html", status=404, kind="group", name=name, selected=None)
        return inventory_page(request, "group.html", group=g, selected=("group", name))

    @app.post("/inventory/reload")
    def reload(request: Request) -> RedirectResponse:
        state = service.reload()
        # Only ever return to a page of this app, whatever the Referer says.
        back = urlsplit(request.headers.get("referer", "")).path
        if not back.startswith("/inventory"):
            back = "/inventory"
        flag = "failed" if state.error else datetime.now().strftime("%H:%M:%S")
        return RedirectResponse(f"{back}?reloaded={flag}", status_code=303)

    # --- runs ------------------------------------------------------------------
    def known_playbooks() -> list[str]:
        return playbooks.discover(settings.project_dir, settings.playbook_dir)

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
        }

    def launcher(request: Request, form: dict, form_error: str | None = None, status: int = 200) -> HTMLResponse:
        books = known_playbooks()
        if form["playbook"] not in books:
            form["playbook"] = books[0] if len(books) == 1 else ""
        pv = playbooks.preview(opts, form["playbook"], form["target"]) if form["playbook"] else playbooks.Preview()
        inv = service.current().inventory
        return render(
            request, "run_new.html", status,
            section="runs", playbooks=books, form=form, preview=pv, form_error=form_error,
            hosts=sorted(inv.hosts) if inv else [], groups=sorted(g for g in inv.groups if g != "all") if inv else [],
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return render(request, "runs.html", section="runs", runs=store.recent())

    @app.get("/runs/new", response_class=HTMLResponse)
    def new_run(request: Request):
        form = read_form(request.query_params)
        if "diff" not in request.query_params:
            form["diff"] = True
        return launcher(request, form)

    @app.get("/runs/preview", response_class=HTMLResponse)
    def run_preview(request: Request):
        form = read_form(request.query_params)
        form["diff"] = request.query_params.get("diff") == "1"
        if form["playbook"] not in known_playbooks():
            form["playbook"] = ""
        pv = playbooks.preview(opts, form["playbook"], form["target"]) if form["playbook"] else playbooks.Preview()
        return render(request, "_run_preview.html", section="runs", form=form, preview=pv, form_error=None)

    @app.post("/runs")
    async def start_run(request: Request):
        data = await request.form()
        form = read_form(data)
        form["diff"] = data.get("diff") == "1"
        if form["playbook"] not in known_playbooks():
            return launcher(request, form, "Choose one of the playbooks listed.", status=400)
        pv = playbooks.preview(opts, form["playbook"], form["target"])
        if pv.error or not pv.hosts:
            return launcher(request, form, "Nothing to run: the targets match no hosts.", status=400)
        expected = sorted(data.getlist("expected"))
        if expected and expected != sorted(pv.hosts):
            return launcher(
                request, form,
                f"The targets changed since the preview: they now match {len(pv.hosts)} host(s). Check the list below, then run again.",
                status=409,
            )
        run = store.create(
            playbook=form["playbook"], target=form["target"], tags=form["tags"], skip_tags=form["skip_tags"],
            check_mode=form["mode"] != "apply", diff=form["diff"], verbosity=form["verbosity"],
            expected_hosts=pv.hosts, trigger="web UI",
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

    return app


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "ansible_gui.web:create_app",
        factory=True,
        host=os.environ.get("ANSIBLE_GUI_HOST", "127.0.0.1"),
        port=int(os.environ.get("ANSIBLE_GUI_PORT", "8080")),
    )

