#!/usr/bin/env bash
# Starts the image and checks what a green build can't: it refuses to run open, it starts,
# it runs as the unprivileged user with private data, and the tools it shells out to are there.
#   deploy/smoke-test.sh ansible-gui:test
set -euo pipefail
image=${1:?usage: smoke-test.sh IMAGE}
name=agui-smoke-$$
fetcher=agui-smoke-fetcher-$$ runner=agui-smoke-runner-$$ repos=agui-smoke-repos-$$
fsock=agui-smoke-fsock-$$ rsock=agui-smoke-rsock-$$
fail() {
  echo "FAIL: $*" >&2
  for c in "$name" "$fetcher" "$runner"; do docker logs "$c" 2>&1 | tail -20 | sed "s/^/$c: /" >&2 || true; done
  exit 1
}
cleanup() {
  docker rm -f "$name" "$fetcher" "$runner" >/dev/null 2>&1 || true
  docker volume rm "$fsock" "$rsock" "$repos" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# The hardening deploy/docker-compose.yml applies; the last check runs the image under it.
HARDENED=(--read-only --tmpfs /tmp:size=512m --tmpfs /home/app:size=64m,uid=10001,gid=10001,mode=0700
          --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 512 --memory 1536m --cpus 2)

# refuses EXPECTED [docker run args...]: the image must exit, saying EXPECTED
refuses() {
  local expected=$1 out; shift
  if out=$(docker run --rm "$@" "$image" 2>&1); then fail "started, but should have refused: $expected"; fi
  grep -q "$expected" <<<"$out" || fail "unexpected refusal: $out"
}

echo "- refuses to listen without sign-in"
refuses "Refusing to listen on 0.0.0.0 without sign-in"

echo "- refuses OIDC without allowed groups"
refuses "ANSIBLE_GUI_OIDC_GROUPS" -e ANSIBLE_GUI_OIDC_ISSUER=https://id.example.test -e ANSIBLE_GUI_OIDC_CLIENT_ID=x \
  -e ANSIBLE_GUI_OIDC_CLIENT_SECRET=y

echo "- refuses to listen beyond loopback without the name people use"
refuses "Refusing to listen on 0.0.0.0 without ANSIBLE_GUI_PUBLIC_URL" -e ANSIBLE_GUI_INSECURE_NO_AUTH=1

echo "- starts and answers"
RUN=(-e ANSIBLE_GUI_INSECURE_NO_AUTH=1 -e ANSIBLE_GUI_PUBLIC_URL=http://ansible.example.test
     -e ANSIBLE_GUI_SECRET_KEY=smoke-test-key-smoke-test-key-0123456789)
docker run -d --name "$name" "${RUN[@]}" "$image" >/dev/null
# status of GET $1, asked for under the name $2 (the Host header); by default as the healthcheck asks, by address
get() { docker exec "$name" python -c "
import urllib.request, urllib.error, sys
req = urllib.request.Request('http://127.0.0.1:8080$1', headers={'Host': '${2:-127.0.0.1:8080}'})
try: sys.stdout.write(str(urllib.request.urlopen(req, timeout=4).status))
except urllib.error.HTTPError as e: sys.stdout.write(str(e.code))"; }
wait_healthy() {
  for _ in $(seq 60); do [ "$(get /healthz 2>/dev/null)" = 200 ] && return; sleep 1; done
  fail "${1:-}/healthz never answered"
}
wait_healthy
for page in / /inventory /runs /runs/new /schedules /repos /credentials /static/app.css /static/htmx.min.js; do
  [ "$(get "$page")" = 200 ] || fail "$page didn't return 200"
done

echo "- answers to its own names only"
[ "$(get /runs ansible.example.test)" = 200 ] || fail "the public URL's host wasn't answered"
[ "$(get /runs rebind.evil.example:8080)" = 400 ] || fail "a request addressed to another name was answered"
[ "$(get /healthz ansible-gui-app:8080)" = 200 ] || fail "/healthz should answer a probe under any name"

echo "- runs as uid 10001 with private data"
[ "$(docker exec "$name" id -u)" = 10001 ] || fail "not running as uid 10001"
[ "$(docker exec "$name" stat -c %a /data)" = 700 ] || fail "/data isn't 0700"
for f in ansible-gui.db secret.key; do
  if docker exec "$name" test -e "/data/$f"; then
    [ "$(docker exec "$name" stat -c %a "/data/$f")" = 600 ] || fail "/data/$f isn't 0600"
  fi
done

echo "- has the tools it runs"
docker exec "$name" ansible-playbook --version | head -1 | grep -q "core 2.22.0" || fail "ansible-core isn't 2.22.0"
docker exec "$name" ansible-galaxy --version >/dev/null || fail "ansible-galaxy missing"
docker exec "$name" git --version >/dev/null || fail "git missing"
docker exec "$name" ssh -V >/dev/null 2>&1 || fail "ssh missing"
for tool in ssh-keygen ssh-agent ssh-add ssoossh; do
  docker exec "$name" sh -c "command -v $tool" >/dev/null || fail "$tool missing"
done
# For playbooks' local tasks; the image is Alpine, whose own sh and tar are BusyBox's.
# (grep without -q, here and below: -q stops reading at the first match, and under pipefail a docker exec
# still writing the rest of its output then fails the line.)
docker exec "$name" bash -c 'set -o pipefail; echo ok | grep -q ok' || fail "bash missing"
docker exec "$name" tar --version | grep "GNU tar" >/dev/null || fail "tar isn't GNU tar"
docker exec "$name" python -c "import ansible" 2>/dev/null && echo "  (ansible-core is importable in the venv, as expected; the app never imports it)"

echo "- carries nothing it has no use for"
docker exec "$name" sh -c 'command -v pip || command -v pip3 || command -v uv' >/dev/null && fail "an installer is in the image"
docker exec "$name" python -c "import pip" 2>/dev/null && fail "pip is importable"
docker exec "$name" python -c "import ensurepip" 2>/dev/null && fail "ensurepip is importable"
[ "$(docker exec "$name" sh -c 'ls -A /app | sort | tr "\n" " "')" = ".venv LICENSE " ] \
  || fail "/app holds more than the venv and the license"
for pkg in krb5-libs libtirpc libnsl tcl tk; do
  docker exec "$name" apk info -e "$pkg" >/dev/null 2>&1 && fail "$pkg is installed, and nothing here uses it"
done
docker exec "$name" python -c "import websockets" 2>/dev/null && fail "websockets is installed: uvicorn[standard] is back?"
# What was removed took no library a remaining module needs, and the server keeps its fast loop and parser.
docker exec "$name" python -c "import sqlite3, ssl, hashlib, ctypes, readline, curses, dbm.gnu, bz2, lzma, zlib, uuid" \
  || fail "a Python module lost its library"
docker exec "$name" python -c "import uvloop, httptools, cryptography.fernet, yaml" || fail "a compiled package doesn't load"

echo "- runs hardened: read-only root, no capabilities, no new privileges, limited processes and memory"
docker rm -f "$name" >/dev/null
docker run -d --name "$name" "${HARDENED[@]}" "${RUN[@]}" "$image" >/dev/null
wait_healthy "hardened: "
[ "$(get /credentials)" = 200 ] || fail "hardened: /credentials didn't return 200"
docker exec "$name" grep -q "^CapEff:[[:space:]]*0000000000000000" /proc/1/status || fail "hardened: capabilities left"
[ "$(docker exec "$name" cat /sys/fs/cgroup/pids.max)" = 512 ] || fail "hardened: no process limit"
[ "$(docker exec "$name" cat /sys/fs/cgroup/memory.max)" = $((1536 * 1024 * 1024)) ] || fail "hardened: no memory limit"
docker exec "$name" ansible localhost -c local -m setup -a filter=ansible_distribution \
  -e ansible_python_interpreter=/app/.venv/bin/python | grep '"Alpine"' >/dev/null || fail "hardened: a local Ansible task failed"
docker exec "$name" ansible localhost -c local -m ansible.builtin.shell \
  -a 'set -o pipefail && echo "bash $BASH_VERSION" | cat executable=/bin/bash' \
  -e ansible_python_interpreter=/app/.venv/bin/python | grep '^bash [0-9]' >/dev/null || fail "hardened: a local bash task failed"
docker exec "$name" sh -c 'eval "$(ssh-agent -s -a /tmp/smoke-agent.sock)" >/dev/null && ssh-agent -k >/dev/null' \
  || fail "hardened: ssh-agent didn't start"

echo "- runs split: the runner runs a hostile playbook, and reaches none of the app's secrets or the clones"
docker rm -f "$name" >/dev/null
docker volume create --driver local --opt type=tmpfs --opt device=tmpfs \
  --opt o=uid=10002,gid=10010,mode=0750 "$fsock" >/dev/null
docker volume create --driver local --opt type=tmpfs --opt device=tmpfs \
  --opt o=uid=10003,gid=10011,mode=0750 "$rsock" >/dev/null
docker volume create "$repos" >/dev/null   # filled from the image's /repos: owned by the fetcher, 0755
WORKER=(--read-only --tmpfs /tmp:size=512m --cap-drop ALL --security-opt no-new-privileges:true
        --pids-limit 512 --memory 1536m --cpus 2 -e ANSIBLE_GUI_REPOS_DIR=/repos)
docker run -d --name "$fetcher" "${WORKER[@]}" --user 10002:10010 \
  --tmpfs /home/fetcher:size=64m,uid=10002,gid=10010,mode=0700 -e ANSIBLE_HOME=/home/fetcher/.ansible \
  --health-cmd "test -S /run/fetcher/fetcher.sock" -v "$repos:/repos" -v "$fsock:/run/fetcher" \
  "$image" ansible-gui worker --jobs fetcher --socket /run/fetcher/fetcher.sock --allow-uid 10001 >/dev/null
docker run -d --name "$runner" "${WORKER[@]}" --user 10003:10011 \
  --tmpfs /home/runner:size=64m,uid=10003,gid=10011,mode=0700 -e ANSIBLE_HOME=/home/runner/.ansible \
  --health-cmd "test -S /run/runner/runner.sock" -v "$repos:/repos:ro" -v "$rsock:/run/runner" \
  "$image" ansible-gui worker --jobs runner --socket /run/runner/runner.sock --allow-uid 10001 \
  --clean-between-jobs /tmp /home/runner /dev/shm /dev/mqueue /run/runner >/dev/null
for w in fetcher runner; do
  c=agui-smoke-$w-$$
  for _ in $(seq 30); do docker exec "$c" test -S "/run/$w/$w.sock" 2>/dev/null && break; sleep 1; done
  docker exec "$c" test -S "/run/$w/$w.sock" || fail "split: the $w never opened its socket"
  [ "$(docker exec "$c" stat -c %a "/run/$w/$w.sock")" = 660 ] || fail "split: the $w socket isn't 0660"
done
docker run -d --name "$name" "${HARDENED[@]}" "${RUN[@]}" --group-add 10010 --group-add 10011 \
  -e ANSIBLE_GUI_REPOS_DIR=/repos -e ANSIBLE_GUI_FETCHER_SOCKET=/run/fetcher/fetcher.sock \
  -e ANSIBLE_GUI_RUNNER_SOCKET=/run/runner/runner.sock \
  -v "$repos:/repos:ro" -v "$fsock:/run/fetcher" -v "$rsock:/run/runner" "$image" >/dev/null
wait_healthy "split: "

# A repository whose playbook reaches for what only the app has, and for the clones. Made in the fetcher's
# /tmp, so git there trusts it; the app adds it by URL like any other.
docker exec "$fetcher" sh -c 'git init -q -b main /tmp/src && mkdir /tmp/src/inventory'
docker exec -i "$fetcher" sh -c 'cat > /tmp/src/inventory/hosts.yml' <<'YAML'
all:
  hosts:
    localhost:
      ansible_connection: local
      ansible_python_interpreter: /app/.venv/bin/python
YAML
docker exec -i "$fetcher" sh -c 'cat > /tmp/src/hostile.yml' <<'YAML'
- hosts: localhost
  gather_facts: false
  tasks:
    - name: Reach for what the app holds
      ansible.builtin.shell: |
        r() { if sh -c "$1" >/dev/null 2>&1; then echo open; else echo closed; fi; }
        echo "KEY=$(r 'cat /run/secrets/ansible-gui-key')"
        echo "DB=$(r 'test -e /data/ansible-gui.db || test -e /data/secret.key')"
        echo "ENV=$(r 'cat /proc/[0-9]*/environ 2>/dev/null | tr "\000" "\n" | grep -q -e ANSIBLE_GUI_SECRET_KEY -e ANSIBLE_GUI_PUBLIC_URL')"
        echo "REPOS=$(r 'touch /repos/hostile || touch "$PWD/hostile"')"
        echo "SOCKET=$(r 'test -e /run/fetcher/fetcher.sock')"
        w=$(grep -lx ansible-gui /proc/[0-9]*/comm 2>/dev/null | head -1)
        if [ -z "$w" ]; then echo "WORKER=missing"
        elif cat "$(dirname "$w")/environ" >/dev/null 2>&1; then echo "WORKER=open"; else echo "WORKER=closed"; fi
      register: reach
    - ansible.builtin.debug:
        msg: "{{ reach.stdout_lines | join(' ') }}"
    - name: Plant a certificate authority for every host where the run's known hosts are
      ansible.builtin.shell: |
        ssh-keygen -q -t ed25519 -N '' -f /tmp/planted-ca
        echo "@cert-authority * $(cut -d' ' -f1,2 /tmp/planted-ca.pub)" >> "$(dirname "$SSH_AUTH_SOCK")/known_hosts"
    - name: Stay in the runner for the next job
      ansible.builtin.shell: |
        setsid sleep 86399 >/dev/null 2>&1 </dev/null &
        mkdir -p "$ANSIBLE_HOME/plugins/vars" && echo 'raise SystemExit("planted")' > "$ANSIBLE_HOME/plugins/vars/agui_stayed.py"
        echo '[defaults]' > "$HOME/.ansible.cfg"
        /app/.venv/bin/python - <<'PY'
        import os
        made = 0
        for root, dirs, _ in os.walk("/"):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in ("/proc", "/sys")]
            try:
                open(os.path.join(root, "agui-stayed"), "w").close()
                made += 1
            except OSError:
                pass
        print(f"STAYED={made}")
        PY
        mkdir -p /tmp/agui-stayed-closed/inside && chmod 0 /tmp/agui-stayed-closed/inside /tmp/agui-stayed-closed
      register: stay
    - ansible.builtin.debug:
        msg: "{{ stay.stdout }}"
YAML
docker exec "$fetcher" sh -c 'cd /tmp/src && git add -A && git -c user.name=s -c user.email=s@s commit -qm hostile'
# post PATH FORM: the status and Location of a POST from this app's own pages
post() { docker exec "$name" python -c "
import sys, urllib.request, urllib.error, urllib.parse
class Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a): return None
req = urllib.request.Request('http://127.0.0.1:8080$1', data=sys.argv[1].encode(), headers={'Host': '127.0.0.1:8080'})
try: r = urllib.request.build_opener(Stay).open(req, timeout=120); print(r.status, r.headers.get('Location', ''))
except urllib.error.HTTPError as e: print(e.code, e.headers.get('Location', ''))" "$2"; }
# pulled: wait for the pulls the app started. Adding a repository and Pull now answer at once and pull in the
# background; the Repositories page asks this address to learn when they are done.
pulled() { docker exec "$name" python -c "
import time, urllib.request
for _ in range(240):
    req = urllib.request.Request('http://127.0.0.1:8080/repos/pulling?ids=', headers={'Host': '127.0.0.1:8080'})
    if 'HX-Refresh' not in urllib.request.urlopen(req, timeout=30).headers: break
    time.sleep(0.5)
else: raise SystemExit('still pulling after two minutes')"; }
out=$(post /repos "name=hosts&role=inventory&url=file:///tmp/src&branch=main&paths=inventory/hosts.yml")
[ "${out%% *}" = 303 ] || fail "split: adding the inventory repository: $out"
out=$(post /repos "name=lab&role=playbooks&url=file:///tmp/src&branch=main")
[ "${out%% *}" = 303 ] || fail "split: adding the playbook repository: $out"
pulled || fail "split: the first pulls didn't finish"
docker exec "$name" sh -c 'ls /repos/2/commits/' | grep -E '^[0-9a-f]{40}$' >/dev/null \
  || fail "split: the pull wrote no commit directory the app can see"
out=$(post /runs "playbook=2:hostile.yml&target=localhost&mode=apply&verbosity=0&expected=localhost")
run=${out##*/runs/}
[ "${out%% *}" = 303 ] && [ -n "$run" ] || fail "split: starting the run: $out"
log=/data/runs/$run/output.log
for _ in $(seq 120); do docker exec "$name" grep -q "PLAY RECAP" "$log" 2>/dev/null && break; sleep 1; done
docker exec "$name" grep -q "PLAY RECAP" "$log" || fail "split: the run never finished: $(docker exec "$name" cat "$log" 2>&1 | tail -5)"
reach=$(docker exec "$name" grep -o 'KEY=[a-z]* DB=[a-z]* ENV=[a-z]* REPOS=[a-z]* SOCKET=[a-z]* WORKER=[a-z]*' "$log" | head -1)
echo "  hostile playbook: $reach"
[ "$reach" = "KEY=closed DB=closed ENV=closed REPOS=closed SOCKET=closed WORKER=closed" ] \
  || fail "split: the runner reached what it shouldn't: $reach"
# The playbook also left a process running, a plugin and an ansible.cfg that Ansible would load for every later
# job, and a file in every directory it could write. The runner clears all of that out once the run is over,
# without restarting. Looked for as root with capabilities: the cleaning ends anything else that looks, and
# the runner's home is closed to others.
docker exec "$name" grep -E 'STAYED=[1-9]' "$log" >/dev/null \
  || fail "split: the playbook found nowhere to leave a file, so the next check would show nothing"
inside() { docker exec -u 0 --privileged "$runner" "$@"; }
for _ in $(seq 20); do
  stayed=$(inside sh -c 'ps -o user,comm | awk "\$1 == \"runner\" && \$2 != \"tini\" && \$2 != \"ansible-gui\""
    find / -path /proc -prune -o \( -name "agui[-_]stayed*" -o -name .ansible.cfg \) -print; ls -A /tmp')
  [ -z "$stayed" ] && break
  sleep 1
done
[ -z "$stayed" ] || fail "split: what the playbook left was still in the runner after its run: $stayed"
[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$runner")" = "true 0" ] \
  || fail "split: the runner stopped to get clean"
# And it still takes jobs: the same playbook again, which fails where its first run's files are still there.
out=$(post /runs "playbook=2:hostile.yml&target=localhost&mode=apply&verbosity=0&expected=localhost")
again=${out##*/runs/}
[ "${out%% *}" = 303 ] && [ -n "$again" ] && [ "$again" != "$run" ] || fail "split: starting a second run: $out"
for _ in $(seq 120); do docker exec "$name" grep -q "PLAY RECAP" "/data/runs/$again/output.log" 2>/dev/null && break; sleep 1; done
docker exec "$name" grep "failed=0" "/data/runs/$again/output.log" >/dev/null \
  || fail "split: a run after the cleaning failed: $(docker exec "$name" cat "/data/runs/$again/output.log" 2>&1 | tail -8)"
docker exec "$name" sh -c 'cat /data/ssh/known_hosts 2>/dev/null' | grep cert-authority >/dev/null \
  && fail "split: the playbook's certificate authority was kept in the app's known hosts"
# Each socket takes jobs from the app's uid only: a playbook in the runner can't send one of its own.
# The second run has just ended and the runner is cleaning up after it, which kills every other process in
# its container, this one included: ask until one gets its answer.
for _ in $(seq 20); do
  said=$(docker exec "$runner" python -c "
import socket, json; s = socket.socket(socket.AF_UNIX); s.connect('/run/runner/runner.sock')
s.sendall(json.dumps({'kind': 'cli', 'job': {}}).encode() + b'\n'); print(s.makefile().readline())" 2>/dev/null) || true
  [ -n "$said" ] && break
  sleep 1
done
grep "takes jobs from uid 10001 only, not 10003" <<<"$said" >/dev/null \
  || fail "split: the runner's own uid was let in: ${said:-no answer from its socket}"

echo "- the worker cleans between jobs only where every process is its own"
out=$(docker run --rm --user 0 -e ANSIBLE_GUI_REPOS_DIR=/repos "$image" ansible-gui worker --socket /tmp/w.sock \
  --allow-uid 10001 --clean-between-jobs /tmp 2>&1) && fail "a worker started as root agreed to clean between jobs"
grep "container of its own" <<<"$out" >/dev/null || fail "unexpected refusal: $out"

echo "- runs split: a repository can't have the fetcher write anywhere but its own commit and collections"
# Its ansible.cfg names places in the repositories volume, and its requirements file fails to install, which
# is when ansible-galaxy logs the most. The fetcher itself can't write a commit directory that is finished.
docker exec -i "$fetcher" sh -c 'cat > /tmp/src/ansible.cfg' <<'INI'
[defaults]
log_path = /repos/planted.log
home = /repos/planted-home
local_tmp = /repos/planted-tmp

[galaxy]
cache_dir = /repos/planted-cache
INI
docker exec -i "$fetcher" sh -c 'cat > /tmp/src/requirements.yml' <<'YAML'
collections:
  - name: "not a collection"
YAML
docker exec "$fetcher" sh -c 'cd /tmp/src && git add -A && git -c user.name=s -c user.email=s@s commit -qm config'
post /repos/2/pull "" >/dev/null
pulled || fail "split: the pull didn't finish"
docker exec "$name" python -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:8080/repos', headers={'Host': '127.0.0.1:8080'})
print(urllib.request.urlopen(req, timeout=30).read().decode())" | grep "collections failed" >/dev/null \
  || fail "split: the pull didn't get as far as installing collections"
docker exec "$fetcher" sh -c 'ls -A /repos' | grep planted >/dev/null \
  && fail "split: the repository's ansible.cfg chose where the fetcher wrote: $(docker exec "$fetcher" ls -A /repos)"
docker exec "$fetcher" sh -c 'for d in /repos/*/commits/*/; do touch "$d/planted" 2>/dev/null && exit 0; done; exit 1' \
  && fail "split: the fetcher can still write into a finished commit directory"

echo "- runs split: a git token reaches git in the fetcher, whose temp directory can't run a file"
# /tmp there is a tmpfs, which Docker mounts noexec: a GIT_ASKPASS script written into it can't run, and every
# HTTPS pull with a token failed. `git credential fill` asks for the sign-in the way a server's 401 does.
docker exec "$fetcher" sh -c 'grep " /tmp " /proc/mounts' | grep noexec >/dev/null \
  || fail "split: the fetcher's /tmp isn't noexec any more: this check no longer tests what production runs"
docker exec -w /tmp "$fetcher" python -c '
import subprocess, sys
from ansible_gui.repos import git
env = {**git._base_env(), **git.token_env("https://git.example.test/pb.git", ("smoke", "smoke-token"))}
out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=git.example.test\n\n", env=env,
                     capture_output=True, text=True)
sys.exit(0 if "password=smoke-token" in out.stdout else out.stderr or "no password")' \
  || fail "split: a git token doesn't reach git in the fetcher"

echo "smoke test passed"
