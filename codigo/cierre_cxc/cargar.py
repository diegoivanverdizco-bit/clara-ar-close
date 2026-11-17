"""Load and validate a dataset from an .xlsx workbook."""
from __future__ import annotations
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import openpyxl
from openpyxl.utils import get_column_letter

from .dinero import d
from .modelos import clave_cliente


@dataclass
class Problema:
    """One input error, with the cell it was found in when there is one."""
    mensaje: str
    hoja: str = ""
    fila: Optional[int] = None   # row number as Excel shows it
    columna: str = ""            # header as written in the file
    celda: str = ""              # e.g. "F3"; empty when the error is about a whole row or sheet
    valor: object = None         # the cell as read
    campo: str = ""              # canonical field (subtotal, abono...)
    codigo: str = ""             # kind of error, for the proposed correction (correcciones.py)
    contexto: dict = field(default_factory=dict)   # what the proposal needs: the raw row, other rows, the FIX
    categoria: str = "c"         # a safe, b to confirm, c not corrected, d structure
    candidatos: list = field(default_factory=list)  # correcciones.Candidato, computed by the code


class SchemaError(Exception):
    """The workbook does not look like the expected AR close schema.

    `problemas` lists every error found; the message lists them too, and is the error's own message when there is
    only one. `codigo` and `contexto` say what kind of error it is, for the proposed correction."""

    def __init__(self, mensaje, problemas=None, codigo: str = "", contexto: Optional[dict] = None):
        super().__init__(mensaje)
        self.codigo, self.contexto = codigo, dict(contexto or {})
        self.problemas = list(problemas) if problemas else [Problema(
            str(mensaje), codigo=codigo, contexto=self.contexto, categoria="d" if codigo == "estructura" else "c")]


MAX_LISTADOS = 20   # errors written out in the message; the rest are counted


def _e(codigo: str, contexto: Optional[dict], mensaje: str) -> SchemaError:
    """An input error of a known kind (see correcciones.REGLAS), with what its proposed correction needs."""
    return SchemaError(mensaje, codigo=codigo, contexto=contexto)


def _resumen(problemas: list) -> str:
    if len(problemas) == 1:
        return problemas[0].mensaje
    lineas = [f"The file has {len(problemas)} errors. Fix them all, save the file and run again:"]
    lineas += [f"{i}. {p.mensaje}" for i, p in enumerate(problemas[:MAX_LISTADOS], start=1)]
    if len(problemas) > MAX_LISTADOS:
        lineas.append(f"... and {len(problemas) - MAX_LISTADOS} more like these.")
    return "\n".join(lineas)


class _Errores:
    """Collects input errors instead of stopping at the first one, and remembers which cells failed, so the checks
    that depend on a failed cell are skipped instead of reporting the same mistake twice."""

    def __init__(self, categoria: str = ""):
        self.lista = []
        self.categoria = categoria   # "d" for structure errors
        self._fallos = {}   # id(Fila) -> fields that failed; "" is the row as a whole

    @contextmanager
    def en(self, f=None, campo: str = "", valor=None, contexto: Optional[dict] = None, **lugar):
        try:
            yield
        except SchemaError as e:
            self.agregar(str(e), f, campo, valor, codigo=e.codigo, contexto={**(contexto or {}), **e.contexto}, **lugar)

    def agregar(self, mensaje: str, f=None, campo: str = "", valor=None, hoja: str = "", fila=None,
                celda: str = "", codigo: str = "", contexto: Optional[dict] = None) -> Problema:
        columna = ""
        contexto = dict(contexto or {})
        if f is not None:
            hoja, fila = f.hoja, f.fila
            if campo:
                columna = f.columnas.get(campo, campo)
                if campo in f.indices:
                    celda = f"{get_column_letter(f.indices[campo] + 1)}{f.fila}"
            self._fallos.setdefault(id(f), set()).add(campo)
            contexto.update(tabla=f.tabla, fila=f.crudos, indices=f.indices)
        p = Problema(mensaje, hoja, fila, columna, celda, valor, campo, codigo, contexto)
        if self.categoria:
            p.categoria = self.categoria
        self.lista.append(p)
        return p

    def fallo(self, f, *campos) -> bool:
        """True if one of these fields of the row failed; with no fields, if anything in the row failed."""
        malos = self._fallos.get(id(f), set())
        return bool(malos & set(campos)) if campos else bool(malos)

    def detener(self) -> None:
        if self.lista:
            from .correcciones import proponer   # imported here: correcciones imports this module
            for p in self.lista:
                proponer(p)
            raise SchemaError(_resumen(self.lista), self.lista)


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


# canonical field -> accepted normalized headers (case, accents and punctuation are ignored)
SINONIMOS = {
    "cfdi": {
        "uuid": ["uuid"], "folio": ["seriefolio", "folio"], "fecha": ["fechaemision", "fecha"],
        "rfc": ["rfcreceptor", "rfc"], "cliente": ["cliente", "nombre", "razonsocial"], "moneda": ["moneda"],
        "tc": ["tipocambiocfdi", "tipocambio", "tipodecambio", "tcambio", "tc"], "subtotal": ["subtotal"],
        "tasa": ["tasaiva", "tasa"],
        "iva": ["iva"], "total": ["total"], "metodo": ["metodopago"], "forma": ["formapago"],
        "tipo": ["tipocomprobante", "tipo"],
        "relacionado": ["cfdirelacionadouuid", "cfdirelacionado", "relacionado"],
        "estatus": ["estatussat", "estatus"], "notas": ["notas"],
    },
    "banco": {
        "fecha": ["fecha", "fechaoperacion"], "cuenta": ["cuenta"], "referencia": ["referencia"],
        "descripcion": ["descripcion", "concepto"],
        "cargo": ["cargo", "cargos", "retiro", "retiros", "egreso", "egresos", "debito"],
        "abono": ["abono", "abonos", "deposito", "depositos", "ingreso", "ingresos", "credito"], "moneda": ["moneda"],
    },
    "rep": {
        "uuid_rep": ["uuidrep"], "fecha": ["fechadepago", "fecha"], "uuid_factura": ["uuidfacturarelacionada"],
        "folio": ["seriefolio", "folio"], "parcialidad": ["numparcialidad", "parcialidad"],
        "saldo_anterior": ["saldoanterior"], "pagado": ["importepagado"], "insoluto": ["saldoinsoluto"],
        "moneda": ["monedadepago", "moneda"], "tc": ["tcpago", "tipocambio"],
    },
    "apertura": {
        "folio": ["seriefolio", "folio"], "fecha": ["fechaemision", "fecha"], "cliente": ["cliente"],
        "moneda": ["moneda"], "saldo": ["saldomonedaorigen", "saldo"], "tc": ["tc31ago", "tc", "tipocambio"],
        "saldo_mxn": ["saldomxn"],
    },
}
OBLIGATORIOS = {
    "cfdi": ["uuid", "folio", "fecha", "moneda", "subtotal", "tasa", "iva", "total", "tipo", "estatus"],
    "banco": ["fecha", "descripcion", "cargo", "abono", "moneda"],
    "rep": ["fecha", "folio", "pagado"],
    "apertura": ["folio", "moneda", "saldo"],
}
PREFIJOS_HOJA = {"cfdi": ("cfdi",), "banco": ("banco",), "rep": ("rep",), "fx": ("tc", "fx"),
                 "apertura": ("ar_apertura", "apertura"), "catalogo": ("catalogo",)}


@dataclass
class Fila:
    """One source row with its location, so every number can be traced back."""
    datos: dict
    fuente: str
    columnas: dict = field(default_factory=dict)   # field -> header as written in the file
    hoja: str = ""
    fila: int = 0                                  # row number as Excel shows it
    indices: dict = field(default_factory=dict)    # field -> column index (0 = column A)
    tabla: str = ""                                # cfdi, banco, rep or apertura
    crudos: tuple = ()                             # the whole row as read, before any conversion


@dataclass
class Dataset:
    cfdi: list = field(default_factory=list)
    banco: list = field(default_factory=list)
    rep: list = field(default_factory=list)
    apertura: list = field(default_factory=list)
    fx: dict = field(default_factory=dict)        # date -> Decimal
    catalogo: dict = field(default_factory=dict)  # code -> name
    avisos: list = field(default_factory=list)
    archivo: str = ""


FORMATOS_FECHA = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S")
MONEDAS = {"MXN": "MXN", "MN": "MXN", "MXP": "MXN", "PESOS": "MXN", "PESO": "MXN",
           "USD": "USD", "US": "USD", "DLS": "USD", "DLLS": "USD", "DOLARES": "USD", "DOLAR": "USD"}
TC_MIN, TC_MAX = Decimal("5"), Decimal("50")        # plausible MXN per USD; outside it the rate is inverted or mistyped
ESTATUS = {"vigente": "Vigente", "cancelado": "Cancelado", "cancelada": "Cancelado"}
# a statement balance line STARTS with a balance word ("SALDO INICIAL", "SDO. INIC.", "OPENING BALANCE") and names no
# invoice and no payment; a real payment can start the same way but then says what it pays
SALDO_BANCO = re.compile(r"^(SALDO|SDO|BALANCE|OPENING|CLOSING)\b")
PAGO_BANCO = re.compile(r"\b(PAGO|PAGOS|SPEI|RECIBIDO|FACT|FACTURA|WIRE|TRANSF|TRANSFERENCIA|COBRO|ABONO)\b")
PATRON_UUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def _fecha(v, avisos: Optional[list] = None, donde: str = "") -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        t = v.strip()
        for fmt in FORMATOS_FECHA:
            try:
                f = datetime.strptime(t, fmt).date()
            except ValueError:
                continue
            # a date typed as text like 09/10/2026 could be 9-Oct or 10-Sep: say how it was read
            m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-]\d{4}", t)
            if avisos is not None and m and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2):
                avisos.append(f"{donde}: date typed as text '{t}' was read as day/month ({f:%d-%b-%Y}). "
                              "If it is month/day, type it as a real Excel date.")
            return f
    return None


def _num(v, donde: str = "") -> Decimal:
    try:
        n = d(v.strip() if isinstance(v, str) else v)
    except InvalidOperation:
        n = None
    # Decimal also reads "nan" and "Infinity", which later break every comparison
    if n is None or not n.is_finite():
        raise _e("no_numero", None, f"{donde}: {v!r} is not a number. Type it as a number, without thousands separators, "
                          "currency signs or text.")
    return n


def _moneda(v, donde: str, obligatoria: bool = True) -> str:
    t = re.sub(r"[^A-Z]", "", _norm(v).upper())
    if not t:
        if obligatoria:
            raise _e("moneda_vacia", None, f"{donde}: currency is empty. Write MXN or USD.")
        return "MXN"
    if t not in MONEDAS:
        raise _e("moneda_no_soportada", None, f"{donde}: currency {v!r} is not supported. The tool handles only MXN and USD.")
    return MONEDAS[t]


def _no_negativo(r: dict, campos, donde: str) -> None:
    for k in campos:
        if r.get(k) is not None and r[k] < 0:
            raise _e("negativo", None, f"{donde}: {k} is negative ({r[k]}). Amounts must be positive.")


TIPOS = {"I": "I", "INGRESO": "I", "E": "E", "EGRESO": "E", "P": "P", "PAGO": "P", "T": "T", "TRASLADO": "T",
         "N": "N", "NOMINA": "N"}


def _norm_texto(s) -> str:
    """Upper case, no accents, single spaces: for comparing free text such as bank descriptions."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def _texto_clave(v) -> str:
    """Folio or UUID as text. Excel turns 1001 into 1001.0 and some systems wrap UUIDs in braces."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"[{}\s]", "", str(v)) if v is not None else ""


def _hoja(wb, clave: str):
    hojas = [n for n in wb.sheetnames if any(n.lower().startswith(p) for p in PREFIJOS_HOJA[clave])]
    if len(hojas) > 1:
        raise SchemaError(f"More than one sheet looks like '{clave}': {hojas}. Keep only the one for this close "
                          "(an old month's sheet left in the file would be read instead of the right one).")
    return wb[hojas[0]] if hojas else None


def _vacia(fila) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in fila)


def _leer_tabla(ws, clave: str, avisos: list):
    filas = list(ws.iter_rows(values_only=True))
    if not filas or all(_vacia(f) for f in filas):
        raise SchemaError(f"Sheet '{ws.title}' is empty")
    # the header is the first row (within the first 10) that has every required column
    fila_enc, idx = None, {}
    for i, fila in enumerate(filas[:10]):
        enc = [_norm(c) for c in fila]
        cand = {}
        for campo, aceptados in SINONIMOS[clave].items():
            for a in aceptados:
                if a in enc:
                    if enc.count(a) > 1:
                        raise SchemaError(f"Sheet '{ws.title}': column '{a}' appears more than once in the header. "
                                          "Rename or delete the extra one.")
                    cand[campo] = enc.index(a)
                    break
        if all(c in cand for c in OBLIGATORIOS[clave]):
            fila_enc, idx = i, cand
            break
    if fila_enc is None:
        primera = next(f for f in filas if not _vacia(f))
        faltan = [c for c in OBLIGATORIOS[clave] if not any(a in [_norm(x) for x in primera] for a in SINONIMOS[clave][c])]
        raise SchemaError(f"Sheet '{ws.title}' is missing columns: {', '.join(faltan)}. "
                          f"Found headers: {[str(c) for c in primera if c is not None]}")
    if fila_enc > 0:
        avisos.append(f"Sheet '{ws.title}': header found in row {fila_enc + 1}; rows above it were ignored.")
    opcionales = [c for c in SINONIMOS[clave] if c not in idx]
    if opcionales:
        avisos.append(f"Sheet '{ws.title}': optional column(s) not found and ignored: {', '.join(opcionales)}.")
    nombres = {c: str(filas[fila_enc][i]) for c, i in idx.items()}
    salida = []
    for n, fila in enumerate(filas[fila_enc + 1:], start=fila_enc + 2):
        if _vacia(fila):
            continue
        reg = {c: (fila[i] if i < len(fila) else None) for c, i in idx.items()}
        salida.append(Fila(reg, f"{ws.title}!row {n}", hoja=ws.title, fila=n, tabla=clave, crudos=tuple(fila)))
    for fl in salida:
        fl.columnas, fl.indices = nombres, idx
    return salida


def _abrir(ruta: Path):
    try:
        return openpyxl.load_workbook(ruta, data_only=True)
    except PermissionError:
        raise _e("estructura", None, f"Cannot read {ruta.name}: it is locked by another program. Close it and run again.")
    except Exception as e:  # wrong format (.xls, .csv), corrupt or password-protected file
        raise _e("estructura", None, f"Cannot open {ruta.name} as an Excel workbook (.xlsx or .xlsm): {type(e).__name__}. "
                          "Save it as .xlsx and run again.") from e


def cargar(ruta) -> Dataset:
    ruta = Path(ruta)
    if not ruta.exists():
        raise _e("estructura", None, f"File not found: {ruta}")
    wb = _abrir(ruta)
    ds = Dataset(archivo=ruta.name)
    # 1. structure: every missing sheet or column is listed, and no row is read until the structure is right
    estructura = _Errores("d")
    for clave in ("cfdi", "banco", "rep", "apertura"):
        with estructura.en():
            ws = _hoja(wb, clave)
            if ws is None:
                extra = (" If the opening AR is really zero, add the sheet with its headers only."
                         if clave == "apertura" else "")
                raise SchemaError(f"No sheet found for '{clave}' (expected a name starting with {PREFIJOS_HOJA[clave]}). "
                                  f"Sheets in the file: {wb.sheetnames}.{extra}")
            setattr(ds, clave, _leer_tabla(ws, clave, ds.avisos))
            if clave == "apertura" and not ds.apertura:
                ds.avisos.append(f"Sheet '{ws.title}' has no rows: opening AR is zero.")
    ws = ws_catalogo = None
    with estructura.en():
        ws = _hoja(wb, "fx")
        if ws is None:
            raise SchemaError(f"No exchange-rate sheet found (name starting with 'TC' or 'FX'). Sheets in the file: {wb.sheetnames}.")
    with estructura.en():
        ws_catalogo = _hoja(wb, "catalogo")
    estructura.detener()
    # 2. cells and 3. cross checks: every error is collected, and the run stops once, with all of them
    err = _Errores()
    ambiguas, inicio_datos, fila_fx = [], None, {}
    for n, fila in enumerate(ws.iter_rows(values_only=True), start=1):
        if not fila or _vacia(fila):
            continue
        f = _fecha(fila[0], ambiguas, f"{ws.title}!row {n}")
        if f is None and inicio_datos is None and n <= 10:
            continue                      # title and header rows above the first date
        if f is None:
            err.agregar(f"{ws.title}!row {n}: {fila[0]!r} is not a date. The first column of the exchange-rate "
                        "sheet must be the date and the second the rate (title and header rows are accepted "
                        "only in the first 10 rows).", hoja=ws.title, fila=n, celda=f"A{n}", valor=fila[0],
                        codigo="fx_no_fecha")
            continue
        inicio_datos = inicio_datos or n
        # Banxico publishes "N/E" (no rate) on weekends and holidays
        if len(fila) < 2 or fila[1] is None or (isinstance(fila[1], str) and re.sub(r"[^A-Z]", "", fila[1].upper()) in ("", "NE", "ND", "NA")):
            ds.avisos.append(f"{ws.title}!row {n}: no rate for {f}; the previous available rate is used when needed.")
            continue
        with err.en(hoja=ws.title, fila=n, celda=f"B{n}", valor=fila[1]):
            tc = _num(fila[1], f"{ws.title}!row {n}")
            if tc <= 0:
                raise _e("fx_cero", None, f"{ws.title}!row {n}: exchange rate {tc} must be greater than zero.")
            _tc_plausible(tc, f"{ws.title}!row {n}")
            if f in ds.fx and ds.fx[f] != tc:
                raise _e("fx_fecha_doble", {"fila_previa": fila_fx[f], "tc_previo": ds.fx[f], "tc": tc},
                         f"{ws.title}!row {n}: the date {f} appears twice with different rates "
                                  f"({ds.fx[f]} and {tc}). Keep only the right one.")
            ds.fx[f] = tc
            fila_fx.setdefault(f, n)
    for p in err.lista:                   # the median of the good rates tells which reading of a bad one fits
        if p.codigo in ("tc_fuera_rango", "fx_fecha_doble"):
            p.contexto["mediana"] = _mediana(ds)
    if ambiguas:
        ds.avisos.append(f"{ws.title}: {len(ambiguas)} date(s) typed as text that could be day/month or month/day were "
                         f"read as day/month. First one: {ambiguas[0]}")
    if not ds.fx and not err.lista:
        raise _e("estructura", None, "Exchange-rate sheet has no usable rows")
    if ds.fx:
        _tc_atipicos(ds, ws.title)
    if ws_catalogo is not None:
        for fila in ws_catalogo.iter_rows(min_row=2, values_only=True):
            if fila and fila[0] is not None:
                ds.catalogo[str(fila[0]).strip()] = str(fila[1] or "").strip() if len(fila) > 1 else ""
    if not ds.catalogo:
        ds.avisos.append("No chart of accounts found: journal entries show account codes without names.")
    _normalizar(ds, err)
    _duplicados(ds, err)
    _cruces(ds, err)
    err.detener()
    _plazos_raros(ds)
    _enlazar_rep(ds)
    return ds


def _plazos_raros(ds: Dataset) -> None:
    """Credit terms typed wrong ("Net 3" for "Net 30") move an invoice between aging buckets and change the order in
    which receipts are applied. No rule can tell a typo from real terms, but it leaves a trace: the invoice differs
    from the customer's usual terms, or, for a customer with no other invoice, from the terms most invoices have.
    Only a notice: it never stops the run (decided by Diego, 2026-11-09)."""
    from collections import Counter
    filas = []
    for f in ds.cfdi:
        r = f.datos
        m = re.search(r"\bnet\s*(\d+)", r["notas"], re.I) if r["tipo"] == "I" else None
        if m:
            filas.append((f, clave_cliente(r["rfc"], r["cliente"]) if r["rfc"] else _norm(r["cliente"]),
                          int(m.group(1))))
    del_archivo = Counter(p for _, _, p in filas)
    for f, cliente, plazo in filas:
        otros = Counter(p for g, c, p in filas if c == cliente and g is not f)
        if otros:
            habitual, base = otros.most_common(1)[0][0], "this customer's other invoices"
        elif len(filas) >= 3 and del_archivo.most_common(1)[0][1] * 2 > len(filas):
            habitual, base = del_archivo.most_common(1)[0][0], "most invoices in the file"
        else:
            continue
        if plazo != habitual:
            ds.avisos.append(f"{f.fuente}: {f.datos['folio']} has credit terms of {plazo} days, but {base} have {habitual}. "
                             "Check the notes column: the terms set the due date, the aging and the order in which "
                             "receipts are applied.")


def _cruces(ds: Dataset, err: _Errores) -> None:
    """Figures that must agree between columns or sheets. When they do not, there is no safe way to guess which
    one is right, so the run stops. Rows with an unreadable cell are skipped (their error is already listed), and
    only the first disagreement of a row is listed: the later ones are usually the same wrong cell seen again."""
    med = _mediana(ds)
    for f in ds.cfdi:
        if err.fallo(f):
            continue
        r = f.datos
        with err.en(f):
            if r["moneda"] == "MXN" and r["tc"] not in (None, Decimal(1)):
                raise _e("mxn_con_tc", {"tc": r["tc"], "fix": _fix(ds, r["fecha"])},
                         f"{_donde(f, 'tc')}: {r['folio']} is in MXN but has exchange rate {r['tc']}. If the invoice is "
                                  "in USD, correct the currency; if it is in MXN, leave the rate empty or 1.")
            if r["moneda"] != "MXN":
                _tc_plausible(r["tc"], _donde(f, "tc"), {"mediana": med, "fix": _fix(ds, r["fecha"])})
        if r["moneda"] != "MXN" and r["tc"] is None and r["tipo"] == "I":
            ds.avisos.append(f"{f.fuente}: {r['folio']} is in {r['moneda']} but has no exchange rate in the CFDI: the FIX of "
                             "the issue date is used. Check the currency.")
    cfdi = {f.datos["folio"]: f.datos for f in ds.cfdi if f.datos["tipo"] == "I" and not err.fallo(f)}
    for f in ds.apertura:
        if not err.fallo(f):
            with err.en(f):
                _cruce_apertura(f, cfdi, med)


def _cruce_apertura(f: Fila, cfdi: dict, med) -> None:
    """The opening balance of one invoice must agree with its CFDI and with itself (balance, rate, MXN balance)."""
    a = f.datos
    c = cfdi.get(a["folio"])
    if c and c["moneda"] != a["moneda"]:
        raise _e("apertura_moneda", {"moneda_cfdi": c["moneda"]}, f"{_donde(f, 'moneda')}: opening AR of {a['folio']} is in {a['moneda']} but its CFDI is in "
                          f"{c['moneda']}. Correct the one that is wrong.")
    if a["moneda"] == "MXN":
        if a["tc"] not in (None, Decimal(1)):
            raise _e("apertura_mxn_tc", None, f"{_donde(f, 'tc')}: opening AR of {a['folio']} is in MXN but has rate {a['tc']}. "
                              "Leave it empty or 1.")
        if a["saldo_mxn"] is not None and abs(a["saldo_mxn"] - a["saldo"]) > Decimal("0.01"):
            raise _e("apertura_mxn_saldo", None, f"{f.fuente}: opening AR of {a['folio']} is in MXN, so its MXN balance "
                              f"({a['saldo_mxn']:,.2f}) must equal its balance ({a['saldo']:,.2f}).")
    elif a["saldo_mxn"] is None and a["tc"] is None:
        raise _e("apertura_sin_valor", None, f"{f.fuente}: opening AR of {a['folio']} is in {a['moneda']} but has neither a rate nor an "
                          "MXN balance, so its booked value in MXN is unknown. Fill in one of the two.")
    elif a["saldo_mxn"] is not None and a["tc"] is not None and abs(a["saldo"] * a["tc"] - a["saldo_mxn"]) > Decimal("1"):
        raise _e("apertura_no_cuadra", None, f"{f.fuente}: opening AR of {a['folio']}: {a['saldo']:,.2f} {a['moneda']} x {a['tc']} = "
                          f"{a['saldo'] * a['tc']:,.2f}, but the MXN balance says {a['saldo_mxn']:,.2f}. "
                          "Correct the one that is wrong.")
    if a["moneda"] != "MXN":           # a booked rate of 1 would turn the whole USD balance into an FX result
        if a["tc"] is not None:
            _tc_plausible(a["tc"], _donde(f, "tc"), {"mediana": med})
        elif a["saldo"]:
            _tc_plausible(a["saldo_mxn"] / a["saldo"], f"{f.fuente} (MXN balance / balance)", {"sin_celda": True})


def _enlazar_rep(ds: Dataset) -> None:
    """A REP belongs to the invoice of its related UUID; the folio typed in the REP row is only the fallback."""
    folio_por_uuid = {f.datos["uuid"]: f.datos["folio"] for f in ds.cfdi if PATRON_UUID.match(f.datos["uuid"])}
    for f in ds.rep:
        r = f.datos
        folio = folio_por_uuid.get(r["uuid_factura"])
        if r["uuid_factura"] and not folio:
            ds.avisos.append(f"{f.fuente}: the REP's related UUID {r['uuid_factura']} is not among the CFDI; it was linked by "
                             f"folio '{r['folio']}' instead. Check that the REP points to the right invoice.")
        if folio and folio != r["folio"]:
            ds.avisos.append(f"{f.fuente}: the REP says folio '{r['folio']}' but its related UUID belongs to {folio}; "
                             f"linked to {folio}.")
            r["folio"] = folio


def _mediana(ds: Dataset):
    valores = sorted(ds.fx.values())
    return valores[len(valores) // 2] if valores else None


def _fix(ds: Dataset, fecha):
    """The FIX of a date, or of the last earlier date that has one (weekends and holidays have none)."""
    if not isinstance(fecha, date):
        return None
    previas = [x for x in ds.fx if x <= fecha]
    return ds.fx[max(previas)] if previas else None


def _tc_plausible(tc, donde: str, contexto: Optional[dict] = None) -> None:
    if tc is not None and not TC_MIN <= tc <= TC_MAX:
        raise _e("tc_fuera_rango", {"tc": tc, **(contexto or {})}, f"{donde}: exchange rate {tc:.4f} is outside {TC_MIN}-{TC_MAX} MXN per USD. If it is quoted as "
                          "USD per MXN, invert it; otherwise correct the typo.")


def _tc_atipicos(ds: Dataset, hoja: str) -> None:
    """A rate far from the rest of the table is almost always a typo (1.8 or 182 instead of 18.2)."""
    valores = sorted(ds.fx.values())
    mediana = valores[len(valores) // 2]
    for f, tc in sorted(ds.fx.items()):
        if abs(tc - mediana) > mediana * Decimal("0.10"):
            ds.avisos.append(f"{hoja}: the rate of {f} ({tc}) is more than 10% away from the table's median "
                             f"({mediana:.4f}). Check it for a typo.")


def _lleno(r: dict, campo: str, f) -> None:
    if r.get(campo) is None or (isinstance(r[campo], str) and not r[campo].strip()):
        raise _e("vacia", None, f"{_donde(f, campo)}: the cell is empty. Type the amount (0 if it is zero). If it has a "
                          "formula, open the file in Excel and save it, so the result is stored.")


def _donde(f: Fila, campo: str) -> str:
    col = getattr(f, "columnas", {}).get(campo, campo)
    return f"{f.fuente}, column '{col}'"


def _normalizar(ds: Dataset, err: _Errores) -> None:
    """Convert raw cells to typed values. A cell that cannot be read is listed and its row goes on, so every bad
    cell of the file is found in one run; the checks that need a failed cell are skipped for that row."""
    av = ds.avisos
    for f in ds.cfdi:
        r = f.datos
        crudo = r["fecha"]
        with err.en(f, "fecha", crudo):
            r["fecha"] = _fecha(crudo, av, f.fuente)
            if r["fecha"] is None:
                raise _e("fecha_invalida", None, f"{_donde(f, 'fecha')}: invalid issue date {crudo!r}.")
        for k in ("subtotal", "tasa", "iva", "total"):
            with err.en(f, k, r[k]):
                _lleno(r, k, f)
                r[k] = _num(r[k], _donde(f, k))
        for k in ("subtotal", "iva", "total"):
            if not err.fallo(f, k):
                with err.en(f, k, r[k]):
                    _no_negativo(r, (k,), f.fuente)
        if not err.fallo(f, "tasa"):
            with err.en(f, "tasa", r["tasa"]):
                if r["tasa"] > 1:
                    raise _e("tasa_porcentaje", None, f"{_donde(f, 'tasa')}: IVA rate {r['tasa']} looks like a percentage. Type it as a "
                                      "fraction (0.16 for 16%).")
        with err.en(f, "tc", r.get("tc")):
            r["tc"] = _num(r["tc"], _donde(f, "tc")) if r.get("tc") not in (None, "") else None
        with err.en(f, "moneda", r["moneda"]):
            r["moneda"] = _moneda(r["moneda"], _donde(f, "moneda"))
        with err.en(f, "tipo", r["tipo"]):
            tipo = re.sub(r"[^A-Z]", "", _norm(r["tipo"]).upper())
            if tipo not in TIPOS:
                raise _e("tipo_invalido", None, f"{_donde(f, 'tipo')}: CFDI type {r['tipo']!r} is not valid. Use I (ingreso), "
                                  "E (egreso) or P (pago).")
            r["tipo"] = TIPOS[tipo]
        if not err.fallo(f, "tipo", "total"):
            with err.en(f, "tipo", r["tipo"]):
                if r["tipo"] not in ("I", "E") and r["total"] != 0:
                    raise _e("tipo_con_total", None, f"{_donde(f, 'tipo')}: {r['folio']} has type {r['tipo']} and a total of "
                                      f"{r['total']:,.2f}. Only I (invoice) and E (credit note) are processed: if it is "
                                      "an invoice, type I.")
        # only Vigente or Cancelado/a are known; "Cancelación en proceso" is still valid at SAT
        r["estatus"] = ESTATUS.get(_norm(r["estatus"]), str(r["estatus"] or "").strip())
        crudo = r["folio"]
        r["folio"] = _texto_clave(crudo)
        if not r["folio"]:
            err.agregar(f"{_donde(f, 'folio')}: the folio is empty.", f, "folio", crudo, codigo="folio_vacio")
        r["uuid"] = _texto_clave(r["uuid"]).upper()
        if not PATRON_UUID.match(r["uuid"]):
            av.append(f"{_donde(f, 'uuid')}: '{r['uuid']}' of {r['folio']} does not look like a UUID "
                      "(Excel may have changed it). Credit notes and REPs cannot be linked to it by UUID.")
        r["rfc"] = re.sub(r"\s", "", str(r.get("rfc") or "")).upper()
        r["cliente"] = str(r.get("cliente") or "").strip()
        r["metodo"] = str(r.get("metodo") or "").strip().upper()
        r["forma"] = str(r.get("forma") or "").strip()
        r["notas"] = str(r.get("notas") or "")
        if r["tipo"] == "I":
            if not r["cliente"]:
                av.append(f"{f.fuente}: {r['folio']} has no customer name: receipts cannot be matched to it by name.")
            if r["metodo"] not in ("PUE", "PPD"):
                av.append(f"{f.fuente}: {r['folio']} has payment method {r['metodo'] or 'empty'!r}; it should be PUE or "
                          "PPD. The PUE/PPD checks were skipped for it.")
            plazo = re.search(r"\bnet\s*(\d+)", r["notas"], re.I)
            if plazo and not 0 <= int(plazo.group(1)) <= 180:
                av.append(f"{f.fuente}: {r['folio']} has credit terms of {plazo.group(1)} days. Check them: they set the due "
                          "date, the aging and the order in which receipts are applied.")
        rel = r.get("relacionado")
        r["relacionado"] = _texto_clave(rel).upper() if rel not in (None, "") else None
        if r["tipo"] == "E" and not r["relacionado"]:
            err.agregar(f"{_donde(f, 'relacionado')}: {r['folio']} is a credit note (type E) with no related CFDI, so it "
                        "cannot be applied. If it is an invoice, type I; if it is a credit note, fill in the UUID of "
                        "the invoice it relates to.", f, "relacionado", rel, codigo="nc_sin_relacionado",
                        contexto={"rfc": r["rfc"], "cliente": r["cliente"]})
        if r["tipo"] == "I" and r["relacionado"]:
            av.append(f"{f.fuente}: {r['folio']} is type I but has a related CFDI. If it is a credit note, its type must "
                      "be E; if it replaces another invoice, check that the replaced one is cancelled.")
    # a credit note without its related CFDI: the candidates are the invoices of the same customer
    facturas = [g for g in ds.cfdi if g.datos["tipo"] == "I" and PATRON_UUID.match(g.datos["uuid"]) and not err.fallo(g)]
    for p in err.lista:
        if p.codigo == "nc_sin_relacionado":
            cliente = clave_cliente(p.contexto["rfc"], p.contexto["cliente"])
            p.contexto["facturas"] = [(g.datos["folio"], g.datos["uuid"]) for g in facturas
                                      if clave_cliente(g.datos["rfc"], g.datos["cliente"]) == cliente
                                      and (g.hoja, g.fila) != (p.hoja, p.fila)]
    folios = {_norm_texto(f.datos["folio"]) for f in ds.cfdi if f.datos["folio"]}
    for f in list(ds.banco):
        r = f.datos
        crudo = r["fecha"]
        r["fecha"] = _fecha(crudo, av, f.fuente)
        if r["fecha"] is None:
            # a bank row without a date is usually a totals or notes row: its other cells are not checked
            err.agregar(f"{_donde(f, 'fecha')}: invalid date {crudo!r}. If it is a totals or notes row, delete it.",
                        f, "fecha", crudo, codigo="banco_sin_fecha")
            continue
        for k in ("cargo", "abono"):
            with err.en(f, k, r[k]):
                r[k] = _num(r[k], _donde(f, k))
        if not err.fallo(f, "cargo", "abono"):
            with err.en(f):
                if r["cargo"] < 0 or r["abono"] < 0:
                    raise _e("negativo", None, f"{f.fuente}: negative amount (cargo {r['cargo']}, abono {r['abono']}). Withdrawals go "
                                      "in the cargo column and deposits in the abono column, both as positive numbers.")
                if r["cargo"] > 0 and r["abono"] > 0:
                    raise _e("cargo_y_abono", None, f"{f.fuente}: has both a cargo ({r['cargo']}) and an abono ({r['abono']}). "
                                      "Each bank row must be one or the other: split it into two rows.")
            if r["cargo"] == 0 and r["abono"] == 0:
                av.append(f"{f.fuente}: bank row with no amount ('{r['descripcion']}'): ignored. If the amount has a "
                          "formula, open the file in Excel and save it, so the result is stored.")
                ds.banco.remove(f)
                continue
        cuenta = str(r.get("cuenta") or "")
        en_cuenta = {MONEDAS[t] for t in re.findall(r"[A-Z]+", cuenta.upper()) if t in MONEDAS}
        with err.en(f, "moneda", r["moneda"], contexto={"moneda_cuenta": sorted(en_cuenta)}):
            r["moneda"] = _moneda(r["moneda"], _donde(f, "moneda"))
        r["descripcion"] = str(r["descripcion"] or "")
        limpio = " ".join(re.sub(r"[^A-Z0-9 ]", " ", _norm_texto(r["descripcion"])).split())
        nombra_folio = any(re.sub(r"[^A-Z0-9]", "", fo) in limpio.replace(" ", "") for fo in folios)
        if SALDO_BANCO.search(limpio) and not PAGO_BANCO.search(limpio) and not nombra_folio:
            err.agregar(f"{f.fuente}: '{r['descripcion']}' looks like a balance line of the statement, not a "
                        "movement. If it is a balance, delete the row; if it is a real deposit or withdrawal, add "
                        "the payer or the invoice to its description so it is not read as a balance.",
                        f, "descripcion", r["descripcion"], codigo="linea_saldo")
        r["referencia"] = str(r.get("referencia") or "")
        r["cuenta"] = cuenta
        if len(en_cuenta) == 1 and not err.fallo(f, "moneda") and r["moneda"] not in en_cuenta:
            av.append(f"{f.fuente}: account '{r['cuenta']}' looks like a {en_cuenta.pop()} account but the row says "
                      f"{r['moneda']}. Check the currency: it changes the amount in MXN.")
    for f in ds.rep:
        r = f.datos
        crudo = r["fecha"]
        with err.en(f, "fecha", crudo):
            r["fecha"] = _fecha(crudo, av, f.fuente)
            if r["fecha"] is None:
                raise _e("fecha_invalida", None, f"{_donde(f, 'fecha')}: invalid payment date {crudo!r}.")
        for k in ("saldo_anterior", "pagado", "insoluto", "tc"):
            with err.en(f, k, r.get(k)):
                r[k] = _num(r.get(k), _donde(f, k)) if r.get(k) not in (None, "") else None
        for k in ("saldo_anterior", "pagado", "insoluto", "tc"):
            if not err.fallo(f, k):
                with err.en(f, k, r[k]):
                    _no_negativo(r, (k,), f.fuente)
        with err.en(f, "moneda", r.get("moneda")):
            r["moneda"] = _moneda(r.get("moneda"), _donde(f, "moneda"), obligatoria=False)
        r["folio"] = _texto_clave(r["folio"])
        r["uuid_rep"] = _texto_clave(r.get("uuid_rep")).upper()
        r["uuid_factura"] = _texto_clave(r.get("uuid_factura")).upper()
        with err.en(f, "parcialidad", r.get("parcialidad")):
            r["parcialidad"] = (int(_num(r["parcialidad"], _donde(f, "parcialidad")))
                                if r.get("parcialidad") not in (None, "") else None)
    for f in ds.apertura:
        r = f.datos
        r["fecha"] = _fecha(r.get("fecha"), av, f.fuente)
        with err.en(f, "saldo", r["saldo"]):
            _lleno(r, "saldo", f)
            r["saldo"] = _num(r["saldo"], _donde(f, "saldo"))
        for k in ("tc", "saldo_mxn"):
            with err.en(f, k, r.get(k)):
                r[k] = _num(r[k], _donde(f, k)) if r.get(k) not in (None, "") else None
        for k in ("saldo", "tc", "saldo_mxn"):
            if not err.fallo(f, k):
                with err.en(f, k, r[k]):
                    _no_negativo(r, (k,), f.fuente)
        with err.en(f, "moneda", r["moneda"]):
            r["moneda"] = _moneda(r["moneda"], _donde(f, "moneda"))
        r["folio"] = _texto_clave(r["folio"])
        r["cliente"] = str(r.get("cliente") or "").strip()


def _duplicados(ds: Dataset, err: _Errores) -> None:
    """Keys that must be unique. A repeated key is almost always a row pasted twice. Each repeated row is listed
    once: a row already listed as repeated (by folio) is not listed again (by UUID). A row with an unreadable cell
    is still checked, unless the unreadable cell is its key, so a pasted row shows in the same run as its error."""
    def unico(filas, clave, que, campos=()):
        vistos = {}
        for f in filas:
            if err.fallo(f, "", *campos):
                continue
            k = clave(f.datos)
            if k is None:
                continue
            if k in vistos:
                otra = vistos[k]
                err.agregar(f"{que} {k} appears twice ({otra.fuente} and {f.fuente}). "
                            "Delete the repeated row or correct the key.", f, codigo="repetido",
                            contexto={"otra_fila": otra.fila, "otra_crudos": otra.crudos,
                                      "uuid_valido": f.tabla == "cfdi" and bool(PATRON_UUID.match(f.datos["uuid"]))})
                continue
            vistos[k] = f
    unico(ds.cfdi, lambda r: r["folio"], "CFDI folio", ("folio",))
    unico(ds.cfdi, lambda r: r["uuid"] if PATRON_UUID.match(r["uuid"]) else None, "CFDI UUID")
    unico(ds.apertura, lambda r: r["folio"], "Opening AR folio")
    unico(ds.rep, lambda r: (r["uuid_rep"], r["uuid_factura"] or r["folio"]) if r["uuid_rep"] else None,
          "REP (UUID REP, related invoice)")
