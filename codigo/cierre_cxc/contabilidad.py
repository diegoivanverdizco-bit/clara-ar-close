"""Journal entries, corrected aging, roll-forward and IVA summary, all derived from the engine result."""
from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .dinero import q2, CERO
from .modelos import Poliza, LineaPoliza

NOMBRES = {
    "1101": "Bancos MXN", "1102": "Bancos USD", "1201": "Clientes (CxC)", "1301": "IVA acreditable",
    "1302": "IVA por acreditar", "2101": "Anticipos de clientes / cobros no identificados",
    "2201": "IVA trasladado cobrado", "2202": "IVA trasladado por cobrar (por trasladar)",
    "4101": "Ingresos por servicios", "4102": "Devoluciones y descuentos s/ventas", "6101": "Gastos bancarios",
    "7101": "Utilidad cambiaria realizada", "7102": "Perdida cambiaria realizada",
    "7103": "Utilidad/Perdida cambiaria no realizada",
}
BUCKETS = ["Current", "1-30", "31-60", "60+"]


def _nombre(res, cuenta):
    return res.ds.catalogo.get(cuenta) or NOMBRES.get(cuenta, cuenta)


def _l(res, cuenta, debe=CERO, haber=CERO):
    return LineaPoliza(cuenta, _nombre(res, cuenta), debe, haber)


def _fx_lineas(res, diferencia: Decimal):
    """Gain (>0) goes to the credit side of 7101, loss to the debit side of 7102."""
    c = res.cfg.cuentas
    if diferencia > 0:
        return _l(res, c.utilidad_cambiaria, haber=diferencia)
    if diferencia < 0:
        return _l(res, c.perdida_cambiaria, debe=-diferencia)
    return None


def generar_polizas(res) -> list:
    c = res.cfg.cuentas
    polizas, n = [], 0

    def nueva(fecha, tipo, concepto, lineas, fuentes, estado="propuesta", nota=""):
        nonlocal n
        n += 1
        lineas = [l for l in lineas if l is not None and (l.debe or l.haber)]
        polizas.append(Poliza(f"P{n:02d}", fecha, tipo, concepto, lineas, fuentes, estado, nota))

    for nc in res.notas_credito:
        nueva(nc.fecha, "Credit note", f"Credit note {nc.folio} on {nc.folio_factura}",
              [_l(res, c.devoluciones, debe=nc.subtotal_libro), _l(res, c.iva_por_trasladar, debe=nc.iva_libro),
               _l(res, c.clientes, haber=nc.libro_relevado)], nc.fuentes)

    for a in res.aplicaciones:
        banco = c.banco_usd if a.pago_moneda == "USD" else c.banco_mxn
        nota = ""
        if a.pago_moneda != a.doc_moneda:
            nota = f"Paid in {a.pago_moneda} against a {a.doc_moneda} invoice."
        nueva(a.fecha, "Collection", f"Collection {a.folio} - {a.cliente} ({a.tipo})",
              [_l(res, banco, debe=a.mxn_recibido),
               _fx_lineas(res, a.fx_realizada) if a.fx_realizada > 0 else None,
               _fx_lineas(res, a.fx_realizada) if a.fx_realizada < 0 else None,
               _l(res, c.clientes, haber=a.libro_relevado)], a.fuentes, nota=nota)
        if a.iva_libro_mxn or a.iva_cobrado_mxn:
            dif = a.iva_libro_mxn - a.iva_cobrado_mxn          # booked > collected -> gain
            nueva(a.fecha, "IVA reclass", f"IVA {a.folio}: from por trasladar to cobrado at the FIX of {a.fecha}",
                  [_l(res, c.iva_por_trasladar, debe=a.iva_libro_mxn), _l(res, c.iva_cobrado, haber=a.iva_cobrado_mxn),
                   _fx_lineas(res, dif)], a.fuentes)

    for u in res.efectivo_no_identificado:
        banco = c.banco_usd if u["moneda"] == "USD" else c.banco_mxn
        nueva(u["fecha"], "Unidentified cash", f"Unidentified receipt {u['mov_id']}: {u['descripcion']}",
              [_l(res, banco, debe=u["mxn"]), _l(res, c.anticipos_no_identificado, haber=u["mxn"])], [u["fuente"]],
              nota="Held in 2101 until the customer and invoice are confirmed. No IVA recognized yet.")

    por_dia = defaultdict(list)
    for m in res.comisiones:
        por_dia[(m.fecha, m.cuenta, m.moneda)].append(m)
    for (fecha, cuenta, moneda), movs in sorted(por_dia.items()):
        banco = c.banco_usd if moneda == "USD" else c.banco_mxn
        # USD charges are converted one by one at the FIX of the charge date (MXN charges are posted as they are)
        def en_mxn(m):
            return m.cargo if moneda == "MXN" else res.fx.a_mxn(m.cargo, moneda, m.fecha)
        com = sum((en_mxn(m) for m in movs if m.clase == "comision"), CERO)
        iva = sum((en_mxn(m) for m in movs if m.clase == "iva_comision"), CERO)
        nueva(fecha, "Bank charges", f"Bank charges {cuenta}",
              [_l(res, c.gastos_bancarios, debe=com), _l(res, c.iva_acreditable, debe=iva),
               _l(res, banco, haber=com + iva)], [m.fuente for m in movs],
              nota="IVA acreditable is valid only with the bank's CFDI.")

    for r in res.reval:
        if r["ajuste"] != 0:
            g = r["ajuste"]
            nueva(res.cierre, "FX revaluation", f"Revaluation of {r['folio']} ({r['saldo']} {r['moneda']}) at {r['tc_cierre']}",
                  [_l(res, c.clientes, debe=g if g > 0 else CERO, haber=-g if g < 0 else CERO),
                   _l(res, c.cambiaria_no_realizada, debe=-g if g < 0 else CERO, haber=g if g > 0 else CERO)],
                  r["fuentes"], nota="Unrealized; reverses on the first day of the next period.")

    if True:
        for k in res.correcciones_tc:
            d_t, d_s, d_i = k["delta_total"], k["delta_subtotal"], k["delta_iva"]
            if d_t == 0 or not k["aplicada"]:
                continue
            sg = 1 if d_t > 0 else -1
            nueva(res.cierre, "Correction", f"Rate correction {k['folio']}: CFDI {k['tc_cfdi']} vs FIX {k['tc_fix']}",
                  [_l(res, c.clientes, debe=d_t if sg > 0 else CERO, haber=-d_t if sg < 0 else CERO),
                   _l(res, c.ingresos, debe=-d_s if sg < 0 else CERO, haber=d_s if sg > 0 else CERO),
                   _l(res, c.iva_por_trasladar, debe=-d_i if sg < 0 else CERO, haber=d_i if sg > 0 else CERO)],
                  [k["fuente"]], estado="condicionada",
                  nota="Pending tax-team confirmation: the stamped CFDI carries a rate that is not the FIX of the issue date.")
        for f in (res.canceladas if res.cfg.aplicar_correcciones else []):
            r = f.datos
            tc = r["tc"] or Decimal(1)
            iva, tot = q2(r["iva"] * tc), q2(r["total"] * tc)
            sub = tot - iva                       # the plug: the entry balances exactly and ties to the booked total
            nueva(res.cierre, "Correction", f"Reversal of cancelled CFDI {r['folio']}",
                  [_l(res, c.ingresos, debe=sub), _l(res, c.iva_por_trasladar, debe=iva), _l(res, c.clientes, haber=tot)],
                  [f.fuente], estado="condicionada",
                  nota="Only if the invoicing entry is still in the ledger (the data says all CFDIs were booked).")
    return polizas


def bucket(dias: int) -> str:
    if dias <= 0:
        return "Current"
    if dias <= 30:
        return "1-30"
    if dias <= 60:
        return "31-60"
    return "60+"


def antiguedad(res) -> dict:
    filas = []
    for p in sorted(res.partidas.values(), key=lambda x: (x.cliente, x.vencimiento, x.folio)):
        if p.saldo <= Decimal("0.005"):
            continue
        dias = (res.cierre - p.vencimiento).days
        tc = res.fx.en(res.cierre) if p.moneda != "MXN" else Decimal(1)
        filas.append({"cliente": p.cliente, "folio": p.folio, "emision": p.fecha, "vencimiento": p.vencimiento,
                      "dias_vencido": max(dias, 0), "moneda": p.moneda, "saldo_orig": p.saldo, "tc_cierre": tc,
                      "saldo_mxn": q2(p.saldo * tc), "bucket": bucket(dias), "fuentes": p.fuentes,
                      "libro_mxn": p.libro_mxn})
    por_cliente = defaultdict(lambda: {b: CERO for b in BUCKETS})
    for f in filas:
        por_cliente[f["cliente"]][f["bucket"]] += f["saldo_mxn"]
    totales = {b: sum((f["saldo_mxn"] for f in filas if f["bucket"] == b), CERO) for b in BUCKETS}
    sin_aplicar = sum((u["mxn"] for u in res.efectivo_no_identificado), CERO)
    return {"filas": filas, "por_cliente": dict(por_cliente), "totales": totales,
            "total": sum(totales.values(), CERO), "efectivo_sin_aplicar": sin_aplicar}


def rollforward(res) -> dict:
    nuevas_bruto = sum((p.libro_original_mxn for p in res.nuevas_facturas), CERO)
    canceladas = sum((q2(f.datos["total"] * (f.datos["tc"] or Decimal(1))) for f in res.canceladas), CERO)
    nc = sum((n.libro_relevado for n in res.notas_credito), CERO)
    cobros = sum((a.libro_relevado for a in res.aplicaciones), CERO)
    corr = sum((k["delta_total"] for k in res.correcciones_tc if k["aplicada"]), CERO)
    reval = sum((r["ajuste"] for r in res.reval), CERO)
    lineas = [(f"Opening balance (booked, {res.inicio - timedelta(days=1):%d-%b})", res.apertura_libro),
              ("+ Invoices issued in the period (valid)", nuevas_bruto),
              ("+ Invoices issued in the period and later cancelled", canceladas),
              ("- Cancelled CFDI removed from AR", -canceladas),
              ("- Credit notes", -nc),
              ("- Collections applied (at carrying value)", -cobros),
              ("+/- Rate correction on CFDI issued at a wrong rate (conditional)", corr),
              ("+/- Period-end revaluation of open USD balances", reval)]
    calculado = sum((v for _, v in lineas), CERO)
    return {"lineas": lineas, "cierre_calculado": calculado}


def resumen_iva(res) -> dict:
    filas = [{"folio": a.folio, "cliente": a.cliente, "fecha": a.fecha, "aplicado": a.aplicado_doc,
              "moneda": a.doc_moneda, "iva_doc": a.iva_doc, "tc": a.tc_cobro, "iva_cobrado_mxn": a.iva_cobrado_mxn,
              "iva_libro_mxn": a.iva_libro_mxn, "fuentes": a.fuentes} for a in res.aplicaciones if a.iva_doc]
    cobrado = sum((f["iva_cobrado_mxn"] for f in filas), CERO)
    abre = res.apertura_iva_libro
    fact = sum((p.iva_libro_inicial for p in res.nuevas_facturas), CERO)
    canc = sum((q2(f.datos["iva"] * (f.datos["tc"] or Decimal(1))) for f in res.canceladas), CERO)
    nc = sum((n.iva_libro for n in res.notas_credito), CERO)
    cobr_libro = sum((f["iva_libro_mxn"] for f in filas), CERO)
    corr = sum((k["delta_iva"] for k in res.correcciones_tc if k["aplicada"]), CERO)
    lineas = [("Opening IVA por trasladar (booked)", abre), ("+ IVA on invoices issued (valid)", fact),
              ("+ IVA on invoices issued and later cancelled", canc), ("- IVA on cancelled CFDI", -canc),
              ("- IVA on credit notes", -nc), ("- IVA collected (at booked rate)", -cobr_libro),
              ("+/- Rate correction (conditional)", corr)]
    cierre = sum((p.iva_libro_pend for p in res.partidas.values()), CERO)
    cierre_fix = sum((q2(p.iva_pend * (res.fx.en(res.cierre) if p.moneda != "MXN" else Decimal(1)))
                      for p in res.partidas.values()), CERO)
    return {"filas": filas, "cobrado": cobrado, "lineas": lineas, "cierre": cierre,
            "cierre_calculado": sum((v for _, v in lineas), CERO), "cierre_a_tc_cierre": cierre_fix,
            "diferencia_cambiaria_reclasificacion": cobr_libro - cobrado}
