"""Queries reutilizables para el dashboard y APIs."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta


def month_bounds(d: date) -> tuple[str, str]:
    start = d.replace(day=1)
    days = monthrange(start.year, start.month)[1]
    next_day = (start + timedelta(days=days))
    return start.isoformat(), next_day.isoformat()


def prev_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def add_months(d: date, n: int) -> date:
    m0 = d.month - 1 + n
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    return date(y, m, 1)


def _acc_clause(account_id: int | None) -> tuple[str, list]:
    """Devuelve (sql_extra, params) para el filtro opcional de account_id."""
    if account_id is None:
        return "", []
    return " AND t.account_id = %s", [account_id]


# ----------------- Hero / totals -----------------


def total_month(conn, user_id: str, day_of_month: date,
                currency: str = "ARS",
                account_id: int | None = None,
                use_my_share: bool = True,
                exclude_pagos: bool = True) -> float:
    start, end = month_bounds(day_of_month)
    amount_expr = "t.amount * COALESCE(t.my_share_pct, 1.0)" if use_my_share else "t.amount"
    sql = f"""SELECT COALESCE(SUM({amount_expr}), 0)
              FROM transactions t
              JOIN statements st ON st.id = t.statement_id
              LEFT JOIN categories c ON c.id = t.category_id
              WHERE st.period_end >= %s AND st.period_end < %s
                AND t.amount > 0 AND t.currency = %s
                AND t.user_id = %s"""
    params: list = [start, end, currency, user_id]
    if exclude_pagos:
        sql += " AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0)


def te_deben_month(conn, user_id: str, day_of_month: date,
                   currency: str = "ARS",
                   account_id: int | None = None) -> float:
    start, end = month_bounds(day_of_month)
    sql = """SELECT COALESCE(SUM(p.amount_owed), 0)
             FROM tx_participants p
             JOIN transactions t ON t.id = p.transaction_id
             JOIN statements st ON st.id = t.statement_id
             LEFT JOIN categories c ON c.id = t.category_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.currency = %s
               AND t.user_id = %s
               AND p.paid_back = 0
               AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"""
    params: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0)


def te_pagado_month(conn, user_id: str, day_of_month: date,
                    currency: str = "ARS",
                    account_id: int | None = None) -> float:
    start, end = month_bounds(day_of_month)
    sql = """SELECT COALESCE(SUM(p.amount_owed), 0)
             FROM tx_participants p
             JOIN transactions t ON t.id = p.transaction_id
             JOIN statements st ON st.id = t.statement_id
             LEFT JOIN categories c ON c.id = t.category_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.currency = %s
               AND t.user_id = %s
               AND p.paid_back = 1
               AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"""
    params: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0)


def participants_owed_by_person(conn, user_id: str, only_pending: bool = True) -> list[dict]:
    sql_status = "AND p.paid_back = 0" if only_pending else ""
    rows = conn.execute(
        f"""SELECT p.id, p.person_name, p.amount_owed, p.paid_back, p.paid_back_at,
                   t.id AS tx_id, t.posted_at, t.description_raw, t.amount, t.currency,
                   a.bank, a.card_last4
            FROM tx_participants p
            JOIN transactions t ON t.id = p.transaction_id
            JOIN accounts a ON a.id = t.account_id
            WHERE t.user_id = %s {sql_status}
            ORDER BY LOWER(p.person_name), t.posted_at DESC""",
        (user_id,),
    ).fetchall()
    by_person: dict[str, dict] = {}
    for r in rows:
        key = (r["person_name"] or "").strip() or "Alguien"
        if key not in by_person:
            by_person[key] = {
                "person_name": key,
                "total_owed_pending": 0.0,
                "total_owed_paid": 0.0,
                "pending_count": 0,
                "paid_count": 0,
                "txs": [],
            }
        entry = by_person[key]
        if r["paid_back"]:
            entry["total_owed_paid"] += r["amount_owed"]
            entry["paid_count"] += 1
        else:
            entry["total_owed_pending"] += r["amount_owed"]
            entry["pending_count"] += 1
        entry["txs"].append(dict(r))
    return list(by_person.values())


def avg_last_3_months(conn, user_id: str, day_of_month: date,
                      currency: str = "ARS",
                      account_id: int | None = None) -> float:
    totals = []
    for i in range(1, 4):
        m = add_months(day_of_month.replace(day=1), -i)
        t = total_month(conn, user_id, m, currency, account_id=account_id)
        if t > 0:
            totals.append(t)
    if not totals:
        return 0.0
    return sum(totals) / len(totals)


# ----------------- Distribución por categoría -----------------


def category_distribution(conn, user_id: str, day_of_month: date,
                          currency: str = "ARS",
                          account_id: int | None = None) -> list[tuple[int | None, str, str, float]]:
    start, end = month_bounds(day_of_month)
    sql = """SELECT c.id AS cid,
                    COALESCE(c.name, 'Sin categoría') AS name,
                    COALESCE(c.color, '#94a3b8') AS color,
                    SUM(t.amount * COALESCE(t.my_share_pct, 1.0)) AS total
             FROM transactions t
             JOIN statements st ON st.id = t.statement_id
             LEFT JOIN categories c ON c.id = t.category_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.amount > 0 AND t.currency = %s
               AND t.user_id = %s
               AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"""
    params: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += " GROUP BY c.id, c.name, c.color ORDER BY total DESC"
    rows = conn.execute(sql, params).fetchall()
    return [
        (r["cid"], r["name"], r["color"], float(r["total"] or 0))
        for r in rows
    ]


def transactions_in_category(conn, user_id: str, category_id: int | None,
                             day_of_month: date, currency: str = "ARS",
                             account_id: int | None = None) -> list[dict]:
    start, end = month_bounds(day_of_month)
    base_select = """SELECT t.id, t.posted_at, t.description_raw, t.amount, t.currency,
                            t.my_share_pct, t.share_with, t.notes,
                            t.installment_current, t.installment_total,
                            a.bank, a.card_last4, s.name AS subcategory"""
    base_join = """FROM transactions t
                   JOIN accounts a ON a.id = t.account_id
                   JOIN statements st ON st.id = t.statement_id
                   LEFT JOIN categories s ON s.id = t.subcategory_id"""
    base_where = """WHERE st.period_end >= %s AND st.period_end < %s
                      AND t.amount > 0 AND t.currency = %s
                      AND t.user_id = %s"""
    params: list = [start, end, currency, user_id]
    if category_id is None:
        base_where += " AND t.category_id IS NULL"
    else:
        base_where += """ AND (t.category_id = %s
                               OR t.category_id IN (SELECT id FROM categories WHERE parent_id = %s AND user_id = %s))"""
        params += [category_id, category_id, user_id]
    extra, ep = _acc_clause(account_id)
    base_where += extra
    params += ep
    sql = f"{base_select} {base_join} {base_where} ORDER BY t.amount DESC"
    rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    if items:
        ids = [it["id"] for it in items]
        parts = conn.execute(
            """SELECT id, transaction_id, person_name, amount_owed, paid_back, paid_back_at
               FROM tx_participants
               WHERE transaction_id = ANY(%s)
               ORDER BY transaction_id, sort_order, id""",
            (ids,),
        ).fetchall()
        by_tx: dict[int, list[dict]] = {}
        for p in parts:
            by_tx.setdefault(p["transaction_id"], []).append(dict(p))
        for it in items:
            it["participants"] = by_tx.get(it["id"], [])
    return items


# ----------------- Top merchants -----------------


def top_categories_compare(conn, user_id: str, day_of_month: date,
                           currency: str = "ARS",
                           account_id: int | None = None) -> list[dict]:
    start, end = month_bounds(day_of_month)
    prev_start, prev_end = month_bounds(prev_month(day_of_month))

    sql_cur = """SELECT c.id AS cid, c.name, c.color,
                        SUM(t.amount * COALESCE(t.my_share_pct, 1.0)) AS total,
                        COUNT(*) AS n
                 FROM transactions t
                 JOIN statements st ON st.id = t.statement_id
                 LEFT JOIN categories c ON c.id = t.category_id
                 WHERE st.period_end >= %s AND st.period_end < %s
                   AND t.amount > 0 AND t.currency = %s
                   AND t.user_id = %s
                   AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"""
    params_cur: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql_cur += extra
    params_cur += ep
    sql_cur += " GROUP BY c.id, c.name, c.color ORDER BY total DESC"
    rows_cur = conn.execute(sql_cur, params_cur).fetchall()

    sql_prev = """SELECT c.id AS cid,
                         SUM(t.amount * COALESCE(t.my_share_pct, 1.0)) AS total
                  FROM transactions t
                  JOIN statements st ON st.id = t.statement_id
                  LEFT JOIN categories c ON c.id = t.category_id
                  WHERE st.period_end >= %s AND st.period_end < %s
                    AND t.amount > 0 AND t.currency = %s
                    AND t.user_id = %s
                    AND (c.name IS NULL OR c.name != 'Pagos/Transferencias')"""
    params_prev: list = [prev_start, prev_end, currency, user_id]
    sql_prev += extra
    params_prev += ep
    sql_prev += " GROUP BY c.id"
    prev_map = {
        r["cid"]: float(r["total"] or 0)
        for r in conn.execute(sql_prev, params_prev).fetchall()
    }

    out = []
    for r in rows_cur:
        cur_total = float(r["total"] or 0)
        prev_total = prev_map.get(r["cid"], 0.0)
        out.append({
            "id": r["cid"] if r["cid"] is not None else 0,
            "name": r["name"] or "Sin categoría",
            "color": r["color"] or "#94a3b8",
            "total": cur_total,
            "count": r["n"],
            "prev": prev_total,
            "delta_pct": ((cur_total - prev_total) / prev_total * 100) if prev_total > 0 else None,
        })
    return out


def top_merchants(conn, user_id: str, day_of_month: date,
                  currency: str = "ARS", limit: int = 10,
                  account_id: int | None = None) -> list[dict]:
    start, end = month_bounds(day_of_month)
    prev_start, prev_end = month_bounds(prev_month(day_of_month))
    sql_cur = """SELECT t.description_normalized AS name,
                        SUM(t.amount * COALESCE(t.my_share_pct, 1.0)) AS total,
                        COUNT(*) AS n
                 FROM transactions t
                 JOIN statements st ON st.id = t.statement_id
                 LEFT JOIN categories c ON c.id = t.category_id
                 WHERE st.period_end >= %s AND st.period_end < %s
                   AND t.amount > 0 AND t.currency = %s
                   AND t.user_id = %s
                   AND (c.name IS NULL OR c.name NOT IN ('Pagos/Transferencias', 'Impuestos'))"""
    params_cur: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql_cur += extra
    params_cur += ep
    sql_cur += " GROUP BY t.description_normalized ORDER BY total DESC LIMIT %s"
    params_cur.append(limit)
    rows_cur = conn.execute(sql_cur, params_cur).fetchall()

    sql_prev = """SELECT t.description_normalized AS name,
                         SUM(t.amount * COALESCE(t.my_share_pct, 1.0)) AS total
                  FROM transactions t
                  JOIN statements st ON st.id = t.statement_id
                  WHERE st.period_end >= %s AND st.period_end < %s
                    AND t.amount > 0 AND t.currency = %s
                    AND t.user_id = %s"""
    params_prev: list = [prev_start, prev_end, currency, user_id]
    sql_prev += extra
    params_prev += ep
    sql_prev += " GROUP BY t.description_normalized"
    prev_map = {
        r["name"]: float(r["total"] or 0)
        for r in conn.execute(sql_prev, params_prev).fetchall()
    }
    out = []
    for r in rows_cur:
        cur_total = float(r["total"] or 0)
        prev_total = prev_map.get(r["name"], 0.0)
        out.append({
            "name": (r["name"] or "").strip()[:50],
            "total": cur_total,
            "count": r["n"],
            "prev": prev_total,
            "delta_pct": ((cur_total - prev_total) / prev_total * 100) if prev_total > 0 else None,
        })
    return out


# ----------------- Trend 6M -----------------


def monthly_trend(conn, user_id: str, anchor: date, months: int = 6,
                  currency: str = "ARS",
                  account_id: int | None = None) -> list[tuple[str, float]]:
    out = []
    for i in range(months - 1, -1, -1):
        m = add_months(anchor.replace(day=1), -i)
        t = total_month(conn, user_id, m, currency, account_id=account_id)
        out.append((m.strftime("%b'%y"), t))
    return out


# ----------------- Cuotas forecast -----------------


def _is_likely_closed(it: dict, grace_days: int = 45) -> bool:
    """¿La compra debería haber terminado ya según la cadencia mensual?

    Anchor = period_end del statement donde apareció la última cuota vista
    (NO el posted_at que es la fecha de la compra original). Si la última
    cuota vista fue X y faltan (total-X) cuotas, la última cuota cae
    (total-X) meses después de ese period_end. Si pasaron más de
    `grace_days` desde esa fecha, asumimos compra cerrada (sistema nunca
    ingestó los resúmenes posteriores).
    """
    from datetime import timedelta as _td
    anchor_str = it.get("stmt_period_end") or it.get("posted_at")
    try:
        last = datetime.fromisoformat(anchor_str).date()
    except (TypeError, ValueError):
        return False
    remaining = (it.get("installment_total") or 0) - (it.get("installment_current") or 0)
    # final = fecha esperada de la última cuota (= anchor si remaining=0).
    # Usar timedelta-aproximación (~30 días/mes) porque add_months pierde el día.
    final = last + _td(days=30 * max(remaining, 0))
    return date.today() > final + _td(days=grace_days)


def _is_fully_refunded(conn, user_id: str, it: dict, tolerance: float = 0.10) -> bool:
    """¿Existe una tx negativa cuyo monto ≈ total de la compra Y matchea el
    mismo merchant (por comprobante o por todos los tokens del description)?
    Si sí, la compra fue reembolsada totalmente y no debe contarse como
    cuota pendiente.
    """
    import re as _re
    purchase_total = (it.get("amount") or 0) * (it.get("installment_total") or 0)
    if purchase_total <= 0:
        return False
    low = purchase_total * (1 - tolerance)
    high = purchase_total * (1 + tolerance)

    conds = ["t.amount < 0", "t.user_id = %s", "t.currency = %s",
             "ABS(t.amount) BETWEEN %s AND %s"]
    params: list = [user_id, it.get("currency") or "ARS", low, high]

    comp = (it.get("comprobante") or "").strip()
    if comp:
        conds.append("t.comprobante = %s")
        params.append(comp)
    else:
        desc = (it.get("description_normalized") or "").lower()
        # strip prefix "noviem. 19 000015 * " que mete el parser de Galicia
        desc = _re.sub(r"^[a-z]+\.\s*\d+\s+\d+\s*\*\s*", "", desc)
        toks = [t for t in desc.split() if len(t) >= 4 and not t.isdigit()]
        if not toks:
            return False
        for tok in toks:
            conds.append("t.description_normalized ILIKE %s")
            params.append(f"%{tok}%")

    sql = f"SELECT COUNT(*) AS n FROM transactions t WHERE {' AND '.join(conds)}"
    row = conn.execute(sql, params).fetchone()
    n = row["n"] if hasattr(row, "keys") else row[0]
    return (n or 0) > 0


def cuotas_pending_detail(conn, user_id: str,
                          account_id: int | None = None) -> list[dict]:
    # Dedupe por COMPRA: la misma compra aparece N veces a lo largo de N
    # resúmenes (cuota 1/12, 2/12, ...). DISTINCT ON elige la fila con
    # installment_current más alto = cuota más reciente vista.
    # Robusto a statements duplicados (re-ingestiones, files distintos del
    # mismo período).
    sql = """SELECT * FROM (
               SELECT DISTINCT ON (t.account_id,
                                   CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                                   t.installment_total)
                      t.id, t.posted_at, t.description_raw,
                      t.description_normalized, t.comprobante,
                      t.amount, t.currency,
                      t.notes,
                      t.installment_current, t.installment_total,
                      st.period_end AS stmt_period_end,
                      a.bank, a.card_last4,
                      c.name AS category, s.name AS subcategory
               FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               JOIN statements st ON st.id = t.statement_id
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN categories s ON s.id = t.subcategory_id
               WHERE t.installment_total IS NOT NULL AND t.installment_current IS NOT NULL
                 AND t.amount > 0
                 AND t.user_id = %s"""
    params: list = [user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += """ ORDER BY t.account_id,
                        CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                        t.installment_total,
                        t.installment_current DESC, st.period_end DESC, t.posted_at DESC
             ) dedup
             ORDER BY (installment_total - installment_current) * amount DESC,
                      installment_total - installment_current DESC"""
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if _is_likely_closed(d):
            continue  # cuotas finales estimadas ya pasaron — compra cerrada
        if _is_fully_refunded(conn, user_id, d):
            continue  # compra anulada / reembolsada
        remaining = r["installment_total"] - r["installment_current"]
        out.append({
            **d,
            "remaining_count": remaining,
            "remaining_amount": remaining * r["amount"],
            "total_purchase": r["installment_total"] * r["amount"],
        })
    if out:
        ids = [it["id"] for it in out]
        parts = conn.execute(
            """SELECT id, transaction_id, person_name, amount_owed, paid_back, paid_back_at
               FROM tx_participants
               WHERE transaction_id = ANY(%s)
               ORDER BY transaction_id, sort_order, id""",
            (ids,),
        ).fetchall()
        by_tx: dict[int, list[dict]] = {}
        for p in parts:
            by_tx.setdefault(p["transaction_id"], []).append(dict(p))
        for it in out:
            it["participants"] = by_tx.get(it["id"], [])
    return out


def cuotas_pending_total(conn, user_id: str, currency: str = "ARS",
                         account_id: int | None = None) -> tuple[float, int]:
    # Dedupe por compra + filtro de reembolsos totales.
    sql = """SELECT amount, installment_current, installment_total,
                    comprobante, description_normalized, currency, posted_at,
                    stmt_period_end
             FROM (
               SELECT DISTINCT ON (t.account_id,
                                   CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                                   t.installment_total)
                      t.amount, t.installment_current, t.installment_total,
                      t.comprobante, t.description_normalized, t.currency, t.posted_at,
                      st.period_end AS stmt_period_end
               FROM transactions t
               JOIN statements st ON st.id = t.statement_id
               WHERE t.installment_total IS NOT NULL AND t.installment_current IS NOT NULL
                 AND t.amount > 0 AND t.currency = %s
                 AND t.user_id = %s"""
    params: list = [currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += """ ORDER BY t.account_id,
                        CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                        t.installment_total,
                        t.installment_current DESC, st.period_end DESC, t.posted_at DESC
             ) dedup
             WHERE installment_total > installment_current"""
    rows = conn.execute(sql, params).fetchall()
    total = 0.0
    n = 0
    for r in rows:
        d = dict(r)
        if _is_likely_closed(d):
            continue
        if _is_fully_refunded(conn, user_id, d):
            continue
        total += float(r["amount"]) * (r["installment_total"] - r["installment_current"])
        n += 1
    return total, n


def cuotas_this_month(conn, user_id: str, day_of_month: date,
                      currency: str = "ARS",
                      account_id: int | None = None) -> tuple[float, int]:
    start, end = month_bounds(day_of_month)
    sql = """SELECT COALESCE(SUM(t.amount), 0) AS s, COUNT(*) AS n
             FROM transactions t
             JOIN statements st ON st.id = t.statement_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.installment_total IS NOT NULL
               AND t.amount > 0 AND t.currency = %s
               AND t.user_id = %s"""
    params: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0), int(row[1] or 0)


def cuotas_forecast(conn, user_id: str, anchor: date, months: int = 6,
                    currency: str = "ARS",
                    account_id: int | None = None) -> list[tuple[str, float]]:
    # Dedupe por compra: la cuota más reciente vista determina cuántas
    # quedan. Robusto a statements duplicados.
    sql = """SELECT amount, installment_current, installment_total,
                    comprobante, description_normalized, currency, posted_at,
                    stmt_period_end
             FROM (
               SELECT DISTINCT ON (t.account_id,
                                   CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                                   t.installment_total)
                      t.amount, t.installment_current, t.installment_total,
                      t.comprobante, t.description_normalized, t.currency, t.posted_at,
                      st.period_end AS stmt_period_end
               FROM transactions t
               JOIN statements st ON st.id = t.statement_id
               WHERE t.installment_total IS NOT NULL AND t.installment_current IS NOT NULL
                 AND t.amount > 0 AND t.currency = %s
                 AND t.user_id = %s"""
    params: list = [currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += """ ORDER BY t.account_id,
                        CASE WHEN COALESCE(t.comprobante,'')='' THEN t.description_normalized ELSE t.comprobante END,
                        t.installment_total,
                        t.installment_current DESC, st.period_end DESC, t.posted_at DESC
             ) dedup
             WHERE installment_total > installment_current"""
    rows = conn.execute(sql, params).fetchall()
    buckets: dict[str, float] = defaultdict(float)
    start_month = add_months(anchor.replace(day=1), 1)
    months_list = [add_months(start_month, i) for i in range(months)]
    month_labels = {m.strftime("%b'%y") for m in months_list}
    for r in rows:
        d = dict(r)
        if _is_likely_closed(d):
            continue
        if _is_fully_refunded(conn, user_id, d):
            continue
        # Anchor del forecast = period_end del statement de la última cuota
        # vista (no posted_at — que es la fecha de compra original).
        anchor_str = r["stmt_period_end"] or r["posted_at"]
        try:
            last_seen = datetime.fromisoformat(anchor_str).date().replace(day=1)
        except (TypeError, ValueError):
            continue
        remaining = (r["installment_total"] or 0) - (r["installment_current"] or 0)
        for i in range(1, remaining + 1):
            cuota_month = add_months(last_seen, i)
            label = cuota_month.strftime("%b'%y")
            if label in month_labels:
                buckets[label] += r["amount"]
    return [(m.strftime("%b'%y"), buckets.get(m.strftime("%b'%y"), 0.0)) for m in months_list]


def cuotas_history(conn, user_id: str, anchor: date, months_back: int = 6,
                   currency: str = "ARS",
                   account_id: int | None = None) -> list[tuple[str, float]]:
    """Cuánto se pagó en cuotas en cada uno de los últimos N meses (incluyendo anchor).

    Basado en statements.period_end. Los meses se etiquetan por el statement
    correspondiente (no hace dedupe — cada statement aporta su cuota mensual).
    """
    end_month = anchor.replace(day=1)
    start_month = add_months(end_month, -(months_back - 1))
    start_str = start_month.isoformat()
    # range_end exclusivo: primer día del mes siguiente al anchor
    range_end = add_months(end_month, 1).isoformat()

    sql = """SELECT SUBSTRING(st.period_end, 1, 7) AS ym,
                    COALESCE(SUM(t.amount), 0) AS total
             FROM transactions t
             JOIN statements st ON st.id = t.statement_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.installment_total IS NOT NULL
               AND t.amount > 0 AND t.currency = %s
               AND t.user_id = %s"""
    params: list = [start_str, range_end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += " GROUP BY SUBSTRING(st.period_end, 1, 7)"
    rows = conn.execute(sql, params).fetchall()
    by_ym: dict[str, float] = {r["ym"]: float(r["total"] or 0) for r in rows}
    months_list = [add_months(start_month, i) for i in range(months_back)]
    return [(m.strftime("%b'%y"), by_ym.get(m.strftime("%Y-%m"), 0.0)) for m in months_list]


# ----------------- Recurring / Fixed -----------------


def confirmed_recurring(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT g.id, g.normalized_merchant, g.cadence_days, g.occurrences,
                  g.expected_amount_min, g.expected_amount_max,
                  c.name AS category, g.last_seen_at
           FROM recurring_groups g
           LEFT JOIN categories c ON c.id = g.suggested_category_id
           WHERE g.status = 'confirmed' AND g.user_id = %s
           ORDER BY (g.expected_amount_min + g.expected_amount_max) DESC""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def cross_user_suggested_fixed(conn, user_id: str, limit: int = 20) -> list[dict]:
    """Merchants que otros users confirmaron como fijos y aparecen en las
    transacciones de este user, pero este user no los tiene en recurring_groups."""
    from finanzas.recurring import recurring_merchant_key

    rows = conn.execute(
        "SELECT DISTINCT description_raw FROM transactions "
        "WHERE user_id = %s AND amount > 0",
        (user_id,),
    ).fetchall()
    user_keys = {recurring_merchant_key(r["description_raw"]) for r in rows}
    user_keys.discard("")
    if not user_keys:
        return []

    own = {r["normalized_merchant"] for r in conn.execute(
        "SELECT normalized_merchant FROM recurring_groups WHERE user_id = %s",
        (user_id,),
    ).fetchall()}

    candidates = list(user_keys - own)
    if not candidates:
        return []

    rows = conn.execute(
        """SELECT normalized_merchant, COUNT(DISTINCT user_id) AS users_count
           FROM recurring_groups
           WHERE status = 'confirmed' AND user_id != %s
             AND normalized_merchant = ANY(%s)
           GROUP BY normalized_merchant
           ORDER BY users_count DESC, normalized_merchant
           LIMIT %s""",
        (user_id, candidates, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def suggested_recurring(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT g.id, g.normalized_merchant, g.cadence_days, g.occurrences,
                  g.expected_amount_min, g.expected_amount_max,
                  c.name AS category, g.last_seen_at
           FROM recurring_groups g
           LEFT JOIN categories c ON c.id = g.suggested_category_id
           WHERE g.status = 'suggested' AND g.user_id = %s
           ORDER BY g.occurrences DESC""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ----------------- Review inbox -----------------


def uncategorized(conn, user_id: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT t.id, t.posted_at, t.description_raw, t.description_normalized,
                  t.amount, t.currency, t.comprobante,
                  t.installment_current, t.installment_total,
                  a.bank, a.card_last4
           FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE t.category_id IS NULL AND t.user_id = %s
           ORDER BY t.posted_at DESC, t.id DESC
           LIMIT %s""",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def uncategorized_count(conn, user_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND user_id = %s",
        (user_id,),
    ).fetchone()[0]


# ----------------- Categorías (jerarquía) -----------------


def all_categories(conn, user_id: str) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT * FROM categories WHERE user_id = %s ORDER BY parent_id NULLS FIRST, sort_order, name",
        (user_id,),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def category_tree(conn, user_id: str) -> list[dict]:
    cats = all_categories(conn, user_id)
    roots: list[dict] = []
    children_of: dict[int, list[dict]] = defaultdict(list)
    for c in cats.values():
        if c["parent_id"] is None:
            roots.append({**c, "subcategories": []})
        else:
            children_of[c["parent_id"]].append(dict(c))
    for r in roots:
        r["subcategories"] = sorted(children_of.get(r["id"], []), key=lambda x: x["sort_order"])
    return roots


def all_accounts(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, bank, card_last4, holder_name, color FROM accounts WHERE user_id = %s ORDER BY id",
        (user_id,),
    ).fetchall()]


def transactions_in_month(
    conn, user_id: str, day_of_month: date,
    currency: str = "ARS", account_id: int | None = None,
) -> list[dict]:
    start, end = month_bounds(day_of_month)
    sql = """SELECT t.id, t.posted_at, t.description_raw, t.amount, t.currency,
                    t.my_share_pct, t.share_with, t.notes,
                    t.installment_current, t.installment_total,
                    a.bank, a.card_last4,
                    pc.name AS category, s.name AS subcategory
             FROM transactions t
             JOIN accounts a ON a.id = t.account_id
             JOIN statements st ON st.id = t.statement_id
             LEFT JOIN categories pc ON pc.id = t.category_id
             LEFT JOIN categories s ON s.id = t.subcategory_id
             WHERE st.period_end >= %s AND st.period_end < %s
               AND t.amount > 0 AND t.currency = %s
               AND t.user_id = %s
               AND (pc.name IS NULL OR pc.name != 'Pagos/Transferencias')"""
    params: list = [start, end, currency, user_id]
    extra, ep = _acc_clause(account_id)
    sql += extra
    params += ep
    sql += " ORDER BY t.amount DESC"
    rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    if items:
        ids = [it["id"] for it in items]
        parts = conn.execute(
            """SELECT id, transaction_id, person_name, amount_owed, paid_back, paid_back_at
               FROM tx_participants
               WHERE transaction_id = ANY(%s)
               ORDER BY transaction_id, sort_order, id""",
            (ids,),
        ).fetchall()
        by_tx: dict[int, list[dict]] = {}
        for p in parts:
            by_tx.setdefault(p["transaction_id"], []).append(dict(p))
        for it in items:
            it["participants"] = by_tx.get(it["id"], [])
    return items


def frequent_participants(conn, user_id: str, limit: int = 30) -> list[str]:
    rows = conn.execute(
        """SELECT p.person_name, COUNT(*) AS n
           FROM tx_participants p
           JOIN transactions t ON t.id = p.transaction_id
           WHERE TRIM(p.person_name) != '' AND t.user_id = %s
           GROUP BY p.person_name
           ORDER BY n DESC, p.person_name ASC
           LIMIT %s""",
        (user_id, limit),
    ).fetchall()
    return [r["person_name"] for r in rows]


def search_transactions(
    conn,
    user_id: str,
    text: str | None = None,
    amount: float | None = None,
    tolerance_pct: float = 10.0,
    currency: str | None = None,
    account_id: int | None = None,
    limit: int = 300,
) -> list[dict]:
    where = ["t.user_id = %s"]
    params: list = [user_id]
    if text:
        where.append("t.description_raw ILIKE %s")
        params.append(f"%{text}%")
    if amount is not None and amount > 0:
        margin = max(abs(amount) * (tolerance_pct / 100.0), 0.01)
        where.append("ABS(t.amount) BETWEEN %s AND %s")
        params.extend([abs(amount) - margin, abs(amount) + margin])
    if currency:
        where.append("t.currency = %s")
        params.append(currency)
    if account_id:
        where.append("t.account_id = %s")
        params.append(account_id)
    if len(where) == 1:  # solo el user_id filter, sin criterio de búsqueda real
        return []
    sql = f"""
        SELECT t.id, t.posted_at, t.description_raw, t.amount, t.currency,
               t.my_share_pct, t.share_with, t.notes,
               t.installment_current, t.installment_total,
               a.bank, a.card_last4,
               c.name AS category, s.name AS subcategory
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c ON c.id = t.category_id
        LEFT JOIN categories s ON s.id = t.subcategory_id
        WHERE {' AND '.join(where)}
        ORDER BY t.posted_at DESC, t.amount DESC
        LIMIT %s
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]
    if items:
        ids = [it["id"] for it in items]
        parts = conn.execute(
            """SELECT id, transaction_id, person_name, amount_owed, paid_back, paid_back_at
               FROM tx_participants
               WHERE transaction_id = ANY(%s)
               ORDER BY transaction_id, sort_order, id""",
            (ids,),
        ).fetchall()
        by_tx: dict[int, list[dict]] = {}
        for p in parts:
            by_tx.setdefault(p["transaction_id"], []).append(dict(p))
        for it in items:
            it["participants"] = by_tx.get(it["id"], [])
    return items


def list_statements_by_month(conn, user_id: str) -> list[dict]:
    """Resúmenes del usuario agrupados por mes (basado en period_end).

    Devuelve una lista de meses, cada uno con sus statements. Cada statement trae
    bank/card del account asociado y un tx_count (cuántas transacciones quedan).
    """
    rows = conn.execute(
        """SELECT st.id, st.period_start, st.period_end, st.due_date,
                  st.source_filename, st.raw_total_ars, st.raw_total_usd,
                  st.parsed_at,
                  a.bank, a.card_last4, a.holder_name,
                  COUNT(t.id) AS tx_count
           FROM statements st
           JOIN accounts a ON a.id = st.account_id
           LEFT JOIN transactions t ON t.statement_id = st.id
           WHERE st.user_id = %s
           GROUP BY st.id, a.bank, a.card_last4, a.holder_name
           ORDER BY st.period_end DESC NULLS LAST, st.parsed_at DESC""",
        (user_id,),
    ).fetchall()

    meses_es = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        s = dict(r)
        pe = s.get("period_end")
        key = pe[:7] if pe else "sin-fecha"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)

    out: list[dict] = []
    for k in order:
        if k == "sin-fecha":
            label = "Sin fecha"
        else:
            y, m = k.split("-")
            label = f"{meses_es[int(m)]} {y}"
        out.append({"month": k, "label": label, "statements": groups[k]})
    return out


def delete_statement(conn, user_id: str, statement_id: int) -> bool:
    """Borra un resumen (y sus transactions via cascade). Filtra por user_id."""
    cur = conn.execute(
        "DELETE FROM statements WHERE id = %s AND user_id = %s RETURNING id",
        (statement_id, user_id),
    )
    return cur.fetchone() is not None


def latest_data_anchor(conn, user_id: str) -> date:
    row = conn.execute(
        "SELECT MAX(posted_at) AS m FROM transactions WHERE amount > 0 AND user_id = %s",
        (user_id,),
    ).fetchone()
    if row and row["m"]:
        return datetime.fromisoformat(row["m"]).date().replace(day=1)
    return date.today().replace(day=1)
