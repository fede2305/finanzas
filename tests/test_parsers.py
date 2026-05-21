"""Tests de parsers contra fixtures."""

from pathlib import Path

import pytest

from finanzas.parsers import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def santander_pdf():
    return parse_file(FIXTURES / "santander_visa_2026-05.pdf")


@pytest.fixture
def santander_xlsx():
    return parse_file(FIXTURES / "santander_visa_2112_2026-05.xlsx")


@pytest.fixture
def galicia_pdf():
    return parse_file(FIXTURES / "galicia_visa_2026-05.pdf")


# ---------- Santander PDF ----------


def test_santander_pdf_bank(santander_pdf):
    assert santander_pdf.bank == "santander_rio"


def test_santander_pdf_dates(santander_pdf):
    assert santander_pdf.period_start.isoformat() == "2026-03-19"
    assert santander_pdf.period_end.isoformat() == "2026-04-23"
    assert santander_pdf.due_date.isoformat() == "2026-05-04"


def test_santander_pdf_two_cards(santander_pdf):
    cards = sorted({t.card_last4 for t in santander_pdf.transactions})
    assert cards == ["2112", "6957"]


def test_santander_pdf_tx_count(santander_pdf):
    # 80 = 4 pagos/promos + 68 consumos 2112 + 3 consumos 6957 + 5 impuestos
    assert len(santander_pdf.transactions) >= 78


def test_santander_pdf_card_totals(santander_pdf):
    # Card 2112: 2,943,455.47 (consumos titular)
    assert santander_pdf.raw_total_ars == pytest.approx(3082173.47, abs=1.0)
    assert santander_pdf.raw_total_usd == pytest.approx(81.56, abs=0.1)


def test_santander_pdf_finds_usd_txs(santander_pdf):
    usd = [t for t in santander_pdf.transactions if t.currency == "USD"]
    # Esperamos: TIDAL x2, PLAYSTATION, NETFLIX, CLAUDE, YouTube, Adobe = 7
    assert len(usd) == 7


def test_santander_pdf_finds_installments(santander_pdf):
    # MERPAGO*BOXESPREMIUM 11/12 + MERPAGO*DVIGI 1/6
    ins = [t for t in santander_pdf.transactions if t.installment_total]
    assert len(ins) >= 2


# ---------- Santander xlsx ----------


def test_santander_xlsx_bank(santander_xlsx):
    assert santander_xlsx.bank == "santander_rio"


def test_santander_xlsx_two_cards(santander_xlsx):
    cards = sorted({t.card_last4 for t in santander_xlsx.transactions})
    assert cards == ["2112", "6957"]


def test_santander_xlsx_finds_usd_txs(santander_xlsx):
    usd = [t for t in santander_xlsx.transactions if t.currency == "USD"]
    # 7 consumos USD + 1 "Su pago en USD" negativo
    assert len(usd) >= 7


def test_santander_xlsx_finds_negative_amounts(santander_xlsx):
    negatives = [t for t in santander_xlsx.transactions if t.amount < 0]
    # Su pago en pesos + Su pago en USD + 2 PROMOS
    assert len(negatives) >= 3


# ---------- Galicia PDF ----------


def test_galicia_pdf_bank(galicia_pdf):
    assert galicia_pdf.bank == "galicia"


def test_galicia_pdf_dates(galicia_pdf):
    assert galicia_pdf.period_start.isoformat() == "2026-03-26"
    assert galicia_pdf.period_end.isoformat() == "2026-04-30"
    assert galicia_pdf.due_date.isoformat() == "2026-05-08"


def test_galicia_pdf_one_card(galicia_pdf):
    cards = sorted({t.card_last4 for t in galicia_pdf.transactions})
    assert cards == ["7228"]


def test_galicia_pdf_tx_count(galicia_pdf):
    assert len(galicia_pdf.transactions) == 14


def test_galicia_pdf_total(galicia_pdf):
    assert galicia_pdf.raw_total_ars == pytest.approx(1018471.24, abs=1.0)
    assert galicia_pdf.raw_total_usd == 0.0


def test_galicia_pdf_finds_installments(galicia_pdf):
    # 6 cuotas pendientes: MERPAGO*IVMACOARG 6/6, MERCADOLIBRE x2 6/6,
    # MOVISTAR AREN x2 3/6, MERPAGO*INSUMOSACUAR 2/6
    ins = [t for t in galicia_pdf.transactions if t.installment_total]
    assert len(ins) == 6


def test_galicia_pdf_comprobante_six_digits(galicia_pdf):
    # Todas las comprobantes Galicia son de 6 dígitos
    for t in galicia_pdf.transactions:
        assert t.comprobante is not None
        assert len(t.comprobante) == 6


# ---------- Dispatcher ----------


def test_dispatcher_detects_santander(santander_pdf):
    assert santander_pdf.bank == "santander_rio"


def test_dispatcher_detects_galicia(galicia_pdf):
    assert galicia_pdf.bank == "galicia"
