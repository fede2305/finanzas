"""Supabase Storage — guarda los extractos PDF/XLSX subidos por usuario."""

from __future__ import annotations

import os

from supabase import Client, create_client

_client: Client | None = None
BUCKET = "statements"


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
    return _client


def upload_statement(user_id: str, filename: str, content: bytes) -> str:
    """Sube el archivo a Supabase Storage. Devuelve el path en el bucket."""
    client = _get_client()
    path = f"{user_id}/{filename}"
    client.storage.from_(BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )
    return path
