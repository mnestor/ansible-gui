# ansible-gui

See what Ansible resolves for each host in your inventory, and which file set each value,
then run playbooks against the hosts you pick, watch them live, and schedule them.

## Run it

```sh
uv run ansible-gui            # http://127.0.0.1:8080, then add repositories on the Repositories page
```

Or, for a checkout you manage yourself:

```sh
ANSIBLE_GUI_PROJECT_DIR=~/git/ansible/lxc ANSIBLE_GUI_INVENTORY=inventory/hosts.yml uv run ansible-gui
```

| Variable | Default | Meaning |
|---|---|---|
| `ANSIBLE_GUI_PROJECT_DIR` | none | A local checkout used as inventory and playbooks. A git repository of the same role on the Repositories page replaces it |
| `ANSIBLE_GUI_INVENTORY` | `inventory` | Inventory source(s), comma-separated, relative to the project dir |
| `ANSIBLE_GUI_PLAYBOOK_DIR` | `.` | Where playbook-adjacent `group_vars/` and `host_vars/` live |
| `ANSIBLE_GUI_VAULT_PASSWORD_FILE` | none | Needed only if a vars file is fully vault-encrypted |
| `ANSIBLE_GUI_EDIT_URL` | none | Link template for "open in git", e.g. `https://git.example/me/ansible/src/branch/main/{path}#L{line}`. Must be `https://` or `http://` |
| `ANSIBLE_GUI_DATA_DIR` | `./data` | Run history, credentials (SQLite), run logs, `ssh/known_hosts`, git clones. Made 0700 at start, and everything written in it is private to the server's user |
| `ANSIBLE_GUI_SSH_CONFIG` | none | The only ssh_config runs and git pulls read, relative to the project dir (absolute without one). Unset means none at all |
| `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING` | `accept-new` | `StrictHostKeyChecking` for runs and SSH pulls where the ssh_config doesn't set it: `accept-new` trusts a new host's first answer and refuses a changed key after that, `yes` reaches only hosts listed under Known hosts, `no` trusts any answer |
| `ANSIBLE_GUI_RUN_RETENTION_DAYS` | `90` | Finished runs older than this are deleted with their logs (each schedule keeps its latest). `0` keeps everything |
| `ANSIBLE_GUI_SECRET_KEY` (or `_FILE`) | generated `data/secret.key` | Encrypts credentials and signs sessions. Set it so a copy of `data/` alone can't decrypt credentials |
| `ANSIBLE_GUI_OIDC_ISSUER` | none | OIDC issuer URL, e.g. `https://id.example.com` (Pocket ID). Turns on sign-in |
| `ANSIBLE_GUI_OIDC_CLIENT_ID`, `ANSIBLE_GUI_OIDC_CLIENT_SECRET` (or `_FILE`) | | The OIDC client. Redirect URI: `<public url>/auth/callback` |
| `ANSIBLE_GUI_OIDC_GROUPS` | required with OIDC | Comma-separated groups allowed in (`groups` claim), or `*` for anyone the provider signs in |
| `ANSIBLE_GUI_PUBLIC_URL` | | e.g. `https://ansible.example.com`; used for the redirect URI, the same-origin check and secure cookies |
| `ANSIBLE_GUI_SMTP_HOST` | none | Mail server for notifications. Turns email on |
| `ANSIBLE_GUI_SMTP_PORT`, `ANSIBLE_GUI_SMTP_TLS` | `587`, `starttls` | `TLS` is `starttls`, `ssl` (port 465) or `none` (port 25) |
| `ANSIBLE_GUI_SMTP_USERNAME`, `ANSIBLE_GUI_SMTP_PASSWORD` (or `_FILE`) | | SMTP login, if the server wants one |
| `ANSIBLE_GUI_SMTP_FROM`, `ANSIBLE_GUI_NOTIFY_TO` | required with SMTP | Sender, and comma-separated recipients |
| `ANSIBLE_GUI_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are believed |
| `ANSIBLE_GUI_INSECURE_NO_AUTH` | | `1` allows listening beyond loopback without sign-in (only if a proxy in front does it) |
| `ANSIBLE_GUI_HOST` / `ANSIBLE_GUI_PORT` | `127.0.0.1` / `8080` | Listen address |

Needs `ansible-core` on PATH (`uv run` installs the locked one on Python 3.12+). Ansible is only ever called as a
subprocess, never imported (it is GPLv3). Once read, every `ANSIBLE_GUI_*` variable is removed from the
server's environment, so no playbook, inventory plugin or collection inherits the secrets among them.

## Container

```sh
docker build -t ansible-gui .
```

The image holds ansible-gui, ansible-core 2.21.4, git and OpenSSH, runs as uid 10001 under tini, and listens
on 8080 with data in `/data`. It refuses to start without sign-in: set the `ANSIBLE_GUI_OIDC_*` variables (or
`ANSIBLE_GUI_INSECURE_NO_AUTH=1` only when a proxy in front does the sign-in). `GET /healthz` backs the
healthcheck. `deploy/` has a compose file and an env template for docker01. The compose file runs it with a
read-only root filesystem, every capability dropped and `no-new-privileges`; `deploy/smoke-test.sh` runs a playbook
under exactly that.

## Git repositories

Set up on the Repositories page: any number of inventory and playbook repositories,
each with a branch and, for private repositories, a deploy key picked from the stored SSH keys.
ansible-gui keeps its own clones in `data/repos/`, and resets each to the tip of its branch on every pull,
discarding local changes.

Private repositories sign in with a git token (HTTPS) or an SSH deploy key, both stored encrypted on the
Credentials page. A token goes to git through `GIT_ASKPASS` only: never into the URL, `.git/config` or a credential
helper. Give it read-only access to repository contents.

Every run, by hand or scheduled, first pulls its inventory repository and the playbook's repository,
then asks Ansible again which hosts the targets match. If a pull fails, the run is canceled before anything
touches a host, and an email goes out even for a run started by hand or a schedule set to never email.
If the hosts changed since the preview, a run started by hand is canceled too; a schedule follows the inventory.

Each run records the playbook commit and the inventory commit. A playbook repository has a **Run from**
directory: Ansible starts there, so that directory's `ansible.cfg` applies, and the playbooks directly in it
are listed under their full path from the repository root (`lxc/site.yml`, never just `site.yml`). To run
two directories of one repository, add it twice with different Run from directories.

Collections: after a pull, if the repository's requirements file changed, `ansible-galaxy collection install`
puts its collections in `data/collections/<repo>/`, which runs and previews of that repository use. The file
is the one named on the repository, or else the first of `collections/requirements.yml` and `requirements.yml`
in the Run from directory, then at the top level. A failed install cancels the run like a failed pull. Roles
listed in the file are not installed.

An inventory repository can name several inventory files (`lxc/inventory/hosts.yml, lockdown/inventory/hosts.yml`);
Ansible merges them into one inventory.

Several inventory repositories stay separate inventories. The Hosts and Groups lists show all of them,
but values are never merged across them: a host or group page has one section per inventory it's in,
each exactly what a run against that inventory gets. Host names in more than one inventory are flagged
(groups like `all` aren't). Each run and schedule picks its inventory, records it, and pulls only that
one; last-run results are kept per inventory. Runs from before this keep working while there is a single
inventory.

Playbook-adjacent `group_vars/` and the playbook repository's own variables aren't shown on the
inventory page, which reads the inventory repositories only.

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

ansible-runner's own directory (where it writes the run's environment, the become password and every event) is
inside the run's temp directory and wiped with the keys when the run ends; only `output.log` stays in
`data/runs/<id>/`. Runs older than `ANSIBLE_GUI_RUN_RETENTION_DAYS` are deleted at start and every six hours.

## Facts

Whenever a run gathers facts on a host (`gather_facts`, or the `setup` module), they are saved from the run's
events and shown under **Facts** on the host page: a summary (OS, kernel, virtualization, CPU and RAM, IPv4,
Python) and every fact, filterable. Nothing in `ansible.cfg` changes and no fact cache is configured, so plays
gather exactly as before. Only the latest facts per host are kept: a full gather replaces them, a partial one
(`gather_subset`, `filter`) updates the facts it returned. `ansible_env` is never saved.

**Gather facts** on a host or group page starts a read-only run, `ansible <target> -m ansible.builtin.setup`,
with the default credentials, the first playbook repository's `ansible.cfg`, and `--playbook-dir` so the
group_vars and host_vars next to the playbooks apply as they would in a run. Its log shows a count per host,
not the facts.

## Commands

**Run a command** (on Runs, and on every host and group page) runs one module on the hosts a pattern matches:
`ansible <targets> -m <module> -a <arguments>`, optionally with `--become`. Like Gather facts, it uses the first
playbook repository's `ansible.cfg` and the group_vars next to its playbooks. There is no dry run: `shell` and
`command` are skipped in check mode, so a "dry run" would report nothing. Instead the form lists the matched hosts,
the button needs a second click (except for `ping` and `setup`), and the run is refused if the hosts changed
since the list was shown. Commands appear in run history with **Run again…**; they can't be scheduled.

## Ansible settings

**Ansible settings**, next to each playbook repository on Repositories, shows what `ansible-config dump
--only-changed` reports for runs from it: every setting that isn't Ansible's default, and whether the repository's
`ansible.cfg` or an environment variable set it. Galaxy server tokens and other credential-looking values are shown
as "set, not shown". Below that, `ansible-galaxy collection list`: each installed collection, its version and where
it is, with a copy that an earlier path shadows marked "not used".

## Schedules

A schedule is a saved run (playbook, `--limit`, mode, tags, credentials) plus a five-field cron expression
in the server's time zone. A thread checks every 15 seconds. Each firing re-resolves the hosts, so a schedule
on a group follows the inventory. A schedule never overlaps itself: if its previous run is still going, the
new one is recorded as skipped. Runs missed while the server was down are recorded once as missed and not
caught up. Run one server process; two would both fire every schedule.

## Email

Scheduled runs mail `ANSIBLE_GUI_NOTIFY_TO` according to each schedule's setting: "Problems and recoveries"
(default: failed or unreachable hosts, couldn't start, skipped, missed, interrupted, dry runs that found changes,
and the first good run after a problem), "Every run", or "Never". Runs started by hand never mail. The email lists
each host's result and the failing tasks' lines, not the whole log. Whether it was sent is shown on the run page;
the Schedules page has a test button.

## Sign-in

With `ANSIBLE_GUI_OIDC_ISSUER` set, every page needs an OIDC sign-in (authorization code with PKCE, scopes
`openid profile email groups`) by a member of `ANSIBLE_GUI_OIDC_GROUPS`. Without it there is no sign-in, and
the server refuses to listen anywhere but loopback. Writes (POST) from another origin are refused either way.

Every sign-in is recorded on the server, and a session lasts 12 hours from sign-in however much it's used, so
group membership is checked again at least that often. Signing out ends the session everywhere, including in
a copied cookie. To cut everyone off now (say, after removing someone from the group):

```sh
docker exec ansible-gui-app ansible-gui sign-out-everyone
```

Pages are sent with a Content-Security-Policy that allows scripts from `/static` only, may not be framed, and
aren't cached by the browser.

## Credentials and SSH

Runs never use the server user's `~/.ssh/config`, `known_hosts` or ssh-agent:

- SSH reads only `data/ssh/ssh_config` (passed as `--ssh-common-args -F …`), which includes
  `ANSIBLE_GUI_SSH_CONFIG` first, then falls back to `data/ssh/known_hosts` with `StrictHostKeyChecking` set to
  `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING` (`accept-new` unless changed). Your file wins wherever it sets an option. An
  inventory `ansible_ssh_common_args` replaces the `-F`; the Credentials page lists hosts that do this.
- With the default `accept-new`, a new host's key is recorded under **Known hosts** on the Credentials page
  on its first connection, and a different key is refused after that. Whoever answers that first connection
  is trusted, impostor or not. When a host is rebuilt with a new key, delete its line there.
- With `yes`, a run or SSH pull reaches only hosts whose key is already listed. Paste `ssh-keyscan` output
  from a machine you trust, lines from your own `known_hosts`, or one `@cert-authority` line if your hosts
  have SSH certificates. `no` accepts any key, every time.
- Each run gets its own ssh-agent, holding only the SSH key (and certificate) chosen for it, or nothing.
  `Match exec` commands run with that agent's `SSH_AUTH_SOCK`, so a command that fetches a certificate and
  `ssh-add`s it works.
- Stored credentials (Fernet-encrypted in SQLite): SSH keys with optional certificates (no passphrases),
  vault passwords, become passwords, and environment secrets, which every run gets as `$NAME` and
  `$NAME_FILE`. They are written to a 0700 temp dir for the run and wiped after it. Values are never shown
  back in the UI.

## Security model

Everyone allowed to sign in is an admin: they can run any playbook, so they can run anything on any managed
host, and on this server. What ansible-gui protects against is everyone else: people who reach the port, other
sites in the same browser, a copy of `data/`, and the network between it and git or the hosts (from the
second connection on, unless `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING=yes`).

**Write access to any configured repository is the same as admin access to this server.** Ansible runs code
from the repository as ansible-gui's own user: the inventory page and the launcher's host preview already run its
inventory scripts and plugins and read its `ansible.cfg`; a pull installs the collections it lists; a run runs its
playbooks. That code can read the master key and decrypt every stored credential. Protect the branches
ansible-gui pulls, give it read-only deploy keys or tokens, and don't add a repository you wouldn't give a shell on
this server to.

Credentials are encrypted with a key derived from `ANSIBLE_GUI_SECRET_KEY`, which belongs outside `data/`. Nothing
under `data/` holds a secret in plain text: runs get theirs in a temp directory that is wiped afterwards.

## Tests

```sh
uv run pytest
```

## License

ansible-gui is MIT licensed; see [LICENSE](LICENSE).

It never imports Ansible: it runs `ansible-inventory` and `ansible-playbook` as separate programs. The
container image also contains [ansible-core](https://github.com/ansible/ansible), installed unmodified
from PyPI, which is licensed under the GNU GPL v3; its source is at https://github.com/ansible/ansible
and the version is set by the `ansible` dependency group in `pyproject.toml`. Other bundled components keep
their own licenses.
