import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.credentials import ssh
from ansible_gui.credentials.store import CredentialStore, Invalid
from ansible_gui.keys import Keys
from ansible_gui.web import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "runner"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")


@pytest.fixture
def keypair(tmp_path):
    key = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test@lab", "-f", str(key)], check=True)
    ca = tmp_path / "ca"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca)], check=True)
    subprocess.run(["ssh-keygen", "-q", "-s", str(ca), "-I", "lab-cert", "-n", "ansible", "-V", "+1h",
                    str(key) + ".pub"], check=True, capture_output=True)
    return key.read_text(), (tmp_path / "id-cert.pub").read_text()


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path, Keys.load("k" * 40, tmp_path).fernet)


def test_ssh_key_with_cert_is_checked_and_encrypted(store, keypair, tmp_path):
    key, cert = keypair
    c = store.save(kind="ssh_key", name="lab", values={"private_key": key, "certificate": cert},
                   is_default=True, user="mike")
    assert c.meta["type"] == "ED25519" and c.meta["fingerprint"].startswith("SHA256:")
    assert c.meta["cert"]["principals"] == ["ansible"] and c.meta["cert"]["key_id"] == "lab-cert"
    raw = (tmp_path / "ansible-gui.db").read_bytes()
    assert b"OPENSSH PRIVATE KEY" not in raw
    assert store.secret(c)["private_key"] == ssh.normalize(key)


def test_rejects_passphrase_key_and_foreign_cert(store, keypair, tmp_path):
    key, cert = keypair
    locked = tmp_path / "locked"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "hunter22", "-f", str(locked)], check=True)
    with pytest.raises(Invalid) as e:
        store.save(kind="ssh_key", name="locked", values={"private_key": locked.read_text()}, is_default=False, user="")
    assert "passphrase" in e.value.errors["private_key"]
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    with pytest.raises(Invalid) as e:
        store.save(kind="ssh_key", name="mixed", values={"private_key": other.read_text(), "certificate": cert},
                   is_default=False, user="")
    assert "different key" in e.value.errors["certificate"]


def test_edit_keeps_secret_and_single_default(store):
    a = store.save(kind="vault_password", name="a", values={"password": "one"}, is_default=True, user="")
    b = store.save(kind="vault_password", name="b", values={"password": "two"}, is_default=True, user="")
    assert not store.get(a.id).is_default and store.get(b.id).is_default
    store.save(kind="vault_password", name="b2", values={"password": ""}, is_default=True, user="", cred_id=b.id)
    assert store.secret(store.get(b.id)) == {"password": "two"} and store.get(b.id).name == "b2"
    with pytest.raises(Invalid):
        store.save(kind="vault_password", name="A", values={"password": "x"}, is_default=False, user="")


def test_env_names_are_checked(store):
    for bad in ("1STARTS_WITH_DIGIT", "HAS.DOT", "PATH", "path", "ANSIBLE_BECOME_PASS", "SSH_AUTH_SOCK"):
        with pytest.raises(Invalid):
            store.save(kind="env_secret", name="e", values={"env": bad, "value": "v"}, is_default=False, user="")


def test_changed_master_key_is_reported(tmp_path):
    s1 = CredentialStore(tmp_path, Keys.load("a" * 40, tmp_path).fernet)
    s1.save(kind="vault_password", name="v", values={"password": "pw"}, is_default=False, user="")
    s2 = CredentialStore(tmp_path, Keys.load("b" * 40, tmp_path).fernet)
    assert s2.all()[0].readable is False


def test_ssh_config_wrapper_includes_user_file_first(tmp_path):
    path = ssh.write_ssh_config(tmp_path, FIXTURE / "ssh" / "config")
    text = path.read_text()
    assert text.index("Include") < text.index("UserKnownHostsFile")
    out = subprocess.run(["ssh", "-F", str(path), "-G", "lab-1"], capture_output=True, text=True)
    assert "user ansible" in out.stdout
    assert f"userknownhostsfile {tmp_path}/ssh/known_hosts" in out.stdout


# --- through the app ------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    return create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                               ssh_config=FIXTURE / "ssh" / "config", secret_key="s" * 40))


def wait(app, run_id):
    for _ in range(200):
        run = app.state.store.get(run_id)
        if run.finished:
            return run
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def test_secret_values_never_render(app, keypair):
    c = TestClient(app)
    key, _ = keypair
    r = c.post("/credentials", data={"kind": "ssh_key", "name": "lab", "private_key": key, "is_default": "1"},
               follow_redirects=False)
    assert r.status_code == 303
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "pw-visible?"})
    page = c.get("/credentials/secrets").text
    assert "PRIVATE KEY" not in page and "pw-visible?" not in page and "SHA256:" in page
    assert '<td>SSH key</td>' in page and '<td>Vault password</td>' in page
    cred = app.state.creds.by_kind("vault_password")[0]
    assert "pw-visible?" not in c.get(f"/credentials/{cred.id}").text
    bad = c.post("/credentials", data={"kind": "vault_password", "name": "bad name!", "password": "pw-again"})
    assert bad.status_code == 400 and "pw-again" not in bad.text
    assert c.post("/credentials", data={"kind": "become_password", "name": "sudo", "password": "x"}).status_code == 400
    launcher = c.get("/runs/new").text
    assert ">No account<" in launcher and 'name="ssh_key"' not in launcher  # a run's key comes from its account


@needs_ansible
def test_run_gets_its_own_agent_key_env_and_vault(app, keypair):
    c = TestClient(app)
    key, cert = keypair
    c.post("/credentials", data={"kind": "account", "name": "lab", "private_key": key, "certificate": cert,
                                 "is_default": "1"})
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "s3cret"})
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "fixture-password",
                                 "is_default": "1"})
    creds = app.state.creds
    ids = {k: str(creds.by_kind(k)[0].id) for k in ("account", "vault_password")}

    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              **ids}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert "secret=s3cret file=s3cret" in log
    assert "agent=/" in log and "agui-" in log  # the per-run agent, not the server user's
    assert "ED25519-CERT" in log  # key and certificate loaded into it
    assert "-F " in log and str(app.state.store.data_dir / "ssh" / "ssh_config") in log
    agent_dir = log.split("agent=")[1].split("/agent.sock")[0]
    assert not Path(agent_dir).exists()  # keys and passwords removed after the run
    assert run.credentials["account"]["name"] == "lab" and run.credentials["env_secret"][0]["env"] == "TEST_TOKEN"

    r = c.post("/runs", data={"playbook": "vault.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              **ids}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert "word=opened-the-vault" in app.state.store.log_path(run.id).read_text()


@needs_ansible
def test_deleted_credential_stops_queued_run(app):
    c = TestClient(app)
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "pw"})
    cred = app.state.creds.by_kind("vault_password")[0]
    run = app.state.store.create(playbook="site.yml", target="box1", tags="", skip_tags="", check_mode=True,
                                 diff=False, verbosity=0, expected_hosts=["box1"], trigger="t",
                                 credentials={"vault_password": {"id": cred.id, "name": "v"}})
    app.state.creds.delete(cred.id)
    app.state.executor.submit(run.id)
    done = wait(app, run.id)
    assert done.status == "error" and "deleted" in done.error


def test_data_dir_is_private(tmp_path):
    import os
    import stat

    from ansible_gui.web import lock_down
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    (data / "ansible-gui.db").write_text("")
    os.chmod(data / "ansible-gui.db", 0o644)
    old = os.umask(0o022)
    try:
        lock_down(data)
        assert stat.S_IMODE(data.stat().st_mode) == 0o700
        assert stat.S_IMODE((data / "ansible-gui.db").stat().st_mode) == 0o600
        (data / "runs").mkdir()
        (data / "new.log").write_text("x")
        assert stat.S_IMODE((data / "runs").stat().st_mode) == 0o700
        assert stat.S_IMODE((data / "new.log").stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_data_dir_owned_by_someone_else_says_how_to_fix(tmp_path, monkeypatch):
    import os

    import pytest

    from ansible_gui import web
    data = tmp_path / "data"
    data.mkdir()

    def denied(path, mode):
        raise PermissionError(1, "Operation not permitted", str(path))
    monkeypatch.setattr(web.os, "chmod", denied)
    old = os.umask(0o022)
    try:
        with pytest.raises(SystemExit) as e:
            web.lock_down(data)
    finally:
        os.umask(old)
    assert f"sudo chown -R {os.getuid()}" in str(e.value) and str(data) in str(e.value)


def test_changed_secret_key_fails_the_run_clearly(tmp_path, keypair):
    """If ANSIBLE_GUI_SECRET_KEY changes (or its file goes missing), stored secrets can't be read:
    the page says so and a run using one stops before Ansible starts, with a reason."""
    settings = dict(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                    ssh_config=FIXTURE / "ssh" / "config")
    first = TestClient(create_app(Settings(**settings, secret_key="a" * 40)))
    assert first.post("/credentials", data={"kind": "account", "name": "lab", "private_key": keypair[0],
                                            "is_default": "1"}, follow_redirects=False).status_code == 303

    app = create_app(Settings(**settings, secret_key="b" * 40))
    c = TestClient(app)
    assert "can’t decrypt" in c.get("/credentials").text
    cred = app.state.creds.by_kind("account")[0]
    r = c.post("/runs", data={"playbook": "site.yml", "target": "box1", "mode": "check", "expected": ["box1"],
                              "account": str(cred.id)}, follow_redirects=False)
    assert r.status_code == 303, r.text[-300:]
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error"
    assert "can’t be decrypted" in run.error and "secret key changed" in run.error
    assert run.host_stats == {}


# --- host key checking (ANSIBLE_GUI_SSH_HOST_KEY_CHECKING) --------------------------------
@pytest.mark.parametrize("mode, ssh_says", [("yes", "true"), ("accept-new", "accept-new"), ("no", "false")])
def test_host_key_checking_reaches_ssh(tmp_path, mode, ssh_says):
    path = ssh.write_ssh_config(tmp_path, None, mode)
    out = subprocess.run(["ssh", "-F", str(path), "-G", "lab-1"], capture_output=True, text=True)
    assert f"stricthostkeychecking {ssh_says}" in out.stdout


def test_host_key_checking_defaults_to_accept_new(tmp_path):
    assert "StrictHostKeyChecking accept-new" in ssh.write_ssh_config(tmp_path, None).read_text()
    with pytest.raises(ValueError):
        ssh.write_ssh_config(tmp_path, None, "maybe")


def test_users_ssh_config_still_wins_over_the_fallback(tmp_path):
    mine = tmp_path / "mine"
    mine.write_text("Host legacy\n  StrictHostKeyChecking no\n")
    path = ssh.write_ssh_config(tmp_path, mine, "yes")
    legacy = subprocess.run(["ssh", "-F", str(path), "-G", "legacy"], capture_output=True, text=True).stdout
    other = subprocess.run(["ssh", "-F", str(path), "-G", "other"], capture_output=True, text=True).stdout
    assert "stricthostkeychecking false" in legacy and "stricthostkeychecking true" in other


@pytest.fixture
def host_line(tmp_path):
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return " ".join(key.with_suffix(".pub").read_text().split()[:2])


def test_known_hosts_takes_keyscan_hashed_and_ca_lines(host_line):
    text, n = ssh.check_known_hosts(f"# from ssh-keyscan\r\ngit.lab {host_line}\n|1|abc=|def= {host_line}\n\n"
                                    f"@cert-authority *.lab {host_line}\n[box]:2222,10.0.0.5 {host_line} c\n")
    assert n == 4 and "\r" not in text


@pytest.mark.parametrize("bad, says", [
    ("git.lab", "Line 2 needs the host names"),
    ("git.lab ssh-ed25519 AAAAnotakey", "Line 2: ssh-keygen can’t read the key for git.lab"),
    ("@trusted git.lab ssh-ed25519 AAAA", "@trusted isn’t a known_hosts marker"),
])
def test_known_hosts_names_the_bad_line(host_line, bad, says):
    with pytest.raises(ssh.InvalidKey, match=says):
        ssh.check_known_hosts(f"ok.lab {host_line}\n{bad}\n")


def test_known_hosts_editor(app, host_line):
    c = TestClient(app)
    page = c.get("/credentials/ssh").text
    assert "A new host’s key is added here on its first connection" in page
    r = c.post("/credentials/known-hosts", data={"known_hosts": f"git.lab {host_line}\n"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/credentials/ssh?hosts=1#known-hosts"
    path = app.state.store.data_dir / "ssh" / "known_hosts"
    assert path.read_text() == f"git.lab {host_line}\n"
    assert "Saved 1 host key." in c.get(r.headers["location"]).text
    bad = c.post("/credentials/known-hosts", data={"known_hosts": f"git.lab {host_line}\nnope\n"})
    assert bad.status_code == 400 and "Not saved. Line 2" in bad.text and "nope" in bad.text  # input kept
    assert path.read_text() == f"git.lab {host_line}\n"  # unchanged


@pytest.mark.parametrize("mode, says", [("yes", "Runs and SSH pulls connect only to hosts whose key is listed"),
                                        ("no", "Host keys aren’t checked")])
def test_credentials_page_explains_the_other_modes(tmp_path, mode, says):
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path,
                              secret_key="s" * 40, ssh_host_key_checking=mode))
    assert says in TestClient(app).get("/credentials/ssh").text


@needs_ansible
def test_an_account_signs_in_with_its_user_key_and_become_password(app, keypair):
    c = TestClient(app)
    key, cert = keypair
    c.post("/credentials", data={"kind": "ssh_key", "name": "old-key", "private_key": key, "certificate": cert})
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "s3cret"})
    creds = app.state.creds
    r = c.post("/credentials", data={"kind": "account", "name": "admin", "username": "deploy", "is_default": "1",
                                     "copy_key_from": str(creds.by_kind("ssh_key")[0].id),
                                     "become_password": "pw-copied"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    acct = creds.by_kind("account")[0]
    assert acct.meta["username"] == "deploy" and acct.meta["has_key"] and acct.meta["has_become"] and acct.meta["cert"]
    assert creds.secret(acct)["become_password"] == "pw-copied"
    listing = c.get("/credentials").text
    assert "pw-copied" not in listing and "PRIVATE KEY" not in listing and "become password" in listing

    launcher = c.get("/runs/new", params={"playbook": "env.yml", "target": "box1"}).text
    assert f'<option value="{acct.id}" selected data-desc="user deploy · SSH certificate · become password">admin (default)</option>' in launcher

    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id)}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert "ED25519-CERT" in log and "user=deploy" in log and "pw-copied" not in log
    assert run.credentials["account"] == {"id": acct.id, "name": "admin", "user": "deploy"}

    # A Connect as from an old link is ignored: the account decides the user.
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id), "remote_user": "other"}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert "user=deploy" in app.state.store.log_path(run.id).read_text()


def test_an_account_needs_something_and_a_certificate_needs_its_key(app, keypair):
    c = TestClient(app)
    _, cert = keypair
    empty = c.post("/credentials", data={"kind": "account", "name": "nothing"})
    assert empty.status_code == 400 and "at least a user" in empty.text
    lone = c.post("/credentials", data={"kind": "account", "name": "certonly", "username": "u", "certificate": cert})
    assert lone.status_code == 400 and "private key this certificate is for" in lone.text
    ok = c.post("/credentials", data={"kind": "account", "name": "useronly", "username": "ansible"}, follow_redirects=False)
    assert ok.status_code == 303


def test_accounts_secrets_and_ssh_settings_are_separate_pages(app):
    c = TestClient(app)
    c.post("/credentials", data={"kind": "account", "name": "admin", "username": "deploy"})
    r = c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/credentials/secrets?saved=")
    accounts = c.get("/credentials").text
    assert ">admin</a>" in accounts and '<td class="mono">deploy</td>' in accounts and ">v</a>" not in accounts
    assert '<a class="rail-item rail-sub" href="/credentials" aria-current="page">' in accounts
    secrets = c.get("/credentials/secrets").text
    assert ">v</a>" in secrets and ">admin</a>" not in secrets and "<td>Vault password</td>" in secrets
    acct = app.state.creds.by_kind("account")[0]
    form = c.get(f"/credentials/{acct.id}").text
    assert '<a class="crumb" href="/credentials">← Accounts</a>' in form
    assert '<a class="rail-item rail-sub" href="/credentials" aria-current="page">' in form
    assert "Known hosts" in c.get("/credentials/ssh").text


def test_env_secret_names_are_capitalised_and_deletes_sit_in_a_danger_zone(app):
    c = TestClient(app)
    r = c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "enroll_token", "value": "v"},
               follow_redirects=False)
    assert r.status_code == 303
    cred = app.state.creds.by_kind("env_secret")[0]
    assert cred.meta["env"] == "ENROLL_TOKEN"
    page = c.get(f"/credentials/{cred.id}").text
    assert "data-upper" in page
    assert 'class="section-title">Danger zone</h2>' in page and '<div class="danger-zone">' in page
    assert '<button class="btn btn-danger" type="submit"' in page and ">Delete environment secret</button>" in page


def test_an_account_says_what_it_lacks(app):
    c = TestClient(app)
    c.post("/credentials", data={"kind": "account", "name": "plain", "username": "ansible"})
    page = c.get("/runs/new").text
    assert 'data-desc="user ansible · no SSH key · no become password">plain' in page


def test_there_is_no_connect_as_field(app):
    c = TestClient(app)
    for page in (c.get("/runs/new", params={"playbook": "site.yml", "target": "box1", "remote_user": "root"}).text,
                 c.get("/runs/command").text):
        assert 'name="remote_user"' not in page and 'name="account"' in page
    fresh = c.get("/runs/new", params={"playbook": "site.yml", "target": "box1"}).text
    assert 'id="inv-ansible_user"' in fresh  # the inventory's user, under Sign in as


def test_the_host_header_names_the_user_a_run_signs_in_as(app):
    c = TestClient(app)
    plain = c.get("/inventory/hosts/box1").text
    assert "default account" not in plain
    c.post("/credentials", data={"kind": "account", "name": "ops", "username": "deploy", "is_default": "1"})
    head = c.get("/inventory/hosts/box1").text.split('class="subject-meta"')[1].split("</p>")[0]
    assert 'as <span class="mono">deploy</span>' in head and "default account" in head


def test_the_key_note_is_folded_on_secrets_only(tmp_path):
    c = TestClient(create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path)))
    assert 'class="key-note"' in c.get("/credentials/secrets").text
    assert 'class="key-note"' not in c.get("/credentials").text
