import pytest

from ansible_gui import config, repofiles
from ansible_gui.repos.workspace import Workspace


def pytest_configure(config):
    config.addinivalue_line("markers", "background_pulls: pulls the web UI starts run on their own thread, as "
                                       "in production, instead of before the response")


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


@pytest.fixture(autouse=True)
def pulls_finish_before_the_response(request, monkeypatch):
    """A pull started from the web UI runs on a thread of its own, and the page shows it going. The tests read
    what it did straight after the request, so here it runs before the response; a test marked
    background_pulls gets the thread."""
    if not request.node.get_closest_marker("background_pulls"):
        monkeypatch.setattr(Workspace, "_spawn", staticmethod(lambda work: work()))
