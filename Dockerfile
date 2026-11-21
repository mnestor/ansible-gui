# ansible-gui: the web UI plus ansible-core, git and ssh, running as an unprivileged user.
# ansible-core is GPLv3 and is installed here as a separate program that ansible-gui
# only runs as a subprocess; it is never imported.
#
# One image, three users. deploy/docker-compose.yml runs it as the app (10001), which holds the secrets and
# never runs repository code; as the fetcher (10002), which pulls the repositories and installs their
# collections; and as the runner (10003), which runs Ansible from a read-only mount of what the fetcher wrote.
# Neither worker holds a secret of its own. OpenSSH refuses to run for a uid with no passwd entry, so each has one.

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
# ansible-gui itself goes into the venv as a package, not as a link back to /app/src:
# the venv is all the next stage takes, so the image holds no source tree, lock file or uv.
RUN uv sync --frozen --no-dev --group ansible --no-editable

FROM python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
LABEL org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later"
# apk upgrade: the fixes Alpine has published since the base image was built.
# bash and GNU tar are for playbooks, not for ansible-gui: tasks that run on the controller
# (delegate_to: localhost) with `executable: /bin/bash`, and unarchive, which wants GNU tar where BusyBox's is.
#
# Then the parts of the base image's Python nothing here can use:
# - pip and ensurepip: the venv is complete, and nothing is installed into a running container;
# - IDLE, tkinter and turtle (there is no Tk) and lib2to3;
# - the nis module, the only thing that needs libnsl, libtirpc and the Kerberos libraries behind them.
# The base image notes the libraries its Python links against as .python-rundeps. They are noted again, the
# same way, without the modules removed, and the old note is dropped: the libraries nothing needs go with it.
RUN apk upgrade --no-cache \
 && apk add --no-cache git openssh-client tini ca-certificates bash tar \
 && cd /usr/local/lib/python3.12 \
 && rm -rf site-packages/pip site-packages/pip-*.dist-info ensurepip idlelib tkinter turtledemo turtle.py lib2to3 \
           __pycache__/turtle.* lib-dynload/_tkinter.*.so lib-dynload/nis.*.so \
 && rm -f /usr/local/bin/pip* /usr/local/bin/idle* /usr/local/bin/2to3* \
 && find /usr/local -type f -executable -exec scanelf --needed --nobanner --format '%n#p' '{}' + \
      | tr ',' '\n' | sort -u \
      | awk 'system("[ -e /usr/local/lib/" $1 " ]") == 0 { next } { print "so:" $1 }' \
      | xargs -rt apk add --no-network --virtual .ansible-gui-python-rundeps \
 && apk del --no-network .python-rundeps \
 && adduser -D -u 10001 -h /home/app -s /sbin/nologin app \
 && adduser -D -u 10002 -h /home/fetcher -s /sbin/nologin fetcher \
 && adduser -D -u 10003 -h /home/runner -s /sbin/nologin runner \
 && mkdir /data && chown app:app /data && chmod 700 /data \
 && mkdir /repos && chown fetcher:fetcher /repos && chmod 755 /repos
COPY --from=build /app/.venv /app/.venv
COPY LICENSE /app/LICENSE
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
