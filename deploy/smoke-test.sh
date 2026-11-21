#!/usr/bin/env bash
# Starts the image and checks what a green build can't: it refuses to run open, it starts,
# it runs as the unprivileged user with private data, and the tools it shells out to are there.
#   deploy/smoke-test.sh ansible-gui:test
set -euo pipefail
image=${1:?usage: smoke-test.sh IMAGE}
name=agui-smoke-$$
fail() { echo "FAIL: $*" >&2; docker logs "$name" 2>&1 | tail -20 >&2 || true; exit 1; }
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
# The hardening deploy/docker-compose.yml applies; the last check runs the image under it.
HARDENED=(--read-only --tmpfs /tmp:size=512m --tmpfs /home/app:size=64m,uid=10001,gid=10001,mode=0700
          --cap-drop ALL --security-opt no-new-privileges:true)

echo "- refuses to listen without sign-in"
if out=$(docker run --rm "$image" 2>&1); then fail "started with no sign-in configured"; fi
grep -q "Refusing to listen on 0.0.0.0 without sign-in" <<<"$out" || fail "unexpected refusal: $out"

echo "- refuses OIDC without allowed groups"
if out=$(docker run --rm -e ANSIBLE_GUI_OIDC_ISSUER=https://id.example.test -e ANSIBLE_GUI_OIDC_CLIENT_ID=x \
         -e ANSIBLE_GUI_OIDC_CLIENT_SECRET=y "$image" 2>&1); then fail "started with no allowed groups"; fi
grep -q "ANSIBLE_GUI_OIDC_GROUPS" <<<"$out" || fail "unexpected refusal: $out"

echo "- refuses to listen beyond loopback without the name people use"
if out=$(docker run --rm -e ANSIBLE_GUI_INSECURE_NO_AUTH=1 "$image" 2>&1); then fail "started with no public URL"; fi
grep -q "Refusing to listen on 0.0.0.0 without ANSIBLE_GUI_PUBLIC_URL" <<<"$out" || fail "unexpected refusal: $out"

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
for _ in $(seq 60); do [ "$(get /healthz 2>/dev/null)" = 200 ] && break; sleep 1; done
[ "$(get /healthz)" = 200 ] || fail "/healthz never answered"
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
docker exec "$name" ansible-playbook --version | head -1 | grep -q "core 2.21.4" || fail "ansible-core isn't 2.21.4"
docker exec "$name" ansible-galaxy --version >/dev/null || fail "ansible-galaxy missing"
docker exec "$name" git --version >/dev/null || fail "git missing"
docker exec "$name" ssh -V >/dev/null 2>&1 || fail "ssh missing"
for tool in ssh-keygen ssh-agent ssh-add; do
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

echo "- runs hardened: read-only root, no capabilities, no new privileges"
docker rm -f "$name" >/dev/null
docker run -d --name "$name" "${HARDENED[@]}" "${RUN[@]}" "$image" >/dev/null
for _ in $(seq 60); do [ "$(get /healthz 2>/dev/null)" = 200 ] && break; sleep 1; done
[ "$(get /credentials)" = 200 ] || fail "hardened: /credentials didn't return 200"
docker exec "$name" grep -q "^CapEff:[[:space:]]*0000000000000000" /proc/1/status || fail "hardened: capabilities left"
docker exec "$name" ansible localhost -c local -m setup -a filter=ansible_distribution \
  -e ansible_python_interpreter=/app/.venv/bin/python | grep '"Alpine"' >/dev/null || fail "hardened: a local Ansible task failed"
docker exec "$name" ansible localhost -c local -m ansible.builtin.shell \
  -a 'set -o pipefail && echo "bash $BASH_VERSION" | cat executable=/bin/bash' \
  -e ansible_python_interpreter=/app/.venv/bin/python | grep '^bash [0-9]' >/dev/null || fail "hardened: a local bash task failed"
docker exec "$name" sh -c 'eval "$(ssh-agent -s -a /tmp/smoke-agent.sock)" >/dev/null && ssh-agent -k >/dev/null' \
  || fail "hardened: ssh-agent didn't start"

echo "smoke test passed"
