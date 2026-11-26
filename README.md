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
| `ANSIBLE_GUI_VAULT_PASSWORD_FILE` | none | Needed only if a vars file is fully vault-encrypted. Its variables are shown as vaulted, never their values |
| `ANSIBLE_GUI_EDIT_URL` | none | Link template for "open in git", e.g. `https://git.example/me/ansible/src/branch/main/{path}#L{line}`. Must be `https://` or `http://` |
| `ANSIBLE_GUI_DATA_DIR` | `./data` | Run history, credentials (SQLite), run logs, `ssh/known_hosts`, and the git clones unless `ANSIBLE_GUI_REPOS_DIR` puts them elsewhere. Made 0700 at start, and everything written in it is private to the server's user |
| `ANSIBLE_GUI_REPOS_DIR` | `data/repos` | Bare clones, commit directories and collections. With workers, the repositories volume, mounted at the same path in the app and both workers |
| `ANSIBLE_GUI_FETCHER_SOCKET`, `ANSIBLE_GUI_RUNNER_SOCKET` | none | The Unix sockets of `ansible-gui worker` processes in other containers: the fetcher pulls and installs collections, the runner runs Ansible (both may be one worker's socket). Set: git and Ansible run there, not in the server. Unset: they run in the server, as its own user |
| `ANSIBLE_GUI_SSH_CONFIG` | none | The only ssh_config runs and git pulls read, relative to the project dir (absolute without one). Unset means none at all |
| `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING` | `accept-new` | `StrictHostKeyChecking` for runs and SSH pulls where the ssh_config doesn't set it: `accept-new` trusts a new host's first answer and refuses a changed key after that, `yes` reaches only hosts listed under Known hosts, `no` trusts any answer |
| `ANSIBLE_GUI_RUN_TIMEOUT_MINUTES` | `120` | A run whose Ansible is still going after this long is stopped and marked failed, so one hung playbook can't hold up every run queued behind it. `0` for no limit |
| `ANSIBLE_GUI_RUN_RETENTION_DAYS` | `90` | Finished runs older than this are deleted with their logs (each schedule keeps its latest). `0` keeps everything |
| `ANSIBLE_GUI_SECRET_KEY` (or `_FILE`) | generated `data/secret.key` | Encrypts credentials and signs sessions. Set it so a copy of `data/` alone can't decrypt credentials |
| `ANSIBLE_GUI_OIDC_ISSUER` | none | OIDC issuer URL, e.g. `https://id.example.com` (Pocket ID). Turns on sign-in |
| `ANSIBLE_GUI_OIDC_CLIENT_ID`, `ANSIBLE_GUI_OIDC_CLIENT_SECRET` (or `_FILE`) | | The OIDC client. Redirect URI: `<public url>/auth/callback` |
| `ANSIBLE_GUI_OIDC_GROUPS` | required with OIDC | Comma-separated groups allowed in (`groups` claim), or `*` for anyone the provider signs in |
| `ANSIBLE_GUI_PUBLIC_URL` | required with sign-in, and beyond loopback | The address people use, e.g. `https://ansible.example.com`. Besides `localhost`, its host is the only name requests may be addressed to; also used for the redirect URI, the same-origin check and secure cookies |
| `ANSIBLE_GUI_SMTP_HOST` | none | Mail server for notifications. Turns email on |
| `ANSIBLE_GUI_SMTP_PORT`, `ANSIBLE_GUI_SMTP_TLS` | `587`, `starttls` | `TLS` is `starttls`, `ssl` (port 465) or `none` (port 25) |
| `ANSIBLE_GUI_SMTP_USERNAME`, `ANSIBLE_GUI_SMTP_PASSWORD` (or `_FILE`) | | SMTP login, if the server wants one |
| `ANSIBLE_GUI_SMTP_FROM`, `ANSIBLE_GUI_NOTIFY_TO` | required with SMTP | Sender, and comma-separated recipients |
| `ANSIBLE_GUI_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are believed |
| `ANSIBLE_GUI_INSECURE_NO_AUTH` | | `1` allows listening beyond loopback without sign-in (only if a proxy in front does it). `ANSIBLE_GUI_PUBLIC_URL` is still needed |
| `ANSIBLE_GUI_HOST` / `ANSIBLE_GUI_PORT` | `127.0.0.1` / `8080` | Listen address |

Needs `ansible-core` on PATH (`uv run` installs the locked one on Python 3.13+). Ansible is only ever called as a
subprocess, never imported (it is GPLv3). Once read, every `ANSIBLE_GUI_*` variable is removed from the
server's environment, so no playbook, inventory plugin or collection inherits the secrets among them.

## Container

```sh
docker build -t ansible-gui .
```

The image is Alpine with ansible-gui, Python 3.14, ansible-core 2.22.0, git and OpenSSH. It runs as uid 10001 under tini and
listens on 8080 with data in `/data`. The same image runs the workers: `ansible-gui worker --jobs fetcher|runner
--socket PATH --allow-uid 10001` takes the app's jobs on a Unix socket and accepts a connection from uid 10001 only
(the kernel reports who is on the other end). `deploy/docker-compose.yml` runs the app, a fetcher (uid 10002, the
only container that can write the repositories) and a runner (uid 10003, with the repositories mounted
read-only, and `--clean-between-jobs`: it clears its container of what its jobs left each time they are done).
It refuses to start without sign-in: set the `ANSIBLE_GUI_OIDC_*` variables
(or `ANSIBLE_GUI_INSECURE_NO_AUTH=1` only when a proxy in front does the sign-in). It also refuses without
`ANSIBLE_GUI_PUBLIC_URL`, the one name besides `localhost` it answers to. `GET /healthz` backs the healthcheck and
answers under any name.

It holds what a run needs and little else: the Python packages in `uv.lock` in one venv, with no pip or other
installer, no source tree, and none of the base image's Python parts that have no use here (IDLE and tkinter,
and the NIS client with the Kerberos libraries it brought along). `deploy/smoke-test.sh` checks that they stay out.

Tasks a playbook runs on the controller (`delegate_to: localhost`, `connection: local`) run in this image: it has
bash and GNU tar for them, and otherwise BusyBox's tools and musl rather than glibc. A playbook that calls a
program built for glibc, or a Python package with no musl wheel, needs an image built on this one that adds it.

`deploy/` has a compose file and an env template for docker01. The compose file runs all three with
a read-only root filesystem, every capability dropped and `no-new-privileges`; `deploy/smoke-test.sh` runs a
playbook in the runner under exactly that, and checks it can't reach the key, the database, the app's environment,
the repositories or the fetcher's socket.

There is no upgrade path from the single-container layout: start the three containers with a fresh `data/`.
[docs/runner-split.md](docs/runner-split.md) explains the layout, the jobs and what it does and doesn't protect.

## Git repositories

Set up on the Repositories page: any number of inventory and playbook repositories,
each with a branch and, for private repositories, a git token or one of the stored SSH keys to sign in with.
ansible-gui keeps a bare clone of each in `data/repos/<id>/git/`, and writes each commit a pull lands on out once,
into its own directory, `data/repos/<id>/commits/<full hash>/`, which nothing writes into again. A pull lands on a
new directory and a running playbook keeps reading its own, so a run executes one commit from its first task to its
last. Directories no repository's current commit and no running run uses are removed.

Private repositories sign in with a git token (HTTPS) or an SSH key (a deploy key, or a key with a
certificate for a server that takes only those), both stored encrypted on the
Credentials page. A token goes to git from the environment, through a credential helper set for that one server
and for that pull only: never into the URL or `.git/config`, never saved, and not offered to another server a
redirect names. Give it read-only access to repository contents.

Adding or saving a repository, and **Pull now** on the Repositories page, start the pull in the background and
answer at once: the page shows the pull going and reloads when it ends, with the commit or the reason it failed.

Every run, by hand or scheduled, first pulls its inventory repository and the playbook's repository,
then asks Ansible again which hosts the targets match. If a pull fails, the run is canceled before anything
touches a host, and an email goes out even for a run started by hand or a schedule set to never email.
If the hosts changed since the preview, a run started by hand is canceled too; a schedule follows the inventory.

Each run records the full playbook commit, the full inventory commit and the collections it used. A playbook repository has a **Run from**
directory: Ansible starts there, so that directory's `ansible.cfg` applies, and the playbooks directly in it
are listed under their full path from the repository root (`lxc/site.yml`, never just `site.yml`). To run
two directories of one repository, add it twice with different Run from directories.

Collections: after a pull, `ansible-galaxy collection install` puts the collections the commit's requirements file
lists in `data/repos/collections/<SHA-256 of the file>/`, unless that directory is already there; runs and previews
of that commit use it. The file
is the one named on the repository, or else the first of `collections/requirements.yml` and `requirements.yml`
in the Run from directory, then at the top level. A failed install cancels the run like a failed pull, and the
repository stays at the commit it was at. Roles listed in the file are not installed. Of the repository's
`ansible.cfg`, the install uses only the Galaxy servers: `server`, `server_list`, `server_timeout`, `ignore_certs`
and the signature settings under `[galaxy]`, and each `[galaxy_server.<name>]` section. A commit's directory and a
collections directory lose their write permission once they are complete.

**Run again…** on a finished run opens the launcher filled in from it, and runs the latest commits. When a
repository has moved since, the launcher says so and offers **Run the commits run #N used instead**: nothing is
pulled, and the earlier run's playbook and inventory commits are written out again from the bare clone if they were
removed. If the branch was force-pushed and a commit is gone, the launcher says that and runs the latest. If the
collections that run used were removed, they are installed again from the same requirements file, and the run's
page notes that unless the file pins exact versions they may be newer. Schedules always run the latest.

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
Pulling while a run is going changes nothing under it. Deleting a repository a run is using is refused until the
run ends. A run whose own pull has to wait for one started from the web UI says so in its log.

The run page colours the output by what each line reports: ok, changed, failed, unreachable, skipped, the lines
a `--diff` added and removed, warnings, and the PLAY and TASK headings. Ansible's own colours stay off: they are
escape codes, and `output.log` (what **Download log** and the email hand out) stays plain text. The page works the
colour out from how Ansible's default output starts each line: with a custom `stdout_callback` that lays its
output out differently, the lines it doesn't recognise are left uncoloured.

ansible-runner's own directory (where it writes the run's environment, the become password and every event) is
inside the run's temp directory and wiped with the keys when the run ends; only `output.log` stays in
`data/runs/<id>/`. Runs older than `ANSIBLE_GUI_RUN_RETENTION_DAYS` are deleted at start and every six hours.

When the server stops (SIGTERM, `docker stop`), it ends the run in progress first: the playbook's processes, the
run's ssh-agent and its temp directory all go, and the run shows as interrupted after the next start. A server
that is killed outright can't do that, so each temp directory is noted in `data/scratch/` while it exists, and the
next start wipes whatever is still noted and stops the agent and playbook that went with it.

## Facts

Whenever a run gathers facts on a host (`gather_facts`, or the `setup` module), they are saved from the run's
events and shown under **Facts** on the host page: a summary (OS, kernel, virtualization, CPU and RAM, IPv4,
Python) and every fact, filterable. Nothing in `ansible.cfg` changes and no fact cache is configured, so plays
gather exactly as before. Only the latest facts per host are kept: a full gather replaces them, a partial one
(`gather_subset`, `filter`) updates the facts it returned. `ansible_env` is never saved.

**Gather facts** on a host or group page starts a read-only run, `ansible <target> -m ansible.builtin.setup`,
with the default credentials, the first playbook repository's `ansible.cfg`, and `--playbook-dir` so the
group_vars and host_vars next to the playbooks apply as they would in a run. With no playbook repository it
starts at the top of the inventory's repository, with that repository's `ansible.cfg` if it has one. Its log
shows a count per host, not the facts.

## Commands

**Run a command** (on Runs, and on every host and group page) runs one module on the hosts a pattern matches:
`ansible <targets> -m <module> -a <arguments>`, optionally with `--become`. Like Gather facts, it uses the first
playbook repository's `ansible.cfg` and the group_vars next to its playbooks, or the inventory's own repository
when there is no playbook repository. There is no dry run: `shell` and
`command` are skipped in check mode, so a "dry run" would report nothing. Instead the form lists the matched hosts,
the button needs a second click (except for `ping` and `setup`), and the run is refused if the hosts changed
since the list was shown. Commands appear in run history with **Run again…**; they can't be scheduled.

To see why a host can't be reached, raise **Verbosity** under More options: `-vvv` shows the ssh command Ansible
runs, `-vvvv` also what ssh itself says (the config files it read, the name it looked up, the keys it offered).
**Test the connection…**, beside Gather facts on a host's page, opens the form with `ansible.builtin.ping` at
`-vvvv`. For Gather facts with that detail, run `ansible.builtin.setup` here: its facts are saved all the same.

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

So is every request not addressed to this server by name: its `Host` must be `localhost`, `127.0.0.1`, `::1` or
the host of `ANSIBLE_GUI_PUBLIC_URL`. Otherwise a web page anywhere could point a DNS name of its own at
127.0.0.1 or a LAN address (DNS rebinding) and, same-origin as far as the browser can tell, run playbooks through
the visitor's browser. That matters most with no sign-in, where nothing else stands in the way.

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

- SSH reads only the ssh_config each run and pull writes into its own temp directory (passed as
  `--ssh-common-args -F …`). It includes the text of `ANSIBLE_GUI_SSH_CONFIG` first, then falls back to a copy of
  the known hosts with `StrictHostKeyChecking` set to `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING` (`accept-new` unless
  changed). The file's text travels with each job, so the workers need no mount for it; a file it `Include`s
  doesn't. Your file wins wherever it sets an option. An
  inventory `ansible_ssh_common_args` replaces the `-F`; the Credentials page lists hosts that do this.
- The app reads `ANSIBLE_GUI_SSH_CONFIG` as the user it runs as (uid 10001 in the image). If the file is missing or
  that user can't read it, no run starts and no pull over SSH is made: the run, the repository and the overview say
  why. Nothing connects without it, since the hosts would be reached as if the file were empty: none of its
  jump hosts, names or host key rules.
- With the default `accept-new`, a new host's key is recorded under **Known hosts** on the Credentials page
  on its first connection, and a different key is refused after that. Whoever answers that first connection
  is trusted, impostor or not. When a host is rebuilt with a new key, delete its line there. The worker reports
  the key and the app keeps it only if it is what ssh adds for a host it has just met: one key, for a name or
  `[name]:port` that has none yet. A certificate authority, a pattern, a hashed name (`HashKnownHosts yes` in
  your ssh_config) or a second key for a listed host is never taken from a run: add those yourself.
- With `yes`, a run or SSH pull reaches only hosts whose key is already listed. Paste `ssh-keyscan` output
  from a machine you trust, lines from your own `known_hosts`, or one `@cert-authority` line if your hosts
  have SSH certificates. `no` accepts any key, every time.
- Each run gets its own ssh-agent, holding only the SSH key (and certificate) chosen for it, or nothing.
  `Match exec` commands run with that agent's `SSH_AUTH_SOCK`, so a command that fetches a certificate and
  `ssh-add`s it works.
- An SSH key is saved once, under Secrets, and picked where it signs in: by an account (who a run signs in as)
  and by a repository pulled over SSH. An account holds no key of its own. Accounts saved before this had one:
  at the first start each becomes an SSH key named after its account, which the account then picks.
- An SSH key can fetch its certificate from [ssoossh](https://github.com/mnestor/ssoossh) instead of holding a
  pasted one: enroll the key once with `ssoossh service enroll --key <file>`, then save that key with the
  server and the enrollment code. Before each run the runner calls `ssoossh service retrieve` for the run's
  copy of the key and loads the certificate into the run's agent with it; the run's log names the certificate.
  Before each pull of a repository that signs in with such a key the fetcher does the same, and ssh presents
  the certificate with the key. The runner (`ansible-gui_hosts` in the compose layout) has to reach the ssoossh
  server, and so does the fetcher (`ansible-gui_fetch`) once a repository uses an enrolled key.
- Stored credentials (Fernet-encrypted in SQLite): SSH keys with optional certificates or ssoossh enrollment
  codes (no passphrases),
  vault passwords, become passwords, and environment secrets. A run gets an environment secret as `$NAME` and
  `$NAME_FILE` only if it is ticked for that run (under More options; a template or a schedule keeps its own
  list), so a playbook never sees a token meant for another. They are written to a 0700 temp dir for the run and
  wiped after it. Values are never shown back in the UI.
- A run's log leaves out the secrets the run was handed: its environment secrets, become and vault passwords,
  key and secret answers are written as `••••••` wherever Ansible prints one, as typed or inside a JSON result.
  That covers the run page, **Download log** and the email, which all read the same file. It is a net, not a
  guarantee: a value a playbook encodes (base64, a URL) or splits gets through, and one shorter than four
  characters is left alone. Keep `no_log: true` on the tasks that handle secrets.
- Hosts on another network are reached through a jump host: a `ProxyJump` line in `ANSIBLE_GUI_SSH_CONFIG`.
  The jump host is offered the run's own key or certificate, and carries an encrypted stream: it never holds the
  key, the become password or the vault password. In the compose layout the runner, on its own network
  (`ansible-gui_hosts`), needs to reach the jump host, and the fetcher (`ansible-gui_fetch`) does too if a git
  server sits behind one.

  ```
  Host *.lab.example
    ProxyJump ansible@jump.lab.example
  ```

## Security model

Everyone allowed to sign in is an admin: they can run any playbook, so they can run anything on any managed
host, and on this server. What ansible-gui protects against is everyone else: people who reach the port, other
sites in the same browser, a copy of `data/`, and the network between it and git or the hosts (from the
second connection on, unless `ANSIBLE_GUI_SSH_HOST_KEY_CHECKING=yes`).

**Write access to any configured repository is access to the credentials of the runs that use it.** Ansible
runs code from the repository: the inventory page and the launcher's host preview run its inventory scripts and
plugins and read its `ansible.cfg`; a pull installs the collections it lists; a run runs its playbooks. With the
workers (the compose file's layout), that code runs in the runner container, as another user, with only the job's
own credentials: the run's account, become and vault passwords, its environment secrets. It can't read the master
key, the other credentials, the OIDC and SMTP secrets or the database, can't sign a session, and can't change the
commit directories and collections later runs read (the runner mounts them read-only). Each time the runner's
jobs are done, it kills every process they left in its container and empties every directory they could write,
so nothing of one job is there for the next. What that code still can: use the run's credentials on the hosts
they reach, read the files of another job that runs in the runner at the same time, and have a key recorded for
a host that has none yet, as a first connection would. Nothing a playbook starts on the controller outlives its
run (a local `async` task with `poll: 0`, for one), and a cache Ansible keeps in the runner (an inventory
plugin's, facts in a file) starts empty for every job. The app
reads the repository's files too, to list playbooks and show where a variable is set: as YAML, and never
through a link that leads out of the repositories. The fetcher writes the repositories and is meant never to run
them. That rests on git (a fetch into a bare clone, then `read-tree` and `checkout-index`) and
`ansible-galaxy collection install` not executing what they fetch. A review on 21 Nov 2026 found no way for
them to, but found ansible-galaxy obeying the repository's `ansible.cfg`, which let one repository write into
another's files; the install now gets only its Galaxy servers, and finished directories can't be written. That
review was reading and testing, not proof: the fetcher holds a repository's key (with its certificate, or the
enrollment code that gets it one) or token only for the length of a pull. Give a repository a key of its own:
picking the key an account signs in to hosts with hands the fetcher what was the runner's alone.
[docs/runner-split.md](docs/runner-split.md) has the full table of what a hostile playbook gets.
Without workers (`ANSIBLE_GUI_FETCHER_SOCKET` unset), that code runs as the server's own user, and write
access to a repository is admin access to this server. Either way: protect the branches ansible-gui pulls, give it
read-only keys or tokens, and don't add a repository you wouldn't trust with those credentials.

Credentials are encrypted with a key derived from `ANSIBLE_GUI_SECRET_KEY`, which belongs outside `data/`. Nothing
under `data/` holds a secret in plain text: runs get theirs in a temp directory of the runner's that is wiped
afterwards, by the runner as the job ends or stops, or by its next start if it was killed. A job carries its
credentials, the known hosts and the configured ssh_config as text, never the server's environment.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

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
