"""Script one-time: migra datos de SQLite local -> Supabase PostgreSQL.

Uso:
    DATABASE_URL=postgresql://... python scripts/migrate_sqlite_to_supabase.py \\
        --sqlite data/data.db \\
        --user-id TU_GOOGLE_SUB \\
        --user-email federicodelfranco@gmail.com

El Google 'sub' es el ID estable de tu cuenta. Para obtenerlo:
1. Deployá la app una vez
2. Iniciá sesión con Google
3. El sub aparece en la sesión — podés loguearlo temporalmente en /auth/callback

Alternativamente, para una primera prueba podés usar tu email como user_id.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrar SQLite -> Supabase PostgreSQL")
    parser.add_argument("--sqlite", required=True, help="Ruta al archivo data.db")
    parser.add_argument("--user-id", required=True, help="Google sub (o email) del usuario")
    parser.add_argument("--user-email", required=True, help="Email del usuario")
    parser.add_argument("--dry-run", action="store_true", help="No escribe nada")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL no seteada", file=sys.stderr)
        sys.exit(1)

    sq = sqlite3.connect(args.sqlite)
    sq.row_factory = sqlite3.Row
    pg_raw = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.DictCursor)
    pg_raw.autocommit = False

    user_id = args.user_id
    print(f"Migrando datos de {args.sqlite} -> Supabase como user_id={user_id!r}")

    try:
        _migrate(sq, pg_raw, user_id, args.dry_run)
        if not args.dry_run:
            pg_raw.commit()
            print("✓ Migración completada exitosamente")
        else:
            pg_raw.rollback()
            print("DRY RUN — no se escribió nada")
    except Exception as e:
        pg_raw.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        sq.close()
        pg_raw.close()


def _migrate(sq: sqlite3.Connection, pg_raw, user_id: str, dry_run: bool) -> None:
    pg = pg_raw.cursor()

    # Limpiar datos previos del usuario (ej: seeds del primer login)
    print("Limpiando datos previos del usuario en Supabase...")
    for table in ("transactions", "manual_expenses",
                  "statements", "rules", "recurring_groups", "accounts", "categories"):
        pg.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
        print(f"  {table}: {pg.rowcount} filas eliminadas")

    # --- categories (primero roots, luego hijos para respetar FK) ---
    cat_id_map: dict[int, int] = {}
    print("Migrando categories...")

    sq_cats = sq.execute(
        "SELECT * FROM categories ORDER BY CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END, id"
    ).fetchall()

    for cat in sq_cats:
        old_id = cat["id"]
        new_parent = cat_id_map.get(cat["parent_id"]) if cat["parent_id"] else None
        pg.execute(
            """INSERT INTO categories
               (user_id, parent_id, name, color, icon, is_user_created, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (user_id, new_parent, cat["name"], cat["color"], cat["icon"],
             cat["is_user_created"], cat["sort_order"]),
        )
        new_id = pg.fetchone()[0]
        cat_id_map[old_id] = new_id
    print(f"  {len(sq_cats)} categorías migradas")

    # --- rules ---
    print("Migrando rules...")
    sq_rules = sq.execute("SELECT * FROM rules").fetchall()
    for r in sq_rules:
        new_cat = cat_id_map.get(r["category_id"]) if r["category_id"] else None
        new_sub = cat_id_map.get(r["subcategory_id"]) if r["subcategory_id"] else None
        pg.execute(
            """INSERT INTO rules
               (user_id, match_type, pattern, category_id, subcategory_id,
                priority, source, hit_count, last_used_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, r["match_type"], r["pattern"], new_cat, new_sub,
             r["priority"], r["source"], r["hit_count"], r["last_used_at"]),
        )
    print(f"  {len(sq_rules)} reglas migradas")

    # --- accounts ---
    print("Migrando accounts...")
    acc_id_map: dict[int, int] = {}
    sq_accs = sq.execute("SELECT * FROM accounts").fetchall()
    for a in sq_accs:
        pg.execute(
            """INSERT INTO accounts
               (user_id, bank, card_last4, holder_name, currency_default, color)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (user_id, a["bank"], a["card_last4"], a["holder_name"],
             a["currency_default"] or "ARS", a["color"] or "#64748b"),
        )
        acc_id_map[a["id"]] = pg.fetchone()[0]
    print(f"  {len(sq_accs)} cuentas migradas")

    # --- statements ---
    print("Migrando statements...")
    stmt_id_map: dict[int, int] = {}
    sq_stmts = sq.execute("SELECT * FROM statements").fetchall()
    for s in sq_stmts:
        new_acc = acc_id_map.get(s["account_id"])
        if not new_acc:
            print(f"  WARN: statement {s['id']} tiene account_id desconocido, saltando")
            continue
        pg.execute(
            """INSERT INTO statements
               (user_id, account_id, period_start, period_end, due_date,
                source_filename, file_sha256, raw_total_ars, raw_total_usd)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (user_id, new_acc, s["period_start"], s["period_end"], s["due_date"],
             s["source_filename"], s["file_sha256"],
             s["raw_total_ars"] or 0, s["raw_total_usd"] or 0),
        )
        stmt_id_map[s["id"]] = pg.fetchone()[0]
    print(f"  {len(stmt_id_map)} statements migrados")

    # --- transactions ---
    print("Migrando transactions...")
    tx_id_map: dict[int, int] = {}
    sq_txs = sq.execute("SELECT * FROM transactions").fetchall()
    skipped = 0
    for t in sq_txs:
        new_stmt = stmt_id_map.get(t["statement_id"]) if t["statement_id"] else None
        new_acc = acc_id_map.get(t["account_id"])
        if not new_acc:
            skipped += 1
            continue
        new_cat = cat_id_map.get(t["category_id"]) if t["category_id"] else None
        new_sub = cat_id_map.get(t["subcategory_id"]) if t["subcategory_id"] else None
        pg.execute(
            """INSERT INTO transactions
               (user_id, statement_id, account_id, posted_at, description_raw,
                description_normalized, comprobante, amount, currency,
                category_id, subcategory_id, is_manually_categorized,
                installment_current, installment_total, notes, hash,
                my_share_pct, share_with)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (user_id, new_stmt, new_acc, t["posted_at"], t["description_raw"],
             t["description_normalized"], t["comprobante"], t["amount"], t["currency"],
             new_cat, new_sub, t["is_manually_categorized"] or 0,
             t["installment_current"], t["installment_total"], t["notes"], t["hash"],
             t["my_share_pct"] if "my_share_pct" in t.keys() else 1.0,
             t["share_with"] if "share_with" in t.keys() else None),
        )
        tx_id_map[t["id"]] = pg.fetchone()[0]
    print(f"  {len(tx_id_map)} transacciones migradas ({skipped} saltadas)")

    # --- manual_expenses ---
    print("Migrando manual_expenses...")
    sq_man = sq.execute("SELECT * FROM manual_expenses").fetchall()
    for m in sq_man:
        new_cat = cat_id_map.get(m["category_id"]) if m["category_id"] else None
        new_sub = cat_id_map.get(m["subcategory_id"]) if m["subcategory_id"] else None
        pg.execute(
            """INSERT INTO manual_expenses
               (user_id, posted_at, description, amount, currency,
                category_id, subcategory_id, is_fixed, recurrence_rule, notes,
                my_share_pct, share_with)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (user_id, m["posted_at"], m["description"], m["amount"], m["currency"],
             new_cat, new_sub, m["is_fixed"] or 0, m["recurrence_rule"], m["notes"],
             m["my_share_pct"] if "my_share_pct" in m.keys() else 1.0,
             m["share_with"] if "share_with" in m.keys() else None),
        )
    print(f"  {len(sq_man)} gastos manuales migrados")

    # --- recurring_groups ---
    print("Migrando recurring_groups...")
    rg_id_map: dict[int, int] = {}
    sq_rgs = sq.execute("SELECT * FROM recurring_groups").fetchall()
    for rg in sq_rgs:
        new_cat = cat_id_map.get(rg["suggested_category_id"]) if rg["suggested_category_id"] else None
        pg.execute(
            """INSERT INTO recurring_groups
               (user_id, normalized_merchant, suggested_category_id,
                expected_amount_min, expected_amount_max, cadence_days,
                occurrences, last_seen_at, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (user_id, rg["normalized_merchant"], new_cat,
             rg["expected_amount_min"], rg["expected_amount_max"],
             rg["cadence_days"], rg["occurrences"], rg["last_seen_at"], rg["status"]),
        )
        rg_id_map[rg["id"]] = pg.fetchone()[0]
    print(f"  {len(sq_rgs)} grupos recurrentes migrados")

    # Actualizar recurring_group_id en transactions
    for old_tx_id, new_tx_id in tx_id_map.items():
        sq_tx = sq.execute("SELECT recurring_group_id FROM transactions WHERE id = ?", (old_tx_id,)).fetchone()
        if sq_tx and sq_tx["recurring_group_id"]:
            new_rg = rg_id_map.get(sq_tx["recurring_group_id"])
            if new_rg:
                pg.execute(
                    "UPDATE transactions SET recurring_group_id = %s WHERE id = %s",
                    (new_rg, new_tx_id),
                )

    # --- tx_participants ---
    print("Migrando tx_participants...")
    sq_parts = sq.execute("SELECT * FROM tx_participants").fetchall()
    migrated_parts = 0
    for p in sq_parts:
        new_tx = tx_id_map.get(p["transaction_id"])
        if not new_tx:
            continue
        pg.execute(
            """INSERT INTO tx_participants
               (transaction_id, person_name, amount_owed, paid_back, paid_back_at, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (new_tx, p["person_name"], p["amount_owed"], p["paid_back"],
             p["paid_back_at"], p["sort_order"]),
        )
        migrated_parts += 1
    print(f"  {migrated_parts} participantes migrados")


if __name__ == "__main__":
    main()
