"""Aplica reglas a transacciones para asignar categoría/subcategoría."""

from __future__ import annotations

import re
from datetime import datetime

from finanzas.parsers.base import normalize_description


def categorize_pending(conn, user_id: str) -> int:
    """Asigna categoría a todas las txs sin categoría del usuario."""
    rules = _load_rules(conn, user_id)
    pending = conn.execute(
        """SELECT id, description_raw, description_normalized
           FROM transactions
           WHERE category_id IS NULL AND user_id = %s""",
        (user_id,),
    ).fetchall()

    updated = 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    for row in pending:
        tx_id = row["id"]
        desc_norm = row["description_normalized"] or normalize_description(row["description_raw"])
        match = _find_match(desc_norm, rules)
        if not match:
            continue
        rule_id, cat_id, subcat_id = match
        conn.execute(
            "UPDATE transactions SET category_id = %s, subcategory_id = %s, is_manually_categorized = 0 WHERE id = %s",
            (cat_id, subcat_id, tx_id),
        )
        conn.execute(
            "UPDATE rules SET hit_count = hit_count + 1, last_used_at = %s WHERE id = %s",
            (now, rule_id),
        )
        updated += 1
    return updated


def categorize_one(conn, user_id: str, description_normalized: str) -> tuple[int, int | None] | None:
    rules = _load_rules(conn, user_id)
    match = _find_match(description_normalized, rules)
    if not match:
        return None
    _, cat_id, subcat_id = match
    return (cat_id, subcat_id)


def add_learned_rule(
    conn,
    user_id: str,
    pattern: str,
    category_id: int,
    subcategory_id: int | None,
    match_type: str = "exact",
) -> int:
    cur = conn.execute(
        """INSERT INTO rules (user_id, match_type, pattern, category_id, subcategory_id, priority, source)
           VALUES (%s, %s, %s, %s, %s, 5000, 'learned') RETURNING id""",
        (user_id, match_type, pattern.lower(), category_id, subcategory_id),
    )
    return cur.lastrowid or 0


def _load_rules(conn, user_id: str) -> list:
    return conn.execute(
        """SELECT id, match_type, pattern, category_id, subcategory_id, priority, source
           FROM rules
           WHERE user_id = %s
           ORDER BY priority DESC, id ASC""",
        (user_id,),
    ).fetchall()


def _find_match(desc_norm: str, rules: list) -> tuple[int, int, int | None] | None:
    desc = desc_norm.lower()
    for r in rules:
        pat = (r["pattern"] or "").lower()
        if not pat:
            continue
        mt = r["match_type"]
        try:
            hit = False
            if mt == "exact":
                hit = desc == pat
            elif mt == "contains":
                hit = pat in desc
            elif mt == "regex":
                hit = re.search(pat, desc) is not None
        except re.error:
            hit = False
        if hit:
            return r["id"], r["category_id"], r["subcategory_id"]
    return None
