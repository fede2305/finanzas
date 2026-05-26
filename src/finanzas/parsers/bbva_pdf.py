"""Parser de resúmenes PDF de BBVA Argentina (Visa Signature/Platinum).

Layout BBVA:
  FECHA       DESCRIPCIÓN                NRO. CUPÓN   PESOS        DÓLARES
  06-Jun-25   MERPAGO*BOXESPREMIUM C.11/12   117381   271.592,75
  05-Abr-26   CURSOR, AI POWER in1TIu3WBUSD 20,00     740743                  20,00

Secciones del DETALLE:
  - "Sus pagos y ajustes realizados": pagos, créditos. Sin cupón.
  - "Consumos <NOMBRE>": consumos del titular. Con cupón 6 dígitos.
  - "Impuestos, cargos e intereses": cargos e impuestos. Sin cupón (ARS).

Fechas con mes en español (Ene-Feb-Mar-...-Dic). Cuotas formato "C.NN/NN".

BBVA no muestra last4 por tx — usamos las últimas 4 de "cuenta XXXXXXXXXX".
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

from finanzas.models import ParsedStatement, ParsedTransaction
from finanzas.parsers.base import file_sha256, parse_amount_ars

MESES_ABBR = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
}

DATE_RE = re.compile(r"(?P<dd>\d{2})-(?P<mon>[A-Za-z]{3})-(?P<yy>\d{2})")
CUENTA_RE = re.compile(r"cuenta\s+(\d{8,})", re.IGNORECASE)
TOTAL_CONSUMOS_RE = re.compile(
    r"^TOTAL\s+CONSUMOS\s+DE\s+(?P<holder>.+?)\s+"
    r"(?P<total_ars>-?[\d.,]+)(?:\s+(?P<total_usd>-?[\d.,]+))?\s*$"
)
SALDO_ACTUAL_RE = re.compile(r"^SALDO\s+ACTUAL\b", re.IGNORECASE)
INSTALLMENT_RE = re.compile(r"\bC\.(\d+)/(\d+)\b")

# Consumo: <date> <desc> <cupon 6 digits> <amount>. Desc puede tener "USD <qty>" para USD.
CONSUMO_RE = re.compile(
    r"^(?P<dd>\d{2})-(?P<mon>[A-Za-z]{3})-(?P<yy>\d{2})\s+"
    r"(?P<desc>.+?)\s+(?P<cupon>\d{6})\s+"
    r"(?P<amount>-?[\d.,]+)\s*$"
)
# Pago / impuesto: <date> <desc> <amount> (sin cupón). Toma el último número.
PAGO_RE = re.compile(
    r"^(?P<dd>\d{2})-(?P<mon>[A-Za-z]{3})-(?P<yy>\d{2})\s+"
    r"(?P<desc>.+?)\s+(?P<amount>-?[\d.,]+)\s*$"
)

# USD trailing en la descripción del consumo: "...USD 20,00"
USD_DESC_TAIL_RE = re.compile(r"USD\s+[\d.,]+\s*$")


def parse(path: Path) -> ParsedStatement:
    p = Path(path)
    with pdfplumber.open(p) as pdf:
        lines: list[str] = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw in text.split("\n"):
                if raw.strip():
                    lines.append(raw.rstrip())

    period_end, period_start, due_date = _extract_dates(lines)
    card_last4 = _extract_card_last4(lines)
    holder_default = _extract_holder(lines)

    txs: list[ParsedTransaction] = []
    total_ars = 0.0
    total_usd = 0.0
    section: str | None = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("Sus pagos y ajustes realizados"):
            section = "pagos"
            continue
        if stripped.startswith("Consumos "):
            section = "consumos"
            continue
        if stripped.startswith("Impuestos, cargos e intereses"):
            section = "impuestos"
            continue
        if stripped.startswith("Legales y avisos"):
            section = None
            continue

        m_total = TOTAL_CONSUMOS_RE.match(stripped)
        if m_total:
            if section == "consumos":
                try:
                    total_ars += parse_amount_ars(m_total.group("total_ars"))
                    if m_total.group("total_usd"):
                        total_usd += parse_amount_ars(m_total.group("total_usd"))
                except Exception:
                    pass
                section = None
            continue

        if SALDO_ACTUAL_RE.match(stripped):
            section = None
            continue

        if section is None:
            continue
        if stripped.startswith("FECHA "):
            continue

        if section == "consumos":
            tx = _parse_consumo(stripped, card_last4, holder_default)
        else:
            tx = _parse_pago(stripped, card_last4, holder_default)
        if tx is not None:
            txs.append(tx)

    if period_start is None and period_end is not None:
        period_start = period_end - timedelta(days=30)

    return ParsedStatement(
        bank="bbva",
        period_start=period_start or date(1970, 1, 1),
        period_end=period_end or date(1970, 1, 1),
        due_date=due_date,
        transactions=txs,
        source_filename=p.name,
        file_sha256=file_sha256(p),
        raw_total_ars=total_ars,
        raw_total_usd=total_usd,
    )


def _extract_card_last4(lines: list[str]) -> str:
    for line in lines:
        m = CUENTA_RE.search(line)
        if m:
            return m.group(1)[-4:]
    return "0000"


def _extract_holder(lines: list[str]) -> str | None:
    for line in lines:
        m = TOTAL_CONSUMOS_RE.match(line.strip())
        if m:
            return m.group("holder").strip()
    return None


def _extract_dates(lines: list[str]) -> tuple[date | None, date | None, date | None]:
    """BBVA renderiza labels y fechas como tabla:
      'CIERRE ACTUAL  VENCIMIENTO ACTUAL  SALDO ACTUAL $ ...'
      '30-Abr-26  08-May-26  2.378.275,97 ...'
    Y aparte:
      'CIERRE ANTERIOR  VENCIMIENTO ANTERIOR  PRÓXIMO CIERRE  PRÓXIMO VENCIMIENTO'
      'Otros períodos'
      '26-Mar-26  09-Abr-26  28-May-26  05-Jun-26'
    """
    period_end: date | None = None
    period_start: date | None = None
    due_date: date | None = None
    for i, line in enumerate(lines):
        low = line.lower()
        if period_end is None and "cierre actual" in low and "vencimiento actual" in low:
            dates = _find_dates_near(lines, i + 1)
            if dates:
                period_end = dates[0]
                if len(dates) >= 2:
                    due_date = dates[1]
        if period_start is None and "cierre anterior" in low:
            dates = _find_dates_near(lines, i + 1)
            if dates:
                period_start = dates[0]
        if period_end and period_start and due_date:
            break
    return period_end, period_start, due_date


def _find_dates_near(lines: list[str], start: int) -> list[date]:
    """Devuelve todas las dd-Mmm-yy de las próximas 3 líneas (primera con matches)."""
    for j in range(start, min(start + 3, len(lines))):
        out: list[date] = []
        for m in DATE_RE.finditer(lines[j]):
            d = _build_date(m.group("dd"), m.group("mon"), m.group("yy"))
            if d is not None:
                out.append(d)
        if out:
            return out
    return []


def _parse_consumo(line: str, card_last4: str, holder: str | None) -> ParsedTransaction | None:
    m = CONSUMO_RE.match(line)
    if not m:
        return None
    posted = _build_date(m.group("dd"), m.group("mon"), m.group("yy"))
    if posted is None:
        return None

    desc_full = m.group("desc").strip()
    amount = parse_amount_ars(m.group("amount"))
    if amount == 0.0:
        return None

    currency = "ARS"
    m_usd = USD_DESC_TAIL_RE.search(desc_full)
    if m_usd:
        currency = "USD"
        desc_full = desc_full[: m_usd.start()].rstrip()

    inst_cur, inst_total = _parse_installment(desc_full)
    desc_clean = INSTALLMENT_RE.sub("", desc_full).strip()
    desc_clean = re.sub(r"\s+", " ", desc_clean)

    return ParsedTransaction(
        posted_at=posted,
        description_raw=desc_clean,
        comprobante=m.group("cupon"),
        amount=amount,
        currency=currency,
        card_last4=card_last4,
        holder_name=holder,
        installment_current=inst_cur,
        installment_total=inst_total,
    )


def _parse_pago(line: str, card_last4: str, holder: str | None) -> ParsedTransaction | None:
    m = PAGO_RE.match(line)
    if not m:
        return None
    posted = _build_date(m.group("dd"), m.group("mon"), m.group("yy"))
    if posted is None:
        return None
    desc = m.group("desc").strip()
    amount = parse_amount_ars(m.group("amount"))
    if amount == 0.0:
        return None
    currency = "USD" if re.search(r"\bUSD\b", desc) else "ARS"
    return ParsedTransaction(
        posted_at=posted,
        description_raw=desc,
        comprobante=None,
        amount=amount,
        currency=currency,
        card_last4=card_last4,
        holder_name=holder,
    )


def _build_date(dd: str, mon: str, yy: str) -> date | None:
    mes = MESES_ABBR.get(mon.lower())
    if not mes:
        return None
    try:
        return date(2000 + int(yy), mes, int(dd))
    except ValueError:
        return None


def _parse_installment(desc: str) -> tuple[int | None, int | None]:
    m = INSTALLMENT_RE.search(desc)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))
