# ansible-gui

See what Ansible resolves for each host in your inventory, and which file set each value,
then run playbooks against the hosts you pick and watch them live. Schedules come next.

## Run it

```sh
ANSIBLE_GUI_PROJECT_DIR=~/git/ansible/lxc \
ANSIBLE_GUI_INVENTORY=inventory/hosts.yml \
uv run ansible-gui            # http://127.0.0.1:8080
```

| Variable | Default | Meaning |
|---|---|---|
| `ANSIBLE_GUI_PROJECT_DIR` | required | Directory you run `ansible-playbook` from |
| `ANSIBLE_GUI_INVENTORY` | `inventory` | Inventory source(s), comma-separated, relative to the project dir |
| `ANSIBLE_GUI_PLAYBOOK_DIR` | `.` | Where playbook-adjacent `group_vars/` and `host_vars/` live |
| `ANSIBLE_GUI_VAULT_PASSWORD_FILE` | none | Needed only if a vars file is fully vault-encrypted |
| `ANSIBLE_GUI_EDIT_URL` | none | Link template for "open in git", e.g. `https://git.example/me/ansible/src/branch/main/{path}#L{line}` |
| `ANSIBLE_GUI_DATA_DIR` | `./data` | Run history (SQLite) and run logs |
| `ANSIBLE_GUI_HOST` / `ANSIBLE_GUI_PORT` | `127.0.0.1` / `8080` | Listen address |

Needs `ansible-core` on PATH. Ansible is only ever called as a subprocess, never imported (it is GPLv3).

## How sources are worked out

`ansible-inventory --list` gives the final values. A second call with vars plugins switched off
gives what the inventory file itself sets. The app then replays Ansible's default precedence
(inventory group vars → `group_vars/all` → `group_vars/<group>` by depth, priority, name →
host line → `host_vars/`), inventory directory before playbook directory, and compares every
result with Ansible's. Any disagreement is flagged on the host; the value shown is always Ansible's.

## Runs

Runs go through [ansible-runner](https://github.com/ansible/ansible-runner), one at a time, with the
project directory as the working directory (so its `ansible.cfg` applies). The launcher asks
`ansible-playbook --list-hosts` which hosts the playbook and `--limit` pattern match, and the run is
refused if that list changed between the preview and the click. Dry run (`--check`) is the default.

Credentials are not managed yet: runs use the SSH config, keys and agent of the user running the server.

## Tests

```sh
uv run pytest
```
