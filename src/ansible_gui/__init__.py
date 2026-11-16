"""ansible-gui: see what Ansible resolves for each host, and where it came from."""


def main() -> None:
    from .web import main as run

    run()
