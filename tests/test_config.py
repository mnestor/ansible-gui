"""Startup settings: the checks that stop ansible-gui from starting open to anyone."""

import pytest

from ansible_gui.config import Settings
from ansible_gui.web import check_bind

OIDC = {"ANSIBLE_GUI_OIDC_ISSUER": "https://id.example.test", "ANSIBLE_GUI_OIDC_CLIENT_ID": "agui",
        "ANSIBLE_GUI_OIDC_CLIENT_SECRET": "s3cret", "ANSIBLE_GUI_OIDC_GROUPS": "ansible_admins"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    import os
    for k in list(os.environ):
        if k.startswith("ANSIBLE_GUI_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("ANSIBLE_GUI_DATA_DIR", str(tmp_path / "data"))


def env(monkeypatch, **values):
    for k, v in values.items():
        monkeypatch.setenv(k, v)


def test_full_oidc_settings_enable_sign_in(monkeypatch):
    env(monkeypatch, **OIDC)
    s = Settings.from_env()
    assert s.auth_enabled and s.oidc_client_id == "agui"


@pytest.mark.parametrize("missing", ["ANSIBLE_GUI_OIDC_CLIENT_ID", "ANSIBLE_GUI_OIDC_CLIENT_SECRET"])
def test_issuer_without_client_refuses_to_start(monkeypatch, missing):
    env(monkeypatch, **{k: v for k, v in OIDC.items() if k != missing})
    with pytest.raises(SystemExit, match="CLIENT_ID and"):
        Settings.from_env()


def test_issuer_without_allowed_groups_refuses_to_start(monkeypatch):
    env(monkeypatch, **{k: v for k, v in OIDC.items() if k != "ANSIBLE_GUI_OIDC_GROUPS"})
    with pytest.raises(SystemExit, match="ANSIBLE_GUI_OIDC_GROUPS"):
        Settings.from_env()


def test_client_secret_can_come_from_a_file(monkeypatch, tmp_path):
    secret = tmp_path / "client-secret"
    secret.write_text("from-file\n")
    env(monkeypatch, **{k: v for k, v in OIDC.items() if k != "ANSIBLE_GUI_OIDC_CLIENT_SECRET"},
        ANSIBLE_GUI_OIDC_CLIENT_SECRET_FILE=str(secret))
    assert Settings.from_env().auth_enabled


def test_relative_ssh_config_without_project_dir_refuses(monkeypatch):
    env(monkeypatch, ANSIBLE_GUI_SSH_CONFIG="ssh/config")
    with pytest.raises(SystemExit, match="absolute path"):
        Settings.from_env()


def test_no_sign_in_only_listens_on_loopback(monkeypatch):
    s = Settings.from_env()
    assert not s.auth_enabled
    for host in ("127.0.0.1", "::1", "localhost"):
        check_bind(s, host, insecure_ok=False)
    with pytest.raises(SystemExit, match="Refusing to listen on 0.0.0.0 without sign-in"):
        check_bind(s, "0.0.0.0", insecure_ok=False)
    check_bind(s, "0.0.0.0", insecure_ok=True)  # explicit opt-out, for a proxy that signs people in


def test_sign_in_may_listen_anywhere(monkeypatch):
    env(monkeypatch, **OIDC)
    check_bind(Settings.from_env(), "0.0.0.0", insecure_ok=False)


def test_smtp_half_configured_refuses(monkeypatch):
    env(monkeypatch, ANSIBLE_GUI_SMTP_HOST="mail.example.test")
    with pytest.raises(SystemExit, match="SMTP_FROM"):
        Settings.from_env()
