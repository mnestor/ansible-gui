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
    for bad in ("lower", "PATH", "ANSIBLE_BECOME_PASS", "SSH_AUTH_SOCK"):
        with pytest.raises(Invalid):
            store.save(kind="env_secret", name="e", values={"env": bad, "value": "v"}, is_default=False, user="")


def test_changed_master_key_is_reported(tmp_path):
    s1 = CredentialStore(tmp_path, Keys.load("a" * 40, tmp_path).fernet)
    s1.save(kind="become_password", name="sudo", values={"password": "pw"}, is_default=False, user="")
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
    c.post("/credentials", data={"kind": "become_password", "name": "sudo", "password": "pw-visible?"})
    page = c.get("/credentials").text
    assert "PRIVATE KEY" not in page and "pw-visible?" not in page and "SHA256:" in page
    cred = app.state.creds.by_kind("become_password")[0]
    assert "pw-visible?" not in c.get(f"/credentials/{cred.id}").text
    bad = c.post("/credentials", data={"kind": "become_password", "name": "bad name!", "password": "pw-again"})
    assert bad.status_code == 400 and "pw-again" not in bad.text
    launcher = c.get("/runs/new").text
    assert "signs in with lab" in launcher


@needs_ansible
def test_run_gets_its_own_agent_key_env_and_vault(app, keypair):
    c = TestClient(app)
    key, cert = keypair
    c.post("/credentials", data={"kind": "ssh_key", "name": "lab", "private_key": key, "certificate": cert,
                                 "is_default": "1"})
    c.post("/credentials", data={"kind": "env_secret", "name": "tok", "env": "TEST_TOKEN", "value": "s3cret"})
    c.post("/credentials", data={"kind": "vault_password", "name": "v", "password": "fixture-password",
                                 "is_default": "1"})
    creds = app.state.creds
    ids = {k: str(creds.by_kind(k)[0].id) for k in ("ssh_key", "vault_password")}

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
    assert run.credentials["ssh_key"]["name"] == "lab" and run.credentials["env_secret"][0]["env"] == "TEST_TOKEN"

    r = c.post("/runs", data={"playbook": "vault.yml", "target": "box1", "mode": "apply", "expected": ["box1"],
                              **ids}, follow_redirects=False)
    run = wait(app, int(r.headers["location"].rsplit("/", 1)[1]))
    assert "word=opened-the-vault" in app.state.store.log_path(run.id).read_text()


@needs_ansible
def test_deleted_credential_stops_queued_run(app):
    c = TestClient(app)
    c.post("/credentials", data={"kind": "become_password", "name": "sudo", "password": "pw"})
    cred = app.state.creds.by_kind("become_password")[0]
    run = app.state.store.create(playbook="site.yml", target="box1", tags="", skip_tags="", check_mode=True,
                                 diff=False, verbosity=0, expected_hosts=["box1"], trigger="t",
                                 credentials={"become_password": {"id": cred.id, "name": "sudo"}})
    app.state.creds.delete(cred.id)
    app.state.executor.submit(run.id)
    done = wait(app, run.id)
    assert done.status == "error" and "deleted" in done.error
