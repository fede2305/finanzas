"""Generador de 'insights' estadísticos para el dashboard."""

from __future__ import annotations

from calendar import monthrange
from datetime import date


def generate(conn, current_month: date, user_id: str) -> list[str]:
    out: list[str] = []
    out += _delta_top_category(conn, current_month, user_id)
    out += _subscription_summary(conn, current_month, user_id)
    out += _fixed_vs_total(conn, current_month, user_id)
    out += _cuotas_pipeline(conn, current_month, user_id)
    return out


def _delta_top_category(conn, current_month: date, user_id: str) -> list[str]:
    cur = _month_range(current_month)
    prev = _month_range(_prev_month(current_month))
    rows_cur = conn.execute(
        """SELECT c.name AS cat, COALESCE(SUM(t.amount), 0) AS total
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.posted_at >= %s AND t.posted_at < %s
             AND t.amount > 0 AND t.currency = 'ARS'
             AND t.user_id = %s
             AND c.name NOT IN ('Pagos/Transferencias', 'Sin categoría')
           GROUP BY c.name""",
        (*cur, user_id),
    ).fetchall()
    rows_prev = conn.execute(
        """SELECT c.name AS cat, COALESCE(SUM(t.amount), 0) AS total
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.posted_at >= %s AND t.posted_at < %s
             AND t.amount > 0 AND t.currency = 'ARS'
             AND t.user_id = %s
             AND c.name NOT IN ('Pagos/Transferencias', 'Sin categoría')
           GROUP BY c.name""",
        (*prev, user_id),
    ).fetchall()
    cur_map = {r["cat"]: r["total"] for r in rows_cur if r["cat"]}
    prev_map = {r["cat"]: r["total"] for r in rows_prev if r["cat"]}
    deltas: list[tuple[str, float, float]] = []
    for cat, total in cur_map.items():
        prev_total = prev_map.get(cat, 0)
        if prev_total <= 0 or total <= 0:
            continue
        pct = (total - prev_total) / prev_total * 100
        if abs(pct) >= 25:
            deltas.append((cat, pct, total))
    if not deltas:
        return []
    deltas.sort(key=lambda x: abs(x[1]), reverse=True)
    cat, pct, total = deltas[0]
    arrow = "↑" if pct > 0 else "↓"
    return [f"Tu gasto en <b>{cat}</b> {arrow} {abs(pct):.0f}% vs mes anterior (${total:,.0f})"]


def _subscription_summary(conn, current_month: date, user_id: str) -> list[str]:
    cur = _month_range(current_month)
    rows = conn.execute(
        """SELECT t.description_raw, t.amount, t.currency
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.posted_at >= %s AND t.posted_at < %s
             AND t.amount > 0
             AND t.user_id = %s
             AND c.name = 'Suscripciones'""",
        (*cur, user_id),
    ).fetchall()
    if not rows:
        return []
    ars = [r["amount"] for r in rows if r["currency"] == "ARS"]
    usd = [r["amount"] for r in rows if r["currency"] == "USD"]
    parts = []
    if ars:
        parts.append(f"${sum(ars):,.0f} ARS")
    if usd:
        parts.append(f"US${sum(usd):,.2f}")
    total_str = " + ".join(parts)
    return [f"Tenés <b>{len(rows)} suscripciones</b> este mes por {total_str}"]


def _fixed_vs_total(conn, current_month: date, user_id: str) -> list[str]:
    cur = _month_range(current_month)
    total = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE posted_at >= %s AND posted_at < %s
             AND amount > 0 AND currency = 'ARS' AND user_id = %s""",
        (*cur, user_id),
    ).fetchone()[0]
    if total <= 0:
        return []
    fixed = conn.execute(
        """SELECT COALESCE(SUM(t.amount), 0)
           FROM transactions t
           JOIN recurring_groups g ON g.id = t.recurring_group_id
           WHERE t.posted_at >= %s AND t.posted_at < %s
             AND t.amount > 0 AND t.currency = 'ARS'
             AND t.user_id = %s
             AND g.status = 'confirmed'""",
        (*cur, user_id),
    ).fetchone()[0]
    if fixed <= 0:
        return []
    pct = fixed / total * 100
    return [f"Tus <b>gastos fijos</b> representan {pct:.0f}% del total del mes (${fixed:,.0f})"]


def _cuotas_pipeline(conn, current_month: date, user_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT amount, installment_current, installment_total, currency
           FROM transactions
           WHERE installment_total IS NOT NULL AND installment_current IS NOT NULL
             AND installment_total > installment_current
             AND amount > 0 AND user_id = %s""",
        (user_id,),
    ).fetchall()
    if not rows:
        return []
    next_3 = 0.0
    next_6 = 0.0
    for r in rows:
        remaining = (r["installment_total"] or 0) - (r["installment_current"] or 0)
        if r["currency"] != "ARS":
            continue
        amt = r["amount"]
        next_3 += amt * min(remaining, 3)
        next_6 += amt * min(remaining, 6)
    if next_3 <= 0:
        return []
    return [
        f"<b>Compromisos por cuotas:</b> ${next_3:,.0f} en los próximos 3 meses (${next_6:,.0f} a 6 meses)"
    ]


def _month_range(d: date) -> tuple[str, str]:
    start = d.replace(day=1)
    days = monthrange(start.year, start.month)[1]
    end = start.replace(day=days)
    from datetime import timedelta
    next_day = end + timedelta(days=1)
    return start.isoformat(), next_day.isoformat()


def _prev_month(d: date) -> date:
    if d.month == 1:
        return d.replace(year=d.year - 1, month=12, day=1)
    return d.replace(month=d.month - 1, day=1)
