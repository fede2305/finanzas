"""Entrypoint: `uv run finanzas` o `python -m finanzas`."""

from __future__ import annotations

import sys
import webbrowser

import uvicorn

from finanzas.db import init_db


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    open_browser = "--no-browser" not in sys.argv

    # Inicializar DB (idempotente)
    init_db()

    print(f"\n🟢 Finanzas arrancando en http://{host}:{port}\n")
    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{port}")
        except Exception:
            pass

    uvicorn.run("finanzas.app:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
