"""Detección de gastos recurrentes (mensuales)."""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta

from finanzas.parsers.base import normalize_description

MIN_OCCURRENCES = 3
MAX_CADENCE_DEVIATION_DAYS = 6
AMOUNT_TOLERANCE = 0.20


def recurring_merchant_key(desc_raw: str) -> str:
    s = normalize_description(desc_raw)
    s = re.sub(r"[.,/_*\-]+", " ", s)
    tokens = [t for t in s.split() if len(t) >= 3 and not t.isdigit()]
    if not tokens:
        return s.strip()
    return tokens[0].lower()


def recompute(conn, user_id: str, window_months: int = 6) -> int:
    today = date.today()
    cutoff = (today - timedelta(days=window_months * 31)).isoformat()

    rows = conn.execute(
        """SELECT id, description_raw, posted_at, amount, currency, category_id
           FROM transactions
           WHERE posted_at >= %s AND amount > 0 AND user_id = %s
           ORDER BY posted_at ASC""",
        (cutoff, user_id),
    ).fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        key = recurring_merchant_key(r["description_raw"])
        if not key or len(key) < 3:
            continue
        groups.setdefault(key, []).append(r)

    detected = 0
    for key, items in groups.items():
        if len(items) < MIN_OCCURRENCES:
            continue
        dates = [datetime.fromisoformat(it["posted_at"]).date() for it in items]
        dates.sort()
        deltas = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        if not deltas:
            continue
        median = statistics.median(deltas)
        if not (28 - MAX_CADENCE_DEVIATION_DAYS <= median <= 32 + MAX_CADENCE_DEVIATION_DAYS):
            continue
        amts = [float(it["amount"]) for it in items]
        avg = statistics.mean(amts)
        if avg == 0:
            continue
        deviations = [abs(a - avg) / avg for a in amts]
        if max(deviations) > AMOUNT_TOLERANCE:
            continue

        cat_id = None
        for it in items:
            if it["category_id"] is not None:
                cat_id = it["category_id"]
                break

        amt_min = min(amts) * 0.85
        amt_max = max(amts) * 1.15
        last_seen = max(dates).isoformat()

        existing = conn.execute(
            "SELECT id, status FROM recurring_groups WHERE normalized_merchant = %s AND user_id = %s",
            (key, user_id),
        ).fetchone()
        if existing:
            if existing["status"] == "rejected":
                continue
            conn.execute(
                """UPDATE recurring_groups
                   SET expected_amount_min = %s, expected_amount_max = %s,
                       cadence_days = %s, occurrences = %s, last_seen_at = %s,
                       suggested_category_id = COALESCE(suggested_category_id, %s)
                   WHERE id = %s""",
                (amt_min, amt_max, int(median), len(items), last_seen, cat_id, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO recurring_groups
                   (user_id, normalized_merchant, suggested_category_id,
                    expected_amount_min, expected_amount_max,
                    cadence_days, occurrences, last_seen_at, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'suggested')""",
                (user_id, key, cat_id, amt_min, amt_max, int(median), len(items), last_seen),
            )
        detected += 1

    return detected


def link_transactions(conn, user_id: str) -> int:
    groups = conn.execute(
        "SELECT id, normalized_merchant FROM recurring_groups WHERE status = 'confirmed' AND user_id = %s",
        (user_id,),
    ).fetchall()
    if not groups:
        return 0
    updated = 0
    txs = conn.execute(
        "SELECT id, description_raw FROM transactions WHERE recurring_group_id IS NULL AND amount > 0 AND user_id = %s",
        (user_id,),
    ).fetchall()
    by_key = {g["normalized_merchant"]: g["id"] for g in groups}
    for t in txs:
        key = recurring_merchant_key(t["description_raw"])
        gid = by_key.get(key)
        if gid:
            conn.execute(
                "UPDATE transactions SET recurring_group_id = %s WHERE id = %s",
                (gid, t["id"]),
            )
            updated += 1
    return updated
