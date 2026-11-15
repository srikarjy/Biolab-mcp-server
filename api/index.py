"""Vercel Python Function entrypoint for the Biolab ASGI application."""

from app import app as biolab_app


async def vercel_app(scope, receive, send):
    """Strip Vercel's `/api` function prefix before dispatching to Biolab."""
    if scope.get("type") == "http":
        scope = dict(scope)
        path = scope.get("path", "")
        if path == "/api":
            scope["path"] = "/"
        elif path.startswith("/api/"):
            scope["path"] = path[4:]
        scope["root_path"] = ""
    await biolab_app(scope, receive, send)


app = vercel_app

__all__ = ["app"]
