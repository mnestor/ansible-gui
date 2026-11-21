# Security

## Reporting a vulnerability

Report it privately, not in a public issue. On GitHub, open this repository's **Security** tab and choose
**Report a vulnerability**: that starts a private advisory only the maintainer can read.

The most useful report says what you sent or did, what happened, and what you expected instead: a request, a
command or a few lines that reproduce it. Include the version or commit, and how ansible-gui was running
(the container image or `uv run`, with or without sign-in).

## Which versions get fixes

The latest release and `main`. There are no maintained older branches.

## What counts

ansible-gui runs Ansible for the people allowed to use it, so some things are how it works, not flaws in it.
The README's [Security model](README.md#security-model) is the long version. In short:

- **Everyone who can sign in is an admin.** They can run any playbook or command on any managed host and on the
  server itself, and so can read every stored credential. One signed-in person doing something another
  signed-in person could do is not a vulnerability.
- **Write access to a configured repository is admin access.** Ansible runs that repository's code as
  ansible-gui's own user.

A vulnerability is anything that lets someone else in, or lets a secret out:

- doing or reading anything without signing in, when sign-in is on;
- another website acting through a signed-in person's browser, or reaching an instance that listens only on
  loopback;
- a secret (a key, a password, a token, a vaulted value) shown on a page, written to a log or left on disk in
  plain text;
- a copy of the data directory alone being enough to read the stored credentials, when the master key is kept
  outside it;
- the network between ansible-gui and git or the managed hosts being able to change what runs, beyond what
  the README says about the first connection to a host.
