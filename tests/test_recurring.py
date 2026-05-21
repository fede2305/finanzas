"""Tests del detector de gastos recurrentes."""

import sqlite3
from datetime import date, timedelta

import pytest

from finanzas import db
from finanzas.recurring import recompute, recurring_merchant_key


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("FINANZAS_DB", str(path))
    db.init_db(path)
    yield path


def _account(conn):
    cur = conn.execute("INSERT INTO accounts (bank, card_last4) VALUES ('test', '0000')")
    return cur.lastrowid


def _tx(conn, acc, when, desc, amount):
    h = f"{acc}_{desc}_{amount}_{when}"
    conn.execute(
        """INSERT INTO transactions (account_id, posted_at, description_raw, description_normalized,
           comprobante, amount, currency, hash) VALUES (?, ?, ?, ?, ?, ?, 'ARS', ?)""",
        (acc, when, desc, desc.lower(), "X", amount, h),
    )


def test_merchant_key_strips_merpago_prefix():
    assert recurring_merchant_key("MERPAGO*COTO ZAPIOLA") == "coto"
    assert recurring_merchant_key("MERPAGO*CARREFOUR VICENTE LOPEZ") == "carrefour"
    assert recurring_merchant_key("NETFLIX.COM 6212556") == "netflix"
    assert recurring_merchant_key("CLAUDE.AI SUBSCR") == "claude"


def test_detects_monthly_recurrence(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _account(conn)
        # 4 ocurrencias mensuales del mismo merchant con monto similar
        base = date(2026, 1, 5)
        for i in range(4):
            d = base.replace(month=base.month + i)
            _tx(conn, acc, d.isoformat(), "MERPAGO*NETFLIX", 8000.0 + i * 50)
        n = recompute(conn)
        assert n == 1
        g = conn.execute("SELECT normalized_merchant, occurrences, cadence_days, status FROM recurring_groups").fetchone()
        assert g[0] == "netflix"
        assert g[1] == 4
        assert 28 <= g[2] <= 32
        assert g[3] == "suggested"


def test_does_not_detect_when_only_2_occurrences(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _account(conn)
        _tx(conn, acc, "2026-01-05", "OneOff", 1000.0)
        _tx(conn, acc, "2026-02-05", "OneOff", 1000.0)
        n = recompute(conn)
        assert n == 0


def test_does_not_detect_when_amount_varies_too_much(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _account(conn)
        # Mismo merchant, mismo mes, pero montos muy distintos (no es fijo, es gasto eventual)
        _tx(conn, acc, "2026-01-05", "VARIANTE", 1000.0)
        _tx(conn, acc, "2026-02-05", "VARIANTE", 20000.0)
        _tx(conn, acc, "2026-03-05", "VARIANTE", 500.0)
        n = recompute(conn)
        assert n == 0


def test_does_not_detect_when_cadence_is_not_monthly(fresh_db):
    with db.connect(fresh_db) as conn:
        acc = _account(conn)
        # Cadencia semanal — no debería considerarse fijo mensual
        for i in range(4):
            d = (date(2026, 4, 1) + timedelta(days=i * 7)).isoformat()
            _tx(conn, acc, d, "Weekly", 500.0)
        n = recompute(conn)
        assert n == 0
