"""Matching bank receipts to open invoices.

Everything here is deterministic. An optional LLM step (asistente_llm) may only suggest
a customer for a description; it never produces or changes an amount.
"""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal
from itertools import combinations

from .dinero import q2, casi_igual
from .modelos import Partida, Movimiento, Propuesta

STOP = {"SA", "DE", "CV", "SAPI", "LLC", "USA", "DEL", "LA", "LOS", "LAS", "Y", "SC", "SRL", "RL", "SAS", "SAB",
        "AC", "EL", "EN", "INC", "LTD", "CORP"}
GENERICOS = {"SPEI", "RECIBIDO", "PAGO", "WIRE", "FACT", "FACTURA", "INV", "INVOICE", "CONV", "USD", "MXN",
             "ABONO", "PARCIAL", "TRANSFERENCIA", "DEPOSITO", "REF", "PAG", "TRANSFER", "RECIBIDA"}
ALIAS = {"GPO": "GRUPO", "CIA": "COMPANIA", "SERV": "SERVICIOS", "MANUF": "MANUFACTURAS", "DIST": "DISTRIBUIDORA"}


def _sin_acentos(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()


def tokens_nombre(nombre: str) -> list:
    return [t for t in re.findall(r"[A-Z0-9]+", _sin_acentos(nombre))
            if t not in STOP and len(t) >= 3 and not t.isdigit()]


def tokens_descripcion(texto: str) -> list:
    out = []
    for t in re.findall(r"[A-Z0-9]+", _sin_acentos(texto)):
        t = ALIAS.get(t, t)
        if t in GENERICOS or t in STOP or len(t) < 3 or t.isdigit():
            continue
        out.append(t)
    return out


def _tok_igual(desc_tok: str, nombre_tok: str) -> bool:
    if desc_tok == nombre_tok:
        return True
    corto, largo = sorted((desc_tok, nombre_tok), key=len)
    return len(corto) >= 4 and largo.startswith(corto)


def _distancia(a: str, b: str) -> int:
    """Edit distance (letters added, removed or changed; two swapped letters count as one, a common typo)."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _tok_parecido(desc_tok: str, nombre_tok: str) -> bool:
    """A misspelled name word: one letter off in words of 5-7 letters, two in longer ones. Short words never,
    because 'SOL' and 'SUR' are different names, not typos."""
    if _tok_igual(desc_tok, nombre_tok):
        return True
    if min(len(desc_tok), len(nombre_tok)) < 5:
        return False
    return _distancia(desc_tok, nombre_tok) <= (1 if max(len(desc_tok), len(nombre_tok)) <= 7 else 2)


class Contexto:
    """Per-run lookups: customers, folio patterns, FX."""

    def __init__(self, cfg, fx, facturas: dict, clientes: dict, reglas=None):
        self.cfg = cfg
        self.fx = fx
        self.reglas = reglas                # learned rules (aprendizaje.Reglas) or None
        self.facturas = facturas            # folio -> cfdi row dict (type I)
        self.clientes = clientes            # clave -> nombre
        self.tok_cliente = {k: tokens_nombre(v) for k, v in clientes.items()}
        # weight of a name token = 1 / number of customers sharing it
        self.peso = {}
        for k, toks in self.tok_cliente.items():
            for t in set(toks):
                comparte = sum(1 for k2, t2 in self.tok_cliente.items() if any(_tok_igual(t, x) for x in t2))
                self.peso[(k, t)] = Decimal(1) / Decimal(max(comparte, 1))
        self.patrones = {f: self._patron(f) for f in facturas}

    @staticmethod
    def _patron(folio: str):
        m = re.match(r"^([A-Za-z]*)[\s\-_]*(\d+)$", folio.strip())
        if not m:
            return re.compile(r"(?<![A-Z0-9])" + re.escape(folio.upper()) + r"(?![A-Z0-9])")
        letras, num = m.groups()
        return re.compile(r"(?<![A-Z0-9])" + re.escape(letras.upper()) + r"[\s\-_]*" + re.escape(num) + r"(?![0-9])")

    def folios_en(self, texto: str) -> list:
        t = _sin_acentos(texto)
        return [f for f, p in self.patrones.items() if p.search(t)]

    def puntuar_clientes(self, texto: str) -> dict:
        dt = tokens_descripcion(texto)
        out = {}
        for k, toks in self.tok_cliente.items():
            if not toks:
                continue
            total = sum(self.peso[(k, t)] for t in toks)
            hit = sum(self.peso[(k, t)] for t in toks if any(_tok_igual(x, t) for x in dt))
            out[k] = hit / total if total else Decimal(0)
        return out

    def parecido(self, texto: str):
        """A customer whose name the description spells almost right ("FARMACIAS DEL VALE" for "Farmacias del Valle").
        Returns (clave, [(written, name)], score) or (None, [], 0). Same score and margin as an exact name, so a typo can only
        point to one customer; it is a clue for a person (level 2), never applied on its own."""
        dt = tokens_descripcion(texto)
        puntos = {}
        for k, toks in self.tok_cliente.items():
            total = sum(self.peso[(k, t)] for t in toks)
            hit = sum(self.peso[(k, t)] for t in toks if any(_tok_parecido(x, t) for x in dt))
            if total:
                puntos[k] = hit / total
        p = sorted(puntos.items(), key=lambda kv: kv[1], reverse=True)
        if not p or p[0][1] < Decimal("0.5") or (len(p) > 1 and p[0][1] - p[1][1] < Decimal("0.15")):
            return None, [], Decimal(0)
        k = p[0][0]
        pares = [(x, t) for t in self.tok_cliente[k] for x in dt if _tok_parecido(x, t) and not _tok_igual(x, t)]
        return (k, pares, p[0][1]) if pares else (None, [], Decimal(0))

    def mejor_cliente(self, texto: str):
        """Return (clave, score, ambiguo). Needs score>=0.5 and a clear margin over the runner-up."""
        p = sorted(self.puntuar_clientes(texto).items(), key=lambda kv: kv[1], reverse=True)
        if not p or p[0][1] < Decimal("0.5"):
            return None, Decimal(0), False
        if len(p) > 1 and p[0][1] - p[1][1] < Decimal("0.15"):
            return None, p[0][1], True
        return p[0][0], p[0][1], False


def _equivalente(ctx, mov: Movimiento, p: Partida) -> Decimal:
    """Payment amount expressed in the invoice currency (FIX of the payment date if currencies differ)."""
    return mov.abono if mov.moneda == p.moneda else ctx.fx.convertir(mov.abono, mov.moneda, p.moneda, mov.fecha)


def _es_exacto(ctx, mov, p, eq) -> bool:
    if mov.moneda == p.moneda:
        return casi_igual(eq, p.saldo)
    return abs(eq - p.saldo) <= p.saldo * ctx.cfg.tolerancia_cruzada


def _alertas_fifo(elegidas, abiertas):
    out = []
    for p in elegidas:
        viejas = [o.folio for o in abiertas
                  if o.cliente_clave == p.cliente_clave and o.vencimiento < p.vencimiento
                  and o.folio not in {e.folio for e in elegidas}]
        if viejas:
            out.append(f"{p.cliente} has older open invoice(s) {', '.join(viejas)}; "
                       f"the payment was applied to {p.folio} because the amount matches it. Confirm with the customer.")
    return out


def _mas_antigua_primero(orden, exacta, A, cierre, cruzado) -> Propuesta:
    """Spread a receipt whose amount equals `exacta`'s balance over `orden` (oldest first), in invoice currency."""
    total_doc, resto_doc, resto_pago, items = exacta.saldo, exacta.saldo, A, []
    for x in orden:
        if resto_doc <= 0:
            break
        parte_doc = min(x.saldo, resto_doc)
        resto_doc -= parte_doc
        parte_pago = resto_pago if resto_doc <= 0 else q2(A * parte_doc / total_doc)
        resto_pago -= parte_pago
        items.append((x, parte_doc, parte_pago))
    detalle = ", ".join(f"{x.folio} {q2(d):,.2f} {x.moneda}" for x, d, _ in items)
    aviso = (f"No invoice in the description: applied oldest first ({detalle}), as the SAT payments complement guide 2.0 "
             f"(numeral 4) requires when the payer does not indicate the CFDI. The amount matches {exacta.folio} exactly: "
             f"ask the customer which invoice it paid.")
    tipo = "cruzado" if cruzado else ("multiple" if len(items) > 1 else ("exacto" if items[0][1] == items[0][0].saldo else "parcial"))
    razon = (f"Customer named, no invoice: oldest open invoice first. The amount equals {exacta.folio}, "
             f"which may be what the customer meant.")
    prop = cierre(items, tipo, "media", razon, orden_antiguo=exacta.folio)
    prop.alertas.append(aviso)
    return prop


def proponer(ctx: Contexto, mov: Movimiento, abiertas: list, forzar_cliente=None) -> Propuesta:
    cfg = ctx.cfg
    A = mov.abono
    refs = ctx.folios_en(mov.descripcion)
    refs_abiertos = [p for p in abiertas if p.folio in refs]
    alertas = []
    for f in refs:
        if f not in {p.folio for p in abiertas}:
            alertas.append(f"The description mentions {f}, which has no open balance at the payment date.")
    clave, score, ambiguo = ctx.mejor_cliente(mov.descripcion)
    # a payer can type the wrong invoice number: if the number belongs to another customer than the name, say so
    conflicto = bool(refs_abiertos and clave and not ambiguo and all(p.cliente_clave != clave for p in refs_abiertos))
    if conflicto:
        alertas.append(f"Reference/customer conflict: the description mentions {', '.join(p.folio for p in refs_abiertos)} "
                       f"({refs_abiertos[0].cliente}) but names {ctx.clientes.get(clave, clave)}. Confirm which invoice was paid.")
    regla = regla_pista = None
    # a generic word can match one customer exactly while the misspelled distinctive word points to another
    # ("TECNOLOGIAS FODVER": exact 'TECNOLOGIAS' of Pavevas, but 'FODVER' is Fodaver): then nobody is assumed
    if clave and not refs_abiertos and not forzar_cliente:
        otro, pares_o, sc_o = ctx.parecido(mov.descripcion)
        if otro and otro != clave and sc_o > score:
            alertas.append(f"The description matches {ctx.clientes[clave]} on a common word but is closer to "
                           f"{ctx.clientes[otro]} with a misspelling; the customer was not assumed.")
            clave = None
    if not refs_abiertos and not clave and not forzar_cliente and ctx.reglas is not None:
        regla, choque = ctx.reglas.buscar(mov.descripcion)
        if choque:
            alertas.append("Learned rules for different customers fit this description; none was used.")
        nuevas = ctx.reglas.palabras_nuevas(mov.descripcion, regla) if regla else []
        if regla and regla["cliente_clave"] in ctx.clientes and nuevas:
            # the description says more than the rule ever saw ("ZETA GAS SUR" vs "ZETA"): it may be another payer
            alertas.append(f"Learned rule {regla['id']} ('{' '.join(regla['tokens'])}' -> {regla['cliente']}) fits, but "
                           f"the description also has {', '.join(nuevas)}, which the rule never saw: the payer was not assumed.")
            regla_pista, regla = regla, None
        elif regla and regla["cliente_clave"] in ctx.clientes:
            clave, ambiguo = regla["cliente_clave"], False
            alertas.append(f"Customer identified by learned rule {regla['id']} ('{' '.join(regla['tokens'])}' -> "
                           f"{regla['cliente']}), taught by {regla['usuario'] or 'a reviewer'} on {regla['movimiento']}. "
                           f"If it is wrong: python -m cierre_cxc rules --quitar {regla['id']}")
        else:
            regla = None
    if not refs_abiertos and not clave and not ambiguo and not forzar_cliente and regla is None:
        similar, pares, _ = ctx.parecido(mov.descripcion)
        if similar:
            escrito = ", ".join(f"'{x}' looks like '{t}'" for x, t in pares)
            alertas.append(f"The description spells a customer name almost right ({escrito}): "
                           f"{ctx.clientes[similar]}? A person confirms; nothing is applied on a similar name.")
            return Propuesta(tipo="no_identificado", confianza="baja", modo="parecido", alertas=alertas, remanente=A,
                             razon=f"The name in the description is similar to {ctx.clientes[similar]} ({escrito}).",
                             pista={"cliente_clave": similar, "cliente": ctx.clientes[similar], "pares": pares})
    if forzar_cliente:
        clave, ambiguo = forzar_cliente, False
    if refs_abiertos and not clave:
        clave = refs_abiertos[0].cliente_clave
    if refs_abiertos:
        cand, modo = refs_abiertos, "ref"
    elif clave:
        cand, modo = [p for p in abiertas if p.cliente_clave == clave], ("aprendido" if regla else "cliente")
    else:
        cand, modo = list(abiertas), "monto"
    if ambiguo:
        alertas.append("The description fits more than one customer name; the customer was not assumed.")
    cand = sorted(cand, key=lambda p: (p.vencimiento, p.folio))
    parcial_kw = bool(re.search(cfg.patron_parcial, _sin_acentos(mov.descripcion)))

    def cierre(items, tipo, conf, razon, remanente=Decimal(0), alt=None, orden_antiguo=""):
        # A receipt that names its invoice was indicated by the payer (SAT payments guide 2.0, numeral 3):
        # no older-invoice alert. Without a folio, the alert asks AR to confirm.
        al = list(alertas) + ([] if orden_antiguo or modo == "ref" else _alertas_fifo([i[0] for i in items], abiertas))
        if conflicto:
            conf = "baja"
        return Propuesta(items=items, remanente=remanente, tipo=tipo, confianza=conf, razon=razon,
                         alertas=al, alternativas=alt or [], modo=modo, orden_antiguo=orden_antiguo)

    def por_confirmar(razon):
        # the name says who paid, not which invoice: with several open invoices a person decides (level 2)
        nombre = ctx.clientes.get(clave, clave)
        return Propuesta(tipo="no_identificado", confianza="baja", modo="parcial_cliente", alertas=alertas,
                         remanente=A, razon=f"{nombre} {razon}",
                         pista={"cliente_clave": clave, "cliente": nombre,
                                "evidencia": f"{nombre} is named in the description"})

    def clave_orden(p):
        return (0 if p.folio in refs else 1, 0 if p.moneda == mov.moneda else 1, p.vencimiento, p.folio)

    exactos = sorted([p for p in cand if _es_exacto(ctx, mov, p, _equivalente(ctx, mov, p))], key=clave_orden)
    if exactos:
        if modo == "monto" and len(exactos) > 1:
            return Propuesta(tipo="no_identificado", confianza="baja", modo=modo,
                             razon="No reference or customer in the description and several open invoices have this exact amount.",
                             alertas=alertas, alternativas=[p.folio for p in exactos])
        if modo == "cliente" and len(exactos) > 1:
            return por_confirmar(f"has {len(exactos)} open invoices with this exact amount "
                                 f"({', '.join(x.folio for x in exactos)}): possible duplicate billing.")
        p = exactos[0]
        cruzado = mov.moneda != p.moneda
        tipo = "cruzado" if cruzado else "exacto"
        conf = "alta" if modo in ("ref", "cliente") and len(exactos) == 1 else "media"
        razon = {"ref": "Invoice number in the description and exact amount.",
                 "cliente": "Customer name in the description and exact amount.",
                 "aprendido": "Customer identified by a learned rule and exact amount.",
                 "monto": "Only the amount matches (no reference or customer in the description)."}[modo]
        if cruzado:
            razon += (f" Paid in {mov.moneda} against a {p.moneda} invoice: equivalent at the FIX of "
                      f"{mov.fecha} is {q2(_equivalente(ctx, mov, p))} {p.moneda}.")
        alt = [x.folio for x in exactos[1:]]
        if alt:
            alertas.append(f"{len(exactos)} invoices fit equally ({p.folio} chosen as the oldest; others: {', '.join(alt)}). Possible duplicate billing.")
            conf = "baja"
        # no invoice in the description: the payer did not indicate one, so the payment goes to the oldest pending
        # CFDI first (SAT payments complement guide 2.0, numeral 4), even when the amount matches a newer one.
        # Only invoices in the matched invoice's currency take part (assumption: the guide does not mix currencies).
        if modo in ("cliente", "aprendido") and not alt:
            viejas = sorted([x for x in cand if x is not p and x.moneda == p.moneda and (x.fecha, x.folio) < (p.fecha, p.folio)],
                            key=lambda x: (x.fecha, x.folio))
            if viejas:
                return _mas_antigua_primero(viejas + [p], p, A, cierre, cruzado)
        return cierre([(p, p.saldo, A)], tipo, conf, razon, alt=alt)

    # a learned rule only identifies the payer: it places money on its own only with an exact amount (above);
    # anything else (several invoices, short, partial, overpayment) goes to a person as a proposal
    if modo in ("ref", "cliente"):
        # several invoices paid together
        mismos = [p for p in cand if p.moneda == cand[0].moneda] if cand else []
        for n in range(2, min(cfg.max_facturas_combinadas, len(mismos)) + 1):
            for combo in combinations(mismos, n):
                tot = sum(_equivalente(ctx, mov, p) if mov.moneda != p.moneda else p.saldo for p in combo)
                eq = A if mov.moneda == combo[0].moneda else ctx.fx.convertir(A, mov.moneda, combo[0].moneda, mov.fecha)
                ok = casi_igual(sum(p.saldo for p in combo), eq) if mov.moneda == combo[0].moneda \
                    else abs(sum(p.saldo for p in combo) - eq) <= sum(p.saldo for p in combo) * cfg.tolerancia_cruzada
                if ok:
                    tot_saldo = sum(p.saldo for p in combo)
                    items, resto = [], A
                    for i, p in enumerate(combo):
                        parte = resto if i == len(combo) - 1 else q2(A * p.saldo / tot_saldo)
                        resto -= parte
                        items.append((p, p.saldo, parte))
                    return cierre(items, "multiple", "media",
                                  f"One payment covers {n} invoices: {', '.join(p.folio for p in combo)}.")

        # short payment: just below the balance, not flagged as a partial payment
        if not parcial_kw:
            cortos = sorted([p for p in cand if p.saldo * (1 - cfg.tolerancia_pago_corto) <= _equivalente(ctx, mov, p) < p.saldo],
                            key=clave_orden)
            if cortos:
                p = cortos[0]
                eq = _equivalente(ctx, mov, p)
                alt = [x.folio for x in cortos[1:]]
                if alt:
                    alertas.append(f"{len(cortos)} invoices fit equally ({p.folio} chosen as the oldest; others: {', '.join(alt)}). Possible duplicate billing.")
                return cierre([(p, eq, A)], "corto", "baja" if alt else "media",
                              f"Payment is {q2(p.saldo - eq)} {p.moneda} below the balance of {p.folio}; the difference stays open until the customer explains it.",
                              alt=alt)

        # partial payment
        pendientes = [p for p in cand if _equivalente(ctx, mov, p) < p.saldo]
        if modo == "cliente" and len(cand) > 1:
            # partial or overpayment with several open invoices: which ones it pays is a guess
            return por_confirmar(f"has {len(cand)} open invoices and the amount matches none of them, nor a combination; "
                                 "the description does not say which one(s) it pays.")
        if pendientes:
            p = sorted(pendientes, key=clave_orden)[0]
            eq = _equivalente(ctx, mov, p)
            return cierre([(p, eq, A)], "parcial", "media" if (parcial_kw or modo == "ref") else "baja",
                          f"Payment is lower than the balance of {p.folio}; applied as a partial payment"
                          + (" (the description says so)." if parcial_kw else ". Confirm with the customer."))

        # overpayment: fill the oldest invoices, the rest is unapplied cash
        if cand:
            items, resto = [], A
            for p in cand:
                if resto <= 0:
                    break
                eq_resto = resto if mov.moneda == p.moneda else ctx.fx.convertir(resto, mov.moneda, p.moneda, mov.fecha)
                aplica = min(p.saldo, eq_resto)
                pago = resto if aplica == eq_resto else (aplica if mov.moneda == p.moneda else ctx.fx.convertir(aplica, p.moneda, mov.moneda, mov.fecha))
                items.append((p, aplica, q2(pago)))
                resto -= q2(pago)
            return cierre(items, "sobrepago", "baja",
                          "Payment is larger than the open balance of the customer; the excess is kept as unapplied cash.",
                          remanente=resto)

    return Propuesta(tipo="no_identificado", confianza="baja", modo=modo, alertas=alertas,
                     razon="No invoice reference, no recognizable customer and no open invoice with this amount."
                     if modo == "monto" else
                     "A learned rule names the customer, but no open invoice of that customer matches the amount exactly."
                     if modo == "aprendido" else "The customer has no open invoice that fits this payment.",
                     remanente=A, regla=regla or regla_pista or {})


def candidatos(ctx: Contexto, mov: Movimiento, abiertas: list, limite: int = 3) -> list:
    """Invoices a receipt could pay when only the amount is known, best first, each with its evidence.
    It proposes; it never applies. Exact amounts come first, then a payment slightly below the balance
    (bank fees or a withholding), then the same currency, then the due date closest to the payment."""
    filas = []
    for p in abiertas:
        eq = _equivalente(ctx, mov, p)
        dif = p.saldo - eq
        if _es_exacto(ctx, mov, p, eq):
            rango, ev = 0, "exact amount"
        elif Decimal(0) < dif <= p.saldo * ctx.cfg.tolerancia_pago_corto:
            rango, ev = 1, f"{q2(dif):,.2f} {p.moneda} below the balance (bank fees or a withholding?)"
        else:
            continue
        dias = (mov.fecha - p.vencimiento).days
        ev += f"; due {p.vencimiento} (" + (f"{dias} days overdue" if dias > 0 else
                                            "due that day" if dias == 0 else f"{-dias} days before due") + ")"
        if mov.moneda != p.moneda:
            ev += f"; invoice in {p.moneda}, converted at the FIX of {mov.fecha}"
        filas.append(((rango, mov.moneda != p.moneda, abs(dias), p.folio),
                      {"folio": p.folio, "cliente": p.cliente, "saldo": p.saldo, "moneda": p.moneda, "evidencia": ev}))
    return [d for _, d in sorted(filas, key=lambda x: x[0])][:limite]
