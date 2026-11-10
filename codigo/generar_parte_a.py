"""Builds the Part A workbook for the case from the tool's output plus the preparer's judgment notes.

Usage:  python generar_parte_a.py  (writes into ../entregables)
"""
from pathlib import Path

from cierre_cxc.exportar import a_excel
from cierre_cxc.pipeline import ejecutar

RAIZ = Path(__file__).resolve().parent
DATOS = RAIZ.parent / "material" / "2026-11-06 Case_Data_Sep2026.xlsx"
DESTINO = RAIZ.parent / "entregables" / "2026-11-11 Clara - Part A AR close Sep 2026.xlsx"

NOTAS = [
    "Prepared by Diego Verduzco for the Clara Mexico AR case. Every figure comes from the Part B tool run on "
    "Case_Data_Sep2026.xlsx and was checked by hand; the source row of every number is in the 'fuente' column.",
    "Judgment 1 - B13 (127,600 from Servicios Digitales Omega) names no invoice, so it is applied oldest first: "
    "104,400 to A-1006 (overdue) and 23,200 to A-1015 (SAT payments complement guide 2.0, pp. 4-5, numerals 3-4: "
    "if the payer does not indicate the CFDI within 5 calendar days, the oldest pending one is paid). The amount "
    "equals A-1015 exactly: AR asks Omega by 5-Oct; if it names A-1015, the override reassigns it before the REP.",
    "Judgment 2 - B12 (202,795.84 MXN from Manufacturas Bajio) names no invoice either: it goes to the older A-1013 "
    "(USD 11,600 of 13,920; 202,795.84 / FIX 29-Sep 17.4824 = 11,600.00), although it equals A-1018 exactly. "
    "A-1013 was stamped at a rate other than the FIX (the CFDI is still valid): AR asks Bajio by 4-Oct which invoice it meant.",
    "Judgment 3 - B11 (86,850 from Farmacias del Valle) is a short payment on A-1010 (150 left open). A-1010 and "
    "A-1011 are identical (same day, customer and amount): possible duplicate, kept in AR until Sales confirms; if it is, A-1011 (no cash) is the one to cancel.",
    "Judgment 4 - B08 (34,800, 'Comercializadora Zeta') matches no customer or invoice: held in 2101; IVA of "
    "4,800 not recognized until it is identified. If it pays Clara services, its IVA belongs in the September return "
    "(due 17-Oct; LIVA arts. 1o.-B and 5o.-D); if it is a mistake, it is refunded. Lead for AR: 34,800 is exactly half "
    "of Retail Norte's open A-1003 (69,600), a customer already paying in instalments.",
    "Judgment 5 - Realized FX is measured against the booked rate. The opening USD balances were never revalued at "
    "31-Aug (net +544.68 MXN), so that amount sits inside September's realized FX (exception, no separate entry).",
    "Judgment 6 - IVA reclassification: 2202 is relieved at the booked rate and 2201 is credited at the FIX of the "
    "collection date (cash-basis convention); the difference goes to FX. Net FX on the receivable then equals FX on "
    "the revenue portion only.",
    "Conditional entries (marked 'condicionada'): rate correction of A-1013 (CFDI 16.8728 vs FIX 17.4228) and the "
    "reversal of the cancelled A-1016. A-1013: the Controller approves or rejects it (the CFDI is valid; the "
    "difference is in the books). A-1016: only if the invoicing entry is still in the ledger.",
]


def main():
    s = ejecutar(DATOS)
    assert s.controles_ok, [c for c in s.controles if not c[1]]
    a_excel(s, DESTINO, notas=NOTAS, borrador=False,
            estado_texto="Part A answer - prepared by Diego Verduzco. Entries are proposals for the Controller's approval.")
    print(DESTINO)


if __name__ == "__main__":
    main()
