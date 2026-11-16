#!/usr/bin/env bash
# Starts the image and checks what a green build can't: it refuses to run open, it starts,
# it runs as the unprivileged user with private data, and the tools it shells out to are there.
#   deploy/smoke-test.sh ansible-gui:test
set -euo pipefail
image=${1:?usage: smoke-test.sh IMAGE}
name=agui-smoke-$$
fail() { echo "FAIL: $*" >&2; docker logs "$name" 2>&1 | tail -20 >&2 || true; exit 1; }
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

echo "- refuses to listen without sign-in"
if out=$(docker run --rm "$image" 2>&1); then fail "started with no sign-in configured"; fi
grep -q "Refusing to listen on 0.0.0.0 without sign-in" <<<"$out" || fail "unexpected refusal: $out"

echo "- refuses OIDC without allowed groups"
if out=$(docker run --rm -e ANSIBLE_GUI_OIDC_ISSUER=https://id.example.test -e ANSIBLE_GUI_OIDC_CLIENT_ID=x \
         -e ANSIBLE_GUI_OIDC_CLIENT_SECRET=y "$image" 2>&1); then fail "started with no allowed groups"; fi
grep -q "ANSIBLE_GUI_OIDC_GROUPS" <<<"$out" || fail "unexpected refusal: $out"

echo "- starts and answers"
docker run -d --name "$name" -e ANSIBLE_GUI_INSECURE_NO_AUTH=1 \
  -e ANSIBLE_GUI_SECRET_KEY=smoke-test-key-smoke-test-key-0123456789 "$image" >/dev/null
get() { docker exec "$name" python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080$1', timeout=4); sys.stdout.write(str(r.status))"; }
for _ in $(seq 60); do [ "$(get /healthz 2>/dev/null)" = 200 ] && break; sleep 1; done
[ "$(get /healthz)" = 200 ] || fail "/healthz never answered"
for page in / /inventory /runs /runs/new /schedules /repos /credentials; do
  [ "$(get "$page")" = 200 ] || fail "$page didn't return 200"
done

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
docker exec "$name" python -c "import ansible" 2>/dev/null && echo "  (ansible-core is importable in the venv, as expected; the app never imports it)"

echo "smoke test passed"
