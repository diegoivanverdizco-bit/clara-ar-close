"""A second dataset with the same schema but different content, to prove the tool is not tuned to the case.

October 2026, other customers, other amounts, slightly different header spelling and sheet names,
and patterns the September case does not contain (one receipt for two invoices, an overpayment,
a cross-currency receipt with a small rate difference).
"""
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def u(folio):
    return str(uuid.uuid5(NS, folio)).upper()


def fix_oct(dia: date) -> float:
    base = 17.30 + (dia.day % 7) * 0.013
    return round(base, 4)


def crear(ruta) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "README"
    ws.append(["Synthetic dataset for robustness tests"])

    cfdi = wb.create_sheet("CFDI_Emitidos")
    cfdi.append(["uuid", "Folio", "Fecha Emisión", "RFC Receptor", "Cliente", "Moneda", "Tipo de Cambio", "Subtotal",
                 "Tasa IVA", "IVA", "Total", "Metodo Pago", "Forma Pago", "Tipo Comprobante", "CFDI Relacionado (UUID)",
                 "Estatus SAT", "Notas"])

    def fac(folio, fecha, rfc, cli, mon, tc, sub, tasa, metodo="PPD", forma="99", tipo="I", rel=None, estatus="Vigente"):
        iva = round(sub * tasa, 2)
        cfdi.append([u(folio), folio, fecha, rfc, cli, mon, tc, sub, tasa, iva, round(sub + iva, 2), metodo, forma, tipo,
                     rel, estatus, "Net 30"])

    # open at 30-Sep
    fac("B-2001", datetime(2026, 8, 20), "TRI100101AA1", "Textiles Rio SA de CV", "MXN", None, 100000, 0.16)
    fac("B-2002", datetime(2026, 9, 10), "DSO110202BB2", "Distribuidora Sol SA de CV", "MXN", None, 200000, 0.16)
    fac("B-2003", datetime(2026, 9, 15), "XEXX010101000", "Pacific Foods Inc", "USD", 17.2, 10000, 0.0)
    fac("B-2004", datetime(2026, 9, 20), "LNO120303CC3", "Logistica Norte SA de CV", "MXN", None, 50000, 0.16)
    fac("B-2005", datetime(2026, 9, 25), "LNO120303CC3", "Logistica Norte SA de CV", "MXN", None, 30000, 0.16)
    # October
    fac("B-2006", datetime(2026, 10, 3), "TRI100101AA1", "Textiles Rio SA de CV", "MXN", None, 50000, 0.16)
    fac("B-2007", datetime(2026, 10, 5), "DSO110202BB2", "Distribuidora Sol SA de CV", "USD", fix_oct(date(2026, 10, 5)), 5000, 0.16)
    fac("B-2008", datetime(2026, 10, 7), "MVE130404DD4", "Mercado Verde SA de CV", "MXN", None, 100000, 0.16, metodo="PUE", forma="03")
    fac("B-2009", datetime(2026, 10, 9), "MVE130404DD4", "Mercado Verde SA de CV", "MXN", None, 20000, 0.16, estatus="Cancelado")
    fac("NC-701", datetime(2026, 10, 8), "DSO110202BB2", "Distribuidora Sol SA de CV", "MXN", None, 10000, 0.16, metodo="PUE",
        forma="15", tipo="E", rel=u("B-2002"))

    banco = wb.create_sheet("Bancos_Oct2026")
    banco.append(["Fecha", "Cuenta", "Referencia", "Concepto", "Cargo", "Abono", "Moneda"])
    fx_tabla = {}
    d0 = date(2026, 9, 25)
    while d0 <= date(2026, 11, 3):
        # leave out some weekend days on purpose: the tool must fall back to the previous rate
        if d0.weekday() < 5:
            fx_tabla[d0] = fix_oct(d0)
        d0 += timedelta(days=1)
    tc_20 = fix_oct(date(2026, 10, 20))
    usd_en_mxn = round(5800 * tc_20 * 1.003, 2)         # the customer's bank used a rate 0.3% off the FIX
    filas = [
        (datetime(2026, 10, 2), "MXN-001", "NOM1002", "PAGO NOMINA QNA", 150000, None, "MXN"),
        (datetime(2026, 10, 6), "MXN-001", "SPEI-1", "SPEI RECIBIDO TEXTILES RIO PAGO FACTURA B-2001", None, 116000, "MXN"),
        (datetime(2026, 10, 12), "MXN-001", "SPEI-2", "SPEI RECIBIDO DISTRIBUIDORA SOL", None, 220400, "MXN"),
        (datetime(2026, 10, 14), "USD-001", "WIRE-1", "WIRE IN PACIFIC FOODS INV B-2003", None, 10000, "USD"),
        (datetime(2026, 10, 16), "MXN-001", "SPEI-3", "SPEI RECIBIDO LOGISTICA NORTE PAGO FACTURAS", None, 92800, "MXN"),
        (datetime(2026, 10, 20), "MXN-001", "SPEI-4", "SPEI RECIBIDO DISTR SOL CONV USD", None, usd_en_mxn, "MXN"),
        (datetime(2026, 10, 22), "MXN-001", "SPEI-5", "SPEI RECIBIDO TEXTILES RIO", None, 57500, "MXN"),
        (datetime(2026, 10, 25), "MXN-001", "SPEI-6", "SPEI RECIBIDO PAGO", None, 15000, "MXN"),
        (datetime(2026, 10, 27), "MXN-001", "SPEI-7", "SPEI RECIBIDO MERCADO VERDE", None, 150000, "MXN"),
        (datetime(2026, 10, 28), "MXN-001", "INT1028", "INTERESES GANADOS", None, 320, "MXN"),
        (datetime(2026, 10, 30), "MXN-001", "COM1030", "COMISION MANEJO DE CUENTA", 500, None, "MXN"),
        (datetime(2026, 10, 30), "MXN-001", "IVA1030", "IVA COMISION", 80, None, "MXN"),
    ]
    for f in filas:
        banco.append(list(f))

    rep = wb.create_sheet("REP_Emitidos")
    rep.append(["UUID REP", "Fecha de pago", "UUID factura relacionada", "Serie-Folio", "Num. parcialidad", "Saldo anterior",
                "Importe pagado", "Saldo insoluto", "Moneda de pago", "TC pago"])
    rep.append([u("REP1"), datetime(2026, 10, 6), u("B-2001"), "B-2001", 1, 116000, 116000, 0, "MXN", 1])
    rep.append([u("REP3"), datetime(2026, 10, 14), u("B-2003"), "B-2003", 1, 10000, 10000, 0, "USD", fix_oct(date(2026, 10, 14))])

    ap = wb.create_sheet("AR_Apertura_30Sep")
    ap.append(["Serie-Folio", "Fecha emision", "Cliente", "Moneda", "Saldo (moneda origen)", "TC 30-Sep", "Saldo MXN"])
    ap.append(["B-2001", datetime(2026, 8, 20), "Textiles Rio SA de CV", "MXN", 116000, 1, 116000])
    ap.append(["B-2002", datetime(2026, 9, 10), "Distribuidora Sol SA de CV", "MXN", 232000, 1, 232000])
    ap.append(["B-2003", datetime(2026, 9, 15), "Pacific Foods Inc", "USD", 10000, 17.2, 172000])
    ap.append(["B-2004", datetime(2026, 9, 20), "Logistica Norte SA de CV", "MXN", 58000, 1, 58000])
    ap.append(["B-2005", datetime(2026, 9, 25), "Logistica Norte SA de CV", "MXN", 34800, 1, 34800])

    fxs = wb.create_sheet("TC_FIX_Diario")
    fxs.append(["Fecha", "Tipo de cambio aplicable (MXN por USD)"])
    for k, v in sorted(fx_tabla.items()):
        fxs.append([datetime(k.year, k.month, k.day), v])
    wb.create_sheet("Catalogo_Cuentas").append(["Cuenta", "Nombre"])

    ruta = Path(ruta)
    wb.save(ruta)
    return ruta
