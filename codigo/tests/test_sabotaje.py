"""Human-error sabotage of the case workbook (task A2). Every error found while sabotaging the tool stays here.

Each error must end in one of three states:
- prevented: the tool reads it correctly, so the result is the same as with clean data;
- detected: the tool stops with a message that says where and what to do, or the result shows a notice or an
  exception that names the problem;
- accepted risk: written down and approved by Diego (none so far).
"""
import os
import stat
from datetime import datetime

import openpyxl
import pytest

from cierre_cxc.cargar import SchemaError
from cierre_cxc.cli import main
from cierre_cxc.pipeline import ejecutar

B, C, R, A, T = "Bancos_Sep2026", "CFDI_Emitidos", "REP_Emitidos", "AR_Apertura_31Ago", "TC_FIX_Diario"


def huella(s):
    return (s.res.cierre, s.aging["total"], s.iva["cobrado"], s.iva["cierre"], len(s.res.aplicaciones),
            len(s.res.efectivo_no_identificado), len(s.polizas), s.controles_ok,
            sum(a.fx_realizada for a in s.res.aplicaciones), s.res.apertura_libro)


def señales(s):
    return list(s.res.avisos) + [f"{e.categoria} {e.referencia} {e.que}" for e in s.excepciones]


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


# ---------------------------------------------------------------------------------------------------------
# Detected: the run stops with a message that names the sheet, row and column, and says what to do.
ERRORES = [
    ("amount typed with thousands separators", celda(B, 3, 6, "290,000.00"), "row 3, column 'Abono'"),
    ("amount with a currency sign", celda(B, 3, 6, "$290,000.00"), "is not a number"),
    ("decimal comma", celda(B, 3, 6, "290000,00"), "is not a number"),
    ("Excel error value", celda(B, 3, 6, "#VALUE!"), "is not a number"),
    ("REP amount as text", celda(R, 2, 7, "290,000.00"), "column 'Importe pagado'"),
    ("REP date not a date", celda(R, 2, 2, "10-sep"), "invalid payment date"),
    ("CFDI date month/day that cannot be day/month", celda(C, 2, 3, "07/15/2026"), "invalid issue date"),
    ("unknown column name", celda(B, 1, 6, "Monto entrada"), "missing columns: abono"),
    ("same header twice", celda(B, 1, 2, "Fecha"), "appears more than once"),
    ("sheet renamed", lambda wb: setattr(wb[B], "title", "Estado de cuenta"), "Sheets in the file"),
    ("totals row pasted at the end", agregar(B, ["TOTAL", None, None, None, 1, 2, None]), "totals or notes row"),
    ("CFDI row pasted twice", repetir(C, 2), "CFDI folio A-1001 appears twice"),
    ("opening row pasted twice", repetir(A, 2), "Opening AR folio A-1001 appears twice"),
    ("REP row pasted twice", repetir(R, 2), "appears twice"),
    ("negative deposit", celda(B, 3, 6, -290000), "negative amount"),
    ("negative withdrawal", celda(B, 2, 5, -285000), "negative amount"),
    ("cargo and abono in the same row", celda(B, 3, 5, 1000), "has both a cargo"),
    ("IVA rate typed as a percentage", celda(C, 2, 9, 16), "looks like a percentage"),
    ("empty currency", lambda wb: [setattr(r[6], "value", None) for r in wb[B].iter_rows(min_row=2)
                                   if r[6].value == "USD"][:1], "currency is empty"),
    ("unsupported currency", celda(B, 3, 7, "EUR"), "only MXN and USD"),
    ("negative exchange rate", celda(T, 40, 2, -18), "must be greater than zero"),
    ("negative CFDI amount", celda(C, 2, 8, -250000), "subtotal is negative"),
    ("negative opening balance", celda(A, 2, 5, -290000), "saldo is negative"),
    ("no opening AR sheet", lambda wb: wb.remove(wb[A]), "add the sheet with its headers only"),
    ("opening AR sheet without header", lambda wb: wb[A].delete_rows(1, 100), "is empty"),
    ("bank sheet with only the header", lambda wb: wb[B].delete_rows(2, 100), "period of the close cannot be determined"),
    ("whole bank moved to another month", lambda wb: [setattr(r[0], "value", datetime(2026, 8, r[0].value.day))
                                                       for r in wb[B].iter_rows(min_row=2)], "Add the FIX rates"),
    ("empty CFDI type", celda(C, 12, 14, None), "CFDI type None is not valid"),
    ("old month's bank sheet left in the file", lambda wb: setattr(wb.copy_worksheet(wb[B]), "title", "Bancos_Ago2026"),
     "More than one sheet looks like 'banco'"),
    ("same FX date twice with different rates", agregar(T, [datetime(2026, 9, 10), 19.5]), "appears twice with different rates"),
    ("empty CFDI total (formula without stored value)", celda(C, 2, 11, None), "the cell is empty"),
    # round 3
    ("MXN invoice with an exchange rate", celda(C, 12, 7, 17.5), "is in MXN but has exchange rate"),
    ("USD invoice marked as MXN", celda(C, 15, 6, "MXN"), "is in MXN but has exchange rate"),
    ("opening currency differs from the CFDI", celda(A, 5, 4, "MXN"), "but its CFDI is in USD"),
    ("typo in the opening MXN balance", celda(A, 2, 7, 2900000), "must equal its balance"),
    ("typo in the opening exchange rate", celda(A, 5, 6, 1.8), "but the MXN balance says"),
    ("installment number as text", celda(R, 2, 5, "uno"), "column 'Num. parcialidad'"),
    # round 4
    ("FX date that is not a date", celda(T, 40, 1, "sep-10"), "'sep-10' is not a date"),
    ("FX dates typed month/day", lambda wb: [setattr(r[0], "value", r[0].value.strftime("%m/%d/%Y"))
                                             for r in wb[T].iter_rows(min_row=2)], "'08/13/2026' is not a date"),
    # round 5 (independent tester)
    ("invoice typed as CFDI type T", celda(C, 10, 14, "T"), "Only I (invoice) and E (credit note) are processed"),
    ("opening USD row with no rate and no MXN balance", lambda wb: [celda(A, 5, 6, None)(wb), celda(A, 5, 7, None)(wb)],
     "has neither a rate nor an MXN balance"),
    ("SALDO INICIAL row in the bank sheet", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SALDO INICIAL", None, 1500000, "MXN"]),
     "looks like a balance line"),
    ("SALDO FINAL row in the bank sheet", agregar(B, [datetime(2026, 9, 30), "MXN-001", "", "Saldo final", None, 1800000, "MXN"]),
     "looks like a balance line"),
    # round 6
    ("abbreviated balance line SDO INICIAL", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SDO INICIAL", None, 1500000, "MXN"]),
     "looks like a balance line"),
    ("abbreviated balance line SALDO INIC.", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SALDO INIC.", None, 1500000, "MXN"]),
     "looks like a balance line"),
    ("exchange-rate typo far outside the band", celda(T, 40, 2, 1.8), "is outside 5-50 MXN per USD"),
    ("opening USD row booked at rate 1", lambda wb: [celda(A, 5, 6, 1)(wb), celda(A, 5, 7, None)(wb)],
     "is outside 5-50 MXN per USD"),
    ("opening USD row at rate 1 with MXN balance = USD balance", lambda wb: [celda(A, 5, 6, 1)(wb), celda(A, 5, 7, 23200)(wb)],
     "is outside 5-50 MXN per USD"),
    ("USD invoice stamped at rate 1", celda(C, 15, 7, 1), "is outside 5-50 MXN per USD"),
    ("invoice typed as E without a related CFDI", celda(C, 10, 14, "E"), "with no related CFDI"),
    # round 7
    ("balance line SALDO AL <date>", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SALDO AL 31-AGO-2026", None, 1500000, "MXN"]),
     "looks like a balance line"),
    ("balance line OPENING BALANCE", agregar(B, [datetime(2026, 9, 1), "USD-001", "", "OPENING BALANCE", None, 50000, "USD"]),
     "looks like a balance line"),
    ("decorated balance line *** SALDO INICIAL ***", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "*** SALDO INICIAL ***",
                                                                 None, 1500000, "MXN"]), "looks like a balance line"),
    ("balance line SALDO DEL PERIODO ANTERIOR", agregar(B, [datetime(2026, 9, 1), "MXN-001", "", "SALDO DEL PERIODO ANTERIOR",
                                                            None, 1500000, "MXN"]), "looks like a balance line"),
    ("FX table quoted as USD per MXN", lambda wb: [setattr(r[1], "value", 1 / r[1].value) for r in wb[T].iter_rows(min_row=2)],
     "If it is quoted as USD per MXN, invert it"),
    ("FX columns swapped", lambda wb: [(setattr(r[0], "value", r[1].value), setattr(r[1], "value", v))
                                       for r in wb[T].iter_rows(min_row=2) for v in [r[0].value]], "is not a number"),
]


@pytest.mark.parametrize("nombre,mutar,mensaje", ERRORES, ids=[e[0] for e in ERRORES])
def test_error_humano_se_detiene_con_mensaje_claro(saboteado, nombre, mutar, mensaje):
    with pytest.raises(SchemaError) as e:
        ejecutar(saboteado(mutar))
    assert mensaje in str(e.value)


# ---------------------------------------------------------------------------------------------------------
# Prevented: the tool reads the altered input correctly, so every key figure is the same as with clean data.
PREVISTOS = [
    ("amount as text with spaces", celda(B, 3, 6, " 290000 ")),
    ("date as ISO text", celda(B, 3, 1, "2026-09-10")),
    ("date as text with time", celda(B, 3, 1, "2026-09-10 00:00:00")),
    ("date with time", celda(B, 3, 1, datetime(2026, 9, 10, 15, 30))),
    ("UUID in lower case", lambda wb: setattr(wb[C].cell(2, 1), "value", wb[C].cell(2, 1).value.lower())),
    ("UUID wrapped in braces", lambda wb: setattr(wb[C].cell(2, 1), "value", "{" + wb[C].cell(2, 1).value + "}")),
    ("folio with trailing space", celda(C, 2, 2, "A-1001 ")),
    ("numeric folio stored as 1001.0", lambda wb: [celda(C, 2, 2, 1001.0)(wb), celda(A, 2, 1, 1001)(wb), celda(R, 2, 4, 1001)(wb)]),
    ("columns moved", lambda wb: [setattr(r[3], "value", r[4].value) or setattr(r[4], "value", v)
                                  for r in wb[B].iter_rows() for v in [r[3].value]]),
    ("column named Depósito", celda(B, 1, 6, "Depósito")),
    ("column named Ingreso", celda(B, 1, 6, "Ingreso")),
    ("row of blank spaces", agregar(B, [" ", " ", None, None, None, None, None])),
    ("extra column with data", lambda wb: [setattr(wb[B].cell(i, 8), "value", "x") for i in range(1, 16)]),
    ("currency written as Pesos", celda(B, 3, 7, "Pesos")),
    ("CFDI type written as Ingreso", celda(C, 2, 14, "Ingreso")),
    ("CFDI type in lower case", celda(C, 12, 14, "i")),
    ("credit note type written as Egreso", lambda wb: [setattr(r[13], "value", "Egreso")
                                                       for r in wb[C].iter_rows(min_row=2) if r[13].value == "E"]),
    ("RFC in lower case with spaces", lambda wb: setattr(wb[C].cell(12, 4), "value", " " + wb[C].cell(12, 4).value.lower() + " ")),
    ("payment method in lower case", celda(C, 12, 12, "ppd")),
    ("same FX row pasted twice", lambda wb: wb[T].append([c.value for c in wb[T][40]])),
    ("title row above the header", lambda wb: wb[B].insert_rows(1)),
    ("customer name differs in the opening AR", celda(A, 2, 3, "Otro Cliente SA")),
    ("headers in upper case with spaces and line breaks", lambda wb: [setattr(c, "value", f"  {str(c.value).upper()}\n")
                                                                       for c in wb[B][1]]),
    ("sheet names in lower case", lambda wb: [setattr(ws, "title", ws.title.lower()) for ws in wb.worksheets]),
    ("CFDI date with time 23:59", celda(C, 12, 3, datetime(2026, 9, 4, 23, 59))),
    ("amount with float noise", celda(B, 3, 6, 290000.0000001)),
    ("description mentions a folio that does not exist", celda(B, 3, 4, "SPEI RECIBIDO GPO ALFA LOGISTICA PAGO FACT A-9999")),
    # "empty description on a unique amount" moved to test_niveles.py: it is now a level-2 proposal, not applied
    # round 5
    ("title row above the FX header", lambda wb: wb[T].insert_rows(1)),
    ("related UUID of the credit note in braces", lambda wb: [setattr(r[14], "value", "{" + r[14].value + "}")
                                                              for r in wb[C].iter_rows(min_row=2) if r[14].value]),
    ("bank sheet in reverse order", lambda wb: _invertir(wb[B])),
    # round 6
    ("a real receipt that mentions SALDO ANTERIOR", celda(B, 4, 4, "SPEI RECIBIDO RETAIL NORTE PAGO SALDO ANTERIOR")),
    # round 7
    ("a real receipt that starts with SALDO FINAL and names the invoice", celda(B, 4, 4, "SALDO FINAL FACT A-1003 RETAIL NORTE")),
    ("a real receipt that starts with SALDO ANT and names the invoice", celda(B, 4, 4, "SALDO ANT A-1003 RETAIL NORTE")),
    ("Banxico N/E on a day without rate", lambda wb: [setattr(r[1], "value", "N/E") for r in wb[T].iter_rows(min_row=2)
                                                       if r[0].value.weekday() >= 5 and r[0].value.month == 8]),
]


def _invertir(ws):
    filas = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    for i, fila in enumerate(reversed(filas), start=2):
        for j, v in enumerate(fila, start=1):
            ws.cell(i, j).value = v


@pytest.mark.parametrize("nombre,mutar", PREVISTOS, ids=[p[0] for p in PREVISTOS])
def test_error_humano_previsto_no_cambia_el_resultado(saboteado, salida, nombre, mutar):
    assert huella(ejecutar(saboteado(mutar))) == huella(salida)


# ---------------------------------------------------------------------------------------------------------
# Detected: the close runs, and a notice or an exception names the problem so the reviewer sees it.
SEÑALADOS = [
    ("date as text that can be day/month or month/day", celda(B, 3, 1, "09/10/2026"), "was read as day/month"),
    ("bank row from the next month", agregar(B, [datetime(2026, 10, 1), "MXN-001", "X", "SPEI RECIBIDO", None, 1000, "MXN"]),
     "Bank row out of period"),
    ("bank row from the previous month", agregar(B, [datetime(2026, 8, 28), "MXN-001", "X", "SPEI RECIBIDO", None, 1000, "MXN"]),
     "Bank row out of period"),
    ("year typo in a bank date", celda(B, 3, 1, datetime(2025, 9, 10)), "Bank row out of period"),
    ("CFDI from the next month", agregar(C, ["AAAAAAAA-0000-0000-0000-000000000001", "A-2001", datetime(2026, 10, 2),
                                            "GAL100101AB1", "Grupo Alfa Logistica SA de CV", "MXN", None, 1000, 0.16, 160,
                                            1160, "PPD", "99", "I", None, "Vigente", "Net 30"]), "after the close"),
    ("bank row pasted twice", repetir(B, 3), "Possible duplicate bank row"),
    ("bank row without amount", celda(B, 3, 6, 0), "bank row with no amount"),
    ("UUID turned into scientific notation", celda(C, 2, 1, "5.7E+38"), "does not look like a UUID"),
    ("REP folio typed differently", celda(R, 2, 4, "A1001"), "linked to A-1001"),
    ("REP related to another invoice's UUID", lambda wb: setattr(wb[R].cell(2, 3), "value", wb[C].cell(4, 1).value),
     "linked to A-1003"),
    ("USD account row typed as MXN", lambda wb: [setattr(r[6], "value", "MXN") for r in wb[B].iter_rows(min_row=2)
                                                 if r[6].value == "USD"][:1], "looks like a USD account"),
    ("exchange-rate typo", celda(T, 40, 2, 15.0), "more than 10% away"),
    ("opening balance larger than the CFDI", lambda wb: [celda(C, 2, k, 0)(wb) for k in (8, 10, 11)], "larger than its CFDI total"),
    ("opening AR sheet with no rows", lambda wb: wb[A].delete_rows(2, 100), "not in the opening AR"),
    ("empty SAT status", celda(C, 12, 16, None), "unknown SAT status"),
    ("unknown SAT status", celda(C, 12, 16, "Pendiente"), "unknown SAT status"),
    ("empty customer name", celda(C, 12, 5, None), "has no customer name"),
    ("empty payment method", celda(C, 12, 12, None), "should be PUE or PPD"),
    ("absurd credit terms", celda(C, 12, 17, "Net 300"), "credit terms of 300 days"),
    ("credit terms typed Net 3 instead of Net 30 (risk 1, decided by Diego)", celda(C, 12, 17, "Net 3"),
     "has credit terms of 3 days, but"),
    ("REP dated in another month", celda(R, 2, 2, datetime(2026, 10, 10)), "REP without bank"),
    # round 3
    ("MXN invoice marked as USD without rate", celda(C, 12, 6, "USD"), "has no exchange rate in the CFDI"),
    ("credit note larger than the invoice", lambda wb: [setattr(r[k], "value", v) for r in wb[C].iter_rows(min_row=2)
                                                        if r[13].value == "E" for k, v in ((7, 900000), (9, 144000), (10, 1044000))],
     "is larger than the open balance"),
    ("bank row pasted twice, no reference", lambda wb: (setattr(wb[B].cell(3, 3), "value", None),
                                                        wb[B].append([c.value for c in wb[B][3]])), "Possible duplicate bank row"),
    ("REP currency typed as USD on an MXN invoice", celda(R, 2, 9, "USD"), "REP rate"),
    ("typo in the REP outstanding balance", celda(R, 3, 8, 6960), "REP balance"),
    ("typo in the CFDI IVA", celda(C, 12, 10, 1600), "CFDI arithmetic"),
    ("no chart of accounts", lambda wb: wb.remove(wb["Catalogo_Cuentas"]), "No chart of accounts"),
    # round 4
    ("FX dates typed as ambiguous text", lambda wb: [setattr(r[0], "value", r[0].value.strftime("%d/%m/%Y"))
                                                     for r in wb[T].iter_rows(min_row=2)], "were read as day/month"),
    ("FX rate missing for a day", celda(T, 40, 2, None), "no rate for 2026-09-08"),
    ("payer typed another customer's invoice number", celda(B, 3, 4, "SPEI RECIBIDO GPO ALFA LOGISTICA PAGO FACT A-1002"),
     "Reference vs customer"),
    ("REP related to a UUID that is not in the CFDI", celda(R, 2, 3, "11111111-2222-3333-4444-555555555555"),
     "is not among the CFDI"),
    ("customer payment with a not-AR keyword", celda(B, 3, 4, "SPEI RECIBIDO NOMINA EXPRESS PAGO FACT A-1001"),
     "Receipt classified as not AR"),
    # round 5
    ("deposit typed in the cargo column", lambda wb: [celda(B, 3, 5, 290000)(wb), celda(B, 3, 6, None)(wb)],
     "Deposit typed as a withdrawal?"),
    ("receipt pasted twice with a trailing space", lambda wb: wb[B].append(
        [c.value + " " if c.column == 4 else c.value for c in wb[B][3]]), "Possible duplicate bank row"),
    ("receipt pasted twice with another reference", lambda wb: wb[B].append(
        [c.value + "-2" if c.column == 3 else c.value for c in wb[B][3]]), "only the reference differs"),
    ("invoice of the month pasted into the opening AR", agregar(A, ["A-1009", datetime(2026, 9, 2),
                                                                    "Farmaceutica del Valle SA de CV", "MXN", 174000, 1, 174000]),
     "inside the period"),
    ("status 'Cancelación en proceso' is not cancelled", celda(C, 10, 16, "Cancelación en proceso"), "unknown SAT status"),
    ("credit note typed as type I", lambda wb: [setattr(r[13], "value", "I") for r in wb[C].iter_rows(min_row=2)
                                                if r[13].value == "E"], "is type I but has a related CFDI"),
    ("REP dated 2 days after the bank", celda(R, 2, 2, datetime(2026, 9, 12)), "differs from the bank date by 2 day(s)"),
    ("receipt partly applied, rest left over", celda(B, 3, 6, 300000), "were left over"),
    # round 6
    ("deposit in cargo naming only the customer", lambda wb: [celda(B, 3, 5, 290000)(wb), celda(B, 3, 6, None)(wb),
                                                              celda(B, 3, 4, "SPEI GPO ALFA LOGISTICA")(wb)],
     "Deposit typed as a withdrawal?"),
    ("deposit in cargo with an accented keyword", lambda wb: [celda(B, 7, 5, 58000)(wb), celda(B, 7, 6, None)(wb),
                                                             celda(B, 7, 4, "DEPÓSITO TECNOAGRO")(wb)],
     "Deposit typed as a withdrawal?"),
    ("invoice of the month in the opening AR dated 31-Aug", agregar(A, ["A-1009", datetime(2026, 8, 31),
                                                                       "Farmaceutica del Valle SA de CV", "MXN", 174000, 1, 174000]),
     "inside the period"),
    ("invoice of the month in the opening AR without date", agregar(A, ["A-1009", None,
                                                                       "Farmaceutica del Valle SA de CV", "MXN", 174000, 1, 174000]),
     "inside the period"),
    ("bank fee typed as a deposit", lambda wb: [celda(B, 14, 6, 450)(wb), celda(B, 14, 5, None)(wb)], "reads like a bank fee"),
    ("receipt dated before its invoice", celda(B, 13, 1, datetime(2026, 9, 3)), "issued after the receipt"),
    # round 7
    ("deposit in cargo with an empty description", lambda wb: [celda(B, 3, 5, 290000)(wb), celda(B, 3, 6, None)(wb),
                                                              celda(B, 3, 4, None)(wb)], "is a withdrawal of the same amount"),
    ("typo in an opening invoice's CFDI date", celda(C, 3, 3, datetime(2026, 9, 28)), "Either the CFDI date or the opening row is wrong"),
]


@pytest.mark.parametrize("nombre,mutar,señal", SEÑALADOS, ids=[s[0] for s in SEÑALADOS])
def test_error_humano_queda_señalado(saboteado, nombre, mutar, señal):
    s = ejecutar(saboteado(mutar))
    assert any(señal in x for x in señales(s)), señales(s)


def test_fila_fuera_del_periodo_no_mueve_el_cierre(saboteado, salida):
    """Before: one pasted row from October moved the whole close to October."""
    s = ejecutar(saboteado(agregar(B, [datetime(2026, 10, 1), "MXN-001", "X", "SPEI RECIBIDO", None, 1000, "MXN"])))
    assert huella(s) == huella(salida)


# ---------------------------------------------------------------------------------------------------------
# Command line: output open in Excel, approvals from another file, wrong file, wrong period.
def _correr(*args):
    return main([str(a) for a in args])


def test_excel_de_salida_abierto_da_mensaje_y_no_truena(datos_caso, tmp_path, capsys):
    sal = tmp_path / "salida"
    assert _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D") == 0
    xlsx = next(sal.glob("*.xlsx"))
    os.chmod(xlsx, stat.S_IREAD)            # same error Windows gives when Excel has the file open
    try:
        assert _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D") == 1
    finally:
        os.chmod(xlsx, stat.S_IREAD | stat.S_IWRITE)
    assert "open in another program" in capsys.readouterr().err


def test_aprobaciones_de_otro_archivo_no_se_heredan(datos_caso, tmp_path, capsys):
    sal = tmp_path / "salida"
    _correr("approve", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    assert list(sal.glob("*_APROBADO.xlsx"))
    otro = tmp_path / "otro.xlsx"
    wb = openpyxl.load_workbook(datos_caso)
    wb[B].cell(3, 4).value = "SPEI RECIBIDO GPO ALFA"       # one cell corrected after the approval
    wb.save(otro)
    capsys.readouterr()
    assert _correr("run", "--datos", otro, "--salida", sal, "--usuario", "D") == 0
    out = capsys.readouterr().out
    assert "not the one reviewed before" in out and "DRAFT" in out
    assert len(list(sal.glob("revision_*.json"))) == 1        # the earlier decisions are kept, not deleted


@pytest.mark.parametrize("contenido,nombre", [("a,b\n1,2\n", "datos.csv"), ("not a workbook", "falso.xlsx")])
def test_archivo_que_no_es_excel(tmp_path, capsys, contenido, nombre):
    ruta = tmp_path / nombre
    ruta.write_text(contenido)
    assert _correr("run", "--datos", ruta, "--salida", tmp_path / "s") == 1
    assert "Cannot open" in capsys.readouterr().err


def test_cierre_de_un_mes_sin_movimientos(datos_caso, tmp_path, capsys):
    assert _correr("run", "--datos", datos_caso, "--salida", tmp_path / "s", "--cierre", "2026-08-31") == 1
    assert "No bank movement is dated between 2026-08-01 and 2026-08-31" in capsys.readouterr().err


def test_cierre_mal_escrito(datos_caso, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _correr("run", "--datos", datos_caso, "--salida", tmp_path / "s", "--cierre", "30/09/2026")
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_revision_dañada(datos_caso, tmp_path, capsys):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "revision.json").write_text("{roto")
    assert _correr("run", "--datos", datos_caso, "--salida", tmp_path / "s") == 1
    assert "is damaged" in capsys.readouterr().err


def test_carpeta_de_salida_que_es_un_archivo(datos_caso, tmp_path, capsys):
    archivo = tmp_path / "no_es_carpeta.txt"
    archivo.write_text("x")
    assert _correr("run", "--datos", datos_caso, "--salida", archivo) == 1
    assert "Cannot use the output folder" in capsys.readouterr().err


def test_revisor_en_blanco(datos_caso, tmp_path, capsys):
    assert _correr("approve", "--datos", datos_caso, "--salida", tmp_path / "s", "--usuario", "   ") == 1
    assert "reviewer name is required" in capsys.readouterr().err


def test_reasignacion_a_folio_inexistente_avisa(datos_caso):
    s = ejecutar(datos_caso, decisiones={"conciliacion": {"B13": {"accion": "asignar", "folios": ["A-9999"]}}})
    assert any("names A-9999, which is not an open invoice" in a for a in s.res.avisos)


def test_reasignacion_con_folio_repetido_no_crea_utilidad_cambiaria(datos_caso, salida):
    """Before: the second A-1006 created an application of 0 that booked 23,200 as realized FX, with all checks PASS."""
    s = ejecutar(datos_caso, decisiones={"conciliacion": {"B13": {"accion": "asignar", "folios": ["A-1006", "A-1006"]}}})
    assert all(a.aplicado_doc > 0 for a in s.res.aplicaciones)
    assert sum(a.fx_realizada for a in s.res.aplicaciones) == sum(a.fx_realizada for a in salida.res.aplicaciones)
    assert any("more than once" in a for a in s.res.avisos)


@pytest.mark.parametrize("anulacion,señal", [
    ({"b13": {"accion": "asignar", "folios": ["a-1006"]}}, None),                 # lower case is read as B13 / A-1006
    ({"B99": {"accion": "asignar", "folios": ["A-1006"]}}, "B99 is not a deposit of this period"),
    ({"B02": {"accion": "asignar", "folios": ["A-1006"]}}, "B02 is not a deposit of this period"),   # a payroll withdrawal
])
def test_reasignaciones_que_no_aplican_avisan(datos_caso, anulacion, señal):
    s = ejecutar(datos_caso, decisiones={"conciliacion": anulacion})
    if señal:
        assert any(señal in a for a in s.res.avisos)
    else:
        assert [a.folio for a in s.res.aplicaciones if a.mov_id == "B13"] == ["A-1006"]


def test_rechazo_de_correccion_inexistente_avisa(datos_caso):
    s = ejecutar(datos_caso, decisiones={"rechazar_correcciones": ["A-1001"]})
    assert any("A-1001 has no rate correction" in a for a in s.res.avisos)


def test_reasignacion_de_cobro_clasificado_como_no_cxc_se_respeta(saboteado):
    ruta = saboteado(celda(B, 3, 4, "SPEI RECIBIDO NOMINA EXPRESS PAGO FACT A-1001"))
    s = ejecutar(ruta, decisiones={"conciliacion": {"B03": {"accion": "asignar", "folios": ["A-1001"]}}})
    assert [a.folio for a in s.res.aplicaciones if a.mov_id == "B03"] == ["A-1001"]


def _aprobar_todo(datos, sal):
    _correr("approve", "--datos", datos, "--salida", sal, "--usuario", "D")


def test_rechazar_correccion_reabre_las_polizas(datos_caso, tmp_path, capsys):
    sal = tmp_path / "s"
    _aprobar_todo(datos_caso, sal)
    _correr("reject-correction", "A-1013", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    assert "DRAFT" in capsys.readouterr().out


def test_items_vacio_no_aprueba_todo(datos_caso, tmp_path, capsys):
    assert _correr("approve", "--datos", datos_caso, "--salida", tmp_path / "s", "--usuario", "D", "--items", "") == 1
    assert "--items is empty" in capsys.readouterr().err


def test_asignar_solo_comas(datos_caso, tmp_path, capsys):
    assert _correr("override", "B13", "--datos", datos_caso, "--asignar", ",", "--salida", tmp_path / "s",
                   "--usuario", "D") == 1
    assert "at least one invoice folio" in capsys.readouterr().err


@pytest.mark.parametrize("contenido", ["[]", '{"items": {}, "conciliacion": null}', '{"items": []}'])
def test_revision_con_otra_forma(datos_caso, tmp_path, capsys, contenido):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "revision.json").write_text(contenido)
    assert _correr("run", "--datos", datos_caso, "--salida", tmp_path / "s") == 1
    assert "expected structure" in capsys.readouterr().err


def test_revision_con_bom_se_lee(datos_caso, tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "revision.json").write_text('{"items": {}}', encoding="utf-8-sig")
    assert _correr("run", "--datos", datos_caso, "--salida", tmp_path / "s") == 0


def test_revision_sin_huella_no_aprueba_otro_archivo(datos_caso, tmp_path, capsys):
    import json
    sal = tmp_path / "s"
    _aprobar_todo(datos_caso, sal)
    d = json.loads((sal / "revision.json").read_text(encoding="utf-8"))
    d["huella"] = None
    (sal / "revision.json").write_text(json.dumps(d), encoding="utf-8")
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    assert "DRAFT" in capsys.readouterr().out


def test_aprobaciones_escritas_a_mano_no_cuentan(datos_caso, tmp_path, capsys):
    import json
    from cierre_cxc.cli import claves_revisables
    sal = tmp_path / "s"
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    d = json.loads((sal / "revision.json").read_text(encoding="utf-8")) if (sal / "revision.json").exists() else {}
    from cierre_cxc.auditoria import sha256_archivo
    d.update({"huella": sha256_archivo(datos_caso),
              "items": {c: {"estado": "aprobado", "usuario": "Jefe", "comentario": "", "ts": "2026-10-01T00:00:00+00:00"}
                        for c in claves_revisables(ejecutar(datos_caso))}})
    (sal / "revision.json").write_text(json.dumps(d), encoding="utf-8")
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    out = capsys.readouterr().out
    assert "no matching decision in the audit log" in out and "DRAFT" in out


def test_linea_dañada_en_la_bitacora_no_detiene(datos_caso, tmp_path):
    sal = tmp_path / "s"
    _correr("run", "--datos", datos_caso, "--salida", sal)
    with open(sal / "bitacora_auditoria.jsonl", "a", encoding="utf-8") as f:
        f.write("{roto\n")
    assert _correr("run", "--datos", datos_caso, "--salida", sal) == 0


def test_secuencia_de_revision_del_readme_conserva_las_decisiones(datos_caso, tmp_path, capsys):
    """Before: override and reject-correction saved decisions without the file hash, the next run set them aside,
    and approve then produced an APPROVED close without them."""
    sal = tmp_path / "s"
    _correr("override", "B13", "--datos", datos_caso, "--asignar", "A-1006,A-1015", "--comentario", "FIFO",
            "--salida", sal, "--usuario", "D")
    _correr("reject-correction", "A-1013", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    _correr("approve", "--datos", datos_caso, "--items", "all", "--salida", sal, "--usuario", "D")
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    out = capsys.readouterr().out
    assert "APPROVED" in out and "not the one reviewed before" not in out
    from cierre_cxc.revision import Revision
    rev = Revision(sal / "revision.json")
    assert "B13" in rev.datos["conciliacion"] and "A-1013" in rev.datos["rechazar_correcciones"]


def test_override_sin_datos_pide_el_archivo(tmp_path):
    with pytest.raises(SystemExit):
        _correr("override", "B13", "--asignar", "A-1006", "--salida", tmp_path / "s", "--usuario", "D")


def test_override_reabre_todas_las_aprobaciones(datos_caso, tmp_path, capsys):
    """Before: an override that moved another receipt left that receipt's approval in place."""
    sal = tmp_path / "s"
    _aprobar_todo(datos_caso, sal)
    _correr("override", "B03", "--datos", datos_caso, "--asignar", "A-1002", "--salida", sal, "--usuario", "D")
    from cierre_cxc.revision import Revision
    assert Revision(sal / "revision.json").datos["items"] == {}


def test_revision_vieja_restaurada_no_revive_una_aprobacion_revocada(datos_caso, tmp_path, capsys):
    import shutil
    sal = tmp_path / "s"
    _correr("approve", "--datos", datos_caso, "--items", "match:B03", "--salida", sal, "--usuario", "D")
    shutil.copy(sal / "revision.json", tmp_path / "copia.json")
    _correr("approve", "--datos", datos_caso, "--items", "match:B03", "--estado", "rechazado", "--salida", sal,
            "--usuario", "D")
    shutil.copy(tmp_path / "copia.json", sal / "revision.json")
    from cierre_cxc.auditoria import Bitacora
    from cierre_cxc.revision import Revision
    assert Revision(sal / "revision.json", Bitacora(sal / "bitacora_auditoria.jsonl")).estado("match:B03") == "pendiente"


def test_revision_restaurada_antes_de_una_reasignacion_avisa(datos_caso, tmp_path, capsys):
    """Before: putting back a revision.json from before an override silently dropped the override."""
    import shutil
    sal = tmp_path / "s"
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    shutil.copy(sal / "revision.json", tmp_path / "antes.json") if (sal / "revision.json").exists() else (
        tmp_path / "antes.json").write_text("{}")
    _correr("override", "B13", "--datos", datos_caso, "--asignar", "A-1006", "--salida", sal, "--usuario", "D")
    shutil.copy(tmp_path / "antes.json", sal / "revision.json")
    _aprobar_todo(datos_caso, sal)
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    out = capsys.readouterr().out
    assert "does not match the audit log" in out and "override of B13" in out and "DRAFT" in out


def test_reparar_la_discrepancia_rehaciendo_la_reasignacion(datos_caso, tmp_path, capsys):
    import shutil
    sal = tmp_path / "s"
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    (tmp_path / "antes.json").write_text((sal / "revision.json").read_text(encoding="utf-8")
                                         if (sal / "revision.json").exists() else "{}")
    _correr("override", "B13", "--datos", datos_caso, "--asignar", "A-1006", "--salida", sal, "--usuario", "D")
    shutil.copy(tmp_path / "antes.json", sal / "revision.json")
    _correr("override", "B13", "--datos", datos_caso, "--asignar", "A-1006", "--salida", sal, "--usuario", "D")
    _aprobar_todo(datos_caso, sal)
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    out = capsys.readouterr().out
    assert "does not match" not in out and "APPROVED" in out


def test_aprobaciones_de_otro_archivo_editando_solo_la_huella(datos_caso, tmp_path, capsys):
    """Before: copying file A's approvals into file B's review and editing only the hash approved file B."""
    import json
    from cierre_cxc.auditoria import sha256_archivo
    sal = tmp_path / "s"
    _aprobar_todo(datos_caso, sal)
    otro = tmp_path / "otro.xlsx"
    wb = openpyxl.load_workbook(datos_caso)
    wb[B].cell(3, 4).value = "SPEI RECIBIDO GPO ALFA"
    wb.save(otro)
    d = json.loads((sal / "revision.json").read_text(encoding="utf-8"))
    d["huella"] = sha256_archivo(otro)
    (sal / "revision.json").write_text(json.dumps(d), encoding="utf-8")
    capsys.readouterr()
    _correr("run", "--datos", otro, "--salida", sal, "--usuario", "D")
    assert "DRAFT" in capsys.readouterr().out


def test_linea_dañada_corta_las_aprobaciones_anteriores(datos_caso, tmp_path, capsys):
    sal = tmp_path / "s"
    _aprobar_todo(datos_caso, sal)
    with open(sal / "bitacora_auditoria.jsonl", "a", encoding="utf-8") as f:
        f.write("{roto\n")
    capsys.readouterr()
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    out = capsys.readouterr().out
    assert "1 damaged line(s)" in out and "DRAFT" in out


def test_huella_del_excel_exportado_en_la_bitacora(datos_caso, tmp_path):
    from cierre_cxc.auditoria import Bitacora, sha256_archivo
    sal = tmp_path / "s"
    _correr("run", "--datos", datos_caso, "--salida", sal, "--usuario", "D")
    export = [e for e in Bitacora(sal / "bitacora_auditoria.jsonl").leer() if e["evento"] == "export"][-1]
    assert export["sha256"] == sha256_archivo(sal / export["archivo"])
