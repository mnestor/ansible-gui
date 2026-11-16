"""HTTP routes. Server-rendered pages; htmx boosts navigation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings
from .inventory.attribution import edit_url
from .inventory.model import Layer, Vaulted
from .inventory.service import InventoryService

HERE = Path(__file__).parent


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = InventoryService(settings.cli_options())

    app = FastAPI(title="ansible-gui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals["present"] = present
    env.globals["edit_url"] = lambda layer: edit_url(settings.edit_url, layer)
    env.filters["clock"] = lambda dt: dt.astimezone().strftime("%H:%M:%S") if dt else ""
    env.filters["stamp"] = lambda dt: dt.astimezone().isoformat(timespec="seconds") if dt else ""

    def layer_text(layer: Layer) -> str:
        if not layer.path:
            return "set outside the repo"
        return f"{layer.path}:{layer.line}" if layer.line else layer.path

    env.filters["where"] = layer_text

    def page(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        state = service.current()
        return templates.TemplateResponse(
            request,
            template,
            {
                "state": state,
                "inv": state.inventory,
                "settings": settings,
                "reloaded": request.query_params.get("reloaded"),
                **ctx,
            },
            status_code=status,
        )

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/inventory", status_code=303)

    @app.get("/inventory", response_class=HTMLResponse)
    def overview(request: Request):
        return page(request, "overview.html", selected=None)

    @app.get("/inventory/hosts/{name}", response_class=HTMLResponse)
    def host(request: Request, name: str):
        inv = service.current().inventory
        h = inv.hosts.get(name) if inv else None
        if inv and not h:
            return page(request, "missing.html", status=404, kind="host", name=name, selected=None)
        return page(request, "host.html", host=h, selected=("host", name))

    @app.get("/inventory/groups/{name}", response_class=HTMLResponse)
    def group(request: Request, name: str):
        inv = service.current().inventory
        g = inv.groups.get(name) if inv else None
        if inv and not g:
            return page(request, "missing.html", status=404, kind="group", name=name, selected=None)
        return page(request, "group.html", group=g, selected=("group", name))

    @app.post("/inventory/reload")
    def reload(request: Request) -> RedirectResponse:
        state = service.reload()
        # Only ever return to a page of this app, whatever the Referer says.
        back = urlsplit(request.headers.get("referer", "")).path
        if not back.startswith("/inventory"):
            back = "/inventory"
        flag = "failed" if state.error else datetime.now().strftime("%H:%M:%S")
        return RedirectResponse(f"{back}?reloaded={flag}", status_code=303)

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
