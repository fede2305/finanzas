"""Google OAuth + session helpers."""

from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def require_user(request: Request) -> dict | None:
    """Devuelve el dict del usuario en sesión, o None si no está autenticado."""
    return request.session.get("user")


def callback_url(request: Request) -> str:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    if app_url:
        return app_url + "/auth/callback"
    # fallback para desarrollo local
    base = str(request.base_url).rstrip("/")
    return base + "/auth/callback"
