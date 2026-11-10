"""Human-in-the-loop: reviewer decisions persisted next to the output and mirrored in the audit log.

Nothing is final until every item is approved by a named user. Matching overrides are fed back
into the engine, which recomputes all amounts; the reviewer never types an amount.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

ESTADOS = ("pendiente", "aprobado", "rechazado")


class RevisionError(ValueError):
    """The review file cannot be used as it is."""


def clave_match(mov_id: str) -> str:
    return f"match:{mov_id}"


def clave_excepcion(e) -> str:
    return f"exc:{e.categoria}:{e.referencia}"


def clave_poliza(p) -> str:
    return f"pol:{p.tipo}:{p.concepto}"


def _unicas(bases) -> list:
    """Make keys unique and stable: the first item keeps its key, the next ones with the same key get #2, #3...
    in list order (the engine builds the lists in a deterministic order, so the same input gives the same keys)."""
    vistas, salida = {}, []
    for b in bases:
        vistas[b] = vistas.get(b, 0) + 1
        salida.append(b if vistas[b] == 1 else f"{b}#{vistas[b]}")
    return salida


def claves_excepciones(excepciones) -> list:
    """One review key per exception, in the order of the list given (the single place that produces them)."""
    return _unicas([clave_excepcion(e) for e in excepciones])


def claves_polizas(polizas) -> list:
    """One review key per journal entry, in the order of the list given (the single place that produces them)."""
    return _unicas([clave_poliza(p) for p in polizas])


def _validar_forma(d, ruta) -> None:
    ok = (isinstance(d, dict) and isinstance(d.get("items", {}), dict) and isinstance(d.get("conciliacion", {}), dict)
          and isinstance(d.get("rechazar_correcciones", []), list)
          and all(isinstance(v, dict) for v in d.get("items", {}).values()))
    if not ok:
        raise RevisionError(f"{ruta} does not have the expected structure (it was probably edited by hand). Move it aside "
                            "to start the review again; the audit log keeps every decision.")


class Revision:
    """Decisions belong to one input file, identified by its SHA-256 (`huella`).

    If the input changes (another month, an old file, or a corrected cell), the earlier decisions are moved
    aside to revision_<hash>.json, nothing is deleted, and the review starts again: an approval given to
    other data never carries over.
    """

    def __init__(self, ruta, bitacora=None, huella: str | None = None):
        self.ruta = Path(ruta)
        self.bitacora = bitacora
        self.reiniciada = None   # hash of the earlier input, when its decisions were set aside
        self.apartado = None     # file the earlier decisions were moved to
        self.datos = {"version": 1, "huella": huella, "items": {}, "conciliacion": {}, "rechazar_correcciones": []}
        if self.ruta.exists():
            try:
                previos = json.loads(self.ruta.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise RevisionError(f"{self.ruta} is damaged ({e}). Restore it from the audit log or move it aside "
                                    "to start the review again.") from e
            _validar_forma(previos, self.ruta)
            anterior = previos.get("huella")
            tiene = bool(previos.get("items") or previos.get("conciliacion") or previos.get("rechazar_correcciones"))
            # decisions without a file hash are not trusted for a known file: they could belong to any file
            if huella and anterior != huella and (anterior or tiene):
                apartado = self.ruta.with_name(f"revision_{(anterior or 'sin_huella')[:12]}.json")
                self.ruta.replace(apartado)
                self.reiniciada = anterior or "(no hash)"
                self.apartado = apartado.name
                self._log("review_reset", antes=anterior, despues=huella, apartado=apartado.name,
                          decisiones=len(previos.get("items", {})), anulaciones=len(previos.get("conciliacion", {})))
            else:
                self.datos.update(previos)
                self.datos["huella"] = anterior or huella
        self.sin_respaldo = self._sin_respaldo()

    def _sin_respaldo(self) -> list:
        """Cross-check revision.json against the audit log, which is append-only.

        Approvals count only if the LAST decision on that item in the log, for this input file, matches them;
        overrides and rejected corrections must match what the log says was decided since the review started.
        If anything differs (an older revision.json put back, a hash edited by hand, damaged log lines), the
        differences are listed in `discrepancias` and every approval counts as pending."""
        self.discrepancias, self.lineas_danadas = [], 0
        if not self.bitacora:
            return []
        huella = self.datos.get("huella")
        ultima, conc, rechazos = {}, {}, set()
        for e in self.bitacora.leer():
            ev = e.get("evento")
            if ev == "review_reset":                       # another file: the review starts from scratch
                ultima, conc, rechazos = {}, {}, set()
            elif ev == "damaged_line":                     # unreadable history: nothing before it is trusted
                self.lineas_danadas += 1
                ultima = {}
            elif ev == "decision":
                if e.get("huella") == huella:
                    ultima[e.get("item")] = (e.get("despues"), e.get("revisor"), e.get("ts_decision"))
            elif ev in ("override", "override_removed", "correction_rejected", "review_reopened"):
                ultima = {}                                # earlier approvals were reopened here
                mov = str(e.get("movimiento") or "").strip().upper()
                if ev == "override":
                    conc[mov] = (e.get("accion"), [str(f).strip().upper() for f in e.get("folios") or []])
                elif ev == "override_removed":
                    conc.pop(mov, None)
                elif ev == "correction_rejected":
                    rechazos.add(str(e.get("folio") or "").strip().upper())
        actual = {str(k).strip().upper(): (v.get("accion"), [str(f).strip().upper() for f in v.get("folios") or []])
                  for k, v in self.datos["conciliacion"].items()}
        for mov in sorted(set(conc) | set(actual)):
            if conc.get(mov) != actual.get(mov):
                self.discrepancias.append(f"override of {mov}: the audit log says {conc.get(mov) or 'none'}, "
                                          f"revision.json says {actual.get(mov) or 'none'}")
        guardados = {str(x).strip().upper() for x in self.datos["rechazar_correcciones"]}
        for folio in sorted(rechazos ^ guardados):
            self.discrepancias.append(f"rejected correction of {folio}: in the "
                                      + ("audit log but not in revision.json" if folio in rechazos else
                                         "revision.json but not in the audit log"))
        aprobadas = [c for c, v in self.datos["items"].items() if v.get("estado") == "aprobado"]
        if self.discrepancias:
            return aprobadas
        return [c for c in aprobadas
                if ultima.get(c) != (self.datos["items"][c].get("estado"), self.datos["items"][c].get("usuario"),
                                     self.datos["items"][c].get("ts"))]

    def _guardar(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps(self.datos, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log(self, evento, **kw):
        if self.bitacora:
            self.bitacora.registrar(evento, **kw)

    def decidir(self, clave: str, estado: str, usuario: str, comentario: str = "") -> None:
        if estado not in ESTADOS:
            raise RevisionError(f"estado must be one of {ESTADOS}")
        if not usuario.strip():
            raise RevisionError("A reviewer name is required")
        anterior = self.datos["items"].get(clave, {}).get("estado", "pendiente")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.datos["items"][clave] = {"estado": estado, "usuario": usuario, "comentario": comentario, "ts": ts}
        self._guardar()
        self._log("decision", item=clave, antes=anterior, despues=estado, comentario=comentario, revisor=usuario,
                  ts_decision=ts, huella=self.datos.get("huella"))
        if clave in self.sin_respaldo and not self.discrepancias:
            self.sin_respaldo.remove(clave)

    def anular_conciliacion(self, mov_id: str, accion: str, usuario: str, folios=None, comentario: str = "") -> None:
        """Override how a bank movement is applied: assign to given invoices, mark not-AR, or leave unidentified."""
        if accion not in ("asignar", "no_cxc", "no_identificado"):
            raise RevisionError("accion must be asignar, no_cxc or no_identificado")
        if not (usuario or "").strip():
            raise RevisionError("A reviewer name is required")
        mov_id = str(mov_id).strip().upper()
        folios = [str(f).strip().upper() for f in (folios or []) if str(f).strip()]
        if accion == "asignar" and not folios:
            raise RevisionError("Give at least one invoice folio to assign to")
        self.datos["conciliacion"][mov_id] = {"accion": accion, "folios": folios, "usuario": usuario,
                                              "comentario": comentario}
        self._reabrir("override " + mov_id)
        self._guardar()
        self._log("override", movimiento=mov_id, accion=accion, folios=list(folios or []), comentario=comentario,
                  revisor=usuario)
        self.sin_respaldo = self._sin_respaldo()

    def quitar_anulacion(self, mov_id: str, usuario: str) -> None:
        if self.datos["conciliacion"].pop(mov_id, None) is not None:
            self._reabrir("override removed " + mov_id)
            self._guardar()
            self._log("override_removed", movimiento=mov_id, revisor=usuario)
            self.sin_respaldo = self._sin_respaldo()

    def reabrir(self, motivo: str) -> None:
        """Something outside this file changed the figures (e.g. a learned rule was removed): approvals go back to pending."""
        self._reabrir(motivo)
        self._guardar()

    def _reabrir(self, motivo: str) -> None:
        """An override or a rejected correction can move other receipts, exceptions and entries, so every approval
        goes back to pending (approvals carry no amounts, so a stale one could otherwise survive)."""
        n = sum(1 for v in self.datos["items"].values() if v.get("estado") == "aprobado")
        self.datos["items"] = {k: v for k, v in self.datos["items"].items() if v.get("estado") != "aprobado"}
        self._log("review_reopened", motivo=motivo, aprobaciones=n)

    def rechazar_correccion(self, folio: str, usuario: str, comentario: str = "") -> None:
        folio = str(folio).strip().upper()
        # logged even if it is already in the list, so a revision.json that disagrees with the log can be repaired
        if folio not in self.datos["rechazar_correcciones"]:
            self.datos["rechazar_correcciones"].append(folio)
        self._reabrir("correction rejected " + folio)
        self._guardar()
        self._log("correction_rejected", folio=folio, revisor=usuario, comentario=comentario)
        self.sin_respaldo = self._sin_respaldo()

    def estado(self, clave: str) -> str:
        if clave in self.sin_respaldo:
            return "pendiente"
        return self.datos["items"].get(clave, {}).get("estado", "pendiente")

    def como_decisiones(self) -> dict:
        return {"conciliacion": self.datos["conciliacion"], "rechazar_correcciones": self.datos["rechazar_correcciones"],
                "conciliaciones_rechazadas": sorted(c.split(":", 1)[1] for c, v in self.datos["items"].items()
                                                    if c.startswith("match:") and v.get("estado") == "rechazado")}

    def pendientes(self, claves) -> list:
        return [c for c in claves if self.estado(c) != "aprobado"]
