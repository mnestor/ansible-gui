"""Startup settings: the checks that stop ansible-gui from starting open to anyone."""

import pytest

from ansible_gui.config import Settings
from ansible_gui.web import check_bind

OIDC = {"ANSIBLE_GUI_OIDC_ISSUER": "https://id.example.test", "ANSIBLE_GUI_OIDC_CLIENT_ID": "agui",
        "ANSIBLE_GUI_OIDC_CLIENT_SECRET": "s3cret", "ANSIBLE_GUI_OIDC_GROUPS": "ansible_admins",
        "ANSIBLE_GUI_PUBLIC_URL": "https://ansible.example.test"}


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


def test_sign_in_needs_the_public_url(monkeypatch):
    """The provider sends people back to <public url>/auth/callback, and the session cookie is marked Secure
    when that URL is https. Neither is left to be worked out from each request's own headers."""
    env(monkeypatch, **{k: v for k, v in OIDC.items() if k != "ANSIBLE_GUI_PUBLIC_URL"})
    with pytest.raises(SystemExit, match="ANSIBLE_GUI_OIDC_ISSUER is set, so ANSIBLE_GUI_PUBLIC_URL is needed too"):
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
    env(monkeypatch, ANSIBLE_GUI_PUBLIC_URL="https://ansible.example.test")
    # explicit opt-out, for a proxy that signs people in
    check_bind(Settings.from_env(), "0.0.0.0", insecure_ok=True)


def test_sign_in_may_listen_anywhere(monkeypatch):
    env(monkeypatch, **OIDC)
    check_bind(Settings.from_env(), "0.0.0.0", insecure_ok=False)


def test_beyond_loopback_needs_the_name_people_use(monkeypatch):
    """Requests are answered only when addressed to this machine or to the public URL's host, so without
    a public URL nothing beyond loopback could reach a page: refuse at start, and say what to set."""
    s = Settings.from_env()  # no sign-in, so only a proxy in front (insecure_ok) could make it listen further
    assert {"127.0.0.1", "localhost", "::1"} <= s.allowed_hosts and "ansible.example.test" not in s.allowed_hosts
    check_bind(s, "127.0.0.1", insecure_ok=True)
    for host in ("0.0.0.0", "::", "192.0.2.10"):
        with pytest.raises(SystemExit, match=f"Refusing to listen on {host} without ANSIBLE_GUI_PUBLIC_URL"):
            check_bind(s, host, insecure_ok=True)
    env(monkeypatch, ANSIBLE_GUI_PUBLIC_URL="https://Ansible.Example.test:8443/")
    s = Settings.from_env()
    assert s.public_url == "https://Ansible.Example.test:8443" and "ansible.example.test" in s.allowed_hosts
    check_bind(s, "0.0.0.0", insecure_ok=True)


@pytest.mark.parametrize("header, name", [
    ("ansible.example.test", "ansible.example.test"), ("Ansible.Example.test:8443", "ansible.example.test"),
    ("127.0.0.1:8080", "127.0.0.1"), ("[::1]:8080", "::1"), ("[::1]", "::1"), ("", ""), ("::1", ""),
])
def test_host_name_drops_the_port(header, name):
    from ansible_gui.config import host_name
    assert host_name(header) == name


def test_smtp_half_configured_refuses(monkeypatch):
    env(monkeypatch, ANSIBLE_GUI_SMTP_HOST="mail.example.test")
    with pytest.raises(SystemExit, match="SMTP_FROM"):
        Settings.from_env()


def test_host_key_checking_and_retention_defaults(monkeypatch):
    s = Settings.from_env()
    assert s.ssh_host_key_checking == "accept-new" and s.run_retention_days == 90
    env(monkeypatch, ANSIBLE_GUI_SSH_HOST_KEY_CHECKING="YES", ANSIBLE_GUI_RUN_RETENTION_DAYS="0")
    s = Settings.from_env()
    assert s.ssh_host_key_checking == "yes" and s.run_retention_days == 0


@pytest.mark.parametrize("name, value, says", [
    ("ANSIBLE_GUI_SSH_HOST_KEY_CHECKING", "off", "yes, accept-new or no"),
    ("ANSIBLE_GUI_RUN_RETENTION_DAYS", "-1", "whole number of days"),
    ("ANSIBLE_GUI_EDIT_URL", "javascript:alert(1)//{path}", "must start with https://"),
    ("ANSIBLE_GUI_PUBLIC_URL", "ansible.example.test", "ANSIBLE_GUI_PUBLIC_URL must be the address"),
    ("ANSIBLE_GUI_PUBLIC_URL", "https://", "ANSIBLE_GUI_PUBLIC_URL must be the address"),
])
def test_bad_values_refuse_to_start(monkeypatch, name, value, says):
    env(monkeypatch, **{name: value})
    with pytest.raises(SystemExit, match=says):
        Settings.from_env()
