"""The tool must work on data it was not written against."""
from decimal import Decimal

import openpyxl
import pytest

from conftest import D
from dataset_sintetico import crear
from cierre_cxc.cargar import SchemaError, cargar
from cierre_cxc.pipeline import ejecutar


@pytest.fixture(scope="module")
def nuevo(tmp_path_factory):
    ruta = crear(tmp_path_factory.mktemp("sint") / "oct2026.xlsx")
    return ejecutar(ruta)


def _ap(s, folio):
    return [a for a in s.res.aplicaciones if a.folio == folio]


def test_corre_sin_datos_del_caso_y_cuadra(nuevo):
    assert nuevo.res.cierre.isoformat() == "2026-10-31"
    fallos = [(n, d) for n, ok, d in nuevo.controles if not ok]
    assert not fallos, fallos


def test_referencia_en_la_descripcion(nuevo):
    a = _ap(nuevo, "B-2001")[0]
    assert (a.tipo, a.confianza, a.saldo_despues) == ("exacto", "alta", 0)


def test_cobro_neto_de_nota_de_credito(nuevo):
    a = _ap(nuevo, "B-2002")[0]
    assert a.aplicado_doc == D(220400) and a.saldo_despues == 0
    assert nuevo.res.notas_credito[0].folio_factura == "B-2002"


def test_cobro_en_usd(nuevo):
    a = _ap(nuevo, "B-2003")[0]
    assert (a.pago_moneda, a.doc_moneda, a.aplicado_doc) == ("USD", "USD", D(10000))


def test_un_cobro_para_dos_facturas(nuevo):
    folios = sorted(a.folio for a in nuevo.res.aplicaciones if a.mov_id == "B06")
    assert folios == ["B-2004", "B-2005"]
    assert all(a.tipo == "multiple" for a in nuevo.res.aplicaciones if a.mov_id == "B06")


def test_cruzado_con_diferencia_de_tasa_menor_al_medio_punto(nuevo):
    a = _ap(nuevo, "B-2007")[0]
    assert a.tipo == "cruzado" and a.saldo_despues == 0
    assert a.fx_realizada != 0


def test_pago_corto_por_menos_del_2_por_ciento(nuevo):
    a = _ap(nuevo, "B-2006")[0]
    assert a.tipo == "corto" and a.saldo_despues == D(500)


def test_sobrepago_deja_efectivo_sin_aplicar(nuevo):
    a = _ap(nuevo, "B-2008")[0]
    assert a.tipo == "sobrepago" and a.saldo_despues == 0
    sobrante = [u for u in nuevo.res.efectivo_no_identificado if u["monto"] == D(34000)]
    assert sobrante, nuevo.res.efectivo_no_identificado


def test_sin_pistas_queda_sin_identificar_y_no_se_inventa(nuevo):
    montos = sorted(u["monto"] for u in nuevo.res.efectivo_no_identificado)
    assert montos == [D(15000), D(34000)]


def test_clasificacion_de_movimientos_no_cxc(nuevo):
    ids_no = {m.descripcion for m in nuevo.res.no_cxc}
    assert "PAGO NOMINA QNA" in ids_no and "INTERESES GANADOS" in ids_no
    assert {m.descripcion for m in nuevo.res.comisiones} == {"COMISION MANEJO DE CUENTA", "IVA COMISION"}


def test_cancelada_y_pue_abierto_se_marcan(nuevo):
    cats = {(e.categoria, e.referencia) for e in nuevo.excepciones}
    assert ("Cancelled CFDI", "B-2009") in cats
    assert ("PUE not collected", "B-2008") not in cats      # it was paid (overpaid) in the period
    assert ("Missing REP", "B-2004") in cats and ("Missing REP", "B-2001") not in cats
    assert ("Missing REP", "B-2008") not in cats            # PUE paid in its month of issuance: no REP (B3, G3)


def test_antiguedad_no_incluye_cancelada_ni_pagadas(nuevo):
    folios = {f["folio"] for f in nuevo.aging["filas"]}
    assert "B-2009" not in folios and "B-2001" not in folios and "B-2002" not in folios


def test_tipo_de_cambio_faltante_usa_el_dia_anterior(tmp_path):
    ruta = crear(tmp_path / "x.xlsx")
    s = ejecutar(ruta)
    assert any("No FIX rate" in a for a in s.res.avisos)


def test_columna_obligatoria_ausente_da_error_claro(tmp_path):
    ruta = crear(tmp_path / "x.xlsx")
    wb = openpyxl.load_workbook(ruta)
    ws = wb["Bancos_Oct2026"]
    ws["F1"] = "Monto entrada"                  # the 'Abono' column is renamed to an unknown name
    wb.save(ruta)
    with pytest.raises(SchemaError) as e:
        cargar(ruta)
    assert "abono" in str(e.value)


def test_etiquetas_con_las_fechas_del_periodo(tmp_path):
    """The roll-forward and the Summary used to say 31-Aug and 30-Sep for any month."""
    s = ejecutar(crear(tmp_path / "x.xlsx"))
    assert s.rollforward["lineas"][0][0] == "Opening balance (booked, 30-Sep)"


def test_archivo_inexistente(tmp_path):
    with pytest.raises(SchemaError):
        cargar(tmp_path / "no_existe.xlsx")
