"""One-page memo to the Controller (Part A6). Every figure is read from the engine output.

Usage:  python generar_memo.py  (writes into ../entregables)
"""
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Inches, RGBColor

from cierre_cxc.pipeline import ejecutar

RAIZ = Path(__file__).resolve().parent
DATOS = RAIZ.parent / "material" / "2026-11-06 Case_Data_Sep2026.xlsx"
DESTINO = RAIZ.parent / "entregables" / "2026-11-11 Clara - Part A memo to the Controller.docx"


def m(x):
    return f"{x:,.2f}"


def main():
    s = ejecutar(DATOS)
    r = s.res
    exc = {(e.categoria, e.referencia): e for e in s.excepciones}
    rep = [e for e in s.excepciones if e.categoria == "Missing REP"]
    fx_ar = sum(a.fx_realizada for a in r.aplicaciones)
    fx_iva = s.iva["diferencia_cambiaria_reclasificacion"]
    unreal = sum(x["ajuste"] for x in r.reval)
    unid = sum(u["mxn"] for u in r.efectivo_no_identificado)
    pue = next(e for e in s.excepciones if e.categoria == "PUE not collected")
    tasa0 = [e for e in s.excepciones if e.categoria == "IVA rate" and e.severidad == "alta"]
    tc = [e for e in s.excepciones if e.categoria == "FX rate on CFDI"]
    dup = [e for e in s.excepciones if e.categoria == "Possible duplicate"]
    canc = [e for e in s.excepciones if e.categoria == "Cancelled CFDI"]
    orden = [e for e in s.excepciones if e.categoria == "Application order"]
    por_confirmar = sorted({a.folio for a in r.aplicaciones if a.mov_id in {e.referencia for e in orden}})
    vencidas = [f["folio"] for f in s.aging["filas"] if f["dias_vencido"] > 0]

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    for lado in ("left_margin", "right_margin"):
        setattr(sec, lado, Inches(0.7))
    sec.top_margin, sec.bottom_margin = Inches(0.55), Inches(0.5)
    base = doc.styles["Normal"]
    base.font.name, base.font.size = "Arial", Pt(9)
    base.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    base.paragraph_format.space_after = Pt(2)
    base.paragraph_format.space_before = Pt(0)
    azul = RGBColor(0x1F, 0x3A, 0x5F)

    def p(texto="", bold=False, size=None, color=None, after=2):
        par = doc.add_paragraph()
        par.paragraph_format.space_after = Pt(after)
        if texto:
            run = par.add_run(texto)
            run.bold = bold
            if size:
                run.font.size = Pt(size)
            if color:
                run.font.color.rgb = color
        return par

    def titulo(texto):
        par = p(texto, bold=True, size=10, color=azul, after=1)
        par.paragraph_format.space_before = Pt(5)
        borde = OxmlElement("w:pBdr")
        b = OxmlElement("w:bottom")
        for k, v in (("w:val", "single"), ("w:sz", "4"), ("w:space", "1"), ("w:color", "1F3A5F")):
            b.set(qn(k), v)
        borde.append(b)
        par._p.get_or_add_pPr().append(borde)

    def vineta(etiqueta, texto):
        par = doc.add_paragraph(style="List Bullet")
        par.paragraph_format.space_after = Pt(1)
        if etiqueta:
            par.add_run(etiqueta).bold = True
        par.add_run(texto)

    p("MEMO - September 2026 AR close, Clara Servicios MX", bold=True, size=13, color=azul, after=1)
    p("To: Controller   |   From: Diego Verduzco, Mexico accounting   |   Prepared as of: 1 October 2026 (September close)",
      after=3)

    titulo("1. Headline results (MXN)")
    vineta("Closing AR, corrected: ", f"{m(s.aging['total'])} (opening {m(s.rollforward['lineas'][0][1])}). "
           f"Current {m(s.aging['totales']['Current'])}; 1-30 days {m(s.aging['totales']['1-30'])}; nothing over 30 days.")
    vineta("Cash application: ", f"{len({a.mov_id for a in r.aplicaciones})} receipts applied to {len(r.aplicaciones)} invoices "
           f"(AR relieved {m(sum(a.libro_relevado for a in r.aplicaciones))}), including a credit note, a partial, a short "
           f"and a cross-currency payment; {m(unid)} unidentified, held in 2101. Payroll and a supplier payment are not AR.")
    vineta("IVA: ", f"trasladado cobrado {m(s.iva['cobrado'])} (cash basis, at the FIX of each collection date as the case instructs); "
           f"closing IVA por trasladar {m(s.iva['cierre'])} at booked rates (at the closing FIX it would be "
           f"{m(s.iva['cierre_a_tc_cierre'] - s.iva['cierre'])} higher; not booked, it settles at the FIX of collection).")
    vineta("FX: ", f"realized {m(fx_ar + fx_iva)} (receivables {m(fx_ar)}, IVA reclassification {m(fx_iva)}); "
           f"unrealized on open USD balances {m(unreal)}.")
    vineta("Ledger: ", f"{len(s.polizas)} proposed entries, all balanced; "
           f"{sum(p.estado == 'condicionada' for p in s.polizas)} are conditional on tax/ledger confirmation. "
           f"{len(s.excepciones)} exceptions logged ({sum(e.severidad == 'alta' for e in s.excepciones)} high).")

    titulo("2. The three issues to worry about")
    vineta("Payments without REP. ", f"{len(rep)} September payments on invoices {', '.join(e.referencia for e in rep)} have no "
           f"Complemento de Pago (IVA collected {m(sum(e.monto_mxn for e in rep))}). The REP is due by the 5th calendar day "
           f"of the following month (RMF 2026 rule 2.7.1.32): {rep[0].fecha_limite:%d %b %Y}. "
           + (f"{', '.join(por_confirmar)} were applied oldest first because receipts {', '.join(e.referencia for e in orden)} "
              "named no invoice; AR confirms with the customers before those REPs are stamped." if orden else ""))
    vineta("PUE invoice not collected. ", f"{pue.referencia} ({m(pue.monto_mxn)}) was stamped as paid in one exhibition (PUE) "
           "but no cash arrived. SAT sees it as paid while our books and aging show it open; the customer may credit its "
           "IVA only when it pays (LIVA art. 5o fr. III). It must be cancelled and reissued as PPD with payment form 99 "
           "(RMF 2026 rule 2.7.1.39).")
    vineta("Billing quality. ", "; ".join(
        [f"{e.referencia} billed at 0% IVA to a Mexican customer (exposure {m(e.monto_mxn)})" for e in tasa0]
        + [f"{e.referencia} booked at its CFDI rate, not the FIX: the CFDI is valid, the books need a conditional "
           f"correction of {m(e.monto_mxn)}" for e in tc]
        + [f"{e.referencia} look like duplicates ({m(e.monto_mxn)} each)" for e in dup]
        + [f"{e.referencia} is cancelled at SAT but was booked ({m(e.monto_mxn)})" for e in canc]) + ".")

    titulo("3. Compliance actions")
    acciones = [(f"{e.referencia}: {e.accion}", e.responsable, e.fecha_limite) for e in
                [rep[0], pue] + tasa0 + tc + dup + [exc[k] for k in exc if k[0] == "Unidentified cash"]]
    acciones[0] = (f"Issue and stamp the {len(rep)} missing REPs ({', '.join(e.referencia for e in rep)})"
                   + (f"; for {', '.join(por_confirmar)}, after the customers confirm the invoice." if orden else "."),
                   rep[0].responsable, rep[0].fecha_limite)
    acciones[1:1] = [(f"{e.referencia}: ask the customer which invoice it paid; if it names a newer one, "
                      "reassign the receipt before the REP.", e.responsable, e.fecha_limite)
                     for e in sorted(orden, key=lambda e: e.fecha_limite)]
    texto_tc = {f"{e.referencia}:": f"{e.referencia}: approve or reject the conditional rate correction; "
                "reissuing the CFDI is optional." for e in tc}
    acciones = [(t, "Controller", fecha) if (t := next((t for k, t in texto_tc.items() if acc.startswith(k)), None))
                else (acc, resp, fecha) for acc, resp, fecha in acciones]
    tabla = doc.add_table(rows=1, cols=3)
    tabla.style = "Table Grid"
    tabla.alignment = WD_TABLE_ALIGNMENT.CENTER
    for c, t in zip(tabla.rows[0].cells, ("Action", "Owner", "By")):
        c.text = t
        c.paragraphs[0].runs[0].bold = True
        sombra = OxmlElement("w:shd")
        for k, v in (("w:val", "clear"), ("w:color", "auto"), ("w:fill", "DCE6F1")):
            sombra.set(qn(k), v)
        c._tc.get_or_add_tcPr().append(sombra)
    for acc, resp, fecha in acciones:
        fila = tabla.add_row().cells
        fila[0].text, fila[1].text, fila[2].text = acc, resp, f"{fecha:%d %b %Y}"
    anchos = (Inches(5.3), Inches(1.0), Inches(0.8))
    for row in tabla.rows:
        for c, w in zip(row.cells, anchos):
            c.width = w
            for par in c.paragraphs:
                par.paragraph_format.space_after = Pt(0)
                for run in par.runs:
                    run.font.size = Pt(8)

    titulo("4. Standards and laws applied")
    vineta("IFRS: ", "IFRS 9 (receivables at amortized cost; the simplified expected-credit-loss approach needs a provision "
           f"matrix built on loss history, which the data does not include, so no allowance is proposed here; "
           f"{' and '.join(vencidas)} should feed it); IFRS 15 (the credit note reduces revenue); IAS 21, as generally "
           "applied and to be confirmed against the full standard (USD items at the transaction-date rate, monetary "
           "balances revalued at the closing FIX, differences in P&L; realized vs. unrealized split). Opening USD "
           "balances were not revalued at 31-Aug (immaterial): left inside September's FX unless the Controller books "
           "an out-of-period adjustment.")
    vineta("Mexican tax: ", "LIVA arts. 1o.-B and 17 (IVA on services is due when cash is effectively collected, advances "
           "included) and art. 5o fr. III (the customer credits IVA only once it is paid); LIVA art. 1o (16% general "
           "rate) and art. 29 fr. IV (0% only for qualifying exports of services); RMF 2026 rule 2.7.1.32 (REP by the 5th "
           "calendar day of the following month) and rule 2.7.1.39 (a PUE not paid by the end of its month is cancelled "
           "and replaced by a PPD/99 CFDI related as a substitution, key 04 in Anexo 20); Anexo 20 (TipoCambio is "
           "required for USD; using the FIX is optional, and a rate more than 35% off needs a confirmation key). "
           "Items marked 'to confirm' go to the tax team before posting.")
    p("Detail, source rows and every entry: Part A workbook. The same numbers are produced by the Part B tool.",
      size=8, after=0)

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    doc.save(DESTINO)
    print(DESTINO)


if __name__ == "__main__":
    main()
