"""Plain-Spanish wording for exceptions: the four families, who resolves each, one sentence per category and why
it matters. Shared by the demo script and the AI draft (as its deterministic fallback). Text only: no amounts
are computed here."""

FAMILIAS = {
    "Dinero sin dueño": ["Unidentified cash", "Match to confirm", "Reference vs customer",
                         "Receipt classified as not AR", "Deposit typed as a withdrawal?"],
    "Papel del SAT": ["Missing REP", "REP amount", "REP without bank", "REP balance", "REP rate", "PUE paid late",
                      "PUE not collected", "Payment method", "IVA rate", "Cancelled CFDI", "CFDI arithmetic",
                      "FX rate on CFDI"],
    "Factura rara": ["Possible duplicate", "Short payment", "Application order"],
    "Dato mal capturado": ["Possible duplicate bank row", "Bank row out of period", "Opening FX"],
}
FAMILIA = {c: f for f, cats in FAMILIAS.items() for c in cats}

RESPONSABLE = {"AR analyst": "CxC", "Tax team": "Fiscal", "Treasury": "Tesorería", "Controller": "el Controller",
               "Account manager": "el ejecutivo de cuenta", "Billing": "Facturación"}

# one plain sentence per category, said out loud; {refs} = references, {monto} = amount in MXN
FRASE = {
    "Unidentified cash": "Depósito sin identificar, {refs}: se queda en la 2101 sin IVA hasta saber de quién es.",
    "Match to confirm": "Cobro sin cliente ni folio, {refs}: me propone candidatas y no aplica nada hasta que yo elija.",
    "Reference vs customer": "La descripción cita la factura de otro cliente, {refs}: se confirma cuál se pagó.",
    "Receipt classified as not AR": "Un abono que parecía no ser de clientes pero menciona una factura, {refs}: se confirma.",
    "Deposit typed as a withdrawal?": "Un cargo que parece un depósito capturado al revés, {refs}: se revisa la captura.",
    "Missing REP": "Cobros sin complemento de pago, {refs}: Fiscal los emite.",
    "REP amount": "El REP no coincide con el banco, {refs}: se cancela y se emite de nuevo con el monto del banco.",
    "REP without bank": "Hay REP pero el banco no muestra el cobro, {refs}: se revisa y se cancela si no hubo pago.",
    "REP balance": "Los saldos del REP no cuadran, {refs}: se emite de nuevo.",
    "REP rate": "El REP usa un tipo distinto al FIX del día del pago, {refs}: Fiscal lo revisa.",
    "PUE paid late": "Factura PUE cobrada después de su mes, {refs}: debió ser PPD y Fiscal la sustituye.",
    "PUE not collected": "Factura PUE sin cobrar, {refs}: sigue en cuentas por cobrar con su IVA por trasladar.",
    "Payment method": "Método o forma de pago inconsistente, {refs}: Facturación corrige el CFDI.",
    "IVA rate": "Tasa de IVA que no corresponde, {refs}: la exposición es de hasta {monto}.",
    "Cancelled CFDI": "Factura cancelada en el SAT que seguía registrada, {refs}: se reversa su póliza.",
    "CFDI arithmetic": "Subtotal más IVA no da el total, {refs}: Facturación revisa el CFDI.",
    "FX rate on CFDI": "Tipo de cambio distinto al FIX, {refs}: el CFDI es válido; la diferencia de {monto} va en una póliza condicionada que aprueba el Controller.",
    "Possible duplicate": "Misma fecha, monto y cliente, {refs}: se confirma antes de cancelar la que no tiene cobro.",
    "Short payment": "Pagaron de menos, {refs}: la diferencia queda abierta y el REP lleva ese saldo.",
    "Application order": "Cobro sin folio aplicado a la factura más antigua, {refs}: se confirma con el cliente y se reasigna si indica otra.",
    "Possible duplicate bank row": "Una fila del banco parece repetida, {refs}: se revisa el estado de cuenta.",
    "Bank row out of period": "Una fila del banco es de otro mes, {refs}: no se procesó.",
    "Opening FX": "La cartera inicial en dólares no se revaluó al cierre anterior, {refs}: el Controller revisa.",
}

# why it matters, said right after the sentence (only norms already checked in the case)
PORQUE = {
    "Unidentified cash": "sin saber de quién es, no puedo bajar la cuenta de ningún cliente ni emitir un REP. Si resulta ser pago de un servicio, su IVA se causa al cobrarse y entra en la declaración del mes, que vence el día 17 (LIVA 1o.-B y 5o.-D); si fue un error, se devuelve sin IVA.",
    "Match to confirm": "un monto solo no es evidencia: si lo aplicara por monto, podría bajar la factura de otro cliente y el error se escondería en un cierre que cuadra.",
    "Reference vs customer": "si lo aplico a la factura equivocada, quedan mal los saldos de los dos clientes y el REP saldría al cliente equivocado.",
    "Receipt classified as not AR": "si es un cobro y lo dejo fuera, la cartera queda inflada y falta su REP.",
    "Deposit typed as a withdrawal?": "si es un depósito capturado al revés, el banco y la cartera no cuadran.",
    "Missing REP": "en facturas PPD el REP es la prueba del pago: sin él, el cliente no puede acreditar el IVA. Vence el día 5 del mes siguiente al pago (RMF 2026, regla 2.7.1.32).",
    "REP amount": "el REP y el banco tienen que decir lo mismo; si no, el cliente acredita un IVA distinto al que Clara declara como cobrado.",
    "REP without bank": "un REP certifica un pago que el banco no muestra: se estaría declarando IVA cobrado sin que haya llegado el dinero.",
    "REP balance": "la cadena de parcialidades tiene que cuadrar: saldo anterior menos lo pagado debe dar el saldo insoluto.",
    "REP rate": "el tipo de cambio del REP fija el IVA cobrado en pesos; tiene que ser el FIX del día del pago.",
    "PUE paid late": "PUE solo se usa si el pago completo llega a más tardar el último día del mes en que se emitió (RMF 2026, regla 2.7.1.39), y aquí llegó después.",
    "PUE not collected": "PUE dice que ya se pagó, pero no hay dinero: el cliente acreditó un IVA que todavía no paga. Se sustituye por PPD con forma 99 y relación 04 (RMF 2026, regla 2.7.1.39).",
    "Payment method": "PPD lleva forma de pago 99, por definir, y PUE lleva la forma real; si están cruzadas, el CFDI está mal.",
    "IVA rate": "los servicios a un cliente en México llevan la tasa general del 16 % (LIVA art. 1o); el 0 % aplica solo en casos como la exportación. La herramienta no lo decide: lo confirma Fiscal y, si no aplica, se emite de nuevo al 16 %.",
    "Cancelled CFDI": "una factura cancelada no tiene valor fiscal ni se puede cobrar; si sigue en libros, infla la cartera, los ingresos y el IVA.",
    "CFDI arithmetic": "un CFDI timbrado con cifras que no suman es un error fiscal y distorsiona la cartera y el IVA.",
    "FX rate on CFDI": "usar el FIX en el CFDI es opcional (Anexo 20, pág. 9), así que el CFDI vale; la diferencia es contable, porque el ingreso se registró a otro tipo.",
    "Possible duplicate": "facturar dos veces infla ingresos, cartera e IVA; no se cancela sin confirmar porque pudo ser un servicio real prestado dos veces.",
    "Short payment": "la diferencia suele ser una comisión o una retención del cliente; sin documentarla no se castiga. Si Clara la perdona, se emite un CFDI de egreso con relación 01 y forma 15, condonación.",
    "Application order": "sin indicación del cliente, la guía del complemento de pagos dice aplicar a la factura más antigua (numerales 3 y 4); un monto que coincide no es una indicación. Si el cliente dice otra, se reasigna antes del REP.",
    "Possible duplicate bank row": "una fila pegada dos veces duplica el dinero.",
    "Bank row out of period": "un movimiento de otro mes movería el cierre equivocado.",
    "Opening FX": "IAS 21 pide valuar los saldos en dólares al tipo de cierre cada mes; si no, el resultado del mes anterior se mezcla con el de este.",
}

# what each kind of non-exact application means, said out loud
TIPO_DICE = {
    "corto": "{mov} pagó un poco menos de lo que debía {folios}: lo aplica y deja la diferencia abierta, porque no se castiga sin autorización.",
    "parcial": "{mov} es un pago parcial de {folios}: queda saldo abierto y el REP lleva ese saldo insoluto.",
    "cruzado": "{mov} pagó {folios} en otra moneda: se convierte al FIX del día del pago, y el IVA cobrado también va a ese FIX.",
    "multiple": "{mov} es un solo pago que cubre varias facturas, {folios}: el monto cuadra con la suma.",
    "sobrepago": "{mov} pagó de más a {folios}: el excedente se queda en la 2101 hasta saber si se devuelve o se aplica.",
}
