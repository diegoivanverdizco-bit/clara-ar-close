"""Proposed corrections for input errors (docs/2026-11-17 Diseño corrección de errores de captura.md).

Every value proposed here is computed by the code from the file itself; nothing is guessed and no language model is
called. Each error the load collects gets a category:

  a  safe: one reading and a check that passes (e.g. IVA rate 16 when subtotal x 0.16 = IVA); approved in bulk
  b  to confirm: one or more candidates, each with the evidence for it; approved one by one
  c  not corrected: only reported (nothing in the file says what the right value is)
  d  structure: no row can be read until it is fixed

A candidate is a list of cell changes, or a row to delete, or a row to add; `escribir_copia` applies the approved
ones to a copy of the file and never touches the original.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from openpyxl.utils import get_column_letter

from .cargar import TC_MAX, TC_MIN

TOLERANCIA = Decimal("0.02")          # same tolerance as the IVA check in excepciones.py
CERCA_MEDIANA = Decimal("0.10")       # same band as the outlier notice of the exchange-rate table
CERCA_FIX = Decimal("0.05")


@dataclass
class Cambio:
    hoja: str
    celda: str
    antes: object      # the cell as it is in the file; the copy is not written if it changed meanwhile
    despues: object    # None empties the cell


@dataclass
class Candidato:
    que: str                                     # what changes, in words
    porque: str = ""                             # the evidence, computed by the code
    cambios: list = field(default_factory=list)  # Cambio
    quitar_fila: Optional[tuple] = None          # (sheet, row) to delete
    agregar_fila: Optional[tuple] = None         # (sheet, values) to add at the end of the sheet


def proponer(p) -> None:
    """Fill p.categoria and p.candidatos. A structure error stays 'd'; anything without a rule or without a
    candidate stays 'c'."""
    if p.categoria == "d":
        return
    regla = REGLAS.get(p.codigo)
    res = regla(p) if regla else None
    candidatos = [c for c in (res[1] if res else []) if c is not None]
    if res and candidatos:
        p.categoria, p.candidatos = res[0], candidatos
    else:
        p.categoria, p.candidatos = "c", []
    # A cross check looks at the whole row and has no cell; when every proposal changes the same cell, that is it
    celdas = {(c.hoja, c.celda) for k in p.candidatos for c in k.cambios}
    if not p.celda and len(celdas) == 1 and next(iter(celdas))[0] == p.hoja and all(
            k.cambios and not (k.quitar_fila or k.agregar_fila) for k in p.candidatos):
        p.celda = celdas.pop()[1]
        if p.valor is None:
            p.valor = p.candidatos[0].cambios[0].antes      # what the cell says, for the table


# ---- helpers --------------------------------------------------------------------------------------------

def _dec(v) -> Optional[Decimal]:
    if v is None or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        return None
    try:
        n = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return n if n.is_finite() else None


def _m(n) -> str:
    return f"{n:,.2f}"


def _t(n: Decimal) -> str:
    """A number as text, without exponent or trailing zeros: 180, 17.4885, 0.16."""
    s = format(n, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _crudo(p, campo):
    i = p.contexto.get("indices", {}).get(campo)
    fila = p.contexto.get("fila", ())
    return fila[i] if i is not None and i < len(fila) else None


def _celda(p, campo) -> str:
    i = p.contexto.get("indices", {}).get(campo)
    return f"{get_column_letter(i + 1)}{p.fila}" if i is not None and p.fila else ""


def _poner(p, campo, valor, que, porque="") -> Optional[Candidato]:
    celda = _celda(p, campo)
    if not celda:
        return None
    return Candidato(que, porque, [Cambio(p.hoja, celda, _crudo(p, campo), valor)])


def _quitar(p, fila, que, porque) -> Candidato:
    return Candidato(que, porque, quitar_fila=(p.hoja, fila))


def _letras(v) -> str:
    s = unicodedata.normalize("NFKD", str(v or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z]", "", s.upper())


# ---- numbers typed as text ------------------------------------------------------------------------------

_US = re.compile(r"^\d{1,3}(,\d{3})+(\.\d{1,2})?$")      # 290,000.00 · 1,500
_EU = re.compile(r"^\d{1,3}(\.\d{3})+(,\d{1,2})?$")      # 1.500,00 · 1.500.000
_COMA_DECIMAL = re.compile(r"^\d+,\d{1,2}$")             # 290000,00
_ESPACIOS = re.compile(r"^\d{1,3}( \d{3})+(\.\d{1,2})?$")  # 290 000
_SIMPLE = re.compile(r"^\d+(\.\d+)?$")


def lecturas(valor) -> tuple:
    """The possible readings of a number typed as text, as [(number, how it was read)], and whether the reading
    is unambiguous. Text that is not a number in any usual format gives no reading."""
    if not isinstance(valor, str):
        return [], False
    t = valor.replace(" ", " ").strip()
    signo = ""
    if t.startswith("-"):
        signo, t = "-", t[1:].strip()
    simbolo = t.startswith("$")
    t = t.lstrip("$").strip()
    if t.startswith("-") and not signo:
        signo, t = "-", t[1:].strip()

    def n(s):
        return Decimal(signo + s)
    if _US.match(t):
        if "." in t or t.count(",") >= 2:
            return [(n(t.replace(",", "")), "commas as thousands separators")], True
        # one comma and three digits: 1,500 is one thousand five hundred, or one point five
        return [(n(t.replace(",", "")), "the comma as thousands separator"),
                (n(t.replace(",", ".")), "the comma as decimal point")], False
    if _EU.match(t):
        return [(n(t.replace(".", "").replace(",", ".")), "dots as thousands separators and a decimal comma")], False
    if _COMA_DECIMAL.match(t):
        return [(n(t.replace(",", ".")), "a decimal comma")], False
    if _ESPACIOS.match(t):
        return [(n(t.replace(" ", "")), "spaces as thousands separators")], False
    if simbolo and _SIMPLE.match(t):
        return [(n(t), "the currency sign removed")], True
    return [], False


def _cuadra_cfdi(p, campo, valor) -> Optional[bool]:
    """Does subtotal + IVA = total hold with this value? None when the other two are not numbers."""
    if p.contexto.get("tabla") != "cfdi" or campo not in ("subtotal", "iva", "total"):
        return None
    v = {k: _dec(_crudo(p, k)) for k in ("subtotal", "iva", "total")}
    v[campo] = valor
    if None in v.values():
        return None
    return abs(v["subtotal"] + v["iva"] - v["total"]) <= TOLERANCIA


def _no_numero(p):
    celda = p.celda
    if not celda:
        return None
    texto = p.valor.strip() if isinstance(p.valor, str) else p.valor
    if p.campo == "tasa" and isinstance(texto, str) and texto.endswith("%"):
        n = _dec(texto[:-1])
        return _tasa(p, n / 100, f"'{texto}' is a percentage") if n is not None else None
    lects, seguro = lecturas(p.valor)
    if not lects:
        return None
    opciones = []
    for n, como in lects:
        cuadra = _cuadra_cfdi(p, p.campo, n)
        porque = f"read with {como}"
        if cuadra is True:
            porque += "; with it, subtotal + IVA = total"
        elif cuadra is False:
            porque += "; with it, subtotal + IVA does not equal the total"
        opciones.append((cuadra, Candidato(f"Type {_t(n)}", porque, [Cambio(p.hoja, celda, p.valor, n)])))
    opciones.sort(key=lambda o: o[0] is not True)      # the reading that ties the invoice first
    if seguro and len(opciones) == 1 and opciones[0][0] is not False:
        return "a", [opciones[0][1]]
    return "b", [c for _, c in opciones]


# ---- IVA rate -------------------------------------------------------------------------------------------

def _tasa(p, nueva: Decimal, como: str):
    celda = p.celda or _celda(p, "tasa")
    cand = Candidato(f"Type {_t(nueva)}", como, [Cambio(p.hoja, celda, _crudo(p, "tasa"), nueva)])
    sub, iva = _dec(_crudo(p, "subtotal")), _dec(_crudo(p, "iva"))
    if sub is None or iva is None:
        return "b", [cand]
    calculado = sub * nueva
    if abs(calculado - iva) <= TOLERANCIA:
        cand.porque = f"{como}; subtotal {_m(sub)} x {_t(nueva)} = IVA {_m(iva)}"
        return "a", [cand]
    cand.porque = (f"{como}; but subtotal {_m(sub)} x {_t(nueva)} = {_m(calculado)} and the IVA says {_m(iva)}: "
                   "the IVA exception will show it")
    return "b", [cand]


def _tasa_porcentaje(p):
    n = _dec(p.valor)
    return _tasa(p, n / 100, f"{_t(n)} looks like a percentage") if n is not None else None


# ---- empty or negative amounts --------------------------------------------------------------------------

def _vacia(p):
    if p.contexto.get("tabla") != "cfdi" or p.campo not in ("subtotal", "iva", "total"):
        return None
    v = {k: _dec(_crudo(p, k)) for k in ("subtotal", "iva", "total") if k != p.campo}
    if None in v.values():
        return None
    n = {"total": lambda: v["subtotal"] + v["iva"], "iva": lambda: v["total"] - v["subtotal"],
         "subtotal": lambda: v["total"] - v["iva"]}[p.campo]()
    if n < 0:
        return None
    return "b", [_poner(p, p.campo, n, f"Type {_m(n)}", "calculated from the other two: subtotal + IVA = total")]


def _negativo(p):
    tabla = p.contexto.get("tabla")
    if tabla in ("cfdi", "rep") and p.campo:
        n = _dec(p.valor)
        return ("b", [_poner(p, p.campo, abs(n), f"Type {_m(abs(n))}", "the amount without its minus sign")]) \
            if n is not None else None
    if tabla != "banco":
        return None                    # a negative opening balance is a credit balance: a person decides it
    opciones = []
    for col, otra in (("cargo", "abono"), ("abono", "cargo")):
        n = _dec(_crudo(p, col))
        if n is None or n >= 0:
            continue
        opciones.append(_poner(p, col, -n, f"Type {_m(-n)} in {col}", "the amount without its minus sign"))
        if not _dec(_crudo(p, otra)):
            opciones.append(Candidato(
                f"Move {_m(-n)} to {otra}", f"a minus sign in {col} can mean the movement goes the other way",
                [Cambio(p.hoja, _celda(p, col), _crudo(p, col), None),
                 Cambio(p.hoja, _celda(p, otra), _crudo(p, otra), -n)]))
    return "b", opciones


# ---- bank rows ------------------------------------------------------------------------------------------

def _cargo_y_abono(p):
    i_cargo = p.contexto["indices"]["cargo"]
    nueva = list(p.contexto["fila"])
    nueva[i_cargo] = None
    cand = _poner(p, "abono", None, "Split it into two rows",
                  f"the cargo {_m(_dec(_crudo(p, 'cargo')))} stays in row {p.fila}; the abono "
                  f"{_m(_dec(_crudo(p, 'abono')))} goes to a new row at the end of the sheet")
    if cand:
        cand.agregar_fila = (p.hoja, nueva)
    return "b", [cand]


def _banco_sin_fecha(p):
    texto = " ".join(str(v) for v in p.contexto.get("fila", ()) if v is not None).upper()
    if re.search(r"\b(TOTAL|TOTALES|SUMA|SUBTOTAL)\b", texto):
        return "b", [_quitar(p, p.fila, f"Delete row {p.fila}", "it looks like a totals row, not a movement")]
    return None


def _linea_saldo(p):
    return "b", [_quitar(p, p.fila, f"Delete row {p.fila}", "it is a balance line of the statement, not a movement")]


def _moneda_vacia(p):
    monedas = p.contexto.get("moneda_cuenta") or []
    if len(monedas) != 1:
        return None
    return "b", [_poner(p, "moneda", monedas[0], f"Type {monedas[0]}",
                        f"the account '{_crudo(p, 'cuenta')}' is a {monedas[0]} account")]


# ---- exchange rates -------------------------------------------------------------------------------------

def _tc_fuera_rango(p):
    tc = p.contexto.get("tc")
    if tc is None or tc <= 0 or p.contexto.get("sin_celda"):
        return None
    if "indices" in p.contexto:
        celda, antes = _celda(p, "tc"), _crudo(p, "tc")
    else:
        celda, antes = p.celda, p.valor
    if not celda:
        return None
    med = p.contexto.get("mediana")
    opciones = [(tc * 10, "a digit missing (rate x 10)"), (tc / 10, "a digit too many (rate / 10)"),
                ((1 / tc).quantize(Decimal("0.0001")),
                 "quoted as USD per MXN (1 / rate, rounded to 4 decimals: check it against the source)")]
    salida = []
    for n, como in opciones:
        cerca = abs(n - med) <= med * CERCA_MEDIANA if med else TC_MIN <= n <= TC_MAX
        if cerca:
            porque = como + (f"; the FIX table's median is {med:.4f}" if med else "")
            salida.append(Candidato(f"Type {_t(n)}", porque, [Cambio(p.hoja, celda, antes, n)]))
    fix = p.contexto.get("fix")
    if fix and p.contexto.get("tabla") == "cfdi":
        salida.append(Candidato(f"Type {_t(fix)}", "the FIX of the issue date; the CFDI rate can differ from the FIX: "
                                "check it against the XML", [Cambio(p.hoja, celda, antes, fix)]))
    return "b", salida


def _fx_fecha_doble(p):
    previa, tc_previo, tc, med = (p.contexto.get(k) for k in ("fila_previa", "tc_previo", "tc", "mediana"))
    quitar_esta = _quitar(p, p.fila, f"Delete row {p.fila} ({_t(tc)})", f"keeps row {previa} ({_t(tc_previo)})")
    quitar_previa = _quitar(p, previa, f"Delete row {previa} ({_t(tc_previo)})", f"keeps row {p.fila} ({_t(tc)})")
    opciones = [quitar_esta, quitar_previa]
    if med:
        if abs(tc_previo - med) > abs(tc - med):
            opciones.reverse()
        opciones[0].porque += f"; it is the closer one to the table's median ({med:.4f})"
    return "b", opciones


def _mxn_con_tc(p):
    tc, fix = p.contexto.get("tc"), p.contexto.get("fix")
    vaciar = _poner(p, "tc", None, "Leave the rate empty", "an invoice in MXN carries no exchange rate")
    porque = f"the rate {_t(tc)} looks like a USD rate"
    cerca = bool(fix) and abs(tc - fix) <= fix * CERCA_FIX
    if fix:
        porque = f"the rate {_t(tc)} is {abs(tc - fix) / fix:.1%} from the FIX of the issue date ({_t(fix)})"
    usd = _poner(p, "moneda", "USD", "Change the currency to USD", porque)
    return "b", [usd, vaciar] if cerca else [vaciar, usd]


# ---- CFDI type ------------------------------------------------------------------------------------------

_PALABRAS_TIPO = {"FACTURA": "I", "FACT": "I", "FAC": "I", "NC": "E", "NOTACREDITO": "E", "NOTADECREDITO": "E",
                  "NOTACRED": "E"}


def _tipo_invalido(p):
    tipo = _PALABRAS_TIPO.get(_letras(p.valor))
    return ("b", [_poner(p, "tipo", tipo, f"Type {tipo}", f"'{p.valor}' means type {tipo}")]) if tipo else None


def _tipo_con_total(p):
    return "b", [_poner(p, "tipo", "I", "Type I", f"type {p.valor} (payment, transfer or payroll) has no total; "
                        "this row has one")]


def _nc_sin_relacionado(p):
    opciones = [_poner(p, "relacionado", uuid, f"Relate it to {folio}",
                       f"an invoice of the same customer (RFC {p.contexto.get('rfc')})")
                for folio, uuid in p.contexto.get("facturas", [])]
    opciones.append(_poner(p, "tipo", "I", "Type I", "if it is an invoice, not a credit note"))
    return "b", opciones


# ---- opening AR -----------------------------------------------------------------------------------------

def _apertura_moneda(p):
    m = p.contexto.get("moneda_cfdi")
    return "b", [_poner(p, "moneda", m, f"Type {m}", f"its CFDI is in {m}")]


def _apertura_mxn_tc(p):
    return "b", [_poner(p, "tc", None, "Leave the rate empty", "a balance in MXN carries no exchange rate")]


def _apertura_mxn_saldo(p):
    saldo, smxn = _crudo(p, "saldo"), _crudo(p, "saldo_mxn")
    return "b", [_poner(p, "saldo_mxn", saldo, f"MXN balance = {_m(_dec(saldo))}", "keeps the balance"),
                 _poner(p, "saldo", smxn, f"Balance = {_m(_dec(smxn))}", "keeps the MXN balance")]


def _apertura_no_cuadra(p):
    saldo, tc, smxn = (_dec(_crudo(p, k)) for k in ("saldo", "tc", "saldo_mxn"))
    if not saldo or tc is None or smxn is None:
        return None
    tc_nuevo = (smxn / saldo).quantize(Decimal("0.0001"))
    smxn_nuevo = (saldo * tc).quantize(Decimal("0.01"))
    por_tc = _poner(p, "tc", tc_nuevo, f"Rate = {_t(tc_nuevo)}", f"MXN balance {_m(smxn)} / balance {_m(saldo)}")
    por_saldo = _poner(p, "saldo_mxn", smxn_nuevo, f"MXN balance = {_m(smxn_nuevo)}",
                       f"balance {_m(saldo)} x rate {_t(tc)}")
    creible = lambda x: TC_MIN <= x <= TC_MAX                       # noqa: E731
    return "b", [por_tc, por_saldo] if creible(tc_nuevo) and not creible(tc) else [por_saldo, por_tc]


# ---- repeated rows --------------------------------------------------------------------------------------

def _repetido(p):
    otra = p.contexto.get("otra_fila")
    if p.contexto.get("fila") == p.contexto.get("otra_crudos"):
        if p.contexto.get("uuid_valido"):
            return "a", [_quitar(p, p.fila, f"Delete row {p.fila}",
                                 f"identical to row {otra}, and the SAT never stamps the same UUID twice")]
        return "b", [_quitar(p, p.fila, f"Delete row {p.fila}", f"identical to row {otra}")]
    return "b", [_quitar(p, p.fila, f"Delete row {p.fila}", f"keeps row {otra}"),
                 _quitar(p, otra, f"Delete row {otra}", f"keeps row {p.fila}")]


# ---- approving and writing the corrected copy ----------------------------------------------------------

ETIQUETA = {"a": "SAFE", "b": "TO CONFIRM", "c": "NOT FIXED", "d": "STRUCTURE"}


class AprobacionInvalida(ValueError):
    """An approval that cannot be applied as given; nothing is written."""


def numerar(problemas) -> dict:
    """P1, P2... in the order the load found them: the same file always gives the same numbers."""
    return {f"P{i}": p for i, p in enumerate(problemas, start=1)}


def listar(numerados: dict) -> str:
    lineas = []
    for pid, p in numerados.items():
        lineas.append(f"{pid:<4} {ETIQUETA[p.categoria]:<10} {p.mensaje}")
        for i, c in enumerate(p.candidatos, start=1):
            lineas.append(f"       {i}) {c.que}" + (f" - {c.porque}" if c.porque else ""))
    return "\n".join(lineas)


def _ids(texto: str) -> list:
    return [x.strip().upper() for x in (texto or "").split(",") if x.strip()]


def elegir(numerados: dict, seguras: bool = False, elegir_texto: str = "", rechazar_texto: str = "") -> tuple:
    """The approved (id, problem, candidate number, candidate) and the rejected (id, problem), in list order.
    `--seguras` approves every safe one; a correction to confirm needs its candidate number (P4=2)."""
    elegidos = {}
    for parte in _ids(elegir_texto):
        pid, _, numero = parte.partition("=")
        if pid not in numerados:
            raise AprobacionInvalida(f"{pid} is not in the list (P1-P{len(numerados)}).")
        p = numerados[pid]
        if not p.candidatos:
            raise AprobacionInvalida(f"{pid} has no proposed correction: correct it in the file.")
        if not numero:
            if p.categoria != "a":
                raise AprobacionInvalida(f"{pid} needs a candidate number: {pid}=1 to {pid}={len(p.candidatos)}.")
            numero = "1"
        if not numero.isdigit() or not 1 <= int(numero) <= len(p.candidatos):
            raise AprobacionInvalida(f"{pid} has {len(p.candidatos)} candidate(s): choose {pid}=1 to "
                                     f"{pid}={len(p.candidatos)}.")
        elegidos[pid] = int(numero)
    rechazados = _ids(rechazar_texto)
    for pid in rechazados:
        if pid not in numerados:
            raise AprobacionInvalida(f"{pid} is not in the list (P1-P{len(numerados)}).")
        if pid in elegidos:
            raise AprobacionInvalida(f"{pid} is both approved and rejected.")
    if seguras:
        for pid, p in numerados.items():
            if p.categoria == "a" and pid not in rechazados:
                elegidos.setdefault(pid, 1)
    aprobadas = [(pid, p, elegidos[pid], p.candidatos[elegidos[pid] - 1]) for pid, p in numerados.items()
                 if pid in elegidos]
    return aprobadas, [(pid, numerados[pid]) for pid in numerados if pid in rechazados]


def nombre_copia(origen, carpeta, cuando) -> "Path":
    """`AAAA-MM-DD HHMMSS <name> corregido.xlsx` in the output folder; never an existing file. A copy of a copy
    keeps the original name instead of piling up dates."""
    from pathlib import Path
    base = re.sub(r"^\d{4}-\d{2}-\d{2} \d{6}(-\d+)? (.*) corregido$", r"\2", Path(origen).stem)
    carpeta = Path(carpeta)
    destino, n = carpeta / f"{cuando:%Y-%m-%d %H%M%S} {base} corregido.xlsx", 2
    while destino.exists():
        destino, n = carpeta / f"{cuando:%Y-%m-%d %H%M%S}-{n} {base} corregido.xlsx", n + 1
    return destino


def _igual(a, b) -> bool:
    if a == b:
        return True
    da, db = _dec(a), _dec(b)
    return da is not None and da == db and not isinstance(a, str) and not isinstance(b, str)


def escribir_copia(origen, destino, aprobadas) -> None:
    """Write the corrected copy: values only (the load reads values; openpyxl would drop the stored results of
    formulas), with the approved cell changes, then the deleted rows (bottom up, so row numbers hold), then the
    added rows. Every cell is checked against the value that was proposed first; if the file changed since, or two
    approvals set one cell to different values, nothing is written."""
    import openpyxl
    wb = openpyxl.load_workbook(origen, data_only=True)
    celdas = {}
    for pid, _, _, cand in aprobadas:
        for cam in cand.cambios:
            actual = wb[cam.hoja][cam.celda].value
            if not _igual(actual, cam.antes):
                raise AprobacionInvalida(f"{pid}: {cam.hoja}!{cam.celda} is {actual!r}, not {cam.antes!r}: the file "
                                         "changed since it was checked. Run fix again.")
            previa = celdas.get((cam.hoja, cam.celda))
            if previa and previa[0] != cam.despues:
                raise AprobacionInvalida(f"{previa[1]} and {pid} set {cam.hoja}!{cam.celda} to different values. "
                                         "Approve only one of them.")
            celdas[(cam.hoja, cam.celda)] = (cam.despues, pid)
    for (hoja, ref), (valor, _) in celdas.items():
        wb[hoja][ref].value = valor
    for hoja, fila in sorted({c.quitar_fila for *_, c in aprobadas if c.quitar_fila}, key=lambda x: -x[1]):
        wb[hoja].delete_rows(fila)
    for *_, c in aprobadas:
        if c.agregar_fila:
            wb[c.agregar_fila[0]].append(list(c.agregar_fila[1]))
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)


def _texto(v):
    return None if v is None else str(v)


def para_bitacora(candidato) -> dict:
    """The approved change as the audit log keeps it: values as text, so a Decimal and a float read the same."""
    return {"que": candidato.que, "porque": candidato.porque,
            "cambios": [{"hoja": c.hoja, "celda": c.celda, "antes": _texto(c.antes), "despues": _texto(c.despues)}
                        for c in candidato.cambios],
            "quitar_fila": list(candidato.quitar_fila) if candidato.quitar_fila else None,
            "agregar_fila": ([candidato.agregar_fila[0], [_texto(v) for v in candidato.agregar_fila[1]]]
                             if candidato.agregar_fila else None)}


REGLAS = {
    "no_numero": _no_numero, "tasa_porcentaje": _tasa_porcentaje, "vacia": _vacia, "negativo": _negativo,
    "cargo_y_abono": _cargo_y_abono, "banco_sin_fecha": _banco_sin_fecha, "linea_saldo": _linea_saldo,
    "moneda_vacia": _moneda_vacia, "tc_fuera_rango": _tc_fuera_rango, "fx_fecha_doble": _fx_fecha_doble,
    "mxn_con_tc": _mxn_con_tc, "tipo_invalido": _tipo_invalido, "tipo_con_total": _tipo_con_total,
    "nc_sin_relacionado": _nc_sin_relacionado, "apertura_moneda": _apertura_moneda,
    "apertura_mxn_tc": _apertura_mxn_tc, "apertura_mxn_saldo": _apertura_mxn_saldo,
    "apertura_no_cuadra": _apertura_no_cuadra, "repetido": _repetido,
}


def registrar_lista(bit, archivo: str, huella: str, problemas) -> None:
    """Log that this list of errors was shown for this exact file (the numbers P1, P2... only mean something
    against it)."""
    conteo = {}
    for p in problemas:
        conteo[p.categoria] = conteo.get(p.categoria, 0) + 1
    bit.registrar("input_fix_proposed", archivo=archivo, sha256=huella, problemas=len(problemas),
                  por_categoria=dict(sorted(conteo.items())))


def aplicar(ruta, salida, bit, huella: str, aprobadas, rechazadas, comentario: str, cuando,
            ia: dict | None = None) -> "Path | None":
    """The one place that applies an approval, for the command line and the screen: log the rejections, write the
    corrected copy (never the original) and log each approval and the copy. Returns the copy, or None when nothing
    was approved (no copy is written). `AprobacionInvalida` propagates with nothing written but the rejections.
    `ia` ({P#: option}) is what the AI suggested on the screen; it goes to the log next to what the person chose."""
    from pathlib import Path
    from .auditoria import sha256_archivo
    ruta = Path(ruta)
    for pid, p in rechazadas:
        bit.registrar("input_fix_rejected", id=pid, codigo=p.codigo, hoja=p.hoja, fila=p.fila, celda=p.celda,
                      mensaje=p.mensaje, sha256=huella, comentario=comentario or "")
    if not aprobadas:
        return None
    destino = nombre_copia(ruta, salida, cuando)
    escribir_copia(ruta, destino, aprobadas)
    for pid, p, numero, cand in aprobadas:
        bit.registrar("input_fix_approved", id=pid, codigo=p.codigo, categoria=p.categoria, hoja=p.hoja, fila=p.fila,
                      mensaje=p.mensaje, candidato=numero, sha256=huella, comentario=comentario or "",
                      **({"ia_sugirio": ia[pid]} if ia and pid in ia else {}), **para_bitacora(cand))
    bit.registrar("input_fix_copy", archivo=ruta.name, sha256_original=huella, copia=destino.name,
                  sha256=sha256_archivo(destino), aplicadas=[pid for pid, *_ in aprobadas])
    return destino
