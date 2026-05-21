"""Tests del categorizer."""

import sqlite3

import pytest

from finanzas import db
from finanzas.categorizer import categorize_pending, add_learned_rule
from finanzas.parsers.base import normalize_description


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("FINANZAS_DB", str(path))
    db.init_db(path)
    yield path


def _insert_tx(conn, account_id, desc_raw, amount=1000.0, comp="123456"):
    desc_norm = normalize_description(desc_raw)
    h = f"{account_id}_{desc_norm}_{amount}_{comp}"
    cur = conn.execute(
        """INSERT INTO transactions (account_id, posted_at, description_raw, description_normalized,
           comprobante, amount, currency, hash) VALUES (?, '2026-04-01', ?, ?, ?, ?, 'ARS', ?)""",
        (account_id, desc_raw, desc_norm, comp, amount, h),
    )
    return cur.lastrowid


def _ensure_account(conn):
    cur = conn.execute(
        "INSERT INTO accounts (bank, card_last4) VALUES ('test', '0000')"
    )
    return cur.lastrowid


def test_carrefour_categorized_as_supermercado(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _ensure_account(conn)
        _insert_tx(conn, acc, "MERPAGO*CARREFOUR ZAPIOLA")
        n = categorize_pending(conn)
        assert n == 1
        row = conn.execute(
            "SELECT c.name, s.name FROM transactions t JOIN categories c ON c.id = t.category_id LEFT JOIN categories s ON s.id = t.subcategory_id"
        ).fetchone()
        assert row[0] == "Comida"
        assert row[1] == "Supermercado"


def test_netflix_categorized_as_streaming(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _ensure_account(conn)
        _insert_tx(conn, acc, "NETFLIX.COM 6212556")
        categorize_pending(conn)
        row = conn.execute(
            "SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id"
        ).fetchone()
        assert row[0] == "Suscripciones"


def test_learned_rule_applies_to_future(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _ensure_account(conn)
        # Carregamos una tx sin matchear ninguna seed rule
        tx1 = _insert_tx(conn, acc, "LUISA S", comp="A1")
        n0 = categorize_pending(conn)
        assert n0 == 0  # ninguna seed la categoriza

        # Aprendemos a categorizarla
        comida_id = conn.execute("SELECT id FROM categories WHERE name='Comida' AND parent_id IS NULL").fetchone()[0]
        add_learned_rule(conn, normalize_description("LUISA S"), comida_id, None)

        # Carregamos otra tx idéntica y vemos que se autocategoriza
        tx2 = _insert_tx(conn, acc, "LUISA S", amount=2000.0, comp="A2")
        n1 = categorize_pending(conn)
        assert n1 >= 1


def test_rule_priority_learned_beats_seed(fresh_db):
    """Una regla learned con priority 5000 debe ganarle a una seed."""
    with db.connect(fresh_db) as conn:
        acc = _ensure_account(conn)
        # Crear regla learned que reasigna "rappi" a Salud (insólito a propósito)
        salud_id = conn.execute("SELECT id FROM categories WHERE name='Salud' AND parent_id IS NULL").fetchone()[0]
        add_learned_rule(conn, "rappi", salud_id, None, match_type="exact")
        _insert_tx(conn, acc, "rappi", comp="rappiX")  # description_raw = "rappi" → norm = "rappi"
        categorize_pending(conn)
        row = conn.execute(
            "SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id"
        ).fetchone()
        assert row[0] == "Salud"  # gana la learned
