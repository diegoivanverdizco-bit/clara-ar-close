"""Proposed corrections for input errors (docs/2026-11-17 Diseño corrección de errores de captura.md).

Every value proposed here is computed by the code from the file itself; nothing is guessed and no language model is
called. Corrections were simplified on purpose (Diego, L87): only what is obvious is corrected, everything else is
reported. Each error the load collects gets a category:

  a  safe: one reading and nothing to decide; approved in bulk or one by one
  c  not corrected: only reported, with the cell, the value and what to check (nothing in the file says the right value)
  d  structure: no row can be read until it is fixed

Only three kinds of error are safe: an amount typed as text in Mexican format ("$290,000.00", "69,600"), an IVA rate
typed as a percentage (16) when subtotal x 0.16 equals the captured IVA, and a row pasted twice exactly with a valid
UUID. A correction is a list of cell changes, or a row to delete; `escribir_copia` applies the approved ones to a copy
of the file and never touches the original.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

TOLERANCIA = Decimal("0.02")          # same tolerance as the IVA check in excepciones.py


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


def proponer(p) -> None:
    """Fill p.categoria and p.candidatos. A structure error stays 'd'; anything that is not one of the three safe
    kinds stays 'c', with no candidate."""
    if p.categoria == "d":
        return
    regla = REGLAS.get(p.codigo)
    cand = regla(p) if regla else None
    p.categoria, p.candidatos = ("a", [cand]) if cand else ("c", [])


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


# ---- safe 1: an amount typed as text in Mexican format --------------------------------------------------

# optional $, thousands commas in groups of 3 (or none), optional decimals after a point: $290,000.00 · 69,600 · $290000
_MEXICANO = re.compile(r"^\$?\s*(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$", re.ASCII)


def _monto_en_texto(p):
    if not p.celda or not isinstance(p.valor, str):
        return None
    texto = p.valor.strip()
    if not _MEXICANO.match(texto):
        return None
    n = Decimal(texto.lstrip("$").strip().replace(",", ""))
    como = "commas as thousands separators" if "," in texto else "the currency sign removed"
    return Candidato(f"Type {_t(n)}", f"read with {como}", [Cambio(p.hoja, p.celda, p.valor, n)])


# ---- safe 2: IVA rate typed as a percentage -------------------------------------------------------------

def _tasa_porcentaje(p):
    n, sub, iva = _dec(p.valor), _dec(_crudo(p, "subtotal")), _dec(_crudo(p, "iva"))
    if n is None or sub is None or iva is None or not p.celda:
        return None
    nueva = n / 100
    if abs(sub * nueva - iva) > TOLERANCIA:
        return None                    # the rate does not check: a person decides what is wrong
    return Candidato(f"Type {_t(nueva)}",
                     f"{_t(n)} looks like a percentage; subtotal {_m(sub)} x {_t(nueva)} = IVA {_m(iva)}",
                     [Cambio(p.hoja, p.celda, _crudo(p, "tasa"), nueva)])


# ---- safe 3: a row pasted twice exactly -----------------------------------------------------------------

def _repetido(p):
    if p.contexto.get("fila") != p.contexto.get("otra_crudos") or not p.contexto.get("uuid_valido"):
        return None
    return Candidato(f"Delete row {p.fila}",
                     f"identical to row {p.contexto.get('otra_fila')}, and the SAT never stamps the same UUID twice",
                     quitar_fila=(p.hoja, p.fila))


# ---- approving and writing the corrected copy ----------------------------------------------------------

ETIQUETA = {"a": "SAFE", "c": "NOT FIXED", "d": "STRUCTURE"}


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
    `--seguras` approves every safe one; `--elegir P2` (or P2=1) approves one safe one; a correction that is not
    safe has no candidate and cannot be approved."""
    elegidos = {}
    for parte in _ids(elegir_texto):
        pid, _, numero = parte.partition("=")
        if pid not in numerados:
            raise AprobacionInvalida(f"{pid} is not in the list (P1-P{len(numerados)}).")
        p = numerados[pid]
        if not p.candidatos:
            raise AprobacionInvalida(f"{pid} has no proposed correction: correct it in the file.")
        numero = numero or "1"
        if not numero.isdigit() or not 1 <= int(numero) <= len(p.candidatos):
            raise AprobacionInvalida(f"{pid} has {len(p.candidatos)} candidate(s): choose {pid} or {pid}=1.")
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
    formulas), with the approved cell changes, then the deleted rows (bottom up, so row numbers hold).
    Every cell is checked against the value that was proposed first; if the file changed since, or two
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
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)


def _texto(v):
    return None if v is None else str(v)


def para_bitacora(candidato) -> dict:
    """The approved change as the audit log keeps it: values as text, so a Decimal and a float read the same."""
    return {"que": candidato.que, "porque": candidato.porque,
            "cambios": [{"hoja": c.hoja, "celda": c.celda, "antes": _texto(c.antes), "despues": _texto(c.despues)}
                        for c in candidato.cambios],
            "quitar_fila": list(candidato.quitar_fila) if candidato.quitar_fila else None}


REGLAS = {"no_numero": _monto_en_texto, "tasa_porcentaje": _tasa_porcentaje, "repetido": _repetido}


def registrar_lista(bit, archivo: str, huella: str, problemas) -> None:
    """Log that this list of errors was shown for this exact file (the numbers P1, P2... only mean something
    against it)."""
    conteo = {}
    for p in problemas:
        conteo[p.categoria] = conteo.get(p.categoria, 0) + 1
    bit.registrar("input_fix_proposed", archivo=archivo, sha256=huella, problemas=len(problemas),
                  por_categoria=dict(sorted(conteo.items())))


def aplicar(ruta, salida, bit, huella: str, aprobadas, rechazadas, comentario: str, cuando) -> "Path | None":
    """The one place that applies an approval, for the command line and the screen: log the rejections, write the
    corrected copy (never the original) and log each approval and the copy. Returns the copy, or None when nothing
    was approved (no copy is written). `AprobacionInvalida` propagates with nothing written but the rejections."""
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
                      **para_bitacora(cand))
    bit.registrar("input_fix_copy", archivo=ruta.name, sha256_original=huella, copia=destino.name,
                  sha256=sha256_archivo(destino), aplicadas=[pid for pid, *_ in aprobadas])
    return destino
