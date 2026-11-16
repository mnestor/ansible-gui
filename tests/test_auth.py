from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.web import check_bind, create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"


def settings(tmp_path, **kw):
    base = dict(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path, secret_key="x" * 40,
                public_url="http://testserver", oidc_issuer="https://id.example.test", oidc_client_id="agui",
                oidc_client_secret="shh", oidc_groups=["admins"])
    base.update(kw)
    return Settings(**base)


class FakeOIDC:
    def __init__(self, userinfo):
        self.userinfo = userinfo

    async def authorize_redirect(self, request, redirect_uri):
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"https://id.example.test/authorize?redirect_uri={redirect_uri}", status_code=302)

    async def authorize_access_token(self, request):
        return {"userinfo": self.userinfo}


def signed_in_client(app, userinfo):
    app.state.oauth.oidc = FakeOIDC(userinfo)
    c = TestClient(app)
    c.get("/runs/new?target=box1", follow_redirects=False)  # remembers where to return
    c.get("/auth/login?next=/runs/new%3Ftarget%3Dbox1", follow_redirects=False)
    return c


def test_everything_but_static_needs_sign_in(tmp_path):
    c = TestClient(create_app(settings(tmp_path)))
    for path in ("/inventory", "/runs", "/credentials", "/runs/1/log.txt"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), path
    assert c.get("/static/app.css").status_code == 200
    assert c.get("/healthz").text == "ok"
    r = c.get("/runs/1/live", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers.get("HX-Redirect", "").startswith("/auth/login")
    r = c.post("/runs", data={"playbook": "site.yml"}, follow_redirects=False)
    assert r.status_code == 303


def test_group_member_gets_in_and_lands_where_they_were_going(tmp_path):
    app = create_app(settings(tmp_path))
    c = signed_in_client(app, {"sub": "1", "preferred_username": "mike", "groups": ["admins", "users"]})
    r = c.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs/new?target=box1"
    page = c.get("/runs").text
    assert "mike" in page and "Sign out" in page
    c.post("/auth/logout", follow_redirects=False)
    assert c.get("/runs", follow_redirects=False).status_code == 303


def test_non_member_is_refused(tmp_path):
    app = create_app(settings(tmp_path))
    c = signed_in_client(app, {"sub": "2", "preferred_username": "kid", "groups": ["household_users"]})
    r = c.get("/auth/callback")
    assert r.status_code == 403 and "admins" in r.text
    assert c.get("/runs", follow_redirects=False).status_code == 303


def test_next_cannot_leave_the_app(tmp_path):
    app = create_app(settings(tmp_path))
    app.state.oauth.oidc = FakeOIDC({"sub": "1", "preferred_username": "mike", "groups": ["admins"]})
    c = TestClient(app)
    c.get("/auth/login?next=//evil.example/x", follow_redirects=False)
    assert c.get("/auth/callback", follow_redirects=False).headers["location"] == "/"


def test_cross_site_posts_are_refused(tmp_path):
    c = TestClient(create_app(settings(tmp_path, oidc_issuer=None)))
    r = c.post("/inventory/reload", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/inventory/reload", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_runs_record_who_started_them(tmp_path):
    app = create_app(settings(tmp_path))
    c = signed_in_client(app, {"sub": "1", "preferred_username": "mike", "groups": ["admins"]})
    c.get("/auth/callback")
    r = c.post("/runs", data={"playbook": "slow.yml", "target": "box1", "mode": "check", "expected": ["box1"]},
               follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert app.state.store.get(run_id).trigger == "mike"
    app.state.executor.cancel(run_id)


def test_refuses_open_bind_without_sign_in(tmp_path):
    s = settings(tmp_path, oidc_issuer=None)
    check_bind(s, "127.0.0.1", False)
    with pytest.raises(SystemExit):
        check_bind(s, "0.0.0.0", False)
    check_bind(s, "0.0.0.0", True)
    check_bind(settings(tmp_path), "0.0.0.0", False)
