import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ansible_gui.config import Settings
from ansible_gui.credentials import ssh
from ansible_gui.credentials.store import CredentialStore, Invalid
from ansible_gui.keys import Keys
from ansible_gui.runs.redact import MASK
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


def test_a_git_token_says_who_saved_it_not_the_user_it_signs_in_as(store):
    """Its "Updated by" showed the token's own user name: the name the git server expects with the token had
    taken the place of the person saving it."""
    c = store.save(kind="git_token", name="forge", values={"username": "forge-bot", "token": "t0ken"},
                   is_default=False, user="mike")
    assert c.updated_by == "mike" and c.meta["username"] == "forge-bot"
    c = store.save(kind="git_token", name="forge", values={"username": "forge-bot"}, is_default=False, user="anna",
                   cred_id=c.id)
    assert c.updated_by == "anna" and store.secret(c) == {"token": "t0ken"}


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
    c.post("/credentials", data={"kind": "ssh_key", "name": "lab-key", "private_key": key, "certificate": cert})
    c.post("/credentials", data={"kind": "account", "name": "lab", "is_default": "1",
                                 "ssh_key_id": str(app.state.creds.by_kind("ssh_key")[0].id)})
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "s3cret"})
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "fixture-password",
                                 "is_default": "1"})
    creds = app.state.creds
    ids = {k: str(creds.by_kind(k)[0].id) for k in ("account", "vault_password")}

    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "env_secret": str(creds.by_kind("env_secret")[0].id), **ids}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    # The variable and its file both held the value: only the value itself is masked (runs/redact.py).
    assert f"secret={MASK} file={MASK}" in log and "s3cret" not in log
    assert "agent=/" in log and "agui-" in log  # the per-run agent, not the server user's
    assert "ED25519-CERT" in log  # key and certificate loaded into it
    agent_dir = log.split("agent=")[1].split("/agent.sock")[0]
    assert f"-F {agent_dir}/ssh_config" in log  # the run's own, beside its agent and key
    assert not Path(agent_dir).exists()  # keys and passwords removed after the run
    assert run.credentials["account"]["name"] == "lab" and run.credentials["env_secret"][0]["env"] == "TEST_TOKEN"

    r = c.post("/runs", data={"playbook": "vault.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              **ids}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert "the vault opened" in app.state.store.log_path(run.id).read_text()


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
    first.post("/credentials", data={"kind": "ssh_key", "name": "lab-key", "private_key": keypair[0]})
    assert first.post("/credentials", data={"kind": "account", "name": "lab", "ssh_key_id": "1", "become_password": "pw",
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
                                     "ssh_key_id": str(creds.by_kind("ssh_key")[0].id),
                                     "become_password": "pw-copied"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    acct = creds.by_kind("account")[0]
    # The account names the key; the key, with its certificate, stays the one saved SSH key.
    assert acct.meta == {"username": "deploy", "ssh_key_id": creds.by_kind("ssh_key")[0].id, "has_become": True}
    assert creds.secret(acct) == {"become_password": "pw-copied"}
    listing = c.get("/credentials").text
    assert "pw-copied" not in listing and "PRIVATE KEY" not in listing and "become password" in listing

    launcher = c.get("/runs/new", params={"playbook": "env.yml", "target": "box1"}).text
    assert f'<option value="{acct.id}" selected data-desc="user deploy · SSH certificate · become password">admin (default)</option>' in launcher

    token = str(creds.by_kind("env_secret")[0].id)  # env.yml reads it
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id), "env_secret": token}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert "ED25519-CERT" in log and "user=deploy" in log and "pw-copied" not in log
    assert run.credentials["account"] == {"id": acct.id, "name": "admin", "user": "deploy"}

    # A Connect as from an old link is ignored: the account decides the user.
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id), "env_secret": token, "remote_user": "other"},
               follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert "user=deploy" in app.state.store.log_path(run.id).read_text()


def test_an_account_needs_a_user_a_key_or_a_become_password(app, keypair):
    c = TestClient(app)
    key, _ = keypair
    empty = c.post("/credentials", data={"kind": "account", "name": "nothing"})
    assert empty.status_code == 400 and "at least a user" in empty.text
    gone = c.post("/credentials", data={"kind": "account", "name": "nokey", "username": "u", "ssh_key_id": "99"})
    assert gone.status_code == 400 and "That SSH key no longer exists" in gone.text
    ok = c.post("/credentials", data={"kind": "account", "name": "useronly", "username": "ansible"}, follow_redirects=False)
    assert ok.status_code == 303
    # An account holds no key of its own: one sent with its form is not kept.
    c.post("/credentials", data={"kind": "account", "name": "pasted", "username": "u", "private_key": key})
    pasted = next(a for a in app.state.creds.by_kind("account") if a.name == "pasted")
    assert app.state.creds.secret(pasted) == {} and pasted.meta == {"username": "u", "has_become": False}


def test_the_account_form_offers_the_saved_keys_and_takes_none_of_its_own(app, keypair):
    c = TestClient(app)
    key, cert = keypair
    c.post("/credentials", data={"kind": "ssh_key", "name": "lab-key", "private_key": key, "certificate": cert})
    saved = app.state.creds.by_kind("ssh_key")[0]
    form = c.get("/credentials/new", params={"kind": "account"}).text
    assert '<select id="ssh_key_id"' in form and f'<option value="{saved.id}"' in form and ">lab-key</option>" in form
    for gone in ('name="private_key"', 'name="certificate"', 'name="ssoossh_code"', 'name="copy_key_from"'):
        assert gone not in form
    c.post("/credentials", data={"kind": "account", "name": "admin", "username": "deploy",
                                 "ssh_key_id": str(saved.id)})
    acct = app.state.creds.by_kind("account")[0]
    assert f'<option value="{saved.id}" selected' in c.get(f"/credentials/{acct.id}").text
    accounts = c.get("/credentials").text  # the account's row names its key, and leads to it
    assert f'<a class="mono" href="/credentials/{saved.id}">lab-key</a>' in accounts and "certificate lab-cert" in accounts
    key_page = c.get(f"/credentials/{saved.id}").text  # and the key says who signs in with it
    assert f'<a class="mono" href="/credentials/{acct.id}">admin</a>' in key_page


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


def test_an_ssh_config_that_cant_be_read_stops_runs_and_says_so(tmp_path):
    """Set but unreadable (owned by another user on the server, say), it was sent to no job, and ssh skipped the
    Include of a file the runner doesn't have: every run connected as if the file were empty, saying nothing."""
    conf = tmp_path / "ssh_config"
    conf.write_text("Host *\n  CanonicalizeHostname always\n")
    conf.chmod(0)
    if os.access(conf, os.R_OK):
        pytest.skip("this user reads any file (root)")
    app = create_app(Settings(project_dir=FIXTURE, inventory=["inventory/hosts.yml"], data_dir=tmp_path / "data",
                              ssh_config=conf, secret_key="s" * 40))
    c = TestClient(app)
    assert ssh.config_problem(conf) == f"{conf} can’t be read (Permission denied)"
    home = c.get("/").text
    assert "SSH config can’t be read" in home and "so no run starts" in home and 'href="/credentials/ssh"' in home
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"]},
               follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error" and run.error.startswith(f"Didn’t start: the SSH config {conf} can’t be read")
    assert "PLAY" not in app.state.store.log_path(run.id).read_text()  # nothing connected anywhere

    conf.unlink()
    assert ssh.config_problem(conf) == f"{conf} doesn’t exist" and "SSH config is missing" in c.get("/").text
    conf.write_text("Host *\n  ConnectTimeout 5\n")
    assert ssh.config_problem(conf) is None and ssh.config_problem(None) is None
    home = c.get("/").text
    assert "SSH config is missing" not in home and "SSH config can’t be read" not in home


def test_only_urls_git_reaches_through_ssh_wait_for_the_ssh_config():
    from ansible_gui.repos import git

    for url in ("git@github.com:me/lab.git", "ssh://git@git.example.test:2222/me/lab.git", "deploy@host:lab.git"):
        assert git.is_ssh(url), url
    for url in ("https://git.example.test/me/lab.git", "http://git.example.test/lab.git", "file:///srv/git/lab.git",
                "/srv/git/lab.git", "./lab.git", "C:/git/lab.git".replace("C:", "/c")):
        assert not git.is_ssh(url), url


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


# --- certificates from ssoossh ----------------------------------------------------------------------------

FAKE_SSOOSSH = """#!/bin/sh
# Stands in for `ssoossh --server S service retrieve --key K --code C`: signs K.pub with a test CA.
while [ $# -gt 0 ]; do
  case "$1" in --server) server=$2; shift ;; --key) key=$2; shift ;; --code) code=$2; shift ;; esac
  shift
done
echo "$server $code" >> "$SSOOSSH_CALLS"
[ "$code" = "expired-code" ] && { echo "Error: the server refused this enrollment code (expired)" >&2; exit 1; }
ssh-keygen -q -s "$SSOOSSH_CA" -I "svc-ansible" -n ansible -V +1h "$key.pub" 2>/dev/null
"""


@pytest.fixture
def fake_ssoossh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssoossh").write_text(FAKE_SSOOSSH)
    (bin_dir / "ssoossh").chmod(0o755)
    ca = tmp_path / "sso-ca"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca)], check=True)
    calls = tmp_path / "ssoossh-calls"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SSOOSSH_CA", str(ca))
    monkeypatch.setenv("SSOOSSH_CALLS", str(calls))
    return calls


def test_an_ssh_key_can_fetch_its_certificate_from_ssoossh(store, keypair, tmp_path):
    key, cert = keypair
    c = store.save(kind="ssh_key", name="svc", values={"private_key": key, "certificate": cert},
                   is_default=False, user="t")
    assert c.meta["cert"]
    c = store.save(kind="ssh_key", name="svc", values={"ssoossh_server": "https://sso.lab/", "ssoossh_code": "code-1"},
                   is_default=False, user="t", cred_id=c.id)
    assert c.meta["ssoossh"] == {"server": "https://sso.lab"}
    assert "cert" not in c.meta and "certificate" not in c.meta  # ssoossh's replaces the pasted one
    assert store.secret(c)["ssoossh_code"] == "code-1" and "code-1" not in str(c.meta)

    # Left empty, the code is kept; the server can change.
    c = store.save(kind="ssh_key", name="svc", values={"ssoossh_server": "sso2.lab"},
                   is_default=False, user="t", cred_id=c.id)
    assert c.meta["ssoossh"]["server"] == "sso2.lab" and store.secret(c)["ssoossh_code"] == "code-1"

    # The code was for the old key: a new key needs a new one.
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "new")], check=True)
    with pytest.raises(Invalid) as e:
        store.save(kind="ssh_key", name="svc", values={"private_key": (tmp_path / "new").read_text(),
                                                       "ssoossh_server": "sso2.lab"},
                   is_default=False, user="t", cred_id=c.id)
    assert "code again" in e.value.errors["ssoossh_code"]

    # Clearing the server stops it.
    c = store.save(kind="ssh_key", name="svc", values={}, is_default=False, user="t", cred_id=c.id)
    assert "ssoossh" not in c.meta and "ssoossh_code" not in store.secret(c)


def test_ssoossh_needs_https_a_code_a_key_and_no_pasted_certificate(store, keypair):
    key, cert = keypair
    cases = [
        ({"private_key": key, "ssoossh_server": "http://sso.lab", "ssoossh_code": "x"}, "ssoossh_server", "https"),
        ({"private_key": key, "ssoossh_server": "sso.lab; rm -rf /", "ssoossh_code": "x"}, "ssoossh_server", "address"),
        ({"private_key": key, "ssoossh_code": "x"}, "ssoossh_server", "Enter the ssoossh server"),
        ({"private_key": key, "ssoossh_server": "sso.lab"}, "ssoossh_code", "Paste the enrollment code"),
        ({"ssoossh_server": "sso.lab", "ssoossh_code": "x"}, "private_key", "Paste the private key"),
        ({"private_key": key, "certificate": cert, "ssoossh_server": "sso.lab", "ssoossh_code": "x"},
         "certificate", "not both"),
    ]
    for values, field, says in cases:
        with pytest.raises(Invalid) as e:
            store.save(kind="ssh_key", name="svc", values=values, is_default=False, user="t")
        assert says in e.value.errors.get(field, ""), (values, e.value.errors)
    # An account takes no enrollment: it picks a key that has one.
    acct = store.save(kind="account", name="acct", values={"username": "u", "ssoossh_server": "sso.lab",
                                                           "ssoossh_code": "x"}, is_default=False, user="t")
    assert "ssoossh" not in acct.meta and store.secret(acct) == {}


def test_an_account_picks_a_saved_ssh_key(store, keypair):
    key, _ = keypair
    saved = store.save(kind="ssh_key", name="lab-key", values={"private_key": key}, is_default=False, user="t")
    acct = store.save(kind="account", name="admin", values={"username": "deploy", "ssh_key_id": str(saved.id)},
                      is_default=False, user="t")
    assert acct.meta == {"username": "deploy", "ssh_key_id": saved.id, "has_become": False}
    assert store.key_of(acct).id == saved.id and store.accounts_using(saved.id)[0].id == acct.id
    for not_a_key in (str(acct.id), "99", "x"):  # another kind of credential, one that is gone, nonsense
        with pytest.raises(Invalid) as e:
            store.save(kind="account", name="other", values={"username": "u", "ssh_key_id": not_a_key},
                       is_default=False, user="t")
        assert "no longer exists" in e.value.errors["ssh_key_id"]
    store.delete(saved.id)
    assert store.key_of(store.get(acct.id)) is None


def old_account(tmp_path, fernet, name, key_text, *, certificate="", ssoossh=None, become="", username="ansible"):
    """An account row as ansible-gui wrote it while an account held its own key."""
    info = ssh.inspect_key(ssh.normalize(key_text))
    meta = {"username": username, "type": info.type, "fingerprint": info.fingerprint, "comment": info.comment,
            "public_key": info.public_key, "has_key": True, "has_become": bool(become)}
    secret = {"private_key": ssh.normalize(key_text)}
    if certificate:
        cert = ssh.inspect_cert(certificate, info)
        meta["certificate"] = certificate
        meta["cert"] = {"key_id": cert.key_id, "principals": cert.principals, "valid": cert.valid}
    if ssoossh:
        meta["ssoossh"] = {"server": ssoossh[0]}
        secret["ssoossh_code"] = ssoossh[1]
    if become:
        secret["become_password"] = become
    db = sqlite3.connect(tmp_path / "ansible-gui.db")
    db.execute("insert into credentials (name, kind, is_default, meta, secret, created_at, updated_at, updated_by)"
               " values (?, 'account', 1, ?, ?, '2026-11-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'mike')",
               (name, json.dumps(meta), fernet.encrypt(json.dumps(secret).encode())))
    db.commit()
    db.close()


def test_an_account_from_before_gives_its_key_to_an_ssh_key_of_its_own(tmp_path, keypair):
    """Accounts used to hold their own key, certificate and ssoossh enrollment. On the first start after
    that, each such key becomes an SSH key the account picks, so nothing an account could do is lost."""
    key, _ = keypair
    fernet = Keys.load("k" * 40, tmp_path).fernet
    CredentialStore(tmp_path, fernet)  # makes the table
    old_account(tmp_path, fernet, "svc", key, ssoossh=("https://sso.lab", "code-1"), become="pw")
    store = CredentialStore(tmp_path, fernet)
    [made] = store.by_kind("ssh_key")
    acct = store.by_kind("account")[0]
    assert made.name == "svc key" and made.meta["ssoossh"] == {"server": "https://sso.lab"}
    assert made.meta["fingerprint"].startswith("SHA256:") and made.meta["public_key"]
    assert store.secret(made) == {"private_key": ssh.normalize(key), "ssoossh_code": "code-1"}
    assert acct.meta == {"username": "ansible", "ssh_key_id": made.id, "has_become": True}
    assert store.secret(acct) == {"become_password": "pw"} and acct.is_default
    # Starting again changes nothing.
    again = CredentialStore(tmp_path, fernet)
    assert [c.id for c in again.by_kind("ssh_key")] == [made.id] and again.by_kind("account")[0].meta == acct.meta


def test_an_account_from_before_picks_the_saved_key_it_was_copied_from(tmp_path, keypair):
    """The same key with the same certificate is one SSH key. With another certificate arrangement it is not:
    a repository's deploy key must not start asking ssoossh for certificates because an account did."""
    key, cert = keypair
    fernet = Keys.load("k" * 40, tmp_path).fernet
    first = CredentialStore(tmp_path, fernet)
    saved = first.save(kind="ssh_key", name="deploy", values={"private_key": key, "certificate": cert},
                       is_default=False, user="t")
    db = sqlite3.connect(tmp_path / "ansible-gui.db")  # a key could once be marked as the launcher's default
    db.execute("update credentials set is_default=1 where id=?", (saved.id,))
    db.commit()
    db.close()
    again = tmp_path / "same-key-other-comment"  # the same key in another file: only its comment differs
    again.write_text(key)
    again.chmod(0o600)
    subprocess.run(["ssh-keygen", "-q", "-c", "-C", "renamed@lab", "-P", "", "-f", str(again)], check=True,
                   capture_output=True)
    assert again.read_text() != key
    old_account(tmp_path, fernet, "copied", again.read_text(), certificate=cert)
    old_account(tmp_path, fernet, "enrolled", key, ssoossh=("sso.lab", "code-2"))
    old_account(tmp_path, fernet, "deploy key", key, username="other")  # its name is the one a new key would take
    store = CredentialStore(tmp_path, fernet)
    accounts = {a.name: a for a in store.by_kind("account")}
    assert accounts["copied"].meta["ssh_key_id"] == saved.id
    enrolled = store.key_of(accounts["enrolled"])
    assert enrolled.id != saved.id and enrolled.name == "enrolled key" and enrolled.meta["ssoossh"]["server"] == "sso.lab"
    assert "ssoossh" not in store.get(saved.id).meta and store.get(saved.id).meta["cert"]
    assert not store.get(saved.id).is_default  # nothing picks a key by default
    plain = store.key_of(accounts["deploy key"])
    assert plain.id not in (saved.id, enrolled.id) and plain.name == "deploy key key" and "cert" not in plain.meta
    assert len(store.by_kind("ssh_key")) == 3


def test_an_account_that_cant_be_decrypted_is_left_as_it_is(tmp_path, keypair):
    key, _ = keypair
    fernet = Keys.load("k" * 40, tmp_path).fernet
    CredentialStore(tmp_path, fernet)  # makes the table
    old_account(tmp_path, fernet, "svc", key)
    other = CredentialStore(tmp_path, Keys.load("z" * 40, tmp_path).fernet)  # the master key changed
    acct = other.by_kind("account")[0]
    assert not acct.readable and acct.meta["has_key"] and not other.by_kind("ssh_key")


def test_the_ssh_key_form_shows_ssoossh_and_never_the_code(app, keypair):
    c = TestClient(app)
    key, _ = keypair
    r = c.post("/credentials", data={"kind": "ssh_key", "name": "svc-key", "private_key": key,
                                     "ssoossh_server": "sso.lab", "ssoossh_code": "s3cret-code"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    saved = app.state.creds.by_kind("ssh_key")[0]
    form = c.get(f"/credentials/{saved.id}").text
    assert 'value="sso.lab"' in form and "s3cret-code" not in form and "<details class=\"more\" open>" in form
    listing = c.get("/credentials/secrets").text
    assert "certificate from ssoossh" in listing and "s3cret-code" not in listing
    c.post("/credentials", data={"kind": "account", "name": "svc", "username": "ansible", "ssh_key_id": str(saved.id)})
    assert "certificate from ssoossh" in c.get("/credentials").text
    launcher = c.get("/runs/new", params={"playbook": "env.yml", "target": "box1"}).text
    assert "user ansible · ssoossh certificate · no become password" in launcher

    bad = c.post("/credentials", data={"kind": "ssh_key", "name": "svc2", "private_key": key,
                                       "ssoossh_server": "http://sso.lab", "ssoossh_code": "s3cret-code"})
    assert bad.status_code == 400 and 'value="http://sso.lab"' in bad.text and "s3cret-code" not in bad.text


def enrolled_account(c, app, key, code, server="https://sso.lab"):
    """An SSH key enrolled with ssoossh, and an account that signs in with it."""
    c.post("/credentials", data={"kind": "ssh_key", "name": "svc-key", "private_key": key,
                                 "ssoossh_server": server, "ssoossh_code": code})
    c.post("/credentials", data={"kind": "account", "name": "svc", "username": "ansible", "is_default": "1",
                                 "ssh_key_id": str(app.state.creds.by_kind("ssh_key")[0].id)})
    return app.state.creds.by_kind("account")[0]


@needs_ansible
def test_a_run_loads_the_certificate_ssoossh_fetched(app, keypair, fake_ssoossh):
    c = TestClient(app)
    key, _ = keypair
    acct = enrolled_account(c, app, key, "s3cret-code")
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "s3cret"})  # env.yml reads it
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id),
                              "env_secret": str(app.state.creds.by_kind("env_secret")[0].id)}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    log = app.state.store.log_path(run.id).read_text()
    assert run.status == "successful", log
    assert "[ansible-gui] Certificate from ssoossh (https://sso.lab): svc-ansible · principals ansible" in log
    assert "ED25519-CERT" in log  # loaded into the run's agent with the key
    assert "s3cret-code" not in log
    assert fake_ssoossh.read_text() == "https://sso.lab s3cret-code\n"  # once per run


@needs_ansible
def test_a_run_stops_when_ssoossh_refuses_the_code(app, keypair, fake_ssoossh):
    c = TestClient(app)
    key, _ = keypair
    acct = enrolled_account(c, app, key, "expired-code", server="sso.lab")
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id)}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error"
    assert run.error == ("Didn’t start: ssoossh couldn’t get a certificate from sso.lab: "
                         "the server refused this enrollment code (expired)")
    assert "expired-code" not in app.state.store.log_path(run.id).read_text()


@needs_ansible
def test_a_run_stops_when_its_accounts_key_was_deleted(app, keypair):
    c = TestClient(app)
    key, _ = keypair
    acct = enrolled_account(c, app, key, "s3cret-code")
    warned = c.get(f"/credentials/{acct.meta['ssh_key_id']}").text  # the key's page says so first
    assert "stop with an error until another key is picked" in warned
    c.post(f"/credentials/{acct.meta['ssh_key_id']}/delete")
    assert "key deleted" in c.get("/credentials").text
    assert "user ansible · SSH key deleted · no become password" in c.get("/runs/new").text
    r = c.post("/runs", data={"playbook": "env.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              "account": str(acct.id)}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert run.status == "error" and "The SSH key of the account “svc” was deleted" in run.error
