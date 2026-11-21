import pytest

from ansible_gui import config, repofiles


@pytest.fixture(autouse=True)
def answer_to_testserver(monkeypatch):
    """TestClient calls the app as http://testserver, a name the Host check refuses like any other that
    isn't this machine's own or ANSIBLE_GUI_PUBLIC_URL's. Tests of that check send their own Host header."""
    monkeypatch.setattr(config, "LOOPBACK_HOSTS", config.LOOPBACK_HOSTS | {"testserver"})


@pytest.fixture(autouse=True)
def read_repository_files_anywhere():
    """An app with workers confines what the server reads to its repositories, for the whole process. One
    server is one process; the tests are many apps in one, so each test starts and ends unconfined."""
    repofiles.confine(None)
    yield
    repofiles.confine(None)
