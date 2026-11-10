"""Control answers for the September 2026 case, worked out by hand before the tool existed."""
from datetime import date
from decimal import Decimal

from conftest import D
from cierre_cxc.pipeline import ejecutar


def _ap(salida, folio):
    return [a for a in salida.res.aplicaciones if a.folio == folio]


def test_todos_los_controles_internos_pasan(salida):
    fallos = [(n, d) for n, ok, d in salida.controles if not ok]
    assert not fallos, fallos


def test_cada_cobro_queda_aplicado_o_sin_identificar(salida):
    assert len(salida.res.aplicaciones) == 10            # B13 is spread over A-1006 and A-1015 (oldest first)
    assert [u["mov_id"] for u in salida.res.efectivo_no_identificado] == ["B08"]
    assert salida.res.efectivo_no_identificado[0]["monto"] == D(34800)
    # payroll and the supplier payment are not AR; the two bank charges are classified as such
    assert sorted(m.id for m in salida.res.no_cxc) == ["B02", "B05"]
    assert sorted(m.id for m in salida.res.comisiones) == ["B14", "B15"]


def test_tipos_de_conciliacion(salida):
    tipos = {a.folio: a.tipo for a in salida.res.aplicaciones}
    assert tipos["A-1001"] == "exacto"
    assert tipos["A-1003"] == "parcial"
    assert tipos["A-1010"] == "corto"
    assert tipos["A-1013"] == "cruzado"
    assert tipos["A-1006"] == tipos["A-1015"] == "multiple"
    assert "A-1018" not in tipos                     # B12 goes to the older A-1013 (criterion 1)


def test_pago_corto_deja_150_abiertos(salida):
    a = _ap(salida, "A-1010")[0]
    assert a.saldo_despues == D(150)
    assert a.aplicado_doc == D("86850")


def test_pago_cruzado_aplica_a_la_factura_en_usd_mas_antigua(salida):
    # B12 names Bajio but no invoice: oldest first (SAT payments complement guide 2.0, numeral 4)
    a = _ap(salida, "A-1013")[0]
    assert (a.aplicado_doc, a.doc_moneda, a.pago_moneda) == (D(11600), "USD", "MXN")
    assert a.fx_realizada == D("691.36")             # 202,795.84 - 11,600 x 17.4228


def test_fx_realizada(salida):
    assert _ap(salida, "A-1004")[0].fx_realizada == D("-4660.88")
    assert _ap(salida, "A-1005")[0].fx_realizada == D("163.50")


def test_nota_de_credito_neta_el_cobro_de_a1007(salida):
    nc = salida.res.notas_credito[0]
    assert (nc.folio, nc.folio_factura, nc.total) == ("NC-501", "A-1007", D(11600))
    assert _ap(salida, "A-1007")[0].aplicado_doc == D(58000)
    assert salida.res.partidas["A-1007"].saldo == 0


def test_iva_cobrado_y_por_trasladar(salida):
    assert salida.iva["cobrado"] == D("247757.23")
    assert salida.iva["cierre"] == D("140653.43")
    por_folio = {f["folio"]: f["iva_cobrado_mxn"] for f in salida.iva["filas"]}
    assert por_folio["A-1004"] == D("55806.08")      # USD 3,200 at the FIX of 15-Sep
    assert por_folio["A-1013"] == D("27971.84")      # USD 1,600 at the FIX of 29-Sep
    assert por_folio["A-1007"] == D(8000)            # net of the credit note's IVA


def test_antiguedad_corregida(salida):
    assert salida.aging["totales"]["Current"] == D("1458332.85")
    assert salida.aging["totales"]["1-30"] == D("301600.00")      # A-1006 is paid first by B13
    assert salida.aging["totales"]["31-60"] == 0 and salida.aging["totales"]["60+"] == 0
    assert salida.aging["total"] == D("1759932.85")
    folios = {f["folio"] for f in salida.aging["filas"]}
    assert "A-1016" not in folios                    # cancelled CFDI never ages
    assert folios == {"A-1003", "A-1008", "A-1009", "A-1010", "A-1011", "A-1012", "A-1013", "A-1014", "A-1015",
                      "A-1017", "A-1018"}


def test_roll_forward_parte_de_la_apertura(salida):
    assert salida.rollforward["lineas"][0][1] == D("2062254.96")
    assert salida.rollforward["cierre_calculado"] == salida.aging["total"]


def test_polizas_cuadran_y_las_condicionadas_estan_marcadas(salida):
    assert all(p.cuadra for p in salida.polizas)
    cond = [p.concepto for p in salida.polizas if p.estado == "condicionada"]
    assert any("A-1013" in c for c in cond) and any("A-1016" in c for c in cond)


def test_excepciones_clave_detectadas(salida):
    cats = {(e.categoria, e.referencia) for e in salida.excepciones}
    for esperado in [("Missing REP", "A-1002"), ("Missing REP", "A-1010"), ("Missing REP", "A-1013"),
                     ("Missing REP", "A-1006"), ("Missing REP", "A-1015"), ("Application order", "B12"),
                     ("Application order", "B13"), ("PUE not collected", "A-1008"), ("IVA rate", "A-1012"),
                     ("FX rate on CFDI", "A-1013"), ("Cancelled CFDI", "A-1016"),
                     ("Possible duplicate", "A-1010, A-1011"), ("Unidentified cash", "B08")]:
        assert esperado in cats, esperado
    # A-1001, A-1003, A-1004, A-1005 and A-1007 have their REP
    assert ("Missing REP", "A-1001") not in cats and ("Missing REP", "A-1005") not in cats


def test_rep_vence_el_dia_5_del_mes_siguiente(salida):
    # RMF 2026 rule 2.7.1.32: REP no later than the 5th calendar day of the month after the payment
    limites = {e.referencia: e.fecha_limite.isoformat() for e in salida.excepciones if e.categoria == "Missing REP"}
    assert limites == {"A-1002": "2026-10-05", "A-1010": "2026-10-05", "A-1013": "2026-10-05", "A-1006": "2026-10-05",
                       "A-1015": "2026-10-05"}


def test_rechazar_la_correccion_de_tc_mantiene_los_cuadres(datos_caso):
    s = ejecutar(datos_caso, decisiones={"rechazar_correcciones": ["A-1013"]})
    assert s.controles_ok
    assert s.aging["total"] == D("1759932.85")      # closing value does not depend on the correction
    assert not any("Rate correction" in p.concepto for p in s.polizas)


def test_anulacion_del_revisor_reaplica_el_pago(datos_caso):
    # reviewer says the 127,600 receipt (B13) belongs to the oldest Omega invoice
    s = ejecutar(datos_caso, decisiones={"conciliacion": {"B13": {"accion": "asignar", "folios": ["A-1006", "A-1015"],
                                                                  "usuario": "t", "comentario": "FIFO"}}})
    assert s.controles_ok
    aplic = {a.folio: a.aplicado_doc for a in s.res.aplicaciones if a.mov_id == "B13"}
    assert aplic == {"A-1006": D(104400), "A-1015": D(23200)}
    assert s.res.partidas["A-1015"].saldo == D(104400)


def test_la_corrida_es_determinista(datos_caso):
    a, b = ejecutar(datos_caso), ejecutar(datos_caso)
    assert [(x.mov_id, x.folio, x.mxn_recibido) for x in a.res.aplicaciones] == \
           [(x.mov_id, x.folio, x.mxn_recibido) for x in b.res.aplicaciones]


def test_pue_no_cobrada_no_dice_que_el_cliente_acredita_ya(salida):
    # criterion 2 (B1): the customer credits IVA only once it pays (LIVA art. 5o fr. III, p. 10);
    # RMF 2026 rule 2.7.1.39 (pp. 147-148) sets no deadline to reissue, so the date is internal
    e = next(e for e in salida.excepciones if e.categoria == "PUE not collected" and e.referencia == "A-1008")
    assert "can credit the IVA now" not in e.por_que
    assert "LIVA art. 5o fr. III" in e.por_que
    assert "c_TipoRelacion 04" in e.accion and "internal deadline" in e.accion
    assert e.monto_mxn == D(232000) and e.certeza == "cierto"


def test_a5_muestra_el_iva_por_trasladar_al_fix_de_cierre_sin_registrarlo(salida):
    # criterion 3 (B1): IVA cobrado at the FIX of the collection date (case p. 1; CFF art. 20, p. 30); the 2202 of
    # invoices not yet collected stays at the booked rate. The closing-FIX figure is shown, not booked.
    from cierre_cxc.exportar import todas_las_tablas
    filas = todas_las_tablas(salida)["A5_IVA_Roll_Forward"]
    cierre = next(f for f in filas if f["concepto"] == "Closing IVA por trasladar")
    assert cierre["monto_mxn"] == D("140653.43") == salida.iva["cierre_calculado"]
    memo = [f for f in filas if f["concepto"].startswith("Memo (not booked)")]
    assert [f["monto_mxn"] for f in memo] == [D("140893.84"), D("240.41")]
    assert "FIX of the collection date" in memo[1]["concepto"]


def test_duplicado_dice_cual_cancelar_y_pago_corto_que_cfdi_emitir(salida):
    # criterion 4 (B1): B11 pays A-1010 (150 left open); A-1011 has no cash, so it is the one to cancel if duplicated
    dup = next(e for e in salida.excepciones if e.categoria == "Possible duplicate")
    assert "cancel A-1011" in dup.accion and "A-1010, A-1011)" not in dup.accion
    assert "paid: A-1010" in dup.por_que
    corto = next(e for e in salida.excepciones if e.categoria == "Short payment" and e.referencia == "A-1010")
    # LIVA art. 7o (p. 19); Anexo 20 c_TipoRelacion 01 (p. 14) and payment form 15 while unpaid (pp. 65-66)
    assert "CFDI de egreso" in corto.accion and "c_TipoRelacion 01" in corto.accion and "15" in corto.accion
    assert "20.69" in corto.accion and "LIVA art. 7o" in corto.accion
    assert corto.monto_mxn == D(150)


def test_deposito_no_identificado_pone_la_fecha_de_la_declaracion_de_iva(salida):
    # criterion 5 (B1): IVA is due on receipt only if it pays Clara services (LIVA art. 1o.-B, p. 3); the September
    # return is due 17-Oct (LIVA art. 5o.-D, p. 18), so the tax team decides by the 15th if it is still unidentified
    e = next(e for e in salida.excepciones if e.categoria == "Unidentified cash" and e.referencia == "B08")
    assert "2026-10-17" in e.accion and "2026-10-15" in e.accion and "LIVA art. 5o.-D" in e.accion
    assert "Tax team" in e.accion and "refund" in e.accion
    assert e.fecha_limite == date(2026, 10, 3)            # identification deadline unchanged
    assert e.monto_mxn == D(34800) and "4,800.00" in e.no_asiento


def test_tipo_de_cambio_del_cfdi_es_valido_y_la_diferencia_es_contable(salida):
    # criterion 6 (B1): Anexo 20 p. 9 makes the FIX optional in TipoCambio (only a +/-35% range for USD); no source
    # obliges reissuing. IVA on the return is measured at the collection FIX, so only the ledger is affected.
    e = next(e for e in salida.excepciones if e.categoria == "FX rate on CFDI" and e.referencia == "A-1013")
    assert e.severidad == "media" and e.monto_mxn == D(7656)
    assert "must be the FIX" not in e.por_que and "IAS 21 requires" not in e.por_que
    assert "Anexo 20" in e.por_que and "IVA on the return is not affected" in e.por_que
    assert "optional" in e.accion and "B12" in e.accion          # reissue only before the REP of B12
    assert any("Rate correction A-1013" in p.concepto and p.estado == "condicionada" for p in salida.polizas)


def test_rep_de_a1007_arranca_neto_de_la_nota_de_credito_y_es_correcto(salida):
    # B2 question 6: SAT payments guide 2.0, worked example pp. 73-77 (REP previous balance net of the earlier credit
    # note) and p. 55 note 4 (the credit note is applied to the invoice, not through a REP): 58,000 is right
    assert not [e for e in salida.excepciones if e.categoria == "REP balance" and e.referencia == "A-1007"]
