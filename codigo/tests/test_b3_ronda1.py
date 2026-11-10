"""Judgment Day round 1 of task B3: one test group per confirmed finding (C1, C2, G1, G2, G4, G5)."""
import sys
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import openpyxl
import pytest

from conftest import D
from cierre_cxc.pipeline import ejecutar

NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def u(folio):
    return str(uuid.uuid5(NS, folio)).upper()


def libro(ruta, facturas=(), bancos=(), reps=(), apertura=(), fix=17.3, fix_dias=None):
    """Smallest workbook with the case schema. `facturas` rows are lists in the CFDI_Emitidos column order
    after the uuid (folio, fecha, rfc, cliente, moneda, tc, subtotal, tasa, iva, total, metodo, forma, tipo, rel, estatus, notas)."""
    wb = openpyxl.Workbook()
    wb.active.title = "README"
    c = wb.create_sheet("CFDI_Emitidos")
    c.append(["uuid", "Folio", "Fecha Emisión", "RFC Receptor", "Cliente", "Moneda", "Tipo de Cambio", "Subtotal",
              "Tasa IVA", "IVA", "Total", "Metodo Pago", "Forma Pago", "Tipo Comprobante", "CFDI Relacionado (UUID)",
              "Estatus SAT", "Notas"])
    for f in facturas:
        c.append([u(f[0])] + list(f))
    b = wb.create_sheet("Bancos_Oct2026")
    b.append(["Fecha", "Cuenta", "Referencia", "Concepto", "Cargo", "Abono", "Moneda"])
    for f in bancos:
        b.append(list(f))
    r = wb.create_sheet("REP_Emitidos")
    r.append(["UUID REP", "Fecha de pago", "UUID factura relacionada", "Serie-Folio", "Num. parcialidad", "Saldo anterior",
              "Importe pagado", "Saldo insoluto", "Moneda de pago", "TC pago"])
    for f in reps:
        r.append(list(f))
    a = wb.create_sheet("AR_Apertura_30Sep")
    a.append(["Serie-Folio", "Fecha emision", "Cliente", "Moneda", "Saldo (moneda origen)", "TC 30-Sep", "Saldo MXN"])
    for f in apertura:
        a.append(list(f))
    t = wb.create_sheet("TC_FIX_Diario")
    t.append(["Fecha", "Tipo de cambio aplicable (MXN por USD)"])
    d = date(2026, 9, 25)
    while d <= date(2026, 11, 3):
        t.append([datetime(d.year, d.month, d.day), (fix_dias or {}).get(d, fix)])
        d += timedelta(days=1)
    wb.create_sheet("Catalogo_Cuentas").append(["Cuenta", "Nombre"])
    wb.save(ruta)
    return ruta


def mxn(folio, dia, sub, cliente="Textiles SA", rfc="TRI100101AA1", metodo="PPD", forma="99", estatus="Vigente"):
    iva = (Decimal(str(sub)) * Decimal("0.16")).quantize(Decimal("0.01"))
    return [folio, datetime(2026, 10, dia), rfc, cliente, "MXN", None, sub, 0.16, float(iva), float(Decimal(str(sub)) + iva),
            metodo, forma, "I", None, estatus, "Net 30"]


def banco(dia, desc, cargo=None, abono=None, moneda="MXN", cuenta=None, ref="X"):
    return [datetime(2026, 10, dia), cuenta or ("USD-001" if moneda == "USD" else "MXN-001"), ref, desc, cargo, abono, moneda]


# ---------------------------------------------------------------------------------------------------------
# C1: a month with no exceptions / no entries must still export
# ---------------------------------------------------------------------------------------------------------
def test_c1_excel_sin_excepciones(tmp_path):
    from cierre_cxc.exportar import a_excel
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 3, 1000)],
                 [banco(6, "SPEI RECIBIDO TEXTILES PAGO FACTURA Z-1", abono=1160)],
                 [[u("R1"), datetime(2026, 10, 6), u("Z-1"), "Z-1", 1, 1160, 1160, 0, "MXN", 1]])
    s = ejecutar(ruta)
    assert s.excepciones == [] and s.polizas
    salida = a_excel(s, tmp_path / "o.xlsx")
    wb = openpyxl.load_workbook(salida)
    assert wb["A2_Exceptions"]["A1"].value == "No rows"
    assert wb["A3_Journal_Entries"].max_row > 1


def test_c1_excel_sin_polizas(tmp_path):
    from cierre_cxc.exportar import a_excel
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 3, 1000, metodo="PUE", forma="03")],
                 [banco(6, "PAGO NOMINA", cargo=500)])
    s = ejecutar(ruta)
    assert s.polizas == [] and s.excepciones
    wb = openpyxl.load_workbook(a_excel(s, tmp_path / "o.xlsx"))
    assert wb["A3_Journal_Entries"]["A1"].value == "No rows"
    assert wb["A2_Exceptions"].max_row > 1


# ---------------------------------------------------------------------------------------------------------
# C2: review keys are unique and stable
# ---------------------------------------------------------------------------------------------------------
def _datos_claves(tmp_path):
    return libro(tmp_path / "claves.xlsx", [mxn("Z-1", 3, 10000)],
                 [banco(6, "COMISION MANEJO DE CUENTA", cargo=100), banco(7, "IVA COMISION", cargo=16),
                  banco(20, "COMISION MANEJO DE CUENTA", cargo=100), banco(21, "IVA COMISION", cargo=16),
                  banco(8, "SPEI RECIBIDO TEXTILES PARCIAL FACTURA Z-1", abono=3000),
                  banco(9, "SPEI RECIBIDO TEXTILES PARCIAL FACTURA Z-1", abono=2000)])


def test_c2_las_claves_son_unicas_y_estables(tmp_path):
    from cierre_cxc.revision import claves_excepciones, claves_polizas

    def claves_de(s):
        return claves_excepciones(s.excepciones), claves_polizas(s.polizas)

    s1 = ejecutar(_datos_claves(tmp_path))
    exc, pol = claves_de(s1)
    assert len(s1.excepciones) >= 2 and len(set(exc)) == len(exc)
    assert len(s1.polizas) >= 6 and len(set(pol)) == len(pol)
    assert sum(k.startswith("pol:Bank charges") for k in pol) >= 2
    assert sum(k.startswith("pol:Collection") for k in pol) == 2
    s2 = ejecutar(_datos_claves(tmp_path))
    assert claves_de(s2) == (exc, pol)                           # same input, same keys
    from cierre_cxc.cli import claves_revisables
    todas = claves_revisables(s1)
    assert len(set(todas)) == len(todas)


def test_c2_aprobar_una_no_aprueba_la_otra(tmp_path):
    from cierre_cxc.cli import claves_revisables
    from cierre_cxc.exportar import filas_excepciones, lineas_polizas
    from cierre_cxc.revision import Revision
    s = ejecutar(_datos_claves(tmp_path))
    rev = Revision(tmp_path / "revision.json")
    claves = claves_revisables(s)
    primera = next(k for k in claves if k.startswith("exc:Missing REP"))
    rev.decidir(primera, "aprobado", "Diego")
    estados = [f["revision"] for f in filas_excepciones(s, rev)]
    assert sorted(estados).count("aprobado") == 1
    cobro = [k for k in claves if k.startswith("pol:Collection")][0]
    rev.decidir(cobro, "aprobado", "Diego")
    assert len(rev.pendientes(claves)) == len(claves) - 2
    ap = {f["poliza"] for f in lineas_polizas(s, rev) if f["revision"] == "aprobado"}
    assert len(ap) == 1


def test_c2_la_interfaz_no_falla_con_claves_repetidas(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    from conftest import RAIZ
    ruta = _datos_claves(tmp_path)
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(ruta), "--salida", str(tmp_path / "sal")])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    assert not at.exception
    claves = [b.key for b in at.button if b.key and b.key.startswith("a_exc:Missing REP")]
    assert len(claves) == 2 and len(set(claves)) == 2


# ---------------------------------------------------------------------------------------------------------
# G1: rate-correction and cancelled-CFDI entries balance to the cent and tie to the controls
# ---------------------------------------------------------------------------------------------------------
def _usd(folio, dia, tc, sub, iva, total, estatus="Vigente"):
    return [folio, datetime(2026, 10, dia), "XEXX010101000", "Pacific Foods Inc", "USD", tc, sub, 0.16, iva, total,
            "PPD", "99", "I", None, estatus, "Net 30"]


def test_g1_correccion_de_tc_cuadra_al_centavo(tmp_path):
    ruta = libro(tmp_path / "g1.xlsx", [_usd("U-1", 5, 17.1, 10123.45, 1619.75, 11743.20)], [banco(28, "PAGO NOMINA", cargo=1)], fix=17.1237)
    s = ejecutar(ruta)
    k = s.res.correcciones_tc[0]
    assert k["aplicada"] and k["tc_cfdi"] == D("17.1") and k["tc_fix"] == D("17.1237")
    assert k["delta_total"] == D("278.31") and k["delta_iva"] == D("38.38") and k["delta_subtotal"] == D("239.93")
    corr = next(p for p in s.polizas if p.tipo == "Correction")
    assert corr.cuadra
    assert sorted((l.cuenta, l.debe, l.haber) for l in corr.lineas) == sorted(
        [("1201", D("278.31"), D(0)), ("4101", D(0), D("239.93")), ("2202", D(0), D("38.38"))])
    assert s.controles_ok, [c for c in s.controles if not c[1]]


def test_g1_reversa_de_cfdi_cancelado_cuadra(tmp_path):
    # subtotal + IVA is 0.01 short of the stamped total: each part rounded on its own would not balance
    fila = ["C-1", datetime(2026, 10, 9), "TRI100101AA1", "Textiles SA", "MXN", None, 10.03, 0.16, 1.60, 11.64,
            "PPD", "99", "I", None, "Cancelado", "Net 30"]
    s = ejecutar(libro(tmp_path / "g1b.xlsx", [fila, mxn("Z-1", 3, 1000)], [banco(28, "PAGO NOMINA", cargo=1)]))
    rev = next(p for p in s.polizas if p.concepto.startswith("Reversal of cancelled CFDI C-1"))
    assert rev.cuadra
    assert sorted((l.cuenta, l.debe, l.haber) for l in rev.lineas) == sorted(
        [("1201", D(0), D("11.64")), ("2202", D("1.60"), D(0)), ("4101", D("10.04"), D(0))])
    assert s.controles_ok, [c for c in s.controles if not c[1]]


# ---------------------------------------------------------------------------------------------------------
# G2: bank charges on the USD account are posted in MXN at the FIX of the charge date
# ---------------------------------------------------------------------------------------------------------
def test_g2_comision_en_usd_se_contabiliza_en_mxn(tmp_path):
    ruta = libro(tmp_path / "g2.xlsx", [mxn("Z-1", 3, 1000)],
                 [banco(12, "COMISION MANEJO DE CUENTA", cargo=25.00, moneda="USD"),
                  banco(12, "IVA COMISION", cargo=4.00, moneda="USD"),
                  banco(13, "COMISION MANEJO DE CUENTA", cargo=100, moneda="MXN")], fix=17.5)
    s = ejecutar(ruta)
    usd = next(p for p in s.polizas if p.tipo == "Bank charges" and p.concepto.endswith("USD-001"))
    assert usd.cuadra
    assert sorted((l.cuenta, l.debe, l.haber) for l in usd.lineas) == sorted(
        [("6101", D("437.50"), D(0)), ("1301", D("70.00"), D(0)), ("1102", D(0), D("507.50"))])
    mx = next(p for p in s.polizas if p.tipo == "Bank charges" and p.concepto.endswith("MXN-001"))
    assert sorted((l.cuenta, l.debe, l.haber) for l in mx.lineas) == sorted([("6101", D("100"), D(0)), ("1101", D(0), D("100"))])
    assert s.controles_ok


# ---------------------------------------------------------------------------------------------------------
# G4: same-day bank rows are processed in file order (index 2 before index 10, not "10" before "2")
# ---------------------------------------------------------------------------------------------------------
def test_g4_filas_del_banco_del_mismo_dia_en_orden_de_archivo(tmp_path):
    relleno = [banco(1 + k, "PAGO NOMINA", cargo=1) for k in range(9)]
    bancos = (relleno[:2] + [banco(15, "SPEI RECIBIDO FACT A-1001", abono=10000)] + relleno[2:]
              + [banco(15, "SPEI RECIBIDO TEXTILES SA", abono=3000)])
    assert bancos[2][5] == 10000 and bancos[10][5] == 3000
    ruta = libro(tmp_path / "g4.xlsx", [mxn("A-1001", 3, 8620.69)], bancos)
    s = ejecutar(ruta)
    ap = [a for a in s.res.aplicaciones if a.folio == "A-1001"]
    assert [(a.mov_id, a.aplicado_doc, a.saldo_despues) for a in ap] == [("B04", D("10000"), D(0))]
    assert [u["mov_id"] for u in s.res.efectivo_no_identificado] == ["B12"]
    assert s.controles_ok


# ---------------------------------------------------------------------------------------------------------
# G5: the description patterns match whole words, not pieces of other words
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("desc,cargo,clase", [
    ("SPEI RECIBIDO INTELSAT MEXICO", False, "cobro"),
    ("SPEI TRANSPORTES INTERESTATALES", False, "cobro"),
    ("SPEI PROVEEDORA ALFA SA", False, "cobro"),
    ("PAGO PROVEEDOR TELECOM SA", True, "no_cxc"),
    ("PAGO IVA COMPLEMENTARIO", True, "no_cxc"),
    # round 2: a web domain is not a bank fee
    ("DOMICILIACION TOTALPLAY.COM.MX", True, "no_cxc"),
    ("PAGO PAYPAL *AMAZON.COM", True, "no_cxc"),
    ("PAGO AMAZON.COM MX", True, "no_cxc"),
    # the real forms keep matching
    ("COMISION MANEJO DE CUENTA", True, "comision"),
    ("COMISIONES BANCARIAS", True, "comision"),
    ("COM. SPEI", True, "comision"),
    ("COM SPEI ENVIADO", True, "comision"),
    ("MEMBRESIA ANUAL", True, "comision"),
    ("IVA COMISION", True, "iva_comision"),
    ("IVA COMISIONES", True, "iva_comision"),
    ("IVA COM", True, "iva_comision"),
    ("IVA COM.", True, "iva_comision"),
    ("INTERESES GANADOS", False, "abono_no_cxc"),
    ("INTERES GANADO", False, "abono_no_cxc"),
    ("SPEI RECIBIDO PROVEEDORES SA", False, "abono_no_cxc"),
    ("SPEI RECIBIDO PROVEEDOR", False, "abono_no_cxc"),
    ("DEVOLUCION IMPUESTOS SAT", False, "abono_no_cxc"),
    ("PRESTAMO BANCARIO", False, "abono_no_cxc"),
    ("TRASPASO ENTRE CUENTAS", False, "abono_no_cxc"),
    ("NOMINA QNA", False, "abono_no_cxc"),
    ("IMSS CUOTAS", False, "abono_no_cxc"),
])
def test_g5_patrones_por_palabra(tmp_path, desc, cargo, clase):
    fila = banco(12, desc, cargo=100) if cargo else banco(12, desc, abono=100)
    s = ejecutar(libro(tmp_path / "g5.xlsx", [mxn("Z-1", 3, 1000)], [fila]))
    assert s.res.movimientos[0].clase == clase


# ---------------------------------------------------------------------------------------------------------
# G3: a PUE invoice never asks for a REP; a PUE paid after its month of issuance asks for the PPD substitution
# (RMF 2026 rule 2.7.1.39, p. 148; rule 2.7.1.32, pp. 143-144)
# ---------------------------------------------------------------------------------------------------------
def _cats(s):
    return {(e.categoria, e.referencia) for e in s.excepciones}


def test_g3_pue_cobrada_en_su_mes_no_pide_rep(tmp_path):
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 3, 1000, metodo="PUE", forma="03")],
                 [banco(6, "SPEI RECIBIDO TEXTILES PAGO FACTURA Z-1", abono=1160)])
    s = ejecutar(ruta)
    assert s.res.partidas["Z-1"].saldo == 0
    assert ("Missing REP", "Z-1") not in _cats(s)
    assert ("PUE paid late", "Z-1") not in _cats(s)


def test_g3_pue_cobrada_despues_de_su_mes_pide_sustitucion_y_no_rep_directo(tmp_path):
    fila = mxn("Z-9", 3, 1000, metodo="PUE", forma="03")
    fila[1] = datetime(2026, 9, 28)
    ruta = libro(tmp_path / "m.xlsx", [fila],
                 [banco(6, "SPEI RECIBIDO TEXTILES PAGO FACTURA Z-9", abono=1160)],
                 apertura=[["Z-9", datetime(2026, 9, 28), "Textiles SA", "MXN", 1160, None, 1160]])
    s = ejecutar(ruta)
    assert s.res.partidas["Z-9"].saldo == 0
    assert ("Missing REP", "Z-9") not in _cats(s)
    e = next(e for e in s.excepciones if e.categoria == "PUE paid late" and e.referencia == "Z-9")
    assert e.severidad == "alta"
    assert "2.7.1.39" in e.accion and "PPD" in e.accion and "99" in e.accion and "04" in e.accion and "REP" in e.accion
    assert e.monto_mxn == D("160.00")          # IVA collected on the payment, as in 'Missing REP'
    assert e.fecha_limite == date(2026, 11, 5)  # the REP that follows is due day 5 of the next month


def test_g3_ppd_sigue_pidiendo_rep(tmp_path):
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 3, 1000)],
                 [banco(6, "SPEI RECIBIDO TEXTILES PAGO FACTURA Z-1", abono=1160)])
    assert ("Missing REP", "Z-1") in _cats(ejecutar(ruta))
