"""ansible-gui: see what Ansible resolves for each host, and where it came from."""

USAGE = """usage: ansible-gui                     start the server
       ansible-gui sign-out-everyone   end every session now (in the container:
                                       docker exec ansible-gui-app ansible-gui sign-out-everyone)"""


def main() -> None:
    import sys

    args = sys.argv[1:]
    if not args:
        from .web import main as run

        return run()
    if args == ["sign-out-everyone"]:
        return sign_out_everyone()
    raise SystemExit(USAGE)


def sign_out_everyone() -> None:
    """For when someone must lose access before their session would end on its own."""
    import os
    from pathlib import Path

    from .sessions import SessionStore

    data_dir = Path(os.environ.get("ANSIBLE_GUI_DATA_DIR", "data")).expanduser().resolve()
    if not (data_dir / "ansible-gui.db").exists():
        raise SystemExit(f"No ansible-gui database in {data_dir}. Set ANSIBLE_GUI_DATA_DIR to the data directory.")
    n = SessionStore(data_dir).end_all()
    print(f"Ended {n} session{'s' if n != 1 else ''}. Everyone signs in again on their next click.")
