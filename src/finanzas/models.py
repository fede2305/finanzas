"""Dataclasses compartidas entre parsers, db y rutas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass
class ParsedTransaction:
    """Una transacción dentro de un resumen parseado, antes de tocar la DB."""

    posted_at: date
    description_raw: str
    comprobante: str | None
    amount: float
    currency: Literal["ARS", "USD"]
    card_last4: str
    holder_name: str | None = None
    installment_current: int | None = None
    installment_total: int | None = None
    notes: str | None = None


@dataclass
class ParsedStatement:
    """Resultado del parser para un archivo de resumen."""

    bank: Literal["santander_rio", "galicia", "bbva"]
    period_start: date
    period_end: date
    due_date: date | None
    transactions: list[ParsedTransaction] = field(default_factory=list)
    source_filename: str = ""
    file_sha256: str = ""
    raw_total_ars: float = 0.0
    raw_total_usd: float = 0.0
