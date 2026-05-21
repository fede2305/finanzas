"""Detecta el banco/formato del archivo y delega al parser apropiado."""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from finanzas.models import ParsedStatement


def parse_file(path: str | Path) -> ParsedStatement:
    """Punto de entrada único: detecta y parsea un resumen."""
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix == ".xlsx":
        # Por ahora, los xlsx que manejamos son de Santander. Si en el futuro hay otros,
        # detectaremos por contenido.
        from finanzas.parsers.santander_xlsx import parse as parse_xlsx
        return parse_xlsx(p)

    if suffix == ".pdf":
        bank = _detect_pdf_bank(p)
        if bank == "santander_rio":
            from finanzas.parsers.santander_pdf import parse as parse_santander
            return parse_santander(p)
        if bank == "galicia":
            from finanzas.parsers.galicia_pdf import parse as parse_galicia
            return parse_galicia(p)
        raise ValueError(f"No pude detectar el banco del PDF: {p.name}")

    raise ValueError(f"Formato no soportado: {p.suffix}")


def _detect_pdf_bank(path: Path) -> str:
    """Inspecciona el texto del PDF buscando marcas del banco.

    El logo de Galicia es imagen — el texto recién aparece en página 2 o por CUIT.
    CUITs distintivos: Santander Río = 30-50000845, Galicia = 30-50000173.
    """
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:3]:
            text = (page.extract_text() or "").lower()
            if "santander" in text or "30-50000845" in text:
                return "santander_rio"
            if "galicia" in text or "30-50000173" in text:
                return "galicia"
    return "unknown"
