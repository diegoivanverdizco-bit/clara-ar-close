"""Exact money arithmetic. All amounts are Decimal; rounding happens only at the cent."""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
CERO = Decimal("0")


def d(valor) -> Decimal:
    """Convert any numeric-looking value to Decimal without binary float noise."""
    if valor is None or valor == "":
        return CERO
    if isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor))


def q2(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_HALF_UP)


def casi_igual(a: Decimal, b: Decimal, tol: Decimal = Decimal("0.01")) -> bool:
    return abs(a - b) <= tol
