"""Servicio de ingesta: orquesta parser + dedup + categorizer + recurring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg2.errors

from finanzas import storage
from finanzas.categorizer import categorize_pending
from finanzas.models import ParsedStatement
from finanzas.parsers import parse_file
from finanzas.parsers.base import normalize_description, tx_hash
from finanzas.recurring import link_transactions, recompute


@dataclass
class IngestResult:
    statement_id: int | None
    file_already_imported: bool
    new_transactions: int
    duplicate_transactions: int
    auto_categorized: int
    uncategorized: int
    recurring_groups_detected: int


def ingest_file(
    conn,
    source_path: Path,
    user_id: str,
    file_content: bytes | None = None,
) -> IngestResult:
    """Parsea un archivo y persiste todo lo nuevo. Dedup silencioso por hash.

    Args:
        conn: conexión DB abierta.
        source_path: ruta al PDF/xlsx (archivo temporal).
        user_id: ID del usuario autenticado.
        file_content: bytes del archivo (para subir a Storage). Si None, se lee del path.
    """
    parsed = parse_file(source_path)

    # ¿el archivo ya fue importado antes?
    existing = conn.execute(
        "SELECT id FROM statements WHERE file_sha256 = %s AND user_id = %s",
        (parsed.file_sha256, user_id),
    ).fetchone()
    if existing:
        return IngestResult(
            statement_id=existing["id"],
            file_already_imported=True,
            new_transactions=0,
            duplicate_transactions=0,
            auto_categorized=0,
            uncategorized=0,
            recurring_groups_detected=0,
        )

    # Subir a Supabase Storage (best-effort, no bloquea si falla)
    try:
        content = file_content if file_content is not None else source_path.read_bytes()
        storage.upload_statement(user_id, source_path.name, content)
    except Exception:
        pass  # fallo de storage no interrumpe la ingesta

    # Insertar/encontrar accounts (uno por (bank, card_last4))
    accounts_seen: dict[str, int] = {}
    for tx in parsed.transactions:
        if tx.card_last4 in accounts_seen:
            continue
        accounts_seen[tx.card_last4] = _upsert_account(
            conn, user_id, parsed.bank, tx.card_last4, tx.holder_name
        )

    if not accounts_seen:
        raise ValueError("Statement no contiene cuentas válidas")
    titular_account_id = next(iter(accounts_seen.values()))

    # Dedup por (user_id, account_id, period_end): atrapa re-uploads del mismo
    # resumen con file_sha256 distinto (PDF re-generado, distinta compresión, etc).
    # Solo aplicamos el chequeo si tenemos period_end del parser.
    if parsed.period_end:
        existing_period = conn.execute(
            """SELECT id FROM statements
               WHERE user_id = %s AND account_id = %s AND period_end = %s""",
            (user_id, titular_account_id, parsed.period_end.isoformat()),
        ).fetchone()
        if existing_period:
            return IngestResult(
                statement_id=existing_period["id"],
                file_already_imported=True,
                new_transactions=0,
                duplicate_transactions=0,
                auto_categorized=0,
                uncategorized=0,
                recurring_groups_detected=0,
            )

    cur = conn.execute(
        """INSERT INTO statements
           (user_id, account_id, period_start, period_end, due_date,
            source_filename, file_sha256, raw_total_ars, raw_total_usd)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            user_id,
            titular_account_id,
            parsed.period_start.isoformat() if parsed.period_start else None,
            parsed.period_end.isoformat() if parsed.period_end else None,
            parsed.due_date.isoformat() if parsed.due_date else None,
            parsed.source_filename,
            parsed.file_sha256,
            parsed.raw_total_ars,
            parsed.raw_total_usd,
        ),
    )
    statement_id = cur.lastrowid

    new_count = dup_count = 0
    for tx in parsed.transactions:
        acc_id = accounts_seen[tx.card_last4]
        desc_norm = normalize_description(tx.description_raw)
        h = tx_hash(acc_id, tx.comprobante, tx.amount, tx.posted_at)
        conn.savepoint("sp_tx")
        try:
            conn.execute(
                """INSERT INTO transactions
                   (user_id, statement_id, account_id, posted_at, description_raw,
                    description_normalized, comprobante, amount, currency,
                    installment_current, installment_total, hash, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    user_id,
                    statement_id,
                    acc_id,
                    tx.posted_at.isoformat(),
                    tx.description_raw,
                    desc_norm,
                    tx.comprobante,
                    tx.amount,
                    tx.currency,
                    tx.installment_current,
                    tx.installment_total,
                    h,
                    tx.notes,
                ),
            )
            conn.release("sp_tx")
            new_count += 1
        except psycopg2.errors.UniqueViolation:
            conn.rollback_to("sp_tx")
            conn.release("sp_tx")
            dup_count += 1

    auto = categorize_pending(conn, user_id)
    inherit_participants_for_installments(conn, user_id)

    detected = recompute(conn, user_id)
    link_transactions(conn, user_id)

    uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND user_id = %s",
        (user_id,),
    ).fetchone()[0]

    return IngestResult(
        statement_id=statement_id,
        file_already_imported=False,
        new_transactions=new_count,
        duplicate_transactions=dup_count,
        auto_categorized=auto,
        uncategorized=uncat,
        recurring_groups_detected=detected,
    )


def inherit_participants_for_installments(conn, user_id: str) -> int:
    candidates = conn.execute(
        """SELECT t.id, t.amount, t.description_normalized, t.comprobante, t.installment_total,
                  t.notes,
                  (SELECT COUNT(*) FROM tx_participants p WHERE p.transaction_id = t.id) AS n_parts
           FROM transactions t
           WHERE t.installment_current IS NOT NULL
             AND t.installment_total IS NOT NULL
             AND t.installment_current > 1
             AND t.user_id = %s""",
        (user_id,),
    ).fetchall()
    if not candidates:
        return 0

    inherited = 0
    for cand in candidates:
        needs_parts = cand["n_parts"] == 0
        needs_note = not (cand["notes"] and cand["notes"].strip())
        if not (needs_parts or needs_note):
            continue

        prev = conn.execute(
            """SELECT t.id, t.amount, t.notes,
                      (SELECT COUNT(*) FROM tx_participants p WHERE p.transaction_id = t.id) AS n_parts
               FROM transactions t
               WHERE t.id != %s
                 AND t.description_normalized = %s
                 AND COALESCE(t.comprobante, '') = COALESCE(%s, '')
                 AND t.installment_total = %s
                 AND t.user_id = %s
                 AND (
                   EXISTS (SELECT 1 FROM tx_participants p WHERE p.transaction_id = t.id)
                   OR (t.notes IS NOT NULL AND TRIM(t.notes) != '')
                 )
               ORDER BY t.installment_current DESC, t.posted_at DESC
               LIMIT 1""",
            (cand["id"], cand["description_normalized"], cand["comprobante"],
             cand["installment_total"], user_id),
        ).fetchone()
        if not prev:
            continue

        did_something = False

        if needs_note and prev["notes"] and prev["notes"].strip():
            conn.execute(
                "UPDATE transactions SET notes = %s WHERE id = %s",
                (prev["notes"], cand["id"]),
            )
            did_something = True

        if needs_parts and prev["n_parts"] > 0 and prev["amount"]:
            ratio = cand["amount"] / prev["amount"]
            parts = conn.execute(
                "SELECT person_name, amount_owed, sort_order FROM tx_participants WHERE transaction_id = %s",
                (prev["id"],),
            ).fetchall()
            for p in parts:
                conn.execute(
                    """INSERT INTO tx_participants
                       (transaction_id, person_name, amount_owed, paid_back, sort_order)
                       VALUES (%s, %s, %s, 0, %s)""",
                    (cand["id"], p["person_name"], p["amount_owed"] * ratio, p["sort_order"]),
                )
            total_owed = sum(p["amount_owed"] * ratio for p in parts)
            my_share = max(0.0, min(1.0, 1.0 - total_owed / cand["amount"]))
            names_str = ", ".join(p["person_name"] for p in parts) or None
            conn.execute(
                "UPDATE transactions SET my_share_pct = %s, share_with = %s WHERE id = %s",
                (my_share, names_str, cand["id"]),
            )
            did_something = True

        if did_something:
            inherited += 1
    return inherited


def _upsert_account(conn, user_id: str, bank: str, card_last4: str, holder_name: str | None) -> int:
    existing = conn.execute(
        "SELECT id, holder_name FROM accounts WHERE user_id = %s AND bank = %s AND card_last4 = %s",
        (user_id, bank, card_last4),
    ).fetchone()
    if existing:
        if holder_name and not existing["holder_name"]:
            conn.execute(
                "UPDATE accounts SET holder_name = %s WHERE id = %s",
                (holder_name, existing["id"]),
            )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO accounts (user_id, bank, card_last4, holder_name) VALUES (%s, %s, %s, %s) RETURNING id",
        (user_id, bank, card_last4, holder_name),
    )
    return cur.lastrowid or 0
