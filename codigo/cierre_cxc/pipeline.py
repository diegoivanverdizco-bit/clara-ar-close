"""One call that runs the whole close and verifies that the pieces tie out."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .auditoria import Bitacora, sha256_archivo
from .cargar import cargar, SchemaError
from .config import Config
from .fx import TipoCambioFaltante
from .contabilidad import generar_polizas, antiguedad, rollforward, resumen_iva
from .dinero import CERO, q2
from .excepciones import detectar
from .motor import correr


@dataclass
class Salida:
    res: object
    excepciones: list
    polizas: list
    aging: dict
    rollforward: dict
    iva: dict
    controles: list = field(default_factory=list)   # [(name, ok, detail)]
    huella: str = ""
    archivo: str = ""
    reglas: list = field(default_factory=list)          # active learned rules after this run
    reglas_nuevas: list = field(default_factory=list)   # rules learned in this run
    reglas_retiradas: list = field(default_factory=list)  # rules retired because their override was undone

    @property
    def controles_ok(self) -> bool:
        return all(ok for _, ok, _ in self.controles)


def verificar(res, polizas, aging, rf, iva) -> list:
    c = []
    desc = [p.id for p in polizas if not p.cuadra]
    c.append(("Every journal entry balances (debits = credits)", not desc, f"unbalanced: {desc}" if desc else "ok"))
    c.append(("Roll-forward closing = corrected aging total", rf["cierre_calculado"] == aging["total"],
              f"{rf['cierre_calculado']} vs {aging['total']}"))
    c.append(("IVA por trasladar roll-forward = sum of open items", iva["cierre_calculado"] == iva["cierre"],
              f"{iva['cierre_calculado']} vs {iva['cierre']}"))
    # every bank credit is either applied, unidentified, or classified as not AR
    recibido = sum((a.mxn_recibido for a in res.aplicaciones), CERO) + sum((u["mxn"] for u in res.efectivo_no_identificado), CERO)
    esperado = sum((q2(m.abono) if m.moneda == "MXN" else q2(m.abono * res.fx.en(m.fecha))
                    for m in res.movimientos if m.clase == "cobro" and m.id not in {x.id for x in res.no_cxc}), CERO)
    c.append(("Every AR bank credit is applied or held as unidentified", abs(recibido - esperado) <= Decimal("0.05"),
              f"{recibido} vs {esperado}"))
    sobre = [a.folio for a in res.aplicaciones if a.aplicado_doc > a.saldo_antes + Decimal("0.01")]
    c.append(("No application exceeds the open balance", not sobre, f"{sobre}" if sobre else "ok"))
    neg = [p.folio for p in res.partidas.values() if p.saldo < -Decimal("0.005")]
    c.append(("No invoice has a negative balance", not neg, f"{neg}" if neg else "ok"))
    cobro = sum((a.libro_relevado for a in res.aplicaciones), CERO)
    c.append(("Collections in entries = AR relieved in roll-forward",
              any(abs(v + cobro) <= Decimal("0.01") for k, v in rf["lineas"] if k.startswith("- Collections")), "ok"))
    return c


def _calcular(ruta, cfg, decisiones, sugeridor, reglas) -> Salida:
    ds = cargar(ruta)
    res = None
    try:
        res = correr(ds, cfg, decisiones, sugeridor=sugeridor, reglas=reglas)
        exc = detectar(res)
        pol = generar_polizas(res)
        ag = antiguedad(res)
        rf = rollforward(res)
        iva = resumen_iva(res)
    except TipoCambioFaltante as e:
        cuando = f" of {res.inicio:%B %Y}" if res else ""
        raise SchemaError(f"Exchange-rate table is missing a date the close{cuando} needs: {e}. Add the FIX rates "
                          "for that date, or check that the file and the period are the right ones.") from e
    return Salida(res=res, excepciones=exc, polizas=pol, aging=ag, rollforward=rf, iva=iva,
                  controles=verificar(res, pol, ag, rf, iva), huella=sha256_archivo(ruta), archivo=Path(ruta).name)


def ejecutar(ruta, cfg: Optional[Config] = None, decisiones: Optional[dict] = None, bitacora: Optional[Bitacora] = None,
             sugeridor=None, reglas=None) -> Salida:
    """`reglas` (aprendizaje.Reglas): learned rules used to identify payers, and where new ones are stored."""
    cfg = cfg or Config()
    decisiones = decisiones or {}
    retiradas = []
    if reglas is not None and not reglas.discrepancias:
        retiradas = reglas.retirar(sha256_archivo(ruta), Path(ruta).name, decisiones)
    out = _calcular(ruta, cfg, decisiones, sugeridor, reglas)
    if reglas is not None:
        if reglas.discrepancias:
            out.res.avisos.append("Learned rules are NOT used: reglas_aprendidas.json does not match the audit log ("
                                  + "; ".join(reglas.discrepancias) + "). Move the file aside to start without rules.")
            nuevas = []
        else:
            # a reviewer who rejected the match of a receipt is not teaching anything with it
            rechazadas = {str(m).strip().upper() for m in decisiones.get("conciliaciones_rechazadas") or []}
            vale = lambda o: [a for a in o.res.aprendizajes if str(a["mov_id"]).upper() not in rechazadas]
            nuevas, _ = reglas.aprender(vale(out), out.archivo, out.huella)
            if nuevas:
                # a new rule can place other receipts of the same payer: recompute now, so what the reviewer sees and
                # approves already includes its effect (a later run never moves an approved receipt)
                out = _calcular(ruta, cfg, decisiones, sugeridor, reglas)
            _, avisos = reglas.aprender(vale(out), out.archivo, out.huella)
            out.res.avisos.extend(avisos)
        for r in retiradas:
            out.res.avisos.append(f"Learned rule {r['id']} ('{' '.join(r['tokens'])}' -> {r['cliente']}) was retired: the "
                                  f"reviewer's decision on {r['movimiento']} that taught it was {r['retirada'].split()[-1]}.")
        out.reglas_nuevas, out.reglas, out.reglas_retiradas = nuevas, reglas.activas(), retiradas
    res, exc, pol = out.res, out.excepciones, out.polizas
    if bitacora:
        bitacora.registrar("run", archivo=out.archivo, sha256=out.huella, cierre=str(res.cierre),
                           aplicaciones=len(res.aplicaciones), excepciones=len(exc), polizas=len(pol),
                           controles_ok=out.controles_ok, correcciones_aplicadas=cfg.aplicar_correcciones,
                           anulaciones=list((decisiones.get("conciliacion") or {}).keys()))
    return out
