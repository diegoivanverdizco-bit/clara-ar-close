"""Journal entries as a CSV for NetSuite's CSV Import Assistant (Journal Entry, one file with header and lines).

The entries are the ones the close already computed (the same as the Excel's A3_Journal_Entries); nothing is
recomputed here. Format, from Oracle's documentation (docs/2026-11-17 Formato de importación de pólizas NetSuite.md):
one row per line, the header fields repeated on every row, and the same External ID on every line of an entry.

Assumptions to confirm with the NetSuite administrator, not in the documentation:
  - Amounts are in MXN, the base currency, so Currency and Exchange Rate are left out (blank = base currency, 1.00).
  - Dates are DD/MM/YYYY, the usual date preference in Mexico; NetSuite reads them with the importing user's preference.
  - Accounts go as "number name" (needs "Use Account Numbers"), or through a mapping file to Clara's chart in NetSuite.
"""
from __future__ import annotations

import csv
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

COLUMNAS = ["External ID", "Date", "Memo", "Reversal Date", "Journal Entry - Line: Account",
            "Journal Entry - Line: Debit", "Journal Entry - Line: Credit", "Journal Entry - Line: Memo"]
MAX_MEMO, MAX_MEMO_LINEA = 999, 4000            # NetSuite's limits for the header and line memos


def _fecha(d) -> str:
    return f"{d:%d/%m/%Y}"


def _texto(x: str) -> str:
    return " ".join(str(x).split())                 # one line: a line break inside a memo would split the CSV row


def _linea_mala(lineas):
    """Why NetSuite would reject a line of this entry, or None."""
    for l in lineas:
        if l.debe < 0 or l.haber < 0:
            return f"a line with a negative amount ({l.cuenta})"
        if l.debe and l.haber:
            return f"a line with both a debit and a credit ({l.cuenta})"
        if not l.debe and not l.haber:
            return f"a line with no amount ({l.cuenta})"
    return None


def _monto(x) -> str:
    return f"{Decimal(x):.2f}" if x else ""


def leer_mapa(ruta) -> dict:
    """{case account: NetSuite account} from a CSV with columns cuenta,netsuite."""
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        lector = csv.DictReader(fh)
        if not {"cuenta", "netsuite"} <= set(lector.fieldnames or []):
            raise ValueError(f"{Path(ruta).name} needs the columns cuenta,netsuite.")
        return {str(f["cuenta"]).strip(): str(f["netsuite"]).strip() for f in lector if (f["cuenta"] or "").strip()}


def filas(s, mapa: dict | None = None, subsidiaria: str = "") -> tuple:
    """(rows for the CSV, [(entry id, reason left out)]). An entry that does not balance is never exported. With a
    mapping, every account must be in it: a missing one stops everything (ValueError), so nothing goes to a wrong
    account."""
    if mapa is not None:
        faltan = sorted({l.cuenta for p in s.polizas for l in p.lineas} - set(mapa))
        if faltan:
            raise ValueError("Accounts with no NetSuite account in the mapping: " + ", ".join(faltan)
                             + ". Add them to the mapping file; nothing was written.")
    cierre = s.res.cierre
    reversa = _fecha(cierre + timedelta(days=1)) if cierre.day == monthrange(cierre.year, cierre.month)[1] else ""
    out, omitidas = [], []
    for p in s.polizas:
        debe, haber = sum(l.debe for l in p.lineas), sum(l.haber for l in p.lineas)
        if not p.cuadra:
            omitidas.append((p.id, f"does not balance: debits {debe:,.2f}, credits {haber:,.2f}"))
            continue
        mala = _linea_mala(p.lineas)
        if mala:
            omitidas.append((p.id, mala))
            continue
        # Unrealized FX is reversed on the first day of the next period (the entry says so); NetSuite does it itself
        revierte = reversa if p.tipo == "FX revaluation" else ""
        memo = _texto(p.concepto + (f". {p.nota}" if p.nota else ""))[:MAX_MEMO]
        for l in p.lineas:
            fila = {"External ID": f"CXC-{cierre:%Y%m}-{p.id}"}
            if subsidiaria:
                fila["Subsidiary"] = subsidiaria
            fila.update({"Date": _fecha(p.fecha), "Memo": memo, "Reversal Date": revierte,
                         "Journal Entry - Line: Account": mapa[l.cuenta] if mapa is not None else f"{l.cuenta} {l.nombre}",
                         "Journal Entry - Line: Debit": _monto(l.debe), "Journal Entry - Line: Credit": _monto(l.haber),
                         "Journal Entry - Line: Memo": _texto(p.concepto)[:MAX_MEMO_LINEA]})
            out.append(fila)
    return out, omitidas


def nombre(s, borrador: bool) -> str:
    return f"{s.res.cierre:%Y-%m}_polizas_netsuite{'_BORRADOR' if borrador else ''}.csv"


def escribir(s, carpeta, borrador: bool, mapa: dict | None = None, subsidiaria: str = "") -> tuple:
    """Write the CSV (UTF-8) and return (path, rows, left out). While anything in the close is pending, the file is a
    draft (_BORRADOR), like the Excel: it is for checking, not for importing."""
    datos, omitidas = filas(s, mapa, subsidiaria)
    ruta = Path(carpeta) / nombre(s, borrador)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(datos[0]) if datos else COLUMNAS)
        w.writeheader()
        w.writerows(datos)
    return ruta, datos, omitidas
