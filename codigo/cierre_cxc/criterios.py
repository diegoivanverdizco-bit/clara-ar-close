"""Reviewer criteria learned from the AI draft: when the reviewer answers a judgment question, or rejects a suggestion
with a reason, the tool remembers it and the next draft must follow it ("A-1012 IVA rate: 0 % is not 'exento'").

Criteria only steer the wording of drafts: they never move money (the draft's numbers are still checked against the
close). They live in criterios_aprendidos.json next to the output, are never deleted (a removed criterion is marked
inactive) and every change is written to the audit log.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class Criterios:
    def __init__(self, ruta, bitacora=None):
        self.ruta = Path(ruta)
        self.bitacora = bitacora
        self.criterios = []
        if self.ruta.exists():
            try:
                datos = json.loads(self.ruta.read_text(encoding="utf-8-sig"))
                if isinstance(datos, dict) and isinstance(datos.get("criterios"), list):
                    self.criterios = [c for c in datos["criterios"] if isinstance(c, dict) and c.get("id") and c.get("texto")]
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.criterios = []          # a damaged file only loses wording hints, never money: start clean

    def activos(self) -> list:
        return [c for c in self.criterios if c.get("activo", True)]

    def textos(self) -> list:
        return [c["texto"] for c in self.activos()]

    def aprender(self, categoria: str, texto: str, origen: str, usuario: str) -> dict | None:
        texto = " ".join(str(texto or "").split())
        if not texto or not (usuario or "").strip():
            return None
        for c in self.activos():
            if c["texto"].lower() == texto.lower():
                return c
        c = {"id": f"K{len(self.criterios) + 1:02d}", "categoria": categoria, "texto": texto, "origen": origen,
             "usuario": usuario, "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"), "activo": True}
        self.criterios.append(c)
        self._guardar()
        if self.bitacora:
            self.bitacora.registrar("criterio_aprendido", criterio=c["id"], categoria=categoria, texto=texto,
                                    origen=origen)
        return c

    def quitar(self, cid: str, usuario: str) -> bool:
        for c in self.criterios:
            if c["id"] == cid and c.get("activo", True):
                c["activo"] = False
                c["retirado_por"] = usuario
                self._guardar()
                if self.bitacora:
                    self.bitacora.registrar("criterio_retirado", criterio=cid, texto=c["texto"])
                return True
        return False

    def _guardar(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps({"version": 1, "criterios": self.criterios}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
