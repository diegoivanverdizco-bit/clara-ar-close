"""Proposed corrections (docs/2026-11-17 Diseño corrección de errores de captura.md, simplified by Diego, L87).

Every error the load collects gets a category and, when there is one, a single correction computed by the code:
  a  safe: an amount typed as text in Mexican format, an IVA rate typed as a percentage that checks, a row pasted
     twice exactly; can be approved in bulk
  c  not corrected, only reported (nothing is guessed)
  d  structure: no row can be read until it is fixed
"""
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from cierre_cxc.cargar import SchemaError, cargar

B, C, R, A, T = "Bancos_Sep2026", "CFDI_Emitidos", "REP_Emitidos", "AR_Apertura_31Ago", "TC_FIX_Diario"


@pytest.fixture
def saboteado(datos_caso, tmp_path):
    def hacer(*mutaciones):
        wb = openpyxl.load_workbook(datos_caso)
        for m in mutaciones:
            m(wb)
        ruta = tmp_path / "saboteado.xlsx"
        wb.save(ruta)
        return ruta
    return hacer


def celda(hoja, fila, col, valor):
    def m(wb):
        wb[hoja].cell(fila, col).value = valor
    return m


def agregar(hoja, fila):
    return lambda wb: wb[hoja].append(fila)


def repetir(hoja, n):
    return lambda wb: wb[hoja].append([c.value for c in wb[hoja][n]])


def uno(ruta):
    """The only problem of the file."""
    with pytest.raises(SchemaError) as e:
        cargar(ruta)
    assert len(e.value.problemas) == 1, [p.mensaje for p in e.value.problemas]
    return e.value.problemas[0]


def cambios(candidato):
    return [(c.hoja, c.celda, c.antes, c.despues) for c in candidato.cambios]


# ---- (a) safe -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("texto,valor", [
    ("290,000.00", Decimal("290000.00")), ("$290,000.00", Decimal("290000.00")),
    ("$ 290,000.00", Decimal("290000.00")), ("$290000", Decimal("290000")), ("$116,000.00", Decimal("116000.00")),
    ("69,600", Decimal("69600")), ("1,500", Decimal("1500")), ("$1,234,567.5", Decimal("1234567.5"))])
def test_monto_en_formato_de_mexico_es_seguro_con_una_sola_lectura(saboteado, texto, valor):
    p = uno(saboteado(celda(B, 3, 6, texto)))
    assert p.categoria == "a" and len(p.candidatos) == 1
    (cam,) = cambios(p.candidatos[0])
    assert cam[:3] == (B, "F3", texto) and cam[3] == valor and isinstance(cam[3], Decimal)


def test_tasa_16_que_cuadra_con_el_iva_es_segura(saboteado):
    p = uno(saboteado(celda(C, 2, 9, 16)))
    assert p.categoria == "a"
    assert cambios(p.candidatos[0]) == [(C, "I2", 16, Decimal("0.16"))]
    assert "250,000.00 x 0.16 = IVA 40,000.00" in p.candidatos[0].porque


def test_cfdi_repetido_identico_con_uuid_valido_es_seguro(saboteado):
    p = uno(saboteado(repetir(C, 2)))
    assert p.categoria == "a" and len(p.candidatos) == 1
    assert p.candidatos[0].quitar_fila == (C, p.fila) and p.fila > 2


# ---- (c) only reported, with no candidates --------------------------------------------------------------

def pegar_con_otro_uuid(wb):
    valores = [c.value for c in wb[C][2]]
    valores[0] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    wb[C].append(valores)


def uuid_malo_y_repetido(wb):
    wb[C].cell(2, 1).value = "no es uuid"
    wb[C].append([c.value for c in wb[C][2]])


def tabla_fix_al_reves(wb):
    for r in wb[T].iter_rows(min_row=2):
        if isinstance(r[1].value, (int, float)):
            r[1].value = 1 / r[1].value


NO_SE_CORRIGE = {
    "european format": celda(B, 3, 6, "23.200,00"),
    "decimal comma": celda(B, 3, 6, "290000,00"),
    "spaces as thousands separators": celda(B, 3, 6, "290 000"),
    "negative text": celda(B, 3, 6, "-1,500"),
    "excel error": celda(B, 3, 6, "#VALUE!"),
    "nan": celda(B, 3, 6, "nan"),
    "currency EUR": celda(B, 3, 7, "EUR"),
    "date": celda(C, 2, 3, "07/15/2026"),
    "empty folio": celda(C, 2, 2, None),
    "rate typed as text": celda(C, 2, 9, "16%"),
    "negative opening": celda(A, 2, 5, -290000),
    "empty total": celda(C, 2, 11, None),
    "negative invoice": celda(C, 2, 8, -250000),
    "negative deposit": celda(B, 3, 6, -290000),
    "cargo and abono": celda(B, 3, 5, 1000),
    "empty currency": celda(B, 6, 7, None),
    "type in words": celda(C, 10, 14, "Factura"),
    "type with a total": celda(C, 10, 14, "T"),
    "credit note without related": celda(C, 10, 14, "E"),
    "MXN invoice with a rate": celda(C, 12, 7, 17.5),
    "opening in another currency": celda(A, 5, 4, "MXN"),
    "opening MXN balance differs": celda(A, 2, 7, 2900000),
    "opening USD does not tie": celda(A, 5, 6, 1.8),
    "rate x10 missing digit": celda(T, 40, 2, 1.8),
    "rate one digit too many": celda(T, 40, 2, 174.885),
    "rate inverted": celda(T, 40, 2, 0.0571803),
    "balance line": agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SALDO INICIAL", None, 1500000, "MXN"]),
    "totals row": agregar(B, ["TOTAL", None, None, None, 1, 2, None]),
    "FIX date twice": agregar(T, [datetime(2026, 9, 10), 19.5]),
    "FIX without a date": agregar(T, [None, 18.2]),
    "same folio, other data": pegar_con_otro_uuid,
    "identical row with an invalid UUID": uuid_malo_y_repetido,
    "whole FIX table inverted": tabla_fix_al_reves,
    "rate 16 that does not tie": lambda wb: [celda(C, 2, 9, 16)(wb), celda(C, 2, 10, 1000)(wb), celda(C, 2, 11, 251000)(wb)],
}


@pytest.mark.parametrize("mutar", NO_SE_CORRIGE.values(), ids=NO_SE_CORRIGE.keys())
def test_lo_que_no_es_obvio_solo_se_avisa_sin_candidatos(saboteado, mutar):
    with pytest.raises(SchemaError) as e:
        cargar(saboteado(mutar))
    assert e.value.problemas
    for p in e.value.problemas:
        assert p.categoria == "c" and p.candidatos == [], p.mensaje


def test_tc_fuera_de_rango_avisa_con_su_mensaje_y_sin_candidatos(saboteado):
    p = uno(saboteado(celda(T, 40, 2, 1.8)))
    assert p.categoria == "c" and p.candidatos == [] and "row 40" in p.mensaje and "is outside" in p.mensaje


def test_un_monto_de_un_cfdi_en_texto_es_seguro(saboteado):
    p = uno(saboteado(celda(C, 2, 8, "250,000")))
    assert p.categoria == "a" and cambios(p.candidatos[0]) == [(C, "H2", "250,000", Decimal("250000"))]


def test_errores_de_estructura_son_d(saboteado):
    p = uno(saboteado(lambda wb: wb.remove(wb[A])))
    assert p.categoria == "d" and p.candidatos == []


def test_los_candidatos_los_calcula_el_codigo_sin_ia(saboteado, monkeypatch):
    # nothing in the proposals may call a language model
    import cierre_cxc.asistente_llm as llm
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: (_ for _ in ()).throw(AssertionError("LLM called")))
    p = uno(saboteado(celda(C, 2, 9, 16)))
    assert p.categoria == "a"
