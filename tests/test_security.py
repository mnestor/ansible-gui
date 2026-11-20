"""What the security review found: secrets left on disk or in the environment, and missing response headers."""

import os
import re
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings, scrub_environment
from ansible_gui.runs.store import RunStore
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
TEMPLATES = Path(__file__).parent.parent / "src" / "ansible_gui" / "templates"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


def wait(app, run_id):
    for _ in range(300):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def files_containing(root: Path, needles: list[str]) -> list[str]:
    hits = []
    for f in root.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            hits += [f"{f.relative_to(root)}: {n}" for n in needles if n.encode() in data]
    return hits


@needs_ansible
def test_a_run_leaves_no_secret_on_disk(tmp_path, monkeypatch):
    """ansible-runner writes its environment, the become password and the server's own environment into its
    private_data_dir. That directory has to go with the run, not stay in data/runs/<id>/ for ever."""
    monkeypatch.setenv("ANSIBLE_GUI_SMTP_PASSWORD", "smtp-pw-from-env")
    scrub_environment()  # what main() does once the settings are read
    data = tmp_path / "data"
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=data,
                              secret_key="s" * 40))
    c = TestClient(app)
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "env-secret-1"})
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "vault-pw-2", "is_default": "1"})
    c.post("/credentials", data={"kind": "become_password", "name": "sudo", "password": "become-pw-3",
                                 "is_default": "1"})
    ids = {k: str(app.state.creds.by_kind(k)[0].id) for k in ("vault_password", "become_password")}
    # site.yml prints none of them, so any copy found is one ansible-gui or ansible-runner left behind.
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "apply", "expected": ["box1"], **ids},
               follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "successful", app.state.store.log_path(run.id).read_text()
    assert not (data / "runs" / str(run.id) / "runner").exists()
    assert files_containing(data, ["env-secret-1", "vault-pw-2", "become-pw-3", "smtp-pw-from-env"]) == []


def test_runner_files_from_before_are_wiped_at_start(tmp_path):
    old = tmp_path / "runs" / "7"
    (old / "runner" / "env").mkdir(parents=True)
    (old / "runner" / "env" / "passwords").write_text('{"^BECOME password.*:\\\\s*?$": "hunter2"}')
    (old / "runner" / "artifacts" / "abc").mkdir(parents=True)
    (old / "runner" / "artifacts" / "abc" / "command").write_text('{"env": {"ANSIBLE_GUI_SECRET_KEY": "k"}}')
    (old / "output.log").write_text("PLAY [all]\n")
    RunStore(tmp_path)
    assert not (old / "runner").exists() and (old / "output.log").read_text() == "PLAY [all]\n"


def test_scrub_removes_only_ansible_gui_variables(monkeypatch):
    monkeypatch.setenv("ANSIBLE_GUI_OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("ANSIBLE_GUI_SECRET_KEY_FILE", "/run/secrets/k")
    monkeypatch.setenv("ANSIBLE_HOME", "/data/ansible")
    scrub_environment()
    assert not [k for k in os.environ if k.startswith("ANSIBLE_GUI_")]
    assert os.environ["ANSIBLE_HOME"] == "/data/ansible"


def test_main_reads_settings_then_scrubs_before_starting(monkeypatch, tmp_path):
    import uvicorn

    from ansible_gui import web
    for k in [k for k in os.environ if k.startswith("ANSIBLE_GUI_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("ANSIBLE_GUI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANSIBLE_GUI_SECRET_KEY", "m" * 40)
    monkeypatch.setenv("ANSIBLE_GUI_PORT", "8099")
    started = {}

    def fake_run(app, **kw):
        started.update(kw, app=app, env=[k for k in os.environ if k.startswith("ANSIBLE_GUI_")])
    monkeypatch.setattr(uvicorn, "run", fake_run)
    web.main()
    assert started["port"] == 8099 and started["host"] == "127.0.0.1" and started["env"] == []
    assert TestClient(started["app"]).get("/healthz").text == "ok"


# --- response headers ---------------------------------------------------------------------
def test_pages_carry_security_headers(tmp_path):
    c = TestClient(create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                                       secret_key="s" * 40)))
    page = c.get("/runs")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "same-origin" and page.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in page.headers  # no https public URL
    static = c.get("/static/app.css")
    assert static.headers["x-content-type-options"] == "nosniff" and "no-store" not in static.headers.get("cache-control", "")
    redirect = c.post("/runs/999/cancel", follow_redirects=False)
    assert redirect.headers["x-frame-options"] == "DENY"


def test_hsts_only_behind_https(tmp_path):
    c = TestClient(create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                                       secret_key="s" * 40, public_url="https://ansible.example.test")),
                   base_url="https://ansible.example.test")
    assert c.get("/healthz").headers["strict-transport-security"] == "max-age=31536000"


def test_templates_need_no_inline_script():
    """The CSP allows scripts from /static only: an inline <script> or onclick= would silently not run."""
    for t in TEMPLATES.glob("*.html"):
        text = t.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), t.name
        assert not re.search(r"\son[a-z]+\s*=", text), t.name
        assert "javascript:" not in text and "hx-on" not in text, t.name


def test_static_urls_change_with_the_file_and_are_cached_only_when_versioned(tmp_path):
    """A browser must never run a cached app.js against newer pages."""
    import hashlib
    import re
    from ansible_gui.web import HERE
    c = TestClient(create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                                       secret_key="s" * 40)))
    page = c.get("/runs").text
    for name in ("app.css", "early.js", "htmx.min.js", "app.js"):
        want = hashlib.sha256((HERE / "static" / name).read_bytes()).hexdigest()[:12]
        assert f'/static/{name}?v={want}"' in page, name
    url = re.search(r'/static/app\.js\?v=\w+', page).group(0)
    assert c.get(url).headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
