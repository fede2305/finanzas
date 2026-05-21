"""Parser de resúmenes PDF de Santander Río.

Estrategia: extrae texto con pdfplumber por línea y la procesa con un state
machine que va arrastrando el (año, mes) actual.

Distingue:
  - Líneas con prefijo "[YY MES]" que setean el año/mes actual.
  - Líneas de transacción (con o sin comprobante).
  - Líneas "Tarjeta XXXX Total Consumos de NOMBRE" que cierran un bloque
    de transacciones y las asignan al `card_last4` correspondiente.

Las transacciones que quedan después del último "Total Consumos" (impuestos
del statement) se asignan a la card titular (la primera vista).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

from finanzas.models import ParsedStatement, ParsedTransaction
from finanzas.parsers.base import (
    file_sha256,
    parse_amount_ars,
    parse_es_month,
)

# Línea que arranca con "YY MES" (puede ser tx o no — solo nos da el año/mes contextual)
YEAR_MONTH_RE = re.compile(
    r"^\s*(?P<yy>\d{2})\s+(?P<month>[A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(?P<rest>.*)$"
)

# Cierre estándar — admite ausencia de espacio entre el año y "VENCIMIENTO"
CIERRE_RE = re.compile(
    r"CIERRE\s+(?P<dia>\d{1,2})\s+(?P<mes>[A-Za-z]{3,4})\s+(?P<anio>\d{2})\s*"
    r"VENCIMIENTO\s+(?P<v_dia>\d{1,2})\s+(?P<v_mes>[A-Za-z]{3,4})\s+(?P<v_anio>\d{2})",
    re.IGNORECASE,
)
CIERRE_ANT_RE = re.compile(
    r"Cierre\s+Ant\.?:\s*(?P<dia>\d{1,2})\s+(?P<mes>[A-Za-z]{3,4})\s+(?P<anio>\d{2})",
    re.IGNORECASE,
)

# "Tarjeta XXXX Total Consumos de NOMBRE 2943.455,47 * 81,56 *"
TARJETA_TOTAL_RE = re.compile(
    r"^Tarjeta\s+(?P<card>\d{4})\s+Total\s+Consumos\s+de\s+(?P<holder>.+?)\s+"
    r"(?P<total_ars>-?[\d.,]+)\s*\*?\s+"
    r"(?:(?P<total_usd>-?[\d.,]+)\s*\*?)?\s*$"
)

# Detección de USD al final de línea: "USD <qty> <amt>" (qty == amt usualmente)
USD_TAIL_RE = re.compile(
    r"USD\s+(?P<qty>-?[\d.,]+)\s+(?P<amt>-?[\d.,]+-?)\s*$",
    re.IGNORECASE,
)

# Cabeza de una línea de tx: day + opcional (comprobante [type])
HEAD_RE = re.compile(
    r"^\s*(?P<day>\d{2})\s+"
    r"(?:(?P<comp>\d{4,7})\s+(?:(?P<type>[A-Za-z*]{1,2})\s+)?)?"
    r"(?P<desc>.+)$"
)

INSTALLMENT_RE = re.compile(r"\bC\.(\d+)/(\d+)\b")

# Líneas de "ruido" cuyo prefijo permite descartarlas
SKIP_PREFIXES_EXACT = (
    "Santander Río", "RESUMEN DE CUENTA", "VISA", "Sucursal:", "Grupo:", "Cuenta:",
    "EL PRESENTE", "SALDO ACTUAL", "PAGO MINIMO", "CASA CENTRAL",
    "SUPERCLUB", "Le recordamos", "(PIN)", "no tiene clave", "IVA: CONSUMIDOR",
    "DEL FRANCO", "CNEL", "CAP.FEDERAL", "Cierre Ant", "Prox.Cierre",
    "LIMITES:", "TNA", "TEM", "PLAN V", "Abone", "SUJETO", "Ud. también",
    "teléfono", "Condiciones", "Cuotas a vencer:", "El banco",
    "NO RENOVACIÓN", "Una vez vigentes", "comunicándote",
    "La Percepción", "billetes,", "día de vencimiento", "cambio oficial",
    "Los saldos en moneda", "vencimiento actual", "de la respectiva", "en pesos al",
    "moneda extranjera", "través de medios", "día hábil inmediato",
    "Si pactaste", "Los intereses", "y sobre el importe", "No se capitalizarán",
    "Respondiendo a la RG", "documentos equivalentes", "consumidores finales",
    "cumpliendo", "del concepto", "El pago mínimo", "del período",
    "en 1 pago", "las compras", "saldo que exceda", "anterior impago",
    "online.", "La comisión", "tendrá", "Respecto de las compras",
    "que la norma", "abonando el seguro", "pendientes", "Por la presente",
    "por reposición", "$11.073", "acuerdo a las normas", "tendrá la opción",
    "A partir de octubre", "crédito,", "Para comenzar", "Airport Companion",
    "disfrutar", "Conoce más", "https://", "optar por", "vigencia",
    "las obligaciones", "elaborado", "base de la información", "comparar",
    "financieros.", "Si la operación", "importe del cupón", "conforme",
    "Si Usted es", "comprobante,", "Inclusión Fiscal", "Los intereses de",
    "Tasa", "TEM 6,879", "en pesos TNA:", "Efectiva Anual", "ADELANTO DE",
    "la fecha de operación", "y CFTEA", "y TNA 12,000", "El monto de IVA",
    "Tasa de interés", "documento o cuit", "cualquier sucursal",
    "*USTED DISPONE", "$ 7.000.000", "FINANCIACION", "SUC:",
    "Tasa Nominal Anual", "Tasa Efectiva", "CFTEA",
    "ó PLAN V EN", "3 cuotas de ", "6 cuotas de ", "9 cuotas de ",
    "12 cuotas de ", "24 cuotas de ", "Con IVA:", "Sin IVA:",
    "(1*)", "(2*)", "(3*)", "(4*)", "(5*)", "Cancelación anticipada",
    "Mayo/26", "Junio/26", "Julio/26", "Agosto/26", "Setiembre/26", "Octubre/26",
    "$219.157", "$37.184", "Si hubiera",
)


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

    txs: list[ParsedTransaction] = []
    pending: list[ParsedTransaction] = []
    titular_card: str | None = None
    titular_holder: str | None = None

    current_year: int | None = None
    current_month: int | None = None
    in_tx_section = False
    total_ars = 0.0
    total_usd = 0.0

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("SALDO ANTERIOR"):
            in_tx_section = True
            continue

        # "Tarjeta XXXX Total Consumos..." → cierra el bloque, asigna pending al card
        m_total = TARJETA_TOTAL_RE.match(stripped)
        if m_total:
            card = m_total.group("card")
            holder = m_total.group("holder").strip()
            for tx in pending:
                tx.card_last4 = card
                tx.holder_name = holder
                txs.append(tx)
            pending = []
            if titular_card is None:
                titular_card = card
                titular_holder = holder
            try:
                total_ars += parse_amount_ars(m_total.group("total_ars"))
                if m_total.group("total_usd"):
                    total_usd += parse_amount_ars(m_total.group("total_usd"))
            except Exception:
                pass
            continue

        if not in_tx_section:
            continue
        if _should_skip(stripped):
            continue

        # Año-mes prefix?
        m_ym = YEAR_MONTH_RE.match(line)
        if m_ym:
            month = parse_es_month(m_ym.group("month"))
            if month:
                current_year = 2000 + int(m_ym.group("yy"))
                current_month = month
                tail = m_ym.group("rest")
            else:
                tail = stripped
        else:
            tail = stripped

        if current_year is None or current_month is None:
            continue

        tx = _parse_tx_line(tail, current_year, current_month)
        if tx is not None:
            pending.append(tx)

    # impuestos sueltos al final → asignar a titular
    for tx in pending:
        tx.card_last4 = titular_card or "0000"
        tx.holder_name = titular_holder
        txs.append(tx)

    if period_start is None and period_end is not None:
        period_start = period_end - timedelta(days=30)

    return ParsedStatement(
        bank="santander_rio",
        period_start=period_start or date(1970, 1, 1),
        period_end=period_end or date(1970, 1, 1),
        due_date=due_date,
        transactions=txs,
        source_filename=p.name,
        file_sha256=file_sha256(p),
        raw_total_ars=total_ars,
        raw_total_usd=total_usd,
    )


def _should_skip(s: str) -> bool:
    return any(s.startswith(pref) for pref in SKIP_PREFIXES_EXACT)


def _extract_dates(lines: list[str]) -> tuple[date | None, date | None, date | None]:
    period_end = None
    period_start = None
    due_date = None
    for line in lines:
        m = CIERRE_RE.search(line)
        if m and period_end is None:
            mes = parse_es_month(m.group("mes"))
            v_mes = parse_es_month(m.group("v_mes"))
            if mes:
                period_end = date(2000 + int(m.group("anio")), mes, int(m.group("dia")))
            if v_mes:
                due_date = date(2000 + int(m.group("v_anio")), v_mes, int(m.group("v_dia")))
        m_ant = CIERRE_ANT_RE.search(line)
        if m_ant and period_start is None:
            mes = parse_es_month(m_ant.group("mes"))
            if mes:
                period_start = date(
                    2000 + int(m_ant.group("anio")), mes, int(m_ant.group("dia"))
                )
        if period_end and period_start and due_date:
            break
    return period_end, period_start, due_date


def _parse_tx_line(line: str, year: int, month: int) -> ParsedTransaction | None:
    """Convierte una línea (con o sin prefijo YY-MES ya consumido) en ParsedTransaction.

    Estrategia: detecta primero si hay USD al final (`USD <qty> <amt>$`); si sí, la
    moneda es USD y el monto está en `amt`. Si no, el último número de la línea
    es el monto en ARS. Después parsea la cabeza (day, comprobante, type, desc).
    """
    line = line.strip()

    m_usd = USD_TAIL_RE.search(line)
    if m_usd:
        currency = "USD"
        amount = parse_amount_ars(m_usd.group("amt"))
        head = line[: m_usd.start()].rstrip()
        # Si la cabeza termina con "USD" pegado a una palabra ("HBUSD"), lo desplazamos:
        head = re.sub(r"USD\s*$", "", head, flags=re.IGNORECASE).rstrip()
    else:
        m_amt = re.search(r"(?P<amt>-?[\d][\d.,]*-?)\s*$", line)
        if not m_amt:
            return None
        currency = "ARS"
        amount = parse_amount_ars(m_amt.group("amt"))
        head = line[: m_amt.start()].rstrip()

    if amount == 0.0:
        return None

    m_head = HEAD_RE.match(head)
    if not m_head:
        return None

    try:
        day = int(m_head.group("day"))
    except (TypeError, ValueError):
        return None
    comp = m_head.group("comp")
    desc = (m_head.group("desc") or "").strip()
    if not desc:
        return None

    try:
        posted = date(year, month, day)
    except ValueError:
        return None

    inst_cur, inst_total = _parse_installment(desc)
    desc_clean = INSTALLMENT_RE.sub("", desc).strip()
    desc_clean = re.sub(r"\s+", " ", desc_clean)

    return ParsedTransaction(
        posted_at=posted,
        description_raw=desc_clean,
        comprobante=comp,
        amount=amount,
        currency=currency,
        card_last4="?",
        installment_current=inst_cur,
        installment_total=inst_total,
    )


def _parse_installment(desc: str) -> tuple[int | None, int | None]:
    m = INSTALLMENT_RE.search(desc)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))
