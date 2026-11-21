import pytest

from ansible_gui import config


@pytest.fixture(autouse=True)
def answer_to_testserver(monkeypatch):
    """TestClient calls the app as http://testserver, a name the Host check refuses like any other that
    isn't this machine's own or ANSIBLE_GUI_PUBLIC_URL's. Tests of that check send their own Host header."""
    monkeypatch.setattr(config, "LOOPBACK_HOSTS", config.LOOPBACK_HOSTS | {"testserver"})
