"""Close engine: builds open items, replays the month in date order, applies receipts and credit notes.

All amounts are computed here with Decimal. Nothing in this module calls a language model.
"""
from __future__ import annotations
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .cargar import Dataset, SchemaError
from .config import Config
from .conciliar import Contexto, proponer, candidatos, _sin_acentos
from .dinero import q2, CERO
from .fx import TipoCambio
from .modelos import (Partida, Movimiento, Propuesta, Aplicacion, NotaCredito, clave_cliente)


@dataclass
class Resultado:
    cfg: Config
    ds: Dataset
    cierre: date
    inicio: date
    fx: TipoCambio
    partidas: dict = field(default_factory=dict)        # folio -> Partida (state after the replay)
    apertura_libro: Decimal = CERO
    apertura_iva_libro: Decimal = CERO
    movimientos: list = field(default_factory=list)
    aplicaciones: list = field(default_factory=list)
    notas_credito: list = field(default_factory=list)
    no_cxc: list = field(default_factory=list)
    comisiones: list = field(default_factory=list)
    efectivo_no_identificado: list = field(default_factory=list)
    canceladas: list = field(default_factory=list)       # CFDI rows, status Cancelado
    correcciones_tc: list = field(default_factory=list)  # dicts with the TC fix per invoice
    nuevas_facturas: list = field(default_factory=list)  # partidas created in the period
    reval: list = field(default_factory=list)            # dicts per open USD item
    propuestas: dict = field(default_factory=dict)       # mov_id -> Propuesta (what the matcher proposed)
    avisos: list = field(default_factory=list)
    fuera_periodo: list = field(default_factory=list)    # bank rows (Fila) dated outside the period
    excepciones: list = field(default_factory=list)
    polizas: list = field(default_factory=list)
    no_contabilizado: list = field(default_factory=list)
    clientes: dict = field(default_factory=dict)
    facturas: dict = field(default_factory=dict)
    aprendizajes: list = field(default_factory=list)     # reviewer decisions a rule can be learned from


def _plazo(notas: str, default: int) -> int:
    m = re.search(r"\bnet\s*(\d+)", notas or "", re.I)
    return int(m.group(1)) if m else default


def _mes(f: date):
    return date(f.year, f.month, 1), date(f.year, f.month, calendar.monthrange(f.year, f.month)[1])


def periodo(ds: Dataset, cfg: Config):
    """Period of the close: --cierre if given, otherwise the month with the most bank rows.
    The latest date is not used: one row pasted from the next month would move the whole close."""
    if cfg.cierre:
        inicio, _ = _mes(cfg.cierre)
        return inicio, cfg.cierre
    if not ds.banco:
        raise SchemaError("The bank sheet has no movements, so the period of the close cannot be determined.")
    conteo = {}
    for f in ds.banco:
        m = _mes(f.datos["fecha"])
        conteo[m] = conteo.get(m, 0) + 1
    return max(conteo, key=lambda m: (conteo[m], m[1]))


def _revisar_fechas(ds: Dataset, res: Resultado) -> None:
    inicio, cierre = res.inicio, res.cierre
    if not any(inicio <= f.datos["fecha"] <= cierre for f in ds.banco):
        raise SchemaError(f"No bank movement is dated between {inicio} and {cierre}. Check that this is the right "
                          "file and period (--cierre).")
    for f in ds.banco:
        if not (inicio <= f.datos["fecha"] <= cierre):
            res.fuera_periodo.append(f)
    en_apertura = {f.datos["folio"] for f in ds.apertura}
    for f in ds.cfdi:
        r = f.datos
        if r["fecha"] > cierre:
            res.avisos.append(f"{f.fuente}: CFDI {r['folio']} is dated {r['fecha']}, after the close ({cierre}): ignored.")
        elif (r["fecha"] < inicio and r["tipo"] == "I" and r["folio"] not in en_apertura
              and not r["estatus"] == "Cancelado"):
            res.avisos.append(f"{f.fuente}: CFDI {r['folio']} ({r['fecha']}) is from before the period and is not in the "
                              "opening AR: it is treated as collected before the period.")
    cfdi = {f.datos["folio"]: f.datos for f in ds.cfdi if f.datos["tipo"] == "I"}
    for f in ds.apertura:
        a = f.datos
        c = cfdi.get(a["folio"])
        emitida = c["fecha"] if c else a["fecha"]          # the CFDI issue date wins over the date typed in the row
        if emitida and emitida >= inicio:
            res.avisos.append(f"{f.fuente}: opening item {a['folio']} was issued on {emitida}, inside the period, but an "
                              "opening item must be issued before the opening date. Either the CFDI date or the opening "
                              "row is wrong: check the CFDI date first; if the invoice really is of this month, it belongs "
                              "only in the CFDI sheet.")
        if c and c["moneda"] == a["moneda"] and a["saldo"] > c["total"] + Decimal("0.01"):
            res.avisos.append(f"{f.fuente}: opening balance of {a['folio']} ({a['saldo']:,.2f}) is larger than its CFDI "
                              f"total ({c['total']:,.2f}). One of the two is wrong.")


def construir_partidas(ds: Dataset, cfg: Config, fx: TipoCambio, inicio: date, cierre: date, res: Resultado,
                       rechazadas=frozenset()):
    cfdi_i = {f.datos["folio"]: f for f in ds.cfdi if f.datos["tipo"] == "I"}
    por_nombre = {}
    for f in cfdi_i.values():
        r = f.datos
        res.clientes[clave_cliente(r["rfc"], r["cliente"])] = r["cliente"]
        por_nombre[_sin_acentos(r["cliente"])] = clave_cliente(r["rfc"], r["cliente"])
    partidas = {}
    # opening items (as booked at the previous close)
    for fa in ds.apertura:
        a = fa.datos
        f = cfdi_i.get(a["folio"])
        r = f.datos if f else None
        tasa = r["tasa"] if r else cfg.tasa_general
        if not r:
            res.avisos.append(f"Opening item {a['folio']} has no CFDI in the data: tax rate assumed {cfg.tasa_general}.")
        libro = a["saldo_mxn"] if a["saldo_mxn"] is not None else q2(a["saldo"] * (a["tc"] or Decimal(1)))
        clave = clave_cliente(r["rfc"], r["cliente"]) if r else por_nombre.get(_sin_acentos(a["cliente"]), _sin_acentos(a["cliente"]))
        res.clientes.setdefault(clave, a["cliente"] or (r["cliente"] if r else clave))
        total = r["total"] if r else a["saldo"]
        iva_total = r["iva"] if r else q2(a["saldo"] * tasa / (1 + tasa))
        frac = a["saldo"] / total if total else Decimal(1)
        iva_pend = q2(iva_total * frac)
        fecha = r["fecha"] if r else (a["fecha"] or inicio)
        p = Partida(folio=a["folio"], uuid=r["uuid"] if r else "", cliente=res.clientes[clave], cliente_clave=clave,
                    rfc=r["rfc"] if r else "", moneda=a["moneda"], fecha=fecha,
                    vencimiento=fecha + timedelta(days=_plazo(r["notas"] if r else "", cfg.plazo_default)),
                    tasa=tasa, total_orig=total, saldo=a["saldo"], libro_mxn=libro, iva_total=iva_total,
                    iva_pend=iva_pend, iva_libro_pend=q2(iva_pend * (libro / a["saldo"])) if a["saldo"] else CERO,
                    origen="apertura", metodo=r["metodo"] if r else "",
                    fuentes=[fa.fuente] + ([f.fuente] if f else []), libro_original_mxn=libro,
                    iva_libro_inicial=q2(iva_pend * (libro / a["saldo"])) if a["saldo"] else CERO)
        partidas[p.folio] = p
    res.apertura_libro = sum((p.libro_mxn for p in partidas.values()), CERO)
    res.apertura_iva_libro = sum((p.iva_libro_pend for p in partidas.values()), CERO)
    # invoices of the period
    for folio, f in cfdi_i.items():
        r = f.datos
        if folio in partidas or not (inicio <= r["fecha"] <= cierre):
            continue
        if r["estatus"] == "Cancelado":
            res.canceladas.append(f)
            continue
        if r["estatus"] != "Vigente":
            res.avisos.append(f"{folio}: unknown SAT status '{r['estatus']}', treated as valid.")
        tc_ok = Decimal(1) if r["moneda"] == "MXN" else (fx.intentar(r["fecha"]) or r["tc"])
        if tc_ok is None:
            raise SchemaError(f"{folio}: USD invoice without a rate in the CFDI and none in the FIX table.")
        tc_cfdi = r["tc"] if r["tc"] else tc_ok
        libro = q2(r["total"] * tc_cfdi)
        iva_libro = q2(r["iva"] * tc_cfdi)
        corr = None
        if r["moneda"] != "MXN" and abs(tc_cfdi - tc_ok) > cfg.tolerancia_tc_cfdi:
            # total and IVA move from the booked (rounded) value to the corrected one; the subtotal is the plug,
            # so the entry balances to the cent and the roll-forward ties to the corrected carrying value
            d_total = q2(r["total"] * tc_ok) - libro
            d_iva = q2(r["iva"] * tc_ok) - iva_libro
            corr = {"folio": folio, "tc_cfdi": tc_cfdi, "tc_fix": tc_ok, "fuente": f.fuente,
                    "delta_total": d_total, "delta_subtotal": d_total - d_iva,
                    "delta_iva": d_iva, "moneda": r["moneda"],
                    "subtotal_usd": r["subtotal"], "iva_usd": r["iva"],
                    "aplicada": cfg.aplicar_correcciones and folio not in rechazadas}
            res.correcciones_tc.append(corr)
        clave = clave_cliente(r["rfc"], r["cliente"])
        p = Partida(folio=folio, uuid=r["uuid"], cliente=r["cliente"], cliente_clave=clave, rfc=r["rfc"],
                    moneda=r["moneda"], fecha=r["fecha"],
                    vencimiento=r["fecha"] + timedelta(days=_plazo(r["notas"], cfg.plazo_default)),
                    tasa=r["tasa"], total_orig=r["total"], saldo=r["total"], libro_mxn=libro, iva_total=r["iva"],
                    iva_pend=r["iva"], iva_libro_pend=iva_libro, origen="periodo", metodo=r["metodo"],
                    fuentes=[f.fuente], libro_original_mxn=libro, iva_libro_inicial=iva_libro)
        if corr and corr["aplicada"]:
            p.libro_mxn = q2(r["total"] * tc_ok)
            p.iva_libro_pend = q2(r["iva"] * tc_ok)
            p.tc_correccion = tc_ok
        partidas[folio] = p
        res.nuevas_facturas.append(p)
    return partidas


def _clasificar(m: Movimiento, cfg: Config) -> str:
    desc = _sin_acentos(m.descripcion)
    if m.cargo > 0 and m.abono == 0:
        if re.search(cfg.patron_iva_comision, desc):
            return "iva_comision"
        if re.search(cfg.patron_comision, desc):
            return "comision"
        return "no_cxc"
    if m.abono > 0 and re.search(cfg.patron_no_cxc, desc):
        return "abono_no_cxc"
    return "cobro"


def _relevar(p: Partida, aplicado_doc: Decimal):
    """Carrying value and IVA released when `aplicado_doc` of the balance is settled."""
    cierra = (p.saldo - aplicado_doc) <= Decimal("0.005")
    libro = p.libro_mxn if cierra else q2(p.libro_mxn * aplicado_doc / p.saldo)
    if cierra:
        iva_doc, iva_libro = p.iva_pend, p.iva_libro_pend
    else:
        iva_doc = min(p.iva_pend, q2(aplicado_doc * p.tasa / (1 + p.tasa)))
        iva_libro = min(p.iva_libro_pend, q2(iva_doc * p.tc_libro))
    return libro, iva_doc, iva_libro


def _aplicar(res: Resultado, ctxn: int, mov: Movimiento, prop: Propuesta, abiertas_nombre: dict) -> int:
    fxo = res.fx
    n = ctxn
    for (p, monto_doc, monto_pago) in prop.items:
        monto_doc = min(monto_doc, p.saldo)
        libro, iva_doc, iva_libro = _relevar(p, monto_doc)
        tc_fix = fxo.en(mov.fecha) if p.moneda == "USD" else Decimal(1)
        mxn_rec = q2(monto_pago) if mov.moneda == "MXN" else q2(monto_pago * fxo.en(mov.fecha))
        iva_cobrado = q2(iva_doc * tc_fix)
        saldo_antes = p.saldo
        p.saldo -= monto_doc
        p.libro_mxn -= libro
        p.iva_pend -= iva_doc
        p.iva_libro_pend -= iva_libro
        n += 1
        res.aplicaciones.append(Aplicacion(
            id=f"AP{n:02d}", mov_id=mov.id, fecha=mov.fecha, folio=p.folio, cliente=p.cliente, tipo=prop.tipo,
            confianza=prop.confianza, razon=prop.razon, alertas=list(prop.alertas), pago_monto=monto_pago,
            pago_moneda=mov.moneda, doc_moneda=p.moneda, saldo_antes=saldo_antes, aplicado_doc=monto_doc,
            saldo_despues=p.saldo, tc_cobro=tc_fix, mxn_recibido=mxn_rec, libro_relevado=libro,
            fx_realizada=mxn_rec - libro, iva_doc=iva_doc, iva_libro_mxn=iva_libro, iva_cobrado_mxn=iva_cobrado,
            fuentes=[mov.fuente] + p.fuentes, cuenta_banco=mov.cuenta, tasa=p.tasa))
    if prop.remanente > 0:
        mxn = q2(prop.remanente) if mov.moneda == "MXN" else q2(prop.remanente * fxo.en(mov.fecha))
        res.efectivo_no_identificado.append({"mov_id": mov.id, "fecha": mov.fecha, "monto": prop.remanente,
                                             "moneda": mov.moneda, "mxn": mxn, "descripcion": mov.descripcion,
                                             "razon": prop.razon, "alertas": list(prop.alertas),
                                             "fuente": mov.fuente, "cuenta_banco": mov.cuenta,
                                             "nivel": prop.nivel, "candidatos": list(prop.candidatos)})
    return n


def _por_nivel(ctx, mov: Movimiento, abiertas: list, prop: Propuesta, sugerida: Optional[Propuesta] = None) -> Propuesta:
    """Level 1 is applied. When the description names neither an invoice nor a customer, the amount alone is a
    clue, not proof: the receipt stays in 2101 with ranked candidates until a reviewer accepts one (level 2).
    Without even a fitting amount it is level 3: a person has to find out who paid."""
    if prop.modo == "aprendido" and prop.tipo == "no_identificado" and prop.regla:
        # the rule names the customer but no amount fits exactly: propose that customer's invoices, apply nothing
        r = prop.regla
        cands = [{"folio": p.folio, "cliente": p.cliente, "saldo": p.saldo, "moneda": p.moneda,
                  "evidencia": f"learned rule {r['id']} names this customer; the amount does not match exactly"}
                 for p in sorted(abiertas, key=lambda x: (x.vencimiento, x.folio))
                 if p.cliente_clave == r["cliente_clave"]][:3]
        vistos = {c["folio"] for c in cands}
        cands += [c for c in candidatos(ctx, mov, abiertas) if c["folio"] not in vistos][:max(0, 4 - len(cands))]
        if cands:
            return Propuesta(tipo="por_confirmar", confianza="baja", modo=prop.modo, remanente=mov.abono, nivel=2,
                             candidatos=cands, alternativas=[c["folio"] for c in cands], alertas=list(prop.alertas),
                             regla=r, razon=prop.razon + " Nothing is applied until a reviewer accepts a candidate: "
                             + "; ".join(f"{c['folio']} ({c['cliente']}): {c['evidencia']}" for c in cands) + ".")
    if prop.modo in ("parecido", "parcial_cliente") and prop.pista:
        # a misspelled customer name: that customer's invoices first (amount that fits on top), then any other
        # invoice whose amount fits; the reviewer picks from the list
        k, nombre = prop.pista["cliente_clave"], prop.pista["cliente"]
        encajan = {c["folio"]: c for c in candidatos(ctx, mov, abiertas, limite=50)}
        suyas = sorted((p for p in abiertas if p.cliente_clave == k),
                       key=lambda p: (p.folio not in encajan, p.vencimiento, p.folio))
        cands = [{"folio": p.folio, "cliente": p.cliente, "saldo": p.saldo, "moneda": p.moneda,
                  "evidencia": prop.pista.get("evidencia", f"name similar to {nombre}") + "; "
                               + (encajan[p.folio]["evidencia"] if p.folio in encajan
                                                                 else "the amount does not match this invoice")}
                 for p in suyas][:3]
        vistos = {c["folio"] for c in cands}
        cands += [c for c in encajan.values() if c["folio"] not in vistos][:max(0, 4 - len(cands))]
        if cands:
            return Propuesta(tipo="por_confirmar", confianza="baja", modo=prop.modo, remanente=mov.abono, nivel=2,
                             candidatos=cands, alternativas=[c["folio"] for c in cands], alertas=list(prop.alertas),
                             pista=prop.pista, razon=prop.razon + " Nothing is applied until a reviewer accepts a "
                             "candidate: " + "; ".join(f"{c['folio']} ({c['cliente']}): {c['evidencia']}" for c in cands) + ".")
        prop.nivel = 3
        return prop
    if prop.modo not in ("monto",):
        prop.nivel = 1 if prop.tipo != "no_identificado" else 3
        return prop
    cands = candidatos(ctx, mov, abiertas)
    if sugerida is not None:            # a language model suggested the customer: its match goes first, still a proposal
        vistos = {p.folio for p, _, _ in sugerida.items}
        cands = [{"folio": p.folio, "cliente": p.cliente, "saldo": p.saldo, "moneda": p.moneda,
                  "evidencia": "customer suggested by the language model; " + sugerida.razon}
                 for p, _, _ in sugerida.items] + [c for c in cands if c["folio"] not in vistos]
    if not cands:
        prop.nivel = 3
        return prop
    razon = ("A language model suggested the customer; the amounts were matched in code. "
             if sugerida is not None else "The description names no invoice and no customer; only the amount fits. ")
    return Propuesta(tipo="por_confirmar", confianza="baja", modo=prop.modo, remanente=mov.abono, nivel=2,
                     candidatos=cands, alternativas=[c["folio"] for c in cands], alertas=list(prop.alertas),
                     razon=razon + "Nothing is applied until a reviewer accepts a candidate: "
                     + "; ".join(f"{c['folio']} ({c['cliente']}): {c['evidencia']}" for c in cands) + ".")


def _propuesta_manual(res, ctx, mov, decision, abiertas) -> Propuesta:
    """Build a proposal from a reviewer override. Amounts are still computed here, not typed in."""
    accion = decision.get("accion")
    if accion == "no_cxc":
        return Propuesta(tipo="no_cxc", razon=f"Reviewer marked it as not AR: {decision.get('comentario', '')}")
    if accion == "no_identificado":
        return Propuesta(tipo="no_identificado", remanente=mov.abono, confianza="baja", modo="manual",
                         razon=f"Reviewer left it unapplied: {decision.get('comentario', '')}")
    items, resto = [], mov.abono
    por_folio = {p.folio: p for p in abiertas}
    folios = []
    for folio in decision.get("folios", []):
        folio = str(folio).strip().upper()
        if folio in folios:
            res.avisos.append(f"Override of {mov.id} lists {folio} more than once; it was applied once.")
            continue
        folios.append(folio)
    por_folio = {k.upper(): v for k, v in por_folio.items()}
    for folio in folios:
        p = por_folio.get(folio)
        if not p:
            res.avisos.append(f"Override of {mov.id} names {folio}, which is not an open invoice on {mov.fecha}: "
                              "that part of the override was ignored. Check the folio.")
            continue
        if resto <= 0:
            res.avisos.append(f"Override of {mov.id}: nothing left to apply to {folio}; the receipt was used up by "
                              "the invoices listed before it.")
            continue
        eq = resto if mov.moneda == p.moneda else res.fx.convertir(resto, mov.moneda, p.moneda, mov.fecha)
        aplica = min(p.saldo, eq)
        if aplica <= 0:
            continue
        pago = resto if aplica == eq else (aplica if mov.moneda == p.moneda else res.fx.convertir(aplica, p.moneda, mov.moneda, mov.fecha))
        items.append((p, aplica, q2(pago)))
        resto -= q2(pago)
    return Propuesta(items=items, remanente=max(resto, CERO), tipo="manual", confianza="alta", modo="manual",
                     razon=f"Assigned by reviewer {decision.get('usuario', '')}: {decision.get('comentario', '')}".strip())


def correr(ds: Dataset, cfg: Optional[Config] = None, decisiones: Optional[dict] = None,
           sugeridor=None, reglas=None) -> Resultado:
    """`sugeridor(mov, clientes)` may return a customer key for a movement the matcher could not place.
    The suggestion only narrows the candidate invoices; amounts are still matched and computed in code, and
    the result is a proposal (level 2) that a reviewer has to accept.
    `reglas` (aprendizaje.Reglas) identifies payers that reviewers placed before."""
    cfg = cfg or Config()
    decisiones = decisiones or {}
    inicio, cierre = periodo(ds, cfg)
    fxo = TipoCambio(ds.fx, ds.avisos)
    res = Resultado(cfg=cfg, ds=ds, cierre=cierre, inicio=inicio, fx=fxo, avisos=ds.avisos)
    _revisar_fechas(ds, res)
    res.partidas = construir_partidas(ds, cfg, fxo, inicio, cierre, res,
                                      frozenset(str(x).strip().upper() for x in decisiones.get("rechazar_correcciones") or []))
    res.facturas = {f.datos["folio"]: f.datos for f in ds.cfdi if f.datos["tipo"] == "I"}
    ctx = Contexto(cfg, fxo, res.facturas, res.clientes, reglas=reglas)

    # events in date order: credit notes first on a given day, then bank rows in file order
    eventos = []
    uuid_a_folio = {p.uuid: p.folio for p in res.partidas.values() if p.uuid}
    for f in ds.cfdi:
        r = f.datos
        if r["tipo"] == "E" and inicio <= r["fecha"] <= cierre and not r["estatus"] == "Cancelado":
            eventos.append((r["fecha"], 0, f.fuente, "nc", f))
    for i, f in enumerate(ds.banco):
        r = f.datos
        if inicio <= r["fecha"] <= cierre:
            n = f.fuente.split("row ")[-1]
            mov = Movimiento(id=f"B{int(n):02d}", fecha=r["fecha"], cuenta=r["cuenta"], referencia=r["referencia"],
                             descripcion=r["descripcion"], cargo=r["cargo"], abono=r["abono"], moneda=r["moneda"],
                             fuente=f.fuente)
            mov.clase = _clasificar(mov, cfg)
            res.movimientos.append(mov)
            eventos.append((r["fecha"], 1, i, "mov", mov))
    # bank rows (kind 1) carry their numeric position in the file; credit notes (kind 0) keep their source text
    eventos.sort(key=lambda e: (e[0], e[1], e[2] if e[1] == 1 else str(e[2])))

    n_ap = 0
    anulaciones = {str(k).strip().upper(): v for k, v in (decisiones.get("conciliacion") or {}).items()}
    usadas = set()
    for fecha, _, _, tipo, obj in eventos:
        if tipo == "nc":
            _aplicar_nc(res, obj, uuid_a_folio)
            continue
        mov = obj
        if mov.clase in ("comision", "iva_comision"):
            res.comisiones.append(mov)
            continue
        dec = anulaciones.get(mov.id)
        if dec and mov.abono > 0:
            usadas.add(mov.id)
        if mov.clase == "no_cxc" or (mov.clase == "abono_no_cxc" and not dec):
            if mov.cargo > 0:              # a withdrawal that names a customer or an invoice may be a deposit typed wrong
                clave_c, _, ambiguo = ctx.mejor_cliente(mov.descripcion)
                mov.pista = ", ".join(ctx.folios_en(mov.descripcion)) or (
                    res.clientes.get(clave_c, "") if clave_c and not ambiguo else "")
            res.no_cxc.append(mov)
            continue
        abiertas = [p for p in res.partidas.values() if p.saldo > Decimal("0.005") and p.fecha <= mov.fecha]
        if dec:
            prop = _propuesta_manual(res, ctx, mov, dec, abiertas)
            prop.nivel = 3 if prop.tipo == "no_identificado" else 1
            _anotar_aprendizaje(res, ctx, mov, dec, abiertas, prop)
        else:
            prop = proponer(ctx, mov, abiertas)
            sugerida = None
            if sugeridor and prop.tipo == "no_identificado" and prop.modo == "monto":
                clave = sugeridor(mov, dict(res.clientes))
                if clave in res.clientes:
                    alt = proponer(ctx, mov, abiertas, forzar_cliente=clave)
                    if alt.tipo != "no_identificado":
                        sugerida = alt
            prop = _por_nivel(ctx, mov, abiertas, prop, sugerida)
            if sugerida is not None:
                prop.alertas.append("The customer was suggested by a language model; the amounts were matched and "
                                    "computed in code. Nothing is applied until a reviewer accepts it.")
        res.propuestas[mov.id] = prop
        if prop.tipo == "no_cxc":
            res.no_cxc.append(mov)
            continue
        n_ap = _aplicar(res, n_ap, mov, prop, {})

    for mov_id in sorted(set(anulaciones) - usadas):
        res.avisos.append(f"The override of {mov_id} was not used: {mov_id} is not a deposit of this period (it does not "
                          "exist, or it is a withdrawal or a fee). Check the movement id (the B number in A1_Bank_Movements).")
    con_correccion = {c["folio"] for c in res.correcciones_tc}
    for folio in sorted({str(x).strip().upper() for x in decisiones.get("rechazar_correcciones") or []} - con_correccion):
        res.avisos.append(f"The rejection of the rate correction of {folio} was not used: {folio} has no rate correction "
                          "in this period.")

    # period-end revaluation of open foreign-currency items
    for p in res.partidas.values():
        if p.saldo > Decimal("0.005") and p.moneda != "MXN":
            cierre_mxn = q2(p.saldo * fxo.en(cierre))
            res.reval.append({"folio": p.folio, "cliente": p.cliente, "moneda": p.moneda, "saldo": p.saldo,
                              "tc_cierre": fxo.en(cierre), "libro_antes": p.libro_mxn, "cierre_mxn": cierre_mxn,
                              "ajuste": cierre_mxn - p.libro_mxn, "fuentes": p.fuentes})
    return res


def _anotar_aprendizaje(res, ctx, mov, dec, abiertas, prop) -> None:
    """A reviewer placed a receipt whose description carried no invoice number and no customer name, and all of it
    went to one customer: that is something the tool can learn (the pipeline stores it as a rule)."""
    if dec.get("accion") != "asignar" or not prop.items:
        return
    # 'aprendido' too: if a reviewer corrects a receipt a learned rule placed, the newer decision replaces the rule
    if proponer(ctx, mov, abiertas).modo not in ("monto", "aprendido", "parecido"):
        return
    clientes = {p.cliente_clave for p, _, _ in prop.items}
    if len(clientes) != 1:
        return
    p = prop.items[0][0]
    res.aprendizajes.append({"mov_id": mov.id, "descripcion": mov.descripcion, "cliente_clave": p.cliente_clave,
                             "cliente": p.cliente, "usuario": dec.get("usuario", "")})


def _aplicar_nc(res: Resultado, f, uuid_a_folio):
    r = f.datos
    folio_f = uuid_a_folio.get(r["relacionado"] or "")
    p = res.partidas.get(folio_f) if folio_f else None
    if not p:
        res.avisos.append(f"Credit note {r['folio']} is related to a CFDI that is not in the open items ({r['relacionado']}).")
        return
    aplicado = min(r["total"], p.saldo)
    if r["total"] > p.saldo + Decimal("0.01"):
        res.avisos.append(f"Credit note {r['folio']} ({r['total']:,.2f}) is larger than the open balance of {p.folio} "
                          f"({p.saldo:,.2f}) on {r['fecha']}: only the balance was applied. Check the credit note amount "
                          "or whether the invoice was already paid.")
    libro, iva_doc, iva_libro = _relevar(p, aplicado)
    tc = p.tc_libro
    p.saldo -= aplicado
    p.libro_mxn -= libro
    iva_nc = min(r["iva"], p.iva_pend)
    iva_nc_libro = q2(iva_nc * tc) if p.saldo > 0 else p.iva_libro_pend
    p.iva_pend -= iva_nc
    p.iva_libro_pend -= iva_nc_libro
    res.notas_credito.append(NotaCredito(
        folio=r["folio"], uuid=r["uuid"], fecha=r["fecha"], folio_factura=p.folio, cliente=p.cliente,
        subtotal=r["subtotal"], iva=r["iva"], total=r["total"], libro_relevado=libro,
        subtotal_libro=libro - iva_nc_libro, iva_libro=iva_nc_libro, fuentes=[f.fuente] + p.fuentes))
