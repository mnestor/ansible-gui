"""Sign-in through an OIDC provider (Pocket ID, Authelia, Keycloak…), plus two checks on every request
whatever the sign-in: that it is addressed to this server by name, and, for writes, that it comes from it.

Without ANSIBLE_GUI_OIDC_ISSUER there is no sign-in, and main() refuses to listen
anywhere but loopback unless told otherwise.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings, host_name
from .sessions import SessionStore

log = logging.getLogger(__name__)

OPEN_PATHS = ("/static/", "/auth/", "/healthz")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def safe_next(target: str | None) -> str:
    """Only ever go back to a path on this app."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def current_user(request: Request) -> dict | None:
    return request.session.get("user") if "session" in request.scope else None


def display_name(user: dict | None) -> str:
    if not user:
        return "local (no sign-in)"
    return user.get("username") or user.get("name") or user.get("email") or user.get("sub", "?")


class Gate(BaseHTTPMiddleware):
    """Signed-in users only, and writes only from this app's own pages."""

    def __init__(self, app, settings: Settings, sessions: SessionStore):
        super().__init__(app)
        self.settings = settings
        self.sessions = sessions

    def _origin(self, request: Request) -> str:
        if self.settings.public_url:
            u = urlsplit(self.settings.public_url)
        else:
            u = request.url
        return f"{u.scheme}://{u.netloc}"

    def _wrong_host(self) -> str:
        if self.settings.public_url:
            return (f"Refused: ansible-gui is at {self.settings.public_url}, and this request was addressed to "
                    "another name. If that name is right, set ANSIBLE_GUI_PUBLIC_URL to it.")
        return ("Refused: this request was addressed to a name ansible-gui doesn’t answer to. It answers to "
                "localhost and 127.0.0.1; to reach it under another name, set ANSIBLE_GUI_PUBLIC_URL to that "
                "address, like https://ansible.example.com.")

    async def dispatch(self, request: Request, call_next) -> Response:
        # The path as the router sees it, never one worked out from the Host header.
        path = request.scope["path"]
        # First of all, and for every page: is this request addressed to us? (Settings.allowed_hosts says why.)
        # /healthz says only "ok", so a probe may ask for it by container name or address.
        if path != "/healthz" and host_name(request.headers.get("host", "")) not in self.settings.allowed_hosts:
            return PlainTextResponse(self._wrong_host(), status_code=400)
        if request.method in UNSAFE:
            sent = request.headers.get("origin") or request.headers.get("referer")
            if sent:
                u = urlsplit(sent)
                if f"{u.scheme}://{u.netloc}" != self._origin(request):
                    return PlainTextResponse("Refused: this request came from another site.", status_code=403)
        if not self.settings.auth_enabled or path.startswith(OPEN_PATHS):
            return await call_next(request)
        if current_user(request) and self.sessions.valid(request.session.get("sid")):
            return await call_next(request)
        # Signed out elsewhere, older than the session lifetime, or from before sessions were recorded.
        request.session.clear()
        here = path + (f"?{request.url.query}" if request.url.query else "")
        if request.headers.get("hx-request") or request.method != "GET":
            # Return to the page the person was on, not to a fragment URL or a form POST target.
            page = request.headers.get("hx-current-url") or request.headers.get("referer") or "/"
            u = urlsplit(page)
            here = safe_next(u.path + (f"?{u.query}" if u.query else ""))
        login = "/auth/login?" + urlencode({"next": here})
        if request.headers.get("hx-request"):
            # htmx swaps would put the login page inside the current one; reload instead.
            return Response(status_code=204, headers={"HX-Redirect": login})
        return RedirectResponse(login, status_code=303)


def install(app: FastAPI, settings: Settings, templates, sessions: SessionStore) -> None:
    if not settings.auth_enabled:
        return
    oauth = OAuth()
    oauth.register(
        name="oidc",
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": "openid profile email groups", "code_challenge_method": "S256"},
    )
    app.state.oauth = oauth
    allowed = set(settings.oidc_groups)

    def callback_url(request: Request) -> str:
        if settings.public_url:
            return f"{settings.public_url}/auth/callback"
        return str(request.url_for("auth_callback"))

    def problem(request: Request, title: str, detail: str, status: int, provider_logout: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(request, "auth_problem.html",
                                          {"title": title, "detail": detail, "section": None, "user": None, "show_nav": False,
                                           "active_runs": 0, "settings": settings, "status": status,
                                           "provider_logout": provider_logout}, status_code=status)

    async def provider_logout_url() -> str | None:
        try:
            meta = await oauth.oidc.load_server_metadata()
        except Exception:
            return None
        return meta.get("end_session_endpoint")

    @app.get("/auth/login")
    async def auth_login(request: Request, next: str = "/"):
        request.session["next"] = safe_next(next)
        try:
            return await oauth.oidc.authorize_redirect(request, callback_url(request))
        except Exception as e:  # provider down or misconfigured
            log.warning("OIDC discovery failed: %s", e)
            return problem(request, "Can’t reach the sign-in provider",
                           f"ansible-gui couldn’t load {settings.oidc_issuer}/.well-known/openid-configuration "
                           f"({e}). Check that the provider is up and ANSIBLE_GUI_OIDC_ISSUER is right, then try again.",
                           502)

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except OAuthError as e:
            return problem(request, "Sign-in didn’t complete",
                           f"The provider said: {e.description or e.error}. Start again from the sign-in link.", 400)
        info = dict(token.get("userinfo") or {})
        groups = info.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        who = info.get("preferred_username") or info.get("email") or info.get("sub")
        if "*" not in allowed and not allowed.intersection(groups):
            log.warning("sign-in refused for %s: groups %s, need one of %s", who, groups, sorted(allowed))
            return problem(request, "You’re signed in, but not allowed here",
                           f"{who} isn’t in any of the groups allowed to use ansible-gui "
                           f"({', '.join(sorted(allowed))}). Ask whoever runs your sign-in provider to add you, "
                           "or sign out there and sign in as someone who is.", 403,
                           provider_logout=await provider_logout_url())
        target = safe_next(request.session.get("next"))
        request.session.clear()  # new session on sign-in
        request.session["user"] = {"sub": info.get("sub"), "username": info.get("preferred_username"),
                                   "name": info.get("name"), "email": info.get("email"), "groups": groups}
        request.session["sid"] = sessions.start(str(who))
        log.info("signed in: %s", who)
        return RedirectResponse(target, status_code=303)

    @app.post("/auth/logout")
    async def auth_logout(request: Request):
        sessions.end(request.session.get("sid"))  # dead everywhere, not just in this browser
        request.session.clear()
        return RedirectResponse("/auth/signed-out", status_code=303)

    @app.get("/auth/signed-out", response_class=HTMLResponse)
    async def signed_out(request: Request):
        return problem(request, "Signed out",
                       "You’re signed out of ansible-gui. Your sign-in provider may still remember you, "
                       "so signing in again may not ask for a password.", 200)

