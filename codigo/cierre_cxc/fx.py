"""Exchange-rate lookup and conversion (MXN <-> USD) using the FIX table of the dataset."""
from datetime import date, timedelta
from decimal import Decimal
from .dinero import q2


class TipoCambioFaltante(KeyError):
    """The FIX table does not cover a date the close needs."""


class TipoCambio:
    def __init__(self, tabla: dict, avisos: list):
        self.tabla = tabla
        self.avisos = avisos

    def en(self, fecha: date) -> Decimal:
        """Rate applicable on `fecha`; falls back to the latest earlier date (and says so)."""
        if fecha in self.tabla:
            return self.tabla[fecha]
        for k in range(1, 8):
            previa = fecha - timedelta(days=k)
            if previa in self.tabla:
                self.avisos.append(f"No FIX rate for {fecha}: used {previa}.")
                return self.tabla[previa]
        raise TipoCambioFaltante(f"No exchange rate available near {fecha}")

    def intentar(self, fecha: date):
        """Like `en`, but returns None (and notes it) when the table does not cover the date at all."""
        try:
            return self.en(fecha)
        except KeyError:
            self.avisos.append(f"The exchange-rate table does not cover {fecha}: the rate check for that date was skipped.")
            return None

    def a_mxn(self, monto: Decimal, moneda: str, fecha: date) -> Decimal:
        return q2(monto) if moneda == "MXN" else q2(monto * self.en(fecha))

    def convertir(self, monto: Decimal, de: str, a: str, fecha: date) -> Decimal:
        if de == a:
            return monto
        if de == "USD" and a == "MXN":
            return monto * self.en(fecha)
        if de == "MXN" and a == "USD":
            return monto / self.en(fecha)
        raise ValueError(f"Unsupported currency pair {de}->{a}")
