# ansible-gui

Web UI for homelab Ansible: resolved inventory with per-variable sources, runs, schedules.
Plan: https://claude.ai/artifact/Vfe2DZLBXVtAu5qaETDUFD

- Python 3.11+, FastAPI, Jinja templates, htmx (vendored in `src/ansible_gui/static`), plain CSS, no build step.
- Never `import ansible`: ansible-core is GPLv3. Call the CLIs (`ansible-inventory`, `ansible-playbook`) as subprocesses.
- Tests: `uv run pytest`. The fixture inventory in `tests/fixtures/homelab` exercises every precedence layer.

## UI work
Read DESIGN.md before creating or changing any UI, and take every colour, size, spacing and radius from its tokens.
For a new screen or component, consult the UX Engine skills first (ux-intent-discovery, then the ones it names). To fix an existing screen, ux-auditor.
