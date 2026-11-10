"""Learned rules: when a reviewer places a receipt whose description had no invoice number and no customer name,
the tool remembers "this description belongs to this customer". Next time the same payer shows up, the receipt is
identified by that rule (still matched and computed in code, never applied on the rule alone if no amount fits).

Rules live in a JSON file next to the output (reglas_aprendidas.json). They are never deleted: a removed or
replaced rule is marked inactive, and every change is written to the audit log.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from .conciliar import tokens_descripcion
from .revision import RevisionError

# words that say how the money moved, not who sent it: a rule made only of these would catch every deposit
NO_DISTINTIVOS = {
    "CLIENTE", "CLIENTES", "EFECTIVO", "CHEQUE", "BANCO", "VENTANILLA", "SUCURSAL", "CONCEPTO", "ORDENANTE",
    "CTA", "CUENTA", "NUM", "NUMERO", "FOLIO", "DEP", "TRANSF", "OPERACION", "ENVIO", "RECEPCION", "INTERBANCARIA",
    "BBVA", "BANCOMER", "BANAMEX", "CITIBANAMEX", "SANTANDER", "BANORTE", "HSBC", "SCOTIABANK", "INBURSA", "AZTECA",
    "BANREGIO", "BANBAJIO", "BAJIO", "AFIRME", "MONEX", "INTERCAM", "CIBANCO", "BANSI", "MIFEL", "STP",
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO", "SEPTIEMBRE", "SETIEMBRE", "OCTUBRE",
    "NOVIEMBRE", "DICIEMBRE", "MES", "QNA", "QUINCENA", "LIQUIDACION", "SALDO", "TOTAL", "COBRO", "VENTA", "SERVICIO",
    "SERVICIOS", "INCOMING", "PAYMENT", "FROM", "THE", "FOR",
}


def tokens_distintivos(texto: str) -> list:
    """Words of a bank description that can identify the payer: no bank jargon, no numbers or references."""
    return sorted({t for t in tokens_descripcion(texto)
                   if t not in NO_DISTINTIVOS and not any(c.isdigit() for c in t)})


class Reglas:
    def __init__(self, ruta, bitacora=None):
        self.ruta = Path(ruta)
        self.bitacora = bitacora
        self.reglas = []
        if self.ruta.exists():
            try:
                datos = json.loads(self.ruta.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise RevisionError(f"{self.ruta} is damaged ({e}). Move it aside to start without learned rules; "
                                    "the audit log keeps every rule that was learned.") from e
            ok = isinstance(datos, dict) and isinstance(datos.get("reglas"), list) and all(
                isinstance(r, dict) and isinstance(r.get("tokens"), list) and r.get("cliente_clave") and r.get("id")
                and r.get("cliente") and r.get("movimiento") for r in datos["reglas"])
            if not ok:
                raise RevisionError(f"{self.ruta} does not have the expected structure (edited by hand?). Move it aside "
                                    "to start without learned rules.")
            self.reglas = datos["reglas"]
        self.discrepancias = self._cotejar()

    def _cotejar(self) -> list:
        """reglas_aprendidas.json against the audit log (append-only): a rule whose words or customer were edited, or a
        removed rule switched back on by hand, would place money silently. If anything differs, no rule is used."""
        if not self.bitacora:
            return []
        esperado = {}
        for e in self.bitacora.leer():
            ev, rid = e.get("evento"), e.get("regla")
            if ev == "rule_learned":
                esperado[rid] = {"tokens": sorted(str(e.get("palabras") or "").split()),
                                 "cliente": e.get("cliente_clave") or e.get("cliente"), "activa": True}
            elif ev in ("rule_replaced", "rule_removed", "rule_retired") and rid in esperado:
                esperado[rid]["activa"] = False
        difs = []
        for r in self.reglas:
            e = esperado.get(r["id"])
            cliente = r["cliente_clave"] if e and e["cliente"] == r["cliente_clave"] else r["cliente"]
            if not e:
                difs.append(f"rule {r['id']} is not in the audit log")
            elif e["tokens"] != sorted(r["tokens"]) or e["cliente"] != cliente:
                difs.append(f"rule {r['id']} has other words or another customer than the audit log says")
            elif e["activa"] != r.get("activa", True):
                difs.append(f"rule {r['id']} is " + ("active" if r.get("activa", True) else "inactive")
                            + " but the audit log says the opposite")
        return difs

    def activas(self) -> list:
        if getattr(self, "discrepancias", None):
            return []
        return [r for r in self.reglas if r.get("activa", True)]

    def retirar(self, huella: str, archivo: str, decisiones: dict) -> list:
        """A rule learned from a reviewer's override of THIS file is retired when that override is removed or the
        reviewer rejects that match: the undo must undo the rule too."""
        vigentes = {str(k).strip().upper() for k, v in (decisiones.get("conciliacion") or {}).items()
                    if (v or {}).get("accion", "asignar") == "asignar"}
        rechazadas = {str(k).strip().upper() for k in (decisiones.get("conciliaciones_rechazadas") or [])}
        retiradas = []
        for r in self.activas():
            de_este = r.get("huella") == huella if r.get("huella") else r.get("archivo") == archivo
            mov = str(r["movimiento"]).strip().upper()
            if de_este and (mov not in vigentes or mov in rechazadas):
                r["activa"] = False
                r["retirada"] = "override removed or changed" if mov not in vigentes else "match rejected"
                self._log("rule_retired", regla=r["id"], motivo=r["retirada"], movimiento=mov)
                retiradas.append(r)
        if retiradas:
            self._guardar()
        return retiradas

    def buscar(self, texto: str):
        """Return (rule, conflict). The most specific active rule whose words all appear in the description.
        If rules for different customers fit, none is used (conflict=True): a person decides."""
        dt = set(tokens_distintivos(texto))
        hits = [r for r in self.activas() if r["tokens"] and set(r["tokens"]) <= dt]
        if not hits:
            return None, False
        if len({r["cliente_clave"] for r in hits}) > 1:
            return None, True
        return max(hits, key=lambda r: len(r["tokens"])), False

    def palabras_nuevas(self, texto: str, regla: dict) -> list:
        """Identifying words of the description that the rule never saw."""
        return sorted(set(tokens_distintivos(texto)) - set(regla["tokens"]))

    def _guardar(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps({"version": 1, "reglas": self.reglas}, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def _log(self, evento, **kw):
        if self.bitacora:
            self.bitacora.registrar(evento, **kw)

    def aprender(self, aprendizajes: list, archivo: str = "", huella: str = "") -> tuple:
        """Store what reviewers decided. Returns (new rules, notices)."""
        nuevas, avisos = [], []
        for a in aprendizajes:
            toks = tokens_distintivos(a["descripcion"])
            if not toks:
                avisos.append(f"{a['mov_id']}: the description '{a['descripcion']}' has no word that identifies the payer, "
                              "so no rule was learned from the reviewer's decision (it would catch unrelated deposits).")
                continue
            iguales = [r for r in self.activas() if r["tokens"] == toks]
            if any(r["cliente_clave"] == a["cliente_clave"] for r in iguales):
                continue
            # a person removed this exact rule: the override that taught it must not bring it back
            if any(r["tokens"] == toks and r["cliente_clave"] == a["cliente_clave"]
                   and (r.get("quitada_por") or r.get("retirada") == "match rejected") for r in self.reglas):
                continue
            rid = f"R{len(self.reglas) + 1:02d}"
            for r in iguales:              # the reviewer now says another customer: the newer decision wins
                r["activa"] = False
                r["reemplazada_por"] = rid
                self._log("rule_replaced", regla=r["id"], por=rid)
            regla = {"id": rid, "tokens": toks, "cliente_clave": a["cliente_clave"], "cliente": a["cliente"],
                     "usuario": a.get("usuario") or "", "movimiento": a["mov_id"], "descripcion": a["descripcion"],
                     "archivo": archivo, "huella": huella, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "activa": True}
            self.reglas.append(regla)
            nuevas.append(regla)
            self._log("rule_learned", regla=rid, palabras=" ".join(toks), cliente=a["cliente"],
                      cliente_clave=a["cliente_clave"], movimiento=a["mov_id"],
                      revisor=regla["usuario"])
        if nuevas:
            self._guardar()
        return nuevas, avisos

    def quitar(self, rid: str, usuario: str, comentario: str = "") -> dict:
        rid = str(rid).strip().upper()
        regla = next((r for r in self.reglas if r["id"] == rid), None)
        if not regla:
            raise RevisionError(f"There is no learned rule {rid}. List them with: python -m cierre_cxc rules")
        if regla.get("activa", True):
            regla["activa"] = False
            regla["quitada_por"] = usuario
            self._guardar()
            self._log("rule_removed", regla=rid, revisor=usuario, comentario=comentario)
        return regla
