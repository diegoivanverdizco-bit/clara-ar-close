"""Proposed corrections (docs/2026-11-17 Diseño corrección de errores de captura.md, delivery 2).

Every error the load collects gets a category and, when there is one, candidates computed by the code:
  a  safe: one reading and a check that passes; can be approved in bulk
  b  to confirm: one or more candidates, each with its evidence; approved one by one
  c  not corrected, only reported
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

@pytest.mark.parametrize("texto", ["290,000.00", "$290,000.00", "$ 290,000.00", "$290000"])
def test_monto_con_formato_inequivoco_es_seguro(saboteado, texto):
    p = uno(saboteado(celda(B, 3, 6, texto)))
    assert p.categoria == "a" and len(p.candidatos) == 1
    assert cambios(p.candidatos[0]) == [(B, "F3", texto, Decimal("290000") if "." not in texto else Decimal("290000.00"))]


def test_tasa_16_que_cuadra_con_el_iva_es_segura(saboteado):
    p = uno(saboteado(celda(C, 2, 9, 16)))
    assert p.categoria == "a"
    assert cambios(p.candidatos[0]) == [(C, "I2", 16, Decimal("0.16"))]
    assert "250,000.00 x 0.16 = IVA 40,000.00" in p.candidatos[0].porque


def test_tasa_escrita_como_porcentaje_es_segura(saboteado):
    p = uno(saboteado(celda(C, 2, 9, "16%")))
    assert p.categoria == "a" and cambios(p.candidatos[0]) == [(C, "I2", "16%", Decimal("0.16"))]


def test_cfdi_repetido_identico_con_uuid_valido_es_seguro(saboteado):
    p = uno(saboteado(repetir(C, 2)))
    assert p.categoria == "a" and len(p.candidatos) == 1
    assert p.candidatos[0].quitar_fila == (C, p.fila) and p.fila > 2


# ---- (b) to confirm -------------------------------------------------------------------------------------

def test_monto_ambiguo_da_las_dos_lecturas(saboteado):
    p = uno(saboteado(celda(B, 3, 6, "1,500")))
    assert p.categoria == "b"
    assert [c.cambios[0].despues for c in p.candidatos] == [Decimal("1500"), Decimal("1.500")]


@pytest.mark.parametrize("texto,valor", [("1.500,00", Decimal("1500.00")), ("290000,00", Decimal("290000.00")),
                                         ("290 000", Decimal("290000"))])
def test_otros_formatos_se_confirman(saboteado, texto, valor):
    p = uno(saboteado(celda(B, 3, 6, texto)))
    assert p.categoria == "b" and [c.cambios[0].despues for c in p.candidatos] == [valor]


def test_en_un_cfdi_primero_la_lectura_que_cuadra(saboteado):
    # 250,000 can be 250000 or 250.000; only 250000 gives subtotal + IVA = total
    p = uno(saboteado(celda(C, 2, 8, "250,000")))
    assert p.categoria == "b"
    assert p.candidatos[0].cambios[0].despues == Decimal("250000")
    assert "subtotal + IVA = total" in p.candidatos[0].porque


def test_tasa_16_que_no_cuadra_se_confirma(saboteado):
    p = uno(saboteado(celda(C, 2, 9, 16), celda(C, 2, 10, 1000), celda(C, 2, 11, 251000)))
    assert p.categoria == "b" and p.candidatos[0].cambios[0].despues == Decimal("0.16")
    assert "IVA exception" in p.candidatos[0].porque


def test_total_vacio_se_calcula_con_los_otros_dos(saboteado):
    p = uno(saboteado(celda(C, 2, 11, None)))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(C, "K2", None, Decimal("290000"))]


def test_cfdi_negativo_propone_el_positivo(saboteado):
    p = uno(saboteado(celda(C, 2, 8, -250000)))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(C, "H2", -250000, Decimal("250000"))]


def test_deposito_negativo_propone_positivo_o_cambiar_de_columna(saboteado):
    p = uno(saboteado(celda(B, 3, 6, -290000)))
    assert p.categoria == "b"
    assert cambios(p.candidatos[0]) == [(B, "F3", -290000, Decimal("290000"))]
    assert cambios(p.candidatos[1]) == [(B, "F3", -290000, None), (B, "E3", None, Decimal("290000"))]


def test_cargo_y_abono_en_la_misma_fila_se_parte_en_dos(saboteado):
    p = uno(saboteado(celda(B, 3, 5, 1000)))
    assert p.categoria == "b"
    c = p.candidatos[0]
    assert cambios(c) == [(B, "F3", 290000, None)]
    hoja, valores = c.agregar_fila
    assert hoja == B and valores[4] is None and valores[5] == 290000 and valores[3] == "SPEI RECIBIDO GPO ALFA LOGISTICA PAGO FACT A-1001"


@pytest.mark.parametrize("fila", [
    [datetime(2026, 9, 1), "MXN-001", "", "SALDO INICIAL", None, 1500000, "MXN"],
    ["TOTAL", None, None, None, 1, 2, None],
], ids=["balance line", "totals row"])
def test_lineas_que_no_son_movimientos_se_quitan(saboteado, fila):
    p = uno(saboteado(agregar(B, fila)))
    assert p.categoria == "b" and p.candidatos[0].quitar_fila == (B, p.fila)


def test_moneda_vacia_toma_la_de_la_cuenta(saboteado):
    p = uno(saboteado(celda(B, 6, 7, None)))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(B, "G6", None, "USD")]


@pytest.mark.parametrize("valor,esperado", [(1.8, Decimal("18.0")), (174.885, Decimal("17.4885")),
                                            (0.0571803, Decimal("17.4885"))], ids=["x10", "/10", "inverted"])
def test_tc_fuera_de_rango_propone_el_que_cae_cerca_de_la_mediana(saboteado, valor, esperado):
    p = uno(saboteado(celda(T, 40, 2, valor)))
    assert p.categoria == "b" and len(p.candidatos) == 1
    assert p.candidatos[0].cambios[0].celda == "B40"
    assert abs(p.candidatos[0].cambios[0].despues - esperado) < Decimal("0.001")


def test_tabla_fix_al_reves_propone_invertir_cada_fila(saboteado):
    with pytest.raises(SchemaError) as e:
        cargar(saboteado(lambda wb: [setattr(r[1], "value", 1 / r[1].value) for r in wb[T].iter_rows(min_row=2)
                                     if isinstance(r[1].value, (int, float))]))
    assert all(p.categoria == "b" and "1 / rate" in p.candidatos[0].porque for p in e.value.problemas)


def test_misma_fecha_fix_dos_veces_propone_quitar_la_que_se_aleja(saboteado):
    p = uno(saboteado(agregar(T, [datetime(2026, 9, 10), 19.5])))
    assert p.categoria == "b" and len(p.candidatos) == 2
    assert p.candidatos[0].quitar_fila == (T, p.fila)          # 19.5 is the one far from the median


def test_factura_mxn_con_tc_propone_vaciar_o_usd(saboteado):
    p = uno(saboteado(celda(C, 12, 7, 17.5)))
    assert p.categoria == "b"
    assert {tuple(cambios(c)) for c in p.candidatos} == {((C, "G12", 17.5, None),), ((C, "F12", "MXN", "USD"),)}
    assert cambios(p.candidatos[0]) == [(C, "F12", "MXN", "USD")]   # 17.5 is close to the FIX of that day


def test_tipo_de_cfdi_escrito_en_palabras(saboteado):
    p = uno(saboteado(celda(C, 10, 14, "Factura")))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(C, "N10", "Factura", "I")]


def test_tipo_p_con_total_propone_i(saboteado):
    p = uno(saboteado(celda(C, 10, 14, "T")))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(C, "N10", "T", "I")]


def test_nota_de_credito_sin_relacionado_propone_facturas_del_cliente_o_tipo_i(saboteado, datos_caso):
    uuid_a1001 = openpyxl.load_workbook(datos_caso)[C].cell(2, 1).value
    p = uno(saboteado(celda(C, 10, 14, "E")))
    assert p.categoria == "b"
    assert [cambios(c) for c in p.candidatos] == [[(C, "O10", None, uuid_a1001.upper())], [(C, "N10", "E", "I")]]


def test_apertura_en_otra_moneda_que_su_cfdi(saboteado):
    p = uno(saboteado(celda(A, 5, 4, "MXN")))
    assert p.categoria == "b" and cambios(p.candidatos[0]) == [(A, "D5", "MXN", "USD")]


def test_apertura_mxn_con_saldo_mxn_distinto(saboteado):
    p = uno(saboteado(celda(A, 2, 7, 2900000)))
    assert p.categoria == "b"
    assert [cambios(c) for c in p.candidatos] == [[(A, "G2", 2900000, 290000)], [(A, "E2", 290000, 2900000)]]


def test_apertura_usd_que_no_cuadra_propone_primero_el_tc_creible(saboteado):
    p = uno(saboteado(celda(A, 5, 6, 1.8)))
    assert p.categoria == "b"
    assert cambios(p.candidatos[0]) == [(A, "F5", 1.8, Decimal("17.6403"))]
    assert cambios(p.candidatos[1]) == [(A, "G5", 409254.96, Decimal("41760.00"))]


def test_folio_repetido_con_datos_distintos_elige_cual_se_queda(saboteado):
    def pegar_distinta(wb):
        valores = [c.value for c in wb[C][2]]
        valores[0] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        wb[C].append(valores)
    p = uno(saboteado(pegar_distinta))
    assert p.categoria == "b"
    assert [c.quitar_fila for c in p.candidatos] == [(C, p.fila), (C, 2)]


# ---- (c) only reported, (d) structure -------------------------------------------------------------------

@pytest.mark.parametrize("mutar", [celda(B, 3, 6, "#VALUE!"), celda(B, 3, 6, "nan"), celda(B, 3, 7, "EUR"),
                                   celda(C, 2, 3, "07/15/2026"), celda(C, 2, 2, None), celda(A, 2, 5, -290000)],
                         ids=["excel error", "nan", "EUR", "date", "empty folio", "negative opening"])
def test_lo_que_no_se_puede_adivinar_solo_se_avisa(saboteado, mutar):
    p = uno(saboteado(mutar))
    assert p.categoria == "c" and p.candidatos == []


def test_errores_de_estructura_son_d(saboteado):
    p = uno(saboteado(lambda wb: wb.remove(wb[A])))
    assert p.categoria == "d" and p.candidatos == []


def test_los_candidatos_los_calcula_el_codigo_sin_ia(saboteado, monkeypatch):
    # nothing in the proposals may call a language model
    import cierre_cxc.asistente_llm as llm
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: (_ for _ in ()).throw(AssertionError("LLM called")))
    p = uno(saboteado(celda(C, 2, 9, 16)))
    assert p.categoria == "a"
