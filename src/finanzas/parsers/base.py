"""Helpers compartidos por todos los parsers."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path  # noqa: F401

# Meses en español para parseo de fechas
MESES = {
    "ene": 1, "enero": 1,
    "feb": 2, "febrero": 2,
    "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6,
    "jul": 7, "julio": 7,
    "ago": 8, "agosto": 8,
    "sep": 9, "septiembre": 9, "set": 9, "setiembre": 9,
    "oct": 10, "octubre": 10,
    "nov": 11, "noviembre": 11,
    "dic": 12, "diciembre": 12,
}


def parse_es_month(token: str) -> int | None:
    """'Abr' / 'Marzo' / 'May' → 4 / 3 / 5. Insensitive a mayúsculas y a acentos."""
    t = token.strip().lower().rstrip(".")
    return MESES.get(t)


def parse_amount_ars(text: str) -> float:
    """'2.480,00' o '1.018.471,24-' → float (negativo si tiene '-' al final)."""
    s = text.strip().replace(" ", "")
    neg = s.endswith("-") or s.startswith("-")
    s = s.strip("-")
    # Locale AR: . miles, , decimal
    s = s.replace(".", "").replace(",", ".")
    if not s:
        return 0.0
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg else val


def normalize_description(raw: str) -> str:
    """Reduce ruido de la descripción para usar como key de regla / merchant grouping.

    - lower-case
    - dropea prefijos MERPAGO*, MERCPAGO*, EPAGOS*, PVS*, MERCADOPAGO*
    - colapsa espacios
    - quita números largos al final (códigos de comprobante embebidos)
    """
    s = raw.lower().strip()
    s = re.sub(r"^(merpago|mercpago|mercadopago|epagos|pvs)\s*[*\-]\s*", "", s)
    # quita códigos largos al final tipo "100338493321001"
    s = re.sub(r"\s+\d{8,}$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def tx_hash(account_id: int | str, comprobante: str | None, amount: float, posted_at: date) -> str:
    """Hash determinístico para dedup de transacciones entre parsers (PDF vs xlsx).

    Normaliza el comprobante a solo dígitos para que '276079', '276079*' y '276079K'
    coincidan — distintos parsers extraen el comprobante con/sin el código de tipo.
    """
    comp_digits = re.sub(r"\D", "", comprobante or "")
    key = f"{account_id}|{comp_digits}|{amount:.2f}|{posted_at.isoformat()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
