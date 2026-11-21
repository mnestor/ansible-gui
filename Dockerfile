# ansible-gui: the web UI plus ansible-core, git and ssh, running as an unprivileged user.
# ansible-core is GPLv3 and is installed here as a separate program that ansible-gui
# only runs as a subprocess; it is never imported.

# Base images by digest, so a rebuild gets exactly what was tested; Dependabot proposes updates.
# Alpine, with its release in the tag: a digest update stays on 3.24, and moving to the next release
# (a new OpenSSH, OpenSSL and git at once) is a change someone makes on purpose.
FROM python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
# Everything from uv.lock, hashes checked, ansible-core included (the "ansible" group in pyproject.toml).
# Every compiled package in it has a musl wheel, so nothing is built here.
RUN uv sync --frozen --no-dev --group ansible --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --group ansible

FROM python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
LABEL org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later"
ARG UID=10001
# apk upgrade: the fixes Alpine has published since the base image was built.
# bash and GNU tar are for playbooks, not for ansible-gui: tasks that run on the controller
# (delegate_to: localhost) with `executable: /bin/bash`, and unarchive, which wants GNU tar where BusyBox's is.
RUN apk upgrade --no-cache \
 && apk add --no-cache git openssh-client tini ca-certificates bash tar \
 && adduser -D -u "$UID" -h /home/app -s /sbin/nologin app \
 && mkdir /data && chown app:app /data && chmod 700 /data
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_GUI_HOST=0.0.0.0 \
    ANSIBLE_GUI_PORT=8080 \
    ANSIBLE_GUI_DATA_DIR=/data \
    ANSIBLE_HOME=/data/ansible \
    ANSIBLE_LOCAL_TEMP=/tmp/ansible-local
USER app
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status != 200)"]
ENTRYPOINT ["tini", "--"]
CMD ["ansible-gui"]
