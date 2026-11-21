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


def test_requests_addressed_to_another_name_are_refused(tmp_path):
    """DNS rebinding: a page elsewhere points a name of its own at this server, so its requests are
    same-origin as far as the browser and the Origin check can tell. Only the Host header gives it away."""
    app = create_app(settings(tmp_path, oidc_issuer=None, public_url=None))
    c = TestClient(app, base_url="http://127.0.0.1:8080")
    rebound = {"host": "rebind.evil.example:8080", "origin": "http://rebind.evil.example:8080"}
    r = c.post("/runs/command", headers=rebound, follow_redirects=False,
               data={"target": "localhost", "module": "ansible.builtin.shell", "args": "id", "expected": ["localhost"]})
    assert r.status_code == 400 and "ANSIBLE_GUI_PUBLIC_URL" in r.text
    assert app.state.store.recent() == []
    for path in ("/credentials", "/runs/1/log.txt", "/static/app.css", "/auth/login"):
        assert c.get(path, headers={"host": rebound["host"]}).status_code == 400, path
    for bad in ("", "localhost.evil.example", "127.0.0.1.evil.example", "evil.example/static/", "::1"):
        assert c.get("/runs", headers={"host": bad}).status_code == 400, bad
    for own in ("127.0.0.1:8080", "localhost:8080", "LOCALHOST", "[::1]:8080"):
        assert c.get("/runs", headers={"host": own}).status_code == 200, own
    # /healthz says only "ok": a probe may ask for it by container name or address.
    assert c.get("/healthz", headers={"host": "ansible-gui-app:8080"}).text == "ok"


def test_the_public_urls_host_is_answered_and_says_so_to_others(tmp_path):
    c = TestClient(create_app(settings(tmp_path, public_url="https://ansible.example.test")),
                   base_url="https://ansible.example.test")
    assert c.get("/auth/signed-out").status_code == 200
    assert c.get("/auth/signed-out", headers={"host": "ANSIBLE.example.test:443"}).status_code == 200
    r = c.get("/auth/signed-out", headers={"host": "other.example.test"})
    assert r.status_code == 400 and "https://ansible.example.test" in r.text
    assert r.headers["x-frame-options"] == "DENY" and "other.example.test" not in r.text


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


# --- sessions recorded on the server ------------------------------------------------------
MIKE = {"sub": "1", "preferred_username": "mike", "groups": ["admins"]}


def test_signing_out_kills_a_copied_cookie(tmp_path):
    app = create_app(settings(tmp_path))
    c = signed_in_client(app, MIKE)
    c.get("/auth/callback")
    stolen = TestClient(app, cookies={"ansible_gui": c.cookies["ansible_gui"]})
    assert stolen.get("/runs", follow_redirects=False).status_code == 200
    c.post("/auth/logout")
    assert stolen.get("/runs", follow_redirects=False).status_code == 303


def test_session_ends_after_its_lifetime_however_often_its_used(tmp_path, monkeypatch):
    from datetime import timedelta

    from ansible_gui import sessions
    app = create_app(settings(tmp_path))
    c = signed_in_client(app, MIKE)
    c.get("/auth/callback")
    assert c.get("/runs", follow_redirects=False).status_code == 200
    later = sessions._now() + sessions.LIFETIME + timedelta(minutes=1)
    monkeypatch.setattr(sessions, "_now", lambda: later)
    r = c.get("/runs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


def test_cookie_from_before_sessions_were_recorded_signs_in_again(tmp_path):
    """A cookie with a user but no session id (written by an older version) is not trusted."""
    import base64
    import json

    from itsdangerous import TimestampSigner

    from ansible_gui.keys import Keys
    from ansible_gui.sessions import SessionStore
    app = create_app(settings(tmp_path))
    secret = Keys.load("x" * 40, tmp_path).session_secret

    def cookie(session):
        payload = base64.b64encode(json.dumps(session).encode())
        return {"ansible_gui": TimestampSigner(secret).sign(payload).decode()}
    assert TestClient(app, cookies=cookie({"user": MIKE})).get("/runs", follow_redirects=False).status_code == 303
    # the same cookie with a recorded session id gets in, so it was the missing id that kept it out
    sid = SessionStore(tmp_path).start("mike")
    assert TestClient(app, cookies=cookie({"user": MIKE, "sid": sid})).get("/runs", follow_redirects=False).status_code == 200


def test_sign_out_everyone(tmp_path, monkeypatch, capsys):
    from ansible_gui import sign_out_everyone
    app = create_app(settings(tmp_path))
    first, second = signed_in_client(app, MIKE), signed_in_client(app, MIKE)
    first.get("/auth/callback")
    second.get("/auth/callback")
    monkeypatch.setenv("ANSIBLE_GUI_DATA_DIR", str(tmp_path))
    sign_out_everyone()
    assert "Ended 2 sessions" in capsys.readouterr().out
    assert first.get("/runs", follow_redirects=False).status_code == 303
    assert second.get("/runs", follow_redirects=False).status_code == 303
    monkeypatch.setenv("ANSIBLE_GUI_DATA_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit, match="No ansible-gui database"):
        sign_out_everyone()
