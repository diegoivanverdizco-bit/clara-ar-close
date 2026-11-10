"""Append-only audit log (JSON lines). One line per event: runs, reviewer decisions, exports, LLM calls."""
from __future__ import annotations
import getpass
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256_archivo(ruta) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


class Bitacora:
    def __init__(self, ruta, usuario: str | None = None):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.usuario = usuario or getpass.getuser()

    def registrar(self, evento: str, **datos) -> dict:
        linea = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "usuario": self.usuario,
                 "evento": evento, **datos}
        with open(self.ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")
        return linea

    def leer(self) -> list:
        """Every readable event. A damaged line (edited by hand, or cut by a crash) is reported as its own event
        instead of stopping the tool: the log is append-only, so the rest is still valid."""
        if not self.ruta.exists():
            return []
        eventos = []
        with open(self.ruta, encoding="utf-8-sig", errors="replace") as f:
            for n, linea in enumerate(f, start=1):
                if not linea.strip():
                    continue
                try:
                    ev = json.loads(linea)
                    if not isinstance(ev, dict):
                        raise ValueError("not an object")
                except ValueError:
                    ev = {"ts": "", "usuario": "", "evento": "damaged_line", "linea": n, "texto": linea.strip()[:200]}
                eventos.append(ev)
        return eventos
