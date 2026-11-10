"""Exception detectors for the CFDI, REP and bank data.

Each rule states what is wrong, why it matters, how much is at stake, how sure we are,
and who should do what by when. Rules read the engine result; they never change it.
"""
from __future__ import annotations
import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .dinero import q2, CERO
from .modelos import Excepcion, clave_cliente

RESP_AR = "AR analyst"
RESP_FISCAL = "Tax team"
RESP_TESO = "Treasury"
RESP_CTRL = "Controller"
RESP_VTAS = "Account manager"
RESP_FACT = "Billing"


def _m(x: Decimal) -> str:
    return f"{x:,.2f}"


def limite_rep(fecha_pago: date, dia: int = 10) -> date:
    y, m = (fecha_pago.year, fecha_pago.month + 1) if fecha_pago.month < 12 else (fecha_pago.year + 1, 1)
    return date(y, m, dia)


def detectar(res) -> list:
    cfg, fx = res.cfg, res.fx
    out, n = [], 0

    def agregar(cat, sev, ref, que, por_que, monto, certeza, accion, resp, limite, fuentes, no_asiento=""):
        nonlocal n
        n += 1
        out.append(Excepcion(id=f"E{n:02d}", categoria=cat, severidad=sev, referencia=ref, que=que, por_que=por_que,
                             monto_mxn=q2(monto), certeza=certeza, accion=accion, responsable=resp,
                             fecha_limite=limite, fuentes=fuentes, no_asiento=no_asiento))

    d3, d5, d10 = (res.cierre + timedelta(days=k) for k in (3, 5, 10))
    sig = res.cierre.replace(day=1) + timedelta(days=32)
    decl_iva = sig.replace(day=17)          # monthly IVA return: day 17 of the following month (LIVA art. 5o.-D)
    cfdi = {f.datos["folio"]: f for f in res.ds.cfdi}

    # ---- CFDI data -------------------------------------------------------------------------
    for f in res.ds.cfdi:
        r = f.datos
        if r["estatus"] == "Cancelado":
            agregar("Cancelled CFDI", "media", r["folio"],
                    f"{r['folio']} ({r['cliente']}) is cancelled at SAT but the "
                    + ("credit-note" if r["tipo"] == "E" else "invoicing") + " entry was booked.",
                    "A cancelled CFDI has no tax or collection value. If it stays in the ledger, AR, revenue and IVA por trasladar are overstated.",
                    r["total"] * (r["tc"] or Decimal(1)), "cierto",
                    "Reverse the invoicing entry if still booked; confirm the cancellation reason and whether a replacement CFDI exists; confirm the customer did not pay it.",
                    RESP_FACT, d3, [f.fuente],
                    no_asiento="Reversal is proposed as a conditional entry; excluded from aging and IVA.")
        if abs(r["subtotal"] * r["tasa"] - r["iva"]) > Decimal("0.02") or abs(r["subtotal"] + r["iva"] - r["total"]) > Decimal("0.02"):
            agregar("CFDI arithmetic", "alta", r["folio"], f"{r['folio']}: subtotal, IVA and total do not add up.",
                    "A stamped CFDI with inconsistent figures is a compliance defect and distorts AR and IVA.",
                    abs(r["subtotal"] + r["iva"] - r["total"]) * (r["tc"] or Decimal(1)), "cierto",
                    "Review with billing; cancel and reissue if the figures are wrong.", RESP_FACT, d3, [f.fuente])
        if r["tipo"] == "I" and r["tasa"] == 0 and r["rfc"] != cfg.rfc_extranjero:
            potencial = r["subtotal"] * cfg.tasa_general * (r["tc"] or Decimal(1))
            agregar("IVA rate", "alta", r["folio"],
                    f"{r['folio']} to {r['cliente']} (Mexican RFC {r['rfc']}) is billed at 0% IVA.",
                    "Services to a domestic customer carry the 16% general rate (LIVA art. 1o); 0% applies only to the acts listed in arts. 2o.-A and 29. If 0% is wrong, IVA is under-billed and Clara owes it from its own pocket.",
                    potencial, "confirmar con fiscal",
                    "Tax team to confirm whether any exemption or 0% treatment applies; if not, cancel and reissue with 16% and the customer's acceptance.",
                    RESP_FISCAL, d5, [f.fuente],
                    no_asiento="No extra IVA is accrued until the tax team confirms; the amount shown is the potential exposure.")
        if r["tipo"] == "I" and r["tasa"] == 0 and r["rfc"] == cfg.rfc_extranjero:
            agregar("IVA rate", "baja", r["folio"],
                    f"{r['folio']} to a foreign customer is billed at 0% IVA (export of services).",
                    "0% applies only to services used abroad that fall in the categories listed in LIVA art. 29 fr. IV, with evidence kept. Otherwise 16% IVA is due.",
                    r["subtotal"] * cfg.tasa_general * (r["tc"] or Decimal(1)), "confirmar con fiscal",
                    "Confirm the export-of-services evidence is on file (contract, proof the service is used abroad).",
                    RESP_FISCAL, d10, [f.fuente], no_asiento="No entry: 0% is accepted as billed pending the evidence check.")
        ok = fx.intentar(r["fecha"]) if (r["tipo"] == "I" and r["moneda"] != "MXN" and r["tc"]) else None
        if ok is not None:
            if abs(r["tc"] - ok) > cfg.tolerancia_tc_cfdi:
                k = next((c for c in res.correcciones_tc if c["folio"] == r["folio"]), None)
                efecto = abs(k["delta_total"]) if k else abs((ok - r["tc"]) * r["total"])
                # Anexo 20 (p. 9): the FIX is optional in TipoCambio; outside +/-35% of the FIX (c_Moneda, USD) the CFDI
                # needs a confirmation key from the PAC. Inside that range the CFDI is valid as stamped.
                fuera = abs(r["tc"] - ok) > ok * Decimal("0.35")
                pagos = sorted({a.mov_id for a in res.aplicaciones if a.folio == r["folio"]})
                rep = (f" If it is reissued, do it before the REP of {', '.join(pagos)}, so the REP points to the new UUID."
                       if pagos else "")
                agregar("FX rate on CFDI", "alta" if fuera else "media", r["folio"],
                        f"{r['folio']} was stamped at {r['tc']} but the FIX of {r['fecha']} is {ok:.4f} (difference {ok - r['tc']:+.4f}).",
                        ("The rate is more than 35% away from the FIX: Anexo 20 requires a confirmation key for it; check how it was stamped. "
                         if fuera else
                         "The CFDI is valid as stamped: Anexo 20 makes the FIX optional in TipoCambio and this rate is within its range. ")
                        + "IVA on the return is not affected, because it is measured at the FIX of the collection date. The difference "
                        "is in the ledger: revenue and AR were booked at a rate other than the FIX of the invoice date.",
                        efecto, "confirmar con fiscal",
                        "Controller to approve or reject the conditional rate-correction entry (reject-correction). Reissuing the CFDI "
                        "at the FIX is optional; tax team to decide it and confirm the ISR effect." + rep,
                        RESP_FISCAL, d5, [f.fuente],
                        no_asiento="Booked as a conditional correction (see journal entries).")
        if r["tipo"] == "I" and r["metodo"] == "PUE" and r["forma"] == "99":
            agregar("Payment method", "media", r["folio"], f"{r['folio']} is PUE with payment form 99 (por definir).",
                    "PUE requires a real payment form; 99 is only valid with PPD.", r["total"], "cierto",
                    "Correct the CFDI.", RESP_FACT, d5, [f.fuente])
        if r["tipo"] == "I" and r["metodo"] == "PPD" and r["forma"] not in ("99", ""):
            agregar("Payment method", "media", r["folio"], f"{r['folio']} is PPD with payment form {r['forma']}.",
                    "PPD requires payment form 99 (por definir); the real form goes in the REP.", r["total"], "cierto",
                    "Correct the CFDI.", RESP_FACT, d5, [f.fuente])

    for p in res.partidas.values():
        if p.metodo == "PUE" and p.saldo > Decimal("0.005"):
            f = cfdi.get(p.folio)
            agregar("PUE not collected", "alta", p.folio,
                    f"{p.folio} ({p.cliente}) is PUE (paid at issuance) but still has {_m(p.saldo)} {p.moneda} open with no bank receipt.",
                    "The CFDI tells SAT the invoice was paid in a single payment, while the books show it unpaid. The customer may credit "
                    "the IVA only once it actually pays (LIVA art. 5o fr. III), but a PUE CFDI makes an early credit look valid. "
                    "PUE without cash at issuance is allowed only if the full payment arrives by the last day of the month of issuance (RMF 2026 rule 2.7.1.39); "
                    "otherwise the CFDI must be cancelled and replaced." + (" It is also past due in the aging." if p.vencimiento < res.cierre else ""),
                    p.libro_mxn, "cierto",
                    "Confirm with the customer that it is unpaid; then cancel it and issue a new CFDI with method PPD and payment form 99, related as 'Sustitución de los CFDI previos' "
                    "(c_TipoRelacion 04), and issue the REP when the cash arrives (RMF 2026 rule 2.7.1.39). If it was paid, locate the cash. "
                    "The date is an internal deadline: the rule sets none.",
                    RESP_FISCAL, d3, p.fuentes,
                    no_asiento="IVA is NOT reclassified to cobrado: there is no cash. The balance stays in AR and IVA por trasladar.")

    # possible duplicates
    grupos = defaultdict(list)
    for f in res.ds.cfdi:
        r = f.datos
        if r["tipo"] == "I" and not r["estatus"] == "Cancelado":
            # a generic RFC is many customers: two of them billed the same amount the same day are not a duplicate
            cliente = clave_cliente(r["rfc"], r["cliente"]) if r["rfc"] else ""
            grupos[(cliente, r["fecha"], r["subtotal"], r["total"], r["moneda"])].append(f)
    for fs in grupos.values():
        if len(fs) > 1:
            folios = ", ".join(x.datos["folio"] for x in fs)
            r0 = fs[0].datos
            todos = [x.datos["folio"] for x in fs]
            pagadas = sorted({a.folio for a in res.aplicaciones if a.folio in todos})
            sin_pago = [f for f in todos if f not in pagadas]
            if pagadas and sin_pago:            # cancel the ones with no cash; keep the one the customer paid
                cancelar = f"cancel {', '.join(sin_pago)} (no cash received)"
            elif not pagadas:                   # nothing paid yet: keep one
                cancelar = f"cancel {', '.join(todos[1:])} and keep {todos[0]}"
            else:                               # all paid: the customer paid twice
                cancelar = "credit or refund the customer for the second payment, then cancel that invoice"
            agregar("Possible duplicate", "alta", folios,
                    f"{folios} ({r0['cliente']}) have the same date, amount and customer.",
                    "Billing twice for one service overstates revenue, AR and IVA. Invoices with cash received (paid: " +
                    (", ".join(pagadas) or "none") + ").",
                    r0["total"] * (r0["tc"] or Decimal(1)), "confirmar",
                    f"Account manager to confirm whether the service was delivered twice; if not, {cancelar} "
                    "and reverse its entry.",
                    RESP_VTAS, d3, [x.fuente for x in fs],
                    no_asiento="No reversal until confirmed; the unpaid invoice stays in AR and aging.")

    # opening balances not revalued at the prior month end
    fin_ant = res.inicio - timedelta(days=1)
    tot_dif, partes = CERO, []
    for fa in res.ds.apertura:
        a = fa.datos
        if a["moneda"] != "MXN" and a["tc"] is not None:
            ok = fx.en(fin_ant)
            if abs(a["tc"] - ok) > cfg.tolerancia_tc_cfdi:
                dif = q2(a["saldo"] * (ok - a["tc"]))
                tot_dif += dif
                partes.append((fa.fuente, a["folio"], a["tc"], ok, dif))
    if partes:
        detalle = "; ".join(f"{fo}: booked at {tc}, FIX {ok:.4f} -> {dif:+,.2f}" for _, fo, tc, ok, dif in partes)
        agregar("Opening FX", "baja", ", ".join(p[1] for p in partes),
                f"Opening USD balances were carried at the invoice rate, not revalued at the FIX of {fin_ant} ({detalle}). Net {tot_dif:+,.2f} MXN.",
                "IAS 21 requires monetary items at the closing rate every reporting date. The unrealized result of the prior month is missing and ends up inside this month's realized FX.",
                abs(tot_dif), "cierto",
                "Controller to decide whether to leave it in September (immaterial) or book an out-of-period adjustment; no change to September's cash.",
                RESP_CTRL, d10, [p[0] for p in partes],
                no_asiento="Not posted separately: it is already included in the realized FX of September, which is measured against the booked rate.")

    # ---- bank data ---------------------------------------------------------------------------
    for f in res.fuera_periodo:
        r = f.datos
        monto = r["abono"] or r["cargo"]
        agregar("Bank row out of period", "alta", f.fuente.split("!")[-1],
                f"Bank row dated {r['fecha']} ('{r['descripcion']}', {'abono' if r['abono'] else 'cargo'} {_m(monto)} {r['moneda']}) "
                f"is outside the period {res.inicio} to {res.cierre} and was NOT processed.",
                "A row from another month usually means a paste from the wrong statement or a mistyped date. If it belongs "
                "to this month and is left out, cash, AR and IVA are understated.",
                monto if r["moneda"] == "MXN" else monto * fx.en(min(r["fecha"], res.cierre)), "cierto",
                "Check the date against the bank statement. If it is mistyped, correct it and run again; if it belongs to "
                "another month, delete it from this file.", RESP_AR, d3, [f.fuente],
                no_asiento="Not processed: no entry, no application.")
    vistos = {}
    for m in res.movimientos:
        # the reference is left out and the description normalized: a pasted copy often differs only there
        k = (m.fecha, m.cuenta, " ".join(m.descripcion.upper().split()), m.cargo, m.abono, m.moneda)
        if k in vistos:
            agregar("Possible duplicate bank row", "alta", f"{vistos[k].id}, {m.id}",
                    f"Bank rows {vistos[k].id} and {m.id} have the same date, account, description and amount"
                    + (" (only the reference differs)." if vistos[k].referencia != m.referencia else "."),
                    "A row pasted twice doubles the cash. The second copy is processed like any other row, usually as unidentified cash.",
                    fx.a_mxn(m.abono or m.cargo, m.moneda, m.fecha), "confirmar",
                    "Compare with the bank statement; if the movement happened once, delete the repeated row and run again.",
                    RESP_AR, d3, [vistos[k].fuente, m.fuente])
        vistos.setdefault(k, m)
    for m in res.no_cxc:
        texto = m.descripcion.upper()
        mencionadas = [f for f in res.facturas if re.search(r"(?<![A-Z0-9])" + re.escape(f.upper()) + r"(?![A-Z0-9])", texto)]
        sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
        if m.cargo > 0 and (mencionadas or m.pista or re.search(r"\b(RECIBIDO|WIRE IN|DEPOSITO|COBRO)\b", sin_acentos)):
            nombra = ", ".join(mencionadas) or m.pista
            agregar("Deposit typed as a withdrawal?", "media", m.id,
                    f"Bank row {m.id} ('{m.descripcion}', {_m(m.cargo)} {m.moneda}) is a withdrawal (cargo), but its "
                    "description reads like a customer payment" + (f" and names {nombra}." if nombra else "."),
                    "It can be a genuine refund or a supplier with a similar name. If the amount was typed in the cargo "
                    "column by mistake, the receipt is missing: AR stays overstated "
                    "and IVA cobrado understated.", fx.a_mxn(m.cargo, m.moneda, m.fecha), "confirmar",
                    "Check the bank statement; if it is a deposit, move the amount to the abono column and run again.",
                    RESP_AR, d3, [m.fuente])
        if m.abono > 0 and mencionadas:
            agregar("Receipt classified as not AR", "alta", m.id,
                    f"Receipt {m.id} ('{m.descripcion}', {_m(m.abono)} {m.moneda}) was classified as not AR because of a "
                    f"keyword, but it mentions {', '.join(mencionadas)}.",
                    "If it is a customer payment, AR and IVA cobrado are overstated and understated by this amount.",
                    fx.a_mxn(m.abono, m.moneda, m.fecha), "confirmar",
                    f"Confirm with the bank statement; if it is a payment, override it with "
                    f"`override {m.id} --asignar {mencionadas[0]}`.", RESP_AR, d3, [m.fuente])
    conflictos = set()
    for a in res.aplicaciones:
        for t in a.alertas:
            if t.startswith("Reference/customer conflict") and a.mov_id not in conflictos:
                conflictos.add(a.mov_id)
                agregar("Reference vs customer", "alta", a.mov_id, f"{a.mov_id}: {t}",
                        "Applying a payment to the wrong customer's invoice leaves both customers' balances wrong and "
                        "the REP would be issued to the wrong customer.",
                        a.mxn_recibido, "confirmar",
                        "AR to confirm with the payer which invoice was paid; override the application if needed.",
                        RESP_AR, d3, a.fuentes)

    # ---- bank + REP -------------------------------------------------------------------------
    reps_por_folio = defaultdict(list)
    for f in res.ds.rep:
        reps_por_folio[f.datos["folio"]].append(f)
    usados = set()
    pue_sin_rep = defaultdict(list)
    for a in res.aplicaciones:
        cand = [f for f in reps_por_folio.get(a.folio, []) if f.datos["fecha"] and abs((f.datos["fecha"] - a.fecha).days) <= 1]
        p_apl = res.partidas.get(a.folio)
        if not cand and p_apl is not None and p_apl.metodo == "PUE":
            # A PUE paid in its month of issuance needs no REP; one paid later is handled below (RMF 2026 rule 2.7.1.39).
            pue_sin_rep[a.folio].append(a)
            continue
        if not cand:
            limite = limite_rep(a.fecha, cfg.dia_limite_rep)
            extra = (f" It was paid in {a.pago_moneda} against a {a.doc_moneda} invoice, so the REP must show the payment currency and its equivalence."
                     if a.pago_moneda != a.doc_moneda else "")
            agregar("Missing REP", "alta", a.folio,
                    f"Payment of {_m(a.pago_monto)} {a.pago_moneda} on {a.fecha} for {a.folio} ({a.cliente}) has no Complemento de Pago (REP).{extra}",
                    "On PPD invoices the REP is the evidence of payment. Without it the customer cannot credit the IVA (LIVA art. 5o fr. III) or deduct, and Clara risks a fine for not issuing CFDI (CFF art. 83 fr. VII, art. 84 fr. IV). "
                    f"It is due by calendar day {cfg.dia_limite_rep} of the following month (RMF 2026 rule 2.7.1.32).",
                    a.iva_cobrado_mxn, "cierto",
                    f"Issue and stamp the REP for {a.folio} with date {a.fecha}, amount {_m(a.pago_monto)} {a.pago_moneda}"
                    + (f" and remaining balance {_m(a.saldo_despues)}" if a.saldo_despues > 0 else "") + ".",
                    RESP_FISCAL, limite, a.fuentes)
            continue
        f = cand[0]
        usados.add(f.fuente)
        r = f.datos
        a.rep = {"uuid_rep": r.get("uuid_rep"), "fuente": f.fuente}
        if r["pagado"] is not None and abs(r["pagado"] - (a.pago_monto if a.pago_moneda == r["moneda"] else a.aplicado_doc)) > Decimal("0.01"):
            agregar("REP amount", "alta", a.folio,
                    f"REP for {a.folio} reports {_m(r['pagado'])} {r['moneda']} but the bank shows {_m(a.pago_monto)} {a.pago_moneda}.",
                    "REP and bank must agree; otherwise the customer's IVA credit and Clara's IVA by collection are wrong.",
                    abs(r["pagado"] - a.pago_monto), "cierto", "Reissue the REP with the bank amount.", RESP_FISCAL, d3,
                    [f.fuente] + a.fuentes)
        if r["tc"] is not None and r["moneda"] != "MXN" and abs(r["tc"] - fx.en(a.fecha)) > cfg.tolerancia_tc_cfdi:
            agregar("REP rate", "media", a.folio,
                    f"REP for {a.folio} uses rate {r['tc']} but the FIX of {a.fecha} is {fx.en(a.fecha):.4f}.",
                    "The REP rate must be the FIX applicable on the payment date (CFF art. 20); it fixes the IVA collected in MXN.",
                    abs(r["tc"] - fx.en(a.fecha)) * a.aplicado_doc, "confirmar con fiscal", "Check and reissue if wrong.",
                    RESP_FISCAL, d5, [f.fuente])
        if r["saldo_anterior"] is not None and r["pagado"] is not None and r["insoluto"] is not None \
                and abs(r["saldo_anterior"] - r["pagado"] - r["insoluto"]) > Decimal("0.01"):
            agregar("REP balance", "alta", a.folio, f"REP for {a.folio}: previous balance - paid is not the outstanding balance.",
                    "The REP chain of installments must be arithmetically consistent.", abs(r["saldo_anterior"] - r["pagado"] - r["insoluto"]),
                    "cierto", "Reissue the REP.", RESP_FISCAL, d3, [f.fuente])
        cf = cfdi.get(a.folio)
        # first installment: the previous balance is the CFDI total net of credit notes issued before the payment
        # (SAT payments guide 2.0, worked example pp. 73-77; p. 55 note 4: a credit note is not applied through a REP)
        ncs = [nc for nc in res.notas_credito if nc.folio_factura == a.folio and nc.fecha <= r["fecha"]]
        esperado = cf.datos["total"] - sum((nc.total for nc in ncs), CERO) if cf else None
        if cf and r.get("parcialidad") == 1 and r["saldo_anterior"] is not None \
                and abs(r["saldo_anterior"] - esperado) > Decimal("0.01"):
            notas = f" less credit note(s) {', '.join(nc.folio for nc in ncs)}" if ncs else ""
            agregar("REP balance", "media", a.folio,
                    f"REP for {a.folio} starts at a previous balance of {_m(r['saldo_anterior'])}, but it should be "
                    f"{_m(esperado)} (CFDI total {_m(cf.datos['total'])}{notas}).",
                    "The first installment starts from the invoice balance at the payment date: the CFDI total net of credit "
                    "notes already issued (SAT payments guide 2.0, worked example p. 77). Otherwise SAT's chain of balances "
                    "will not match the invoice.",
                    abs(esperado - r["saldo_anterior"]), "confirmar con fiscal",
                    "Tax team to check the REP and reissue it with the right previous balance if confirmed.",
                    RESP_FISCAL, d5, [f.fuente, cf.fuente])
    for folio, apls in pue_sin_rep.items():
        p = res.partidas[folio]
        fin_mes = (p.fecha.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        tardias = [a for a in apls if a.fecha > fin_mes]
        if not tardias or p.saldo > Decimal("0.005"):
            continue                    # paid in its month: no REP; still open: 'PUE not collected' covers it
        fechas = ", ".join(str(a.fecha) for a in tardias)
        agregar("PUE paid late", "alta", folio,
                f"{folio} ({p.cliente}) is PUE (paid at issuance) but was paid on {fechas}, after the last day of its month of issuance ({fin_mes}).",
                "PUE without cash at issuance is allowed only if the full payment arrives by the last day of the month of issuance "
                "(RMF 2026 rule 2.7.1.39). Paid later, the CFDI must be cancelled and replaced, and a REP issued for each payment.",
                sum((a.iva_cobrado_mxn for a in tardias), CERO), "cierto",
                f"Cancel {folio} and issue a new CFDI with method PPD and payment form 99, related as 'Sustitución de los CFDI previos' "
                f"(c_TipoRelacion 04); then issue the REP for each payment (RMF 2026 rule 2.7.1.39), due by calendar day "
                f"{cfg.dia_limite_rep} of the month after the payment (RMF 2026 rule 2.7.1.32).",
                RESP_FISCAL, limite_rep(max(a.fecha for a in tardias), cfg.dia_limite_rep),
                p.fuentes + [x for a in tardias for x in a.fuentes],
                no_asiento="The collection is booked as usual; only the CFDI documents change.")
    for f in res.ds.rep:
        if f.fuente not in usados:
            r = f.datos
            cerca = [a for a in res.aplicaciones if a.folio == r["folio"] and abs((a.fecha - r["fecha"]).days) <= 31]
            pista = (f" The bank shows a payment for {r['folio']} on {cerca[0].fecha}: if it is the same payment, the REP "
                     f"date differs from the bank date by {abs((cerca[0].fecha - r['fecha']).days)} day(s).") if cerca else ""
            retiros = [m for m in res.movimientos if m.cargo > 0 and r["pagado"] is not None and m.moneda == r["moneda"]
                       and abs(m.cargo - r["pagado"]) <= Decimal("0.01") and abs((m.fecha - r["fecha"]).days) <= 3]
            if retiros:
                pista += (f" Bank row {retiros[0].id} is a withdrawal of the same amount on {retiros[0].fecha}: if the "
                          "deposit was typed in the cargo column, move it to abono.")
            agregar("REP without bank", "alta", r["folio"],
                    f"REP for {r['folio']} dated {r['fecha']} ({_m(r['pagado'])} {r['moneda']}) has no matching bank receipt.{pista}",
                    "A REP certifies a payment that the bank does not show: IVA would be declared as collected without cash.",
                    r["pagado"] * (r["tc"] or Decimal(1)), "confirmar",
                    "Check the bank statement and the customer; cancel the REP if no payment exists.", RESP_FISCAL, d3, [f.fuente])

    for u in res.efectivo_no_identificado:
        if u.get("nivel") == 2:
            cands = u["candidatos"]
            agregar("Match to confirm", "media", u["mov_id"],
                    f"Receipt of {_m(u['monto'])} {u['moneda']} on {u['fecha']} ('{u['descripcion']}') names no invoice "
                    "and no customer. Candidates, best first: "
                    + "; ".join(f"{c['folio']} ({c['cliente']}, balance {_m(c['saldo'])} {c['moneda']}): {c['evidencia']}"
                                for c in cands) + ".",
                    "An amount alone can belong to another customer: applying it would relieve the wrong invoice and "
                    "send the REP to the wrong customer. Until someone confirms, the invoice stays open in the aging.",
                    u["mxn"], "confirmar",
                    f"Confirm with the customer or the bank, then accept a candidate (python -m cierre_cxc accept "
                    f"{u['mov_id']} --candidato N) or assign another invoice. The decision is learned for next month.",
                    RESP_AR, d3, [u["fuente"]],
                    no_asiento="Booked to 2101 until a reviewer accepts a candidate; then the engine applies it.")
            continue
        prop = res.propuestas.get(u["mov_id"])
        iva_pot = q2(u["mxn"] * cfg.tasa_general / (1 + cfg.tasa_general))
        parte = [a.folio for a in res.aplicaciones if a.mov_id == u["mov_id"]]
        que = (f"Receipt {u['mov_id']} on {u['fecha']} ('{u['descripcion']}') was applied to {', '.join(parte)}, and "
               f"{_m(u['monto'])} {u['moneda']} were left over (possible overpayment, or an amount typed wrong)."
               if parte else
               f"Receipt of {_m(u['monto'])} {u['moneda']} on {u['fecha']} ('{u['descripcion']}') could not be matched to any invoice.")
        desc_u = unicodedata.normalize("NFKD", u["descripcion"].upper()).encode("ascii", "ignore").decode()
        if re.search(cfg.patron_comision, desc_u):
            que += " Its description reads like a bank fee: if it is a fee, it belongs in the cargo column."
        recibo = next((m.abono for m in res.movimientos if m.id == u["mov_id"]), u["monto"])
        posteriores = [f for f, r in res.facturas.items() if r["fecha"] > u["fecha"] and r["estatus"] != "Cancelado"
                       and r["moneda"] == u["moneda"]
                       and min(abs(r["total"] - u["monto"]), abs(r["total"] - recibo)) <= Decimal("0.01")]
        if posteriores:
            que += (f" Its amount equals {', '.join(posteriores)}, issued after the receipt: check the receipt date and "
                    "the invoice date, or whether it is an advance.")
        agregar("Unidentified cash", "alta", u["mov_id"], que,
                "AR cannot be relieved and no CFDI/REP can be issued. If it is an advance, IVA is due on receipt (LIVA art. 1o.-B treats advances as effectively collected); if it is a mistaken transfer, it must be returned.",
                u["mxn"], "confirmar",
                "Treasury to get the payer's name and RFC from the bank; AR to contact the customer. If it pays Clara services, "
                "apply it (or issue the advance CFDI) and include its IVA in this month's return, due "
                f"{decl_iva} (LIVA art. 5o.-D); if mistaken, refund it (no IVA). Tax team decides by {decl_iva - timedelta(days=2)} "
                "if it is still unidentified.",
                RESP_TESO, d3, [u["fuente"]],
                no_asiento=f"Booked to 2101 only. IVA of {_m(iva_pot)} is not recognized until the receipt is identified.")

    for mov_id, prop in res.propuestas.items():
        if not prop.orden_antiguo:
            continue
        mov = next(m for m in res.movimientos if m.id == mov_id)
        aps = [a for a in res.aplicaciones if a.mov_id == mov_id]
        if not aps:
            continue
        exacta = prop.orden_antiguo
        movido = sum((a.libro_relevado for a in aps if a.folio != exacta), CERO)
        limite = mov.fecha + timedelta(days=5)
        aplicado = ", ".join(f"{a.folio} {_m(a.aplicado_doc)} {a.doc_moneda}" for a in aps)
        que = (f"Receipt {mov_id} on {mov.fecha} ({_m(mov.abono)} {mov.moneda}, '{mov.descripcion}') names {aps[0].cliente} "
               f"but no invoice. It was applied to the oldest open invoice(s) first ({aplicado}), although the amount "
               f"matches {exacta} exactly.")
        if limite <= res.cierre:
            que += f" The customer's 5-day window to indicate another invoice closed on {limite}."
        agregar("Application order", "baja" if limite <= res.cierre else "media", mov_id, que,
                "If the payer does not indicate the invoice within 5 calendar days of the payment, the receiver applies it "
                "to the oldest pending CFDI (SAT payments complement guide 2.0, numerals 3 and 4). The REP must name the "
                "invoice paid; if the customer booked it against another one, its IVA credit will not match.",
                movido, "confirmar",
                f"AR to ask the customer in writing which invoice it paid, by {limite}. If it names {exacta}: "
                f"python -m cierre_cxc override {mov_id} --asignar {exacta} --comentario \"customer indicated {exacta}\", "
                f"and issue the REP for {exacta}. If it does not answer, keep the oldest-first application.",
                RESP_AR, limite, aps[0].fuentes)

    for a in res.aplicaciones:
        if a.tipo == "corto" and a.saldo_despues > Decimal("0.005"):
            agregar("Short payment", "baja", a.folio,
                    f"{a.cliente} paid {_m(a.pago_monto)} against {a.folio}; {_m(a.saldo_despues)} {a.doc_moneda} remains open.",
                    "Small differences are usually bank fees or retentions the customer deducted; if not documented they stay in AR and age.",
                    a.saldo_despues, "confirmar",
                    f"Ask the customer what the difference is. If Clara accepts it as a discount, with the Controller's approval issue a "
                    f"CFDI de egreso related to {a.folio} (c_TipoRelacion 01; payment form 15 'Condonación' while the invoice is unpaid) "
                    f"that restitutes IVA of {_m(q2(a.saldo_despues * a.tasa / (1 + a.tasa)))} {a.doc_moneda} (LIVA art. 7o).",
                    RESP_AR, d10, a.fuentes, no_asiento="Not written off without approval; the REP will show the outstanding balance.")
        for t in a.alertas:
            if "older open invoice" in t:
                viejas = t.split("invoice(s) ")[1].split(";")[0].split(", ")
                monto = sum((res.partidas[v].libro_mxn for v in viejas if v in res.partidas), CERO)
                vencida = any(v in res.partidas and res.partidas[v].vencimiento < a.fecha for v in viejas)
                agregar("Application order", "media" if vencida else "baja", a.folio, t,
                        "Customers normally settle the oldest invoice first. Applying to a newer one leaves an older invoice overdue and distorts the aging.",
                        monto, "confirmar",
                        "AR to confirm the intended invoice with the customer and chase the older one.", RESP_AR, d3, a.fuentes)
    return out
