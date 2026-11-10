"""Criterion 1 (B1): which invoice a receipt pays when the payer does not say.

SAT guide for the payments complement 2.0 (pp. 4-5): the payer may indicate the CFDI within the 5 calendar days after
the payment (numeral 3); if it does not, the receiver applies the payment to the oldest pending CFDI (numeral 4).
An exact amount on a newer invoice is a clue, not the payer's indication: the tool applies oldest first and opens an
exception so AR asks the customer before the window closes.
"""
from datetime import date

from conftest import D
from test_niveles import deposito, libro  # noqa: F401  (fixture)
from cierre_cxc.pipeline import ejecutar


def _aplic(s, mov):
    return {a.folio: a.aplicado_doc for a in s.res.aplicaciones if a.mov_id == mov}


def _orden(s, ref):
    return [e for e in s.excepciones if e.categoria == "Application order" and e.referencia == ref]


def test_b13_sin_factura_va_primero_a_la_mas_antigua(salida):
    assert _aplic(salida, "B13") == {"A-1006": D(104400), "A-1015": D(23200)}
    assert salida.res.partidas["A-1006"].saldo == 0
    assert salida.res.partidas["A-1015"].saldo == D(104400)


def test_b13_abre_excepcion_para_consultar_al_cliente_antes_del_dia_5(salida):
    e = _orden(salida, "B13")
    assert len(e) == 1
    e = e[0]
    assert e.fecha_limite == date(2026, 10, 5)                 # 5 calendar days after the 30-Sep payment
    assert e.monto_mxn == D(104400) and e.certeza == "confirmar"
    assert "A-1015" in e.que and "oldest" in e.que
    assert "override B13" in e.accion and "A-1015" in e.accion


def test_b12_cruzado_va_a_la_mas_antigua_de_bajio(salida):
    assert _aplic(salida, "B12") == {"A-1013": D("11600.00")}
    assert salida.res.partidas["A-1013"].saldo == D(2320)
    assert salida.res.partidas["A-1018"].saldo == D(11600)
    assert len(_orden(salida, "B12")) == 1


def test_las_cifras_de_cierre_que_no_dependen_del_orden_no_cambian(salida):
    assert salida.controles_ok
    assert salida.aging["total"] == D("1759932.85")
    assert salida.iva["cobrado"] == D("247757.23")


def test_si_el_cliente_indica_la_factura_se_reasigna_y_la_excepcion_desaparece(datos_caso):
    s = ejecutar(datos_caso, decisiones={"conciliacion": {"B13": {"accion": "asignar", "folios": ["A-1015"],
                                                                  "usuario": "t", "comentario": "Omega indicated A-1015"}}})
    assert _aplic(s, "B13") == {"A-1015": D(127600)}
    assert not _orden(s, "B13")
    assert s.controles_ok


def test_monto_exacto_a_la_mas_antigua_no_abre_excepcion(libro):
    s = ejecutar(libro(deposito("SPEI RECIBIDO SERV DIGITALES OMEGA", 104400, dia=29)))
    nuevo = next(a.mov_id for a in s.res.aplicaciones if a.mov_id.startswith("B") and a.folio == "A-1006")
    assert _aplic(s, nuevo) == {"A-1006": D(104400)}
    assert _aplic(s, "B13") == {"A-1015": D(127600)}           # A-1006 already paid: A-1015 is now the oldest
    assert not [e for e in s.excepciones if e.categoria == "Application order"
                and e.referencia in (nuevo, "B13")]


def test_con_la_ventana_cerrada_la_excepcion_lo_dice(libro):
    s = ejecutar(libro(deposito("SPEI RECIBIDO SERV DIGITALES OMEGA", 127600, dia=20)))
    mov = next(a.mov_id for a in s.res.aplicaciones if a.folio == "A-1006" and a.fecha == date(2026, 9, 20))
    assert _aplic(s, mov) == {"A-1006": D(104400), "A-1015": D(23200)}
    e = _orden(s, mov)
    assert len(e) == 1 and e[0].severidad == "baja"
    assert e[0].fecha_limite == date(2026, 9, 25) and "closed" in e[0].que


def _factura(folio, dia, rfc, cliente, moneda, tc, subtotal):
    from datetime import datetime
    return lambda wb: wb["CFDI_Emitidos"].append(
        [f"00000000-0000-4000-8000-{folio[-4:]:0>12}", folio, datetime(2026, 9, dia), rfc, cliente, moneda, tc,
         subtotal, 0.16, subtotal * 0.16, subtotal * 1.16, "PPD", "99", "I", None, "Vigente", "Net 30"])


def test_sabotaje_una_factura_vieja_en_otra_moneda_no_entra_al_orden(libro):
    # assumption: oldest first only among invoices in the matched invoice's currency
    s = ejecutar(libro(_factura("A-1090", 1, "SDO130404GH4", "Servicios Digitales Omega SA de CV", "USD", 17.5, 1000)))
    assert _aplic(s, "B13") == {"A-1006": D(104400), "A-1015": D(23200)}
    assert s.res.partidas["A-1090"].saldo == D(1160)


def test_sabotaje_cobro_sin_folio_cae_en_la_pue_no_cobrada_y_lo_avisa(libro):
    # Hotelera has A-1008 (PUE, never collected) and A-1017; a 110,200 receipt matches A-1017 but A-1008 is older.
    # Criterion 2 decides the PUE; here the receipt follows the SAT order and AR is told to ask the customer.
    s = ejecutar(libro(deposito("SPEI RECIBIDO HOTELERA RIVIERA MAYA", 110200, dia=29)))
    mov = next(a.mov_id for a in s.res.aplicaciones if a.folio == "A-1008")
    assert _aplic(s, mov) == {"A-1008": D(110200)}
    e = _orden(s, mov)
    assert len(e) == 1 and "A-1017" in e[0].que and "override" in e[0].accion


def _dup(s):
    return next(e for e in s.excepciones if e.categoria == "Possible duplicate")


def test_sabotaje_duplicado_sin_ningun_pago_conserva_uno(libro):
    from test_sabotaje import B, celda
    s = ejecutar(libro(celda(B, 11, 4, "SPEI RECIBIDO")))          # B11 loses the name: nothing is applied
    assert not [a for a in s.res.aplicaciones if a.folio in ("A-1010", "A-1011")]
    assert "cancel A-1011 and keep A-1010" in _dup(s).accion


def test_sabotaje_duplicado_pagado_dos_veces_pide_devolver(libro):
    s = ejecutar(libro(deposito("SPEI RECIBIDO FARM DEL VALLE", 87000, dia=29)))
    assert {a.folio for a in s.res.aplicaciones if a.cliente.startswith("Farmacias")} == {"A-1010", "A-1011"}
    assert "credit or refund" in _dup(s).accion


def test_sabotaje_tipo_de_cambio_fuera_del_rango_del_anexo_20_es_alta(libro):
    from test_sabotaje import celda
    s = ejecutar(libro(celda("CFDI_Emitidos", 15, 7, 10)))          # A-1013 stamped at 10.00, -43% from the FIX
    e = next(e for e in s.excepciones if e.categoria == "FX rate on CFDI" and e.referencia == "A-1013")
    assert e.severidad == "alta" and "35%" in e.por_que and "confirmation key" in e.por_que


def test_sabotaje_rep_que_ignora_la_nota_de_credito_se_marca(libro):
    from test_sabotaje import R, celda
    # A-1007's REP rewritten to start at the CFDI total (69,600) and leave 11,600 open: the credit note was ignored
    s = ejecutar(libro(celda(R, 6, 6, 69600), celda(R, 6, 8, 11600)))
    e = next(e for e in s.excepciones if e.categoria == "REP balance" and e.referencia == "A-1007")
    assert "58,000.00" in e.que and "NC-501" in e.que and "p. 77" in e.por_que
