"""Tunable conventions. Nothing here depends on one dataset: change values, not code."""
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date
from typing import Optional


@dataclass
class Cuentas:
    banco_mxn: str = "1101"
    banco_usd: str = "1102"
    clientes: str = "1201"
    iva_acreditable: str = "1301"
    anticipos_no_identificado: str = "2101"
    iva_cobrado: str = "2201"
    iva_por_trasladar: str = "2202"
    ingresos: str = "4101"
    devoluciones: str = "4102"
    gastos_bancarios: str = "6101"
    utilidad_cambiaria: str = "7101"
    perdida_cambiaria: str = "7102"
    cambiaria_no_realizada: str = "7103"


@dataclass
class Config:
    cierre: Optional[date] = None             # period end; default = end of month of the latest bank movement
    plazo_default: int = 30                   # credit days when the CFDI notes carry no "Net N"
    tasa_general: Decimal = Decimal("0.16")
    tolerancia_cruzada: Decimal = Decimal("0.005")    # 0.5% for cross-currency matches (bank rate vs FIX)
    tolerancia_pago_corto: Decimal = Decimal("0.02")  # up to 2% below the balance is a short payment
    tolerancia_tc_cfdi: Decimal = Decimal("0.0005")   # CFDI rate vs FIX of the issue date
    max_facturas_combinadas: int = 4
    aplicar_correcciones: bool = True         # include conditional correction entries (TC fix, cancelled reversal)
    rfc_extranjero: str = "XEXX010101000"
    cuentas: Cuentas = field(default_factory=Cuentas)
    # whole words only (plural and abbreviated forms kept): "TELECOM" is not "COM", "INTELSAT" is not "SAT"
    patron_comision: str = r"\bCOMISION(?:ES)?\b|(?<![\w.])COM\.?\s|\bMANEJO DE CUENTA\b|\bMEMBRESIA\b"
    patron_iva_comision: str = r"\bIVA\s+COM(?:ISION(?:ES)?)?\b"
    patron_parcial: str = r"PARCIAL|ABONO|ANTICIPO|A CUENTA"
    patron_no_cxc: str = r"\b(?:NOMINAS?|PROVEEDOR(?:ES)?|IMSS|SAT|IMPUESTOS?|PRESTAMOS?|TRASPASOS?|INTERES(?:ES)?)\b"
    dia_limite_rep: int = 5                   # REP due the 5th calendar day of the next month (RMF 2026 rule 2.7.1.32)
