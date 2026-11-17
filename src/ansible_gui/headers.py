"""Response headers that keep the pages from being framed, sniffed, cached or extended with foreign scripts."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

# Scripts and styles come only from /static (htmx, app.js, app.css) plus the Google Fonts stylesheet.
# style-src-attr allows the style="--level: N" indentation in the group tree; it can't run script.
CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "style-src-attr 'unsafe-inline'",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

COMMON = [
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),  # frame-ancestors, for browsers that predate it
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
]


class SecurityHeaders:
    """Pure ASGI, so streamed responses (a run's log.txt) pass through untouched."""

    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.extra = [("Strict-Transport-Security", "max-age=31536000")] if hsts else []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        static = scope["path"].startswith("/static/")

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in COMMON + self.extra:
                    if name not in headers:
                        headers[name] = value
                # Pages show inventory values and run logs: keep them out of the browser cache and the
                # back-button cache after signing out.
                if not static and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, with_headers)
