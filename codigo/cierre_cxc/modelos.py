"""Plain data containers shared by the engine, the exporters and the UI."""
from __future__ import annotations
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

# SAT generic RFCs: general public (XAXX) and foreign customers (XEXX). Many customers share them.
RFC_GENERICOS = frozenset({"XAXX010101000", "XEXX010101000"})


def clave_cliente(rfc: str, nombre: str) -> str:
    """Key that identifies a customer: its RFC. A generic RFC is shared by many customers, so there the key is
    RFC + name; without the name, a receipt from one foreign customer could be applied to another one's invoice."""
    n = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().upper()
    if rfc in RFC_GENERICOS:
        return f"{rfc} {' '.join(n.split())}"
    return rfc or n


@dataclass
class Partida:
    """An open AR item. `saldo` is in invoice currency; `libro_mxn` is the booked carrying value."""
    folio: str
    uuid: str
    cliente: str
    cliente_clave: str
    rfc: str
    moneda: str
    fecha: date
    vencimiento: date
    tasa: Decimal
    total_orig: Decimal
    saldo: Decimal
    libro_mxn: Decimal
    iva_total: Decimal
    iva_pend: Decimal              # IVA still to be collected, invoice currency
    iva_libro_pend: Decimal        # same, MXN at booked rate
    origen: str                    # 'apertura' | 'periodo'
    metodo: str = ""
    fuentes: list = field(default_factory=list)
    libro_original_mxn: Decimal = Decimal("0")   # carrying before any conditional correction
    tc_correccion: Optional[Decimal] = None
    iva_libro_inicial: Decimal = Decimal("0")    # IVA por trasladar booked when the item entered AR

    @property
    def tc_libro(self) -> Decimal:
        return self.libro_mxn / self.saldo if self.saldo else Decimal("1")


@dataclass
class Movimiento:
    id: str
    fecha: date
    cuenta: str
    referencia: str
    descripcion: str
    cargo: Decimal
    abono: Decimal
    moneda: str
    fuente: str
    clase: str = ""               # cobro | comision | iva_comision | no_cxc | abono_no_cxc
    pista: str = ""               # customer or invoice named in a withdrawal's description (possible deposit typed wrong)


@dataclass
class Propuesta:
    items: list = field(default_factory=list)      # [(Partida, monto_doc, monto_pago)]
    remanente: Decimal = Decimal("0")              # payment currency, goes to unapplied cash
    tipo: str = "no_identificado"
    confianza: str = "baja"
    razon: str = ""
    alertas: list = field(default_factory=list)
    alternativas: list = field(default_factory=list)
    modo: str = ""                                # ref | cliente | aprendido | monto | manual
    # 1 = applied by a rule (invoice number, customer name or learned rule); 2 = only a clue (amount, language
    # model): nothing is applied until a reviewer accepts a candidate; 3 = no clue: held in 2101 for a person
    nivel: int = 0
    candidatos: list = field(default_factory=list)  # [{folio, cliente, saldo, moneda, evidencia}] for level 2
    regla: dict = field(default_factory=dict)       # learned rule that pointed to a customer but could not decide
    pista: dict = field(default_factory=dict)       # customer whose name is SIMILAR to the description (typo): level 2
    # invoice whose amount matched exactly while older invoices were open: the payment went oldest first (SAT payments
    # complement guide 2.0, numeral 4) and AR asks the customer within the 5-day window (numeral 3)
    orden_antiguo: str = ""


@dataclass
class Aplicacion:
    id: str
    mov_id: str
    fecha: date
    folio: Optional[str]
    cliente: str
    tipo: str
    confianza: str
    razon: str
    alertas: list
    pago_monto: Decimal
    pago_moneda: str
    doc_moneda: str
    saldo_antes: Decimal
    aplicado_doc: Decimal
    saldo_despues: Decimal
    tc_cobro: Decimal
    mxn_recibido: Decimal
    libro_relevado: Decimal
    fx_realizada: Decimal          # >0 gain, <0 loss, on the whole receivable
    iva_doc: Decimal
    iva_libro_mxn: Decimal
    iva_cobrado_mxn: Decimal
    fuentes: list
    cuenta_banco: str = ""
    rep: Optional[dict] = None
    tasa: Decimal = Decimal("0")


@dataclass
class NotaCredito:
    folio: str
    uuid: str
    fecha: date
    folio_factura: str
    cliente: str
    subtotal: Decimal
    iva: Decimal
    total: Decimal
    libro_relevado: Decimal
    subtotal_libro: Decimal
    iva_libro: Decimal
    fuentes: list


@dataclass
class Excepcion:
    id: str
    categoria: str
    severidad: str                # alta | media | baja
    referencia: str               # folio / movement id
    que: str
    por_que: str
    monto_mxn: Decimal
    certeza: str                  # 'cierto' | 'confirmar con fiscal' | 'confirmar'
    accion: str
    responsable: str
    fecha_limite: date
    fuentes: list
    estado: str = "abierta"
    no_asiento: str = ""           # why no journal entry is proposed for this item


@dataclass
class LineaPoliza:
    cuenta: str
    nombre: str
    debe: Decimal
    haber: Decimal


@dataclass
class Poliza:
    id: str
    fecha: date
    tipo: str
    concepto: str
    lineas: list
    fuentes: list
    estado: str = "propuesta"      # propuesta | condicionada
    nota: str = ""

    @property
    def cuadra(self) -> bool:
        return sum(l.debe for l in self.lineas) == sum(l.haber for l in self.lineas)
