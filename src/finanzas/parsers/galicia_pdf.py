"""Parser de resúmenes PDF de Galicia.

Galicia tiene un layout tabular más limpio que Santander:
  FECHA       REFERENCIA            CUOTA   COMPROBANTE   PESOS   DÓLARES
  02-11-25 * MERPAGO*IVMACOARG      06/06   903800        7.499,83

Las transacciones aparecen agrupadas dentro de "DETALLE DEL CONSUMO".
Los split por tarjeta vienen como "TARJETA XXXX Total Consumos de NOMBRE".
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

from finanzas.models import ParsedStatement, ParsedTransaction
from finanzas.parsers.base import file_sha256, parse_amount_ars

# Fecha tipo "08-04-26"
DATE_RE = re.compile(r"(?P<dd>\d{2})-(?P<mm>\d{2})-(?P<yy>\d{2})")

# Tail regex: matchea (opcional) "NN/NN", luego comprobante (6 digits), monto, (opcional USD).
# Buscamos en el END de la línea — el head (fecha, type, desc) está antes.
# Esto evita que un código embebido en la descripción (ej "AUTOPISTAS 960004053513801")
# sea confundido con el comprobante.
TAIL_RE = re.compile(
    r"\s+(?:(?P<cur>\d{1,2})/(?P<tot>\d{1,2})\s+)?"
    r"(?P<comp>\d{6})\s+"
    r"(?P<amount>-?[\d.,]+-?)"
    r"(?:\s+(?P<usd>-?[\d.,]+-?))?\s*$"
)
HEAD_DATE_RE = re.compile(
    r"^(?P<dd>\d{2})-(?P<mm>\d{2})-(?P<yy>\d{2})\s+"
    r"(?:(?P<type>[*A-Z])\s+)?"
    r"(?P<desc>.+?)\s*$"
)
TARJETA_TOTAL_RE = re.compile(
    r"^TARJETA\s+(?P<card>\d{4})\s+Total\s+Consumos\s+de\s+(?P<holder>.+?)\s+"
    r"(?P<total_ars>-?[\d.,]+)\s+(?P<total_usd>-?[\d.,]+)\s*$"
)
TOTAL_PAGAR_RE = re.compile(
    r"^TOTAL\s+A\s+PAGAR\s+(?P<ars>-?[\d.,]+)\s+(?P<usd>-?[\d.,]+)?\s*$"
)
CICLO_FECHAS_RE = re.compile(r"(\d{2}-\d{2}-\d{2})")

# Cabeceras de columnas / lineas de cierre que no son txs
SKIP_PREFIXES = (
    "Resumen N°",
    "Tarjeta Crédito",
    "FEDERICO",
    "Consumidor",
    "CNEL",
    "N° Cuenta:",
    "Resumen de tarjeta",
    "Página",
    "PAGO MINIMO",
    "LÍMITES",
    "De compras",
    "De financiación",
    "TASAS",
    "Nominal Anual",
    "Efectiva mensual",
    "En pesos",
    "En dólares",
    "CONSOLIDADO",
    "SALDO ANTERIOR",
    "DETALLE DEL CONSUMO",
    "FECHA",
    "REFERENCIA",
    "Total a pagar",
    "Ciclo de facturación",
    "Período de consumos",
    "Cierre anterior",
    "Vencimiento anterior",
    "Cierre actual",
    "Vencimiento actual",
    "Próximo cierre",
    "Próximo vencimiento",
    "Plan V:",
    "abonando",
    "Cancelación",
    "Condiciones",
    "Cuotas a vencer",
    "Usted puede",
    "previstas en",
    "sueldo y especiales",
    "comparar los costos",
    "ingresando a",
    "http://",
    "A partir del",
    "bonificados",
    "de pases excedentes",
    "Descargarte la",
    "Conocé",
    "Por motivos de seguridad",
    "Si desea",
    "Los adelantos",
    "Comisión",
    "del adelanto",
    "ser reintegrados",
    "Cuotas CFT",
    "Cuotas",
    "1 ",
    "2 ",
    "3 ",
    "6 ",
    "9 ",
    "12 ",
    "Ejemplo,",
    "El monto de IVA",
    "La tasa de Interés",
    "Costo Financiero",
    "Tasa Nominal",
    "Tasa Efectiva",
    "Los intereses",
    "vencimiento del resumen",
    "La Tasa de Interés",
    "A través",
    "los intereses",
    "del Texto Ordenado",
    "los intereses",
    "convenido",
    "se calculan",
    "que permanezcan",
    "En consecuencia,",
    "equivalentes",
    "Composición",
    "consumos del",
    "el saldo",
    "financiación,",
    "correspondiente al día",
    "A partir de la",
    "tasa de interés",
    "Las comisiones",
    "Internacional",
    "Reposición",
    "por adelantos",
    "telefónica",
    "Estos precios",
    "Para la base",
    "se toman los",
    "Los consumos",
    "moneda extranjera",
    "día hábil",
    "Si hubiera pactado",
    "o utiliza",
    "o realice",
    "hábiles en un",
    "Las tasas",
    "Según lo estipulado",
    "prohibido",
    "La TNA",
    "Adelantos",
    "Centro de Atención",
    "comuníquese",
    "Si Usted es",
    "comprobante,",
    "Inclusión Fiscal",
    "*USTED DISPONE",
    "VI00000",
    "*ULTIMO",
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
    total_ars = 0.0
    total_usd = 0.0
    in_consumos = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("DETALLE DEL CONSUMO") or stripped.startswith("Detalle del consumo"):
            in_consumos = True
            continue

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
                total_usd += parse_amount_ars(m_total.group("total_usd"))
            except Exception:
                pass
            continue

        m_total_pagar = TOTAL_PAGAR_RE.match(stripped)
        if m_total_pagar:
            # cierra cualquier pending — los Galicia siempre tienen Tarjeta total antes
            continue

        if not in_consumos:
            continue
        if _should_skip(stripped):
            continue

        m_tail = TAIL_RE.search(stripped)
        if not m_tail:
            # Caso de pago/promo sin comprobante: "01-04-26 Su pago en pesos -1.107.964,62"
            m_payment = _parse_payment(stripped)
            if m_payment:
                pending.append(m_payment)
            continue

        head = stripped[: m_tail.start()]
        m_head = HEAD_DATE_RE.match(head.strip())
        if not m_head:
            continue

        tx = _build_tx(m_head, m_tail)
        if tx is not None:
            pending.append(tx)

    for tx in pending:
        tx.card_last4 = titular_card or "0000"
        tx.holder_name = titular_holder
        txs.append(tx)

    if period_start is None and period_end is not None:
        period_start = period_end - timedelta(days=30)

    return ParsedStatement(
        bank="galicia",
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
    return any(s.startswith(p) for p in SKIP_PREFIXES)


def _extract_dates(lines: list[str]) -> tuple[date | None, date | None, date | None]:
    """Galicia tiene un timeline tipo:
       26-Mar-26 06-Abr-26 30-Abr-26 08-May-26 28-May-26 05-Jun-26
       Cierre ant.  Vto ant.  Cierre act.  Vto. act.  Próx. cierre  Próx. vto.
    También aparecen en formato 26-Mar-26 (texto plano).
    """
    months_es = {
        "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
        "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    }
    re_dt = re.compile(r"(\d{2})-([A-Za-z]{3})-(\d{2})")
    fechas: list[date] = []
    for line in lines:
        for m in re_dt.finditer(line):
            mes = months_es.get(m.group(2).lower())
            if mes:
                try:
                    d = date(2000 + int(m.group(3)), mes, int(m.group(1)))
                    fechas.append(d)
                except ValueError:
                    continue
        if len(fechas) >= 4:
            break
    if len(fechas) >= 4:
        # orden esperado: cierre_ant, vto_ant, cierre_act, vto_act
        return fechas[2], fechas[0], fechas[3]
    return None, None, None


def _build_tx(m_head: re.Match, m_tail: re.Match) -> ParsedTransaction | None:
    try:
        posted = date(
            2000 + int(m_head.group("yy")),
            int(m_head.group("mm")),
            int(m_head.group("dd")),
        )
    except ValueError:
        return None
    desc = (m_head.group("desc") or "").strip()
    ars_str = m_tail.group("amount")
    usd_str = m_tail.group("usd")
    if usd_str:
        amount = parse_amount_ars(usd_str)
        currency = "USD"
    else:
        amount = parse_amount_ars(ars_str)
        currency = "ARS"
    if amount == 0.0:
        return None
    inst_cur = int(m_tail.group("cur")) if m_tail.group("cur") else None
    inst_total = int(m_tail.group("tot")) if m_tail.group("tot") else None
    return ParsedTransaction(
        posted_at=posted,
        description_raw=desc,
        comprobante=m_tail.group("comp"),
        amount=amount,
        currency=currency,
        card_last4="?",
        installment_current=inst_cur,
        installment_total=inst_total,
    )


def _parse_payment(line: str) -> ParsedTransaction | None:
    """Líneas tipo '01-04-26 Su pago en pesos -1.107.964,62' (sin type letter)."""
    m = re.match(
        r"^(?P<dd>\d{2})-(?P<mm>\d{2})-(?P<yy>\d{2})\s+"
        r"(?P<desc>[A-Za-zÁÉÍÓÚáéíóúñÑ ].+?)\s+"
        r"(?P<amount>-?[\d.,]+-?)\s*$",
        line,
    )
    if not m:
        return None
    try:
        posted = date(2000 + int(m.group("yy")), int(m.group("mm")), int(m.group("dd")))
    except ValueError:
        return None
    desc = m.group("desc").strip()
    amount = parse_amount_ars(m.group("amount"))
    if amount == 0.0:
        return None
    return ParsedTransaction(
        posted_at=posted,
        description_raw=desc,
        comprobante=None,
        amount=amount,
        currency="ARS",
        card_last4="?",
    )
