# ansible-gui

See what Ansible resolves for each host in your inventory, and which file set each value,
then run playbooks against the hosts you pick, watch them live, and schedule them.

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
| `ANSIBLE_GUI_DATA_DIR` | `./data` | Run history, credentials (SQLite), run logs, `ssh/known_hosts` |
| `ANSIBLE_GUI_SSH_CONFIG` | none | The only ssh_config runs read, relative to the project dir. Unset means none at all |
| `ANSIBLE_GUI_SECRET_KEY` (or `_FILE`) | generated `data/secret.key` | Encrypts credentials and signs sessions. Set it so a copy of `data/` alone can't decrypt credentials |
| `ANSIBLE_GUI_OIDC_ISSUER` | none | OIDC issuer URL, e.g. `https://id.example.com` (Pocket ID). Turns on sign-in |
| `ANSIBLE_GUI_OIDC_CLIENT_ID`, `ANSIBLE_GUI_OIDC_CLIENT_SECRET` (or `_FILE`) | | The OIDC client. Redirect URI: `<public url>/auth/callback` |
| `ANSIBLE_GUI_OIDC_GROUPS` | required with OIDC | Comma-separated groups allowed in (`groups` claim), or `*` for anyone the provider signs in |
| `ANSIBLE_GUI_PUBLIC_URL` | | e.g. `https://ansible.example.com`; used for the redirect URI, the same-origin check and secure cookies |
| `ANSIBLE_GUI_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are believed |
| `ANSIBLE_GUI_INSECURE_NO_AUTH` | | `1` allows listening beyond loopback without sign-in (only if a proxy in front does it) |
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

## Schedules

A schedule is a saved run (playbook, `--limit`, mode, tags, credentials) plus a five-field cron expression
in the server's time zone. A thread checks every 15 seconds. Each firing re-resolves the hosts, so a schedule
on a group follows the inventory. A schedule never overlaps itself: if its previous run is still going, the
new one is recorded as skipped. Runs missed while the server was down are recorded once as missed and not
caught up. Run one server process; two would both fire every schedule.

## Sign-in

With `ANSIBLE_GUI_OIDC_ISSUER` set, every page needs an OIDC sign-in (authorization code with PKCE, scopes
`openid profile email groups`) by a member of `ANSIBLE_GUI_OIDC_GROUPS`. Without it there is no sign-in, and
the server refuses to listen anywhere but loopback. Writes (POST) from another origin are refused either way.

## Credentials and SSH

Runs never use the server user's `~/.ssh/config`, `known_hosts` or ssh-agent:

- SSH reads only `data/ssh/ssh_config` (passed as `--ssh-common-args -F …`), which includes
  `ANSIBLE_GUI_SSH_CONFIG` first, then falls back to `data/ssh/known_hosts` with `StrictHostKeyChecking accept-new`.
  Your file wins wherever it sets an option. An inventory `ansible_ssh_common_args` replaces the `-F`; the
  Credentials page lists hosts that do this.
- Each run gets its own ssh-agent, holding only the SSH key (and certificate) chosen for it, or nothing.
  `Match exec` commands run with that agent's `SSH_AUTH_SOCK`, so a command that fetches a certificate and
  `ssh-add`s it works.
- Stored credentials (Fernet-encrypted in SQLite): SSH keys with optional certificates (no passphrases),
  vault passwords, become passwords, and environment secrets, which every run gets as `$NAME` and
  `$NAME_FILE`. They are written to a 0700 temp dir for the run and wiped after it. Values are never shown
  back in the UI.

## Tests

```sh
uv run pytest
```
