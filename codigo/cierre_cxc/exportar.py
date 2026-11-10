"""Excel and CSV export. Every row carries its source rows; every sheet states its review status."""
from __future__ import annotations
import csv
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import xlsxwriter
import xlsxwriter.exceptions

from .contabilidad import BUCKETS, NOMBRES
from .dinero import CERO
from .revision import clave_match, claves_excepciones, claves_polizas


def _f(x):
    return float(x) if isinstance(x, Decimal) else x


def _fuentes(lista) -> str:
    return " | ".join(lista)


def _estado(rev, clave) -> str:
    return rev.estado(clave) if rev else "pendiente"


NIVELES = ("1 Automatic", "2 To confirm", "3 Needs a person", "Reviewer decided")


def nivel_cobro(s, mov_id: str) -> str:
    """How a customer receipt was placed: by a rule, proposed for a click, left for a person, or by a reviewer."""
    p = s.res.propuestas.get(mov_id)
    if p is None or p.tipo == "no_cxc":
        return ""
    if p.modo == "manual":
        return NIVELES[3]
    return {1: NIVELES[0], 2: NIVELES[1], 3: NIVELES[2]}.get(p.nivel, "")


def conteo_niveles(s) -> dict:
    out = {n: 0 for n in NIVELES}
    for m in s.res.movimientos:
        n = nivel_cobro(s, m.id)
        if n:
            out[n] += 1
    return out


def filas_movimientos(s, rev=None) -> list:
    res = s.res
    por_mov = {}
    for a in res.aplicaciones:
        por_mov.setdefault(a.mov_id, []).append(a)
    unid = {u["mov_id"]: u for u in res.efectivo_no_identificado}
    no_cxc = {m.id for m in res.no_cxc}
    com = {m.id: m for m in res.comisiones}
    filas = []
    for m in res.movimientos:
        aps = por_mov.get(m.id, [])
        if aps:
            clas = "AR receipt - matched"
            fol = ", ".join(a.folio for a in aps)
            tipo = ", ".join(sorted({a.tipo for a in aps}))
            conf = aps[0].confianza
            razon = aps[0].razon
            alertas = " ".join(sorted({t for a in aps for t in a.alertas}))
        elif m.id in unid and unid[m.id].get("nivel") == 2:
            clas, tipo, conf = "AR receipt - proposed, to confirm", "por_confirmar", "baja"
            fol = "candidates: " + ", ".join(c["folio"] for c in unid[m.id]["candidatos"])
            razon, alertas = unid[m.id]["razon"], " ".join(unid[m.id]["alertas"])
        elif m.id in unid:
            clas, fol, tipo, conf = "AR receipt - unidentified", "", "no_identificado", "baja"
            razon, alertas = unid[m.id]["razon"], " ".join(unid[m.id]["alertas"])
        elif m.id in com:
            clas, fol, tipo, conf, razon, alertas = "Bank charge", "", "comision", "alta", "Bank commission or its IVA.", ""
        else:
            clas, fol, tipo, conf = "Not AR", "", "no_cxc", "alta"
            razon, alertas = "Payroll, supplier or other non-customer movement.", ""
        filas.append({"id": m.id, "fecha": m.fecha, "cuenta": m.cuenta, "referencia": m.referencia,
                      "descripcion": m.descripcion, "cargo": m.cargo, "abono": m.abono, "moneda": m.moneda,
                      "clasificacion": clas, "nivel": nivel_cobro(s, m.id), "facturas": fol, "tipo": tipo,
                      "confianza": conf, "razon": razon,
                      "alertas": alertas, "estado": _estado(rev, clave_match(m.id)), "fuente": m.fuente})
    return filas


def filas_por_factura(s) -> list:
    res = s.res
    nc_por = {}
    for n in res.notas_credito:
        nc_por.setdefault(n.folio_factura, []).append(n)
    ap_por = {}
    for a in res.aplicaciones:
        ap_por.setdefault(a.folio, []).append(a)
    filas = []
    for folio, p in res.partidas.items():
        aps, ncs = ap_por.get(folio, []), nc_por.get(folio, [])
        apertura = p.total_orig if p.origen == "periodo" else None
        sal_ini = None
        if p.origen == "apertura":
            sal_ini = p.libro_original_mxn
        recibido_pago = {}
        for a in aps:
            recibido_pago[a.pago_moneda] = recibido_pago.get(a.pago_moneda, CERO) + a.pago_monto
        efectivo = "; ".join(f"{v:,.2f} {k}" for k, v in recibido_pago.items())
        alivio_doc = sum((a.aplicado_doc for a in aps), CERO)
        alivio_mxn = sum((a.libro_relevado for a in aps), CERO)
        fx_real = sum((a.fx_realizada for a in aps), CERO)
        expl = []
        for n in ncs:
            expl.append(f"Credit note {n.folio} for {n.total:,.2f} reduced the balance.")
        for a in aps:
            if a.tipo == "parcial":
                expl.append(f"Partial payment; {a.saldo_despues:,.2f} {a.doc_moneda} still open.")
            elif a.tipo == "corto":
                expl.append(f"Short payment; {a.saldo_despues:,.2f} {a.doc_moneda} left open pending the customer's explanation.")
            elif a.tipo == "cruzado":
                expl.append(f"Paid in {a.pago_moneda}: equals {a.aplicado_doc:,.2f} {a.doc_moneda} at the FIX of {a.fecha}.")
            elif a.tipo == "multiple":
                expl.append("One receipt covered several invoices.")
            if a.fx_realizada:
                expl.append(f"Realized FX {a.fx_realizada:+,.2f} MXN vs booked rate.")
        if p.saldo > Decimal("0.005") and not aps and not ncs:
            expl.append("No cash received in the period.")
        filas.append({"folio": folio, "cliente": p.cliente, "moneda": p.moneda, "emision": p.fecha,
                      "vencimiento": p.vencimiento, "origen": "Opening AR" if p.origen == "apertura" else "Issued in period",
                      "total_factura": p.total_orig, "notas_credito": sum((n.total for n in ncs), CERO),
                      "efectivo_recibido": efectivo, "cxc_aliviada_doc": alivio_doc, "cxc_aliviada_mxn": alivio_mxn,
                      "fx_realizada_mxn": fx_real, "saldo_abierto_doc": p.saldo,
                      "explicacion": " ".join(expl), "fuentes": _fuentes(p.fuentes + [x for a in aps for x in a.fuentes[:1]])})
    for f in res.canceladas:
        r = f.datos
        filas.append({"folio": r["folio"], "cliente": r["cliente"], "moneda": r["moneda"], "emision": r["fecha"],
                      "vencimiento": None, "origen": "Cancelled at SAT", "total_factura": r["total"], "notas_credito": CERO,
                      "efectivo_recibido": "", "cxc_aliviada_doc": CERO, "cxc_aliviada_mxn": CERO,
                      "fx_realizada_mxn": CERO, "saldo_abierto_doc": CERO,
                      "explicacion": "Cancelled CFDI: removed from AR, aging and IVA.", "fuentes": f.fuente})
    return sorted(filas, key=lambda x: x["folio"])


def lineas_polizas(s, rev=None) -> list:
    filas = []
    for p, clave in zip(s.polizas, claves_polizas(s.polizas)):
        for l in p.lineas:
            filas.append({"poliza": p.id, "fecha": p.fecha, "tipo": p.tipo, "estado": p.estado,
                          "revision": _estado(rev, clave), "cuenta": l.cuenta, "nombre": l.nombre,
                          "debe": l.debe, "haber": l.haber, "concepto": p.concepto, "nota": p.nota,
                          "fuente": _fuentes(p.fuentes)})
    return filas


def filas_excepciones(s, rev=None) -> list:
    sev = {"alta": 0, "media": 1, "baja": 2}
    claves = dict(zip(map(id, s.excepciones), claves_excepciones(s.excepciones)))
    return [{"id": e.id, "severidad": e.severidad, "categoria": e.categoria, "referencia": e.referencia,
             "que_pasa": e.que, "por_que_importa": e.por_que, "monto_mxn": e.monto_mxn, "certeza": e.certeza,
             "accion": e.accion, "responsable": e.responsable, "fecha_limite": e.fecha_limite,
             "sin_asiento": e.no_asiento, "revision": _estado(rev, claves[id(e)]), "fuente": _fuentes(e.fuentes)}
            for e in sorted(s.excepciones, key=lambda x: (sev[x.severidad], -x.monto_mxn))]


def filas_antiguedad(s) -> list:
    return [{**{k: v for k, v in f.items() if k not in ("fuentes", "libro_mxn")}, "fuente": _fuentes(f["fuentes"])}
            for f in s.aging["filas"]]


def todas_las_tablas(s, rev=None) -> dict:
    return {
        "A1_Bank_Movements": filas_movimientos(s, rev),
        "A1_By_Invoice": filas_por_factura(s),
        "A2_Exceptions": filas_excepciones(s, rev),
        "A3_Journal_Entries": lineas_polizas(s, rev),
        "A4_Aging": filas_antiguedad(s),
        "A4_Roll_Forward": [{"concepto": k, "monto_mxn": v} for k, v in s.rollforward["lineas"]]
                           + [{"concepto": "Closing balance (calculated)", "monto_mxn": s.rollforward["cierre_calculado"]}],
        "A5_IVA_Collected": [{**{k: v for k, v in f.items() if k != "fuentes"}, "fuente": _fuentes(f["fuentes"])}
                             for f in s.iva["filas"]],
        "A5_IVA_Roll_Forward": [{"concepto": k, "monto_mxn": v} for k, v in s.iva["lineas"]]
                               + [{"concepto": "Closing IVA por trasladar", "monto_mxn": s.iva["cierre"]},
                                  # shown, not booked: 2202 of uncollected USD invoices stays at the booked rate
                                  {"concepto": "Memo (not booked): IVA por trasladar at the closing FIX",
                                   "monto_mxn": s.iva["cierre_a_tc_cierre"]},
                                  {"concepto": "Memo (not booked): difference vs booked rate. 2202 stays at the booked "
                                               "rate until collected; it is settled at the FIX of the collection date",
                                   "monto_mxn": s.iva["cierre_a_tc_cierre"] - s.iva["cierre"]}],
    }


def a_csv(s, carpeta, rev=None) -> list:
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    rutas = []
    for nombre, filas in todas_las_tablas(s, rev).items():
        ruta = carpeta / f"{nombre}.csv"
        with open(ruta, "w", newline="", encoding="utf-8-sig") as fh:
            if filas:
                w = csv.DictWriter(fh, fieldnames=list(filas[0].keys()))
                w.writeheader()
                w.writerows({k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in filas)
        rutas.append(ruta)
    return rutas


def a_excel(s, ruta, rev=None, bitacora=None, borrador: bool = True, notas: list | None = None,
            estado_texto: str | None = None) -> Path:
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(str(ruta), {"default_date_format": "yyyy-mm-dd"})
    fmt = {
        "h": wb.add_format({"bold": True, "bg_color": "#1F3A5F", "font_color": "white", "text_wrap": True, "valign": "top", "border": 1}),
        "n": wb.add_format({"num_format": "#,##0.00;[Red]-#,##0.00", "valign": "top"}),
        "t": wb.add_format({"text_wrap": True, "valign": "top"}),
        "d": wb.add_format({"num_format": "yyyy-mm-dd", "valign": "top"}),
        "b": wb.add_format({"bold": True}),
        "tot": wb.add_format({"bold": True, "num_format": "#,##0.00;[Red]-#,##0.00", "top": 1}),
        "ok": wb.add_format({"font_color": "#0B6E4F", "bold": True}),
        "mal": wb.add_format({"font_color": "#B00020", "bold": True}),
        "aviso": wb.add_format({"bg_color": "#FFF2CC", "bold": True, "text_wrap": True}),
        "titulo": wb.add_format({"bold": True, "font_size": 14}),
    }
    estado_txt = estado_texto or ("DRAFT - pending review. Nothing here is final until a reviewer approves it."
                                  if borrador else "APPROVED by the reviewer listed in the audit log.")

    # --- Summary ---
    ws = wb.add_worksheet("Summary")
    ws.set_column(0, 0, 58); ws.set_column(1, 1, 22); ws.set_column(2, 2, 70)
    ws.write(0, 0, "AR close - " + str(s.res.cierre), fmt["titulo"])
    ws.merge_range(1, 0, 1, 2, estado_txt, fmt["aviso"])
    ws.write(3, 0, "Source file", fmt["b"]); ws.write(3, 1, s.archivo)
    ws.write(4, 0, "SHA-256 of source file", fmt["b"]); ws.write(4, 1, s.huella[:16] + "...")
    fila = 6
    ws.write(fila, 0, "Key figures (MXN)", fmt["b"]); fila += 1
    claves = [(f"Opening AR ({s.res.inicio - timedelta(days=1):%d-%b}, as booked)", s.rollforward["lineas"][0][1]),
              (f"Closing AR (corrected, {s.res.cierre:%d-%b})", s.aging["total"]),
              ("   Current", s.aging["totales"]["Current"]), ("   1-30 days past due", s.aging["totales"]["1-30"]),
              ("   31-60 days past due", s.aging["totales"]["31-60"]), ("   60+ days past due", s.aging["totales"]["60+"]),
              ("Unapplied / unidentified cash (held in 2101)", s.aging["efectivo_sin_aplicar"]),
              ("IVA trasladado cobrado in the period", s.iva["cobrado"]),
              ("Closing IVA por trasladar (as booked)", s.iva["cierre"]),
              ("Realized FX result of the period", sum((a.fx_realizada for a in s.res.aplicaciones), CERO)),
              ("Unrealized FX on open USD balances", sum((r["ajuste"] for r in s.res.reval), CERO))]
    for k, v in claves:
        ws.write(fila, 0, k); ws.write_number(fila, 1, _f(v), fmt["n"]); fila += 1
    fila += 1
    ws.write(fila, 0, "Internal checks", fmt["b"]); fila += 1
    for nombre, ok, det in s.controles:
        ws.write(fila, 0, nombre); ws.write(fila, 1, "PASS" if ok else "FAIL", fmt["ok"] if ok else fmt["mal"])
        ws.write(fila, 2, det); fila += 1
    fila += 1
    ws.write(fila, 0, "Customer receipts by how they were placed", fmt["b"]); fila += 1
    expl = {NIVELES[0]: "invoice number, customer name or a learned rule, and the amount fits",
            NIVELES[1]: "only a clue (amount or language model): held in 2101 until a reviewer accepts a candidate",
            NIVELES[2]: "no clue at all: held in 2101 for a person to find out who paid",
            NIVELES[3]: "placed by a reviewer's override"}
    for n, k in conteo_niveles(s).items():
        ws.write(fila, 0, "   " + n); ws.write_number(fila, 1, k); ws.write(fila, 2, expl[n]); fila += 1
    fila += 1
    ws.write(fila, 0, "Exceptions by severity", fmt["b"]); fila += 1
    for sv in ("alta", "media", "baja"):
        ws.write(fila, 0, sv.capitalize()); ws.write_number(fila, 1, sum(1 for e in s.excepciones if e.severidad == sv)); fila += 1
    if s.res.avisos:
        fila += 1
        ws.write(fila, 0, "Data notices", fmt["b"]); fila += 1
        for a in s.res.avisos:
            ws.write(fila, 0, a); fila += 1
    for linea in (notas or []):
        fila += 1
        ws.merge_range(fila, 0, fila, 2, linea, fmt["t"])

    def hoja(nombre, filas, anchos=None, num=(), fechas=()):
        w = wb.add_worksheet(nombre[:31])
        if not filas:
            w.write(0, 0, "No rows")
            return w, []
        cols = list(filas[0].keys())
        for j, c in enumerate(cols):
            w.write(0, j, c, fmt["h"])
            w.set_column(j, j, (anchos or {}).get(c, 16))
        for i, r in enumerate(filas, start=1):
            for j, c in enumerate(cols):
                v = r[c]
                if isinstance(v, Decimal):
                    w.write_number(i, j, float(v), fmt["n"])
                elif hasattr(v, "year") and v is not None:
                    w.write_datetime(i, j, __import__("datetime").datetime(v.year, v.month, v.day), fmt["d"])
                elif v is None:
                    w.write_blank(i, j, None)
                else:
                    w.write(i, j, v, fmt["t"])
        w.freeze_panes(1, 0)
        w.autofilter(0, 0, len(filas), len(cols) - 1)
        return w, cols

    t = todas_las_tablas(s, rev)
    hoja("A1_Bank_Movements", t["A1_Bank_Movements"], {"descripcion": 44, "razon": 50, "alertas": 50, "fuente": 22})
    hoja("A1_By_Invoice", t["A1_By_Invoice"], {"cliente": 30, "explicacion": 60, "fuentes": 40, "efectivo_recibido": 22})
    w, cols = hoja("A2_Exceptions", t["A2_Exceptions"], {"que_pasa": 60, "por_que_importa": 60, "accion": 55, "sin_asiento": 40,
                                                          "fuente": 28, "certeza": 18, "categoria": 18})
    w, cols = hoja("A3_Journal_Entries", t["A3_Journal_Entries"], {"nombre": 36, "concepto": 55, "nota": 50, "fuente": 40})
    # entries not posted + why
    nw = wb.add_worksheet("A3_Not_Posted")
    nw.set_column(0, 0, 18); nw.set_column(1, 1, 90)
    nw.write(0, 0, "Item", fmt["h"]); nw.write(0, 1, "Why no entry is proposed", fmt["h"])
    i = 1
    for e in s.excepciones:
        if e.no_asiento:
            nw.write(i, 0, e.referencia, fmt["t"]); nw.write(i, 1, f"{e.categoria}: {e.no_asiento}", fmt["t"]); i += 1
    for m in s.res.no_cxc:
        nw.write(i, 0, m.id); nw.write(i, 1, f"{m.descripcion} ({m.abono or m.cargo:,.2f} {m.moneda}): not an AR movement, outside this close.", fmt["t"]); i += 1
    # aging: detail + by customer
    hoja("A4_Aging", t["A4_Aging"], {"cliente": 32, "fuente": 40})
    aw = wb.add_worksheet("A4_Aging_Summary")
    aw.set_column(0, 0, 38); aw.set_column(1, 6, 16)
    for j, c in enumerate(["Customer"] + BUCKETS + ["Total"]):
        aw.write(0, j, c, fmt["h"])
    for i, (cli, b) in enumerate(sorted(s.aging["por_cliente"].items()), start=1):
        aw.write(i, 0, cli)
        for j, bk in enumerate(BUCKETS, start=1):
            aw.write_number(i, j, _f(b[bk]), fmt["n"])
        aw.write_formula(i, 5, f"=SUM(B{i+1}:E{i+1})", fmt["n"], _f(sum(b.values())))
    n = len(s.aging["por_cliente"])
    aw.write(n + 1, 0, "Total", fmt["b"])
    for j, bk in enumerate(BUCKETS, start=1):
        col = "BCDE"[j - 1]
        aw.write_formula(n + 1, j, f"=SUM({col}2:{col}{n+1})", fmt["tot"], _f(s.aging["totales"][bk]))
    aw.write_formula(n + 1, 5, f"=SUM(F2:F{n+1})", fmt["tot"], _f(s.aging["total"]))
    aw.write(n + 3, 0, "Unapplied cash (not netted above)"); aw.write_number(n + 3, 5, -_f(s.aging["efectivo_sin_aplicar"]), fmt["n"])
    rw = hoja("A4_Roll_Forward", t["A4_Roll_Forward"], {"concepto": 70, "monto_mxn": 20})
    hoja("A5_IVA_Collected", t["A5_IVA_Collected"], {"cliente": 32, "fuente": 40})
    hoja("A5_IVA_Roll_Forward", t["A5_IVA_Roll_Forward"], {"concepto": 60, "monto_mxn": 20})
    iw = wb.get_worksheet_by_name("A5_IVA_Collected")
    n_iva = len(t["A5_IVA_Collected"])
    iw.write(n_iva + 1, 0, "Total IVA cobrado (MXN)", fmt["b"])
    iw.write_formula(n_iva + 1, 7, f"=SUM(H2:H{n_iva+1})", fmt["tot"], _f(s.iva["cobrado"]))
    if getattr(s, "reglas", None):
        hoja("Learned_Rules", [{"id": r["id"], "words": " ".join(r["tokens"]), "customer": r["cliente"],
                                "taught_by": r["usuario"], "from_receipt": r["movimiento"],
                                "description": r["descripcion"], "file": r["archivo"], "when": r["ts"]}
                               for r in s.reglas], {"words": 30, "customer": 32, "description": 44, "file": 30})
    # audit
    if bitacora:
        aud = bitacora.leer()
        aw2 = wb.add_worksheet("Audit_Log")
        aw2.set_column(0, 0, 24); aw2.set_column(1, 2, 16); aw2.set_column(3, 3, 110)
        for j, c in enumerate(["ts", "usuario", "evento", "detalle"]):
            aw2.write(0, j, c, fmt["h"])
        for i, ev in enumerate(aud, start=1):
            det = {k: v for k, v in ev.items() if k not in ("ts", "usuario", "evento")}
            aw2.write(i, 0, ev["ts"]); aw2.write(i, 1, ev["usuario"]); aw2.write(i, 2, ev["evento"])
            aw2.write(i, 3, str(det), fmt["t"])
    try:
        wb.close()
    except xlsxwriter.exceptions.FileCreateError as e:   # the file is open in Excel (Windows locks it)
        raise PermissionError(13, "file is locked", str(ruta)) from e
    return ruta
