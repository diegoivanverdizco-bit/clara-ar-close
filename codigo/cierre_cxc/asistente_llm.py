"""Optional language-model assistance, fenced in so it can never change an amount.

What the model may do:
  1. Suggest which customer a vague bank description refers to (only for receipts the deterministic
     matcher left unidentified). The answer must be one of the known customer keys, otherwise it is ignored.
     The suggested customer only narrows the candidate invoices; amounts are then matched and computed in code,
     the result is capped at 'media' confidence and a reviewer must approve it.
  2. Rewrite an exception explanation in plainer language. Every number in the rewrite must already appear in
     the facts it was given, otherwise the original deterministic text is kept.

What it never receives: bank account numbers, references, invoice amounts or UUIDs. What it never does: compute,
round, post or approve anything. Every call is written to the audit log.
"""
from __future__ import annotations
import hashlib
import json
import os
import re

MODELO_DEFECTO = "claude-sonnet-5-5"

SISTEMA_CLIENTE = (
    "You match a bank transfer description to a customer. You are given the description and a list of "
    "customers with ids. Reply with ONLY a JSON object {\"cliente_id\": <one id from the list or null>, "
    "\"motivo\": <short reason>}. Answer null unless the description clearly refers to one customer. "
    "Never invent an id."
)
SISTEMA_TEXTO = (
    "Rewrite the finding below in two plain sentences for a controller. Use only the facts and numbers given. "
    "Do not add or change any number. Reply with the text only."
)


def _cliente_anthropic():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception:
        return None


def _llamar(cliente, modelo, sistema, usuario, max_tokens=200) -> str:
    r = cliente.messages.create(model=modelo, max_tokens=max_tokens, temperature=0, system=sistema,
                                messages=[{"role": "user", "content": usuario}])
    return "".join(getattr(b, "text", "") for b in r.content).strip()


def crear_sugeridor(bitacora=None, cliente=None, modelo: str | None = None):
    """Return `sugeridor(mov, clientes) -> customer key | None` for the engine."""
    cliente = cliente or _cliente_anthropic()
    modelo = modelo or os.environ.get("CIERRE_LLM_MODEL", MODELO_DEFECTO)

    def log(**kw):
        if bitacora:
            bitacora.registrar("llm_call", **kw)

    if cliente is None:
        log(uso="cliente", disponible=False, nota="No ANTHROPIC_API_KEY: deterministic matching only.")
        return lambda mov, clientes: None

    def sugeridor(mov, clientes: dict):
        ids = {f"C{i + 1}": k for i, k in enumerate(clientes)}
        lista = [{"id": i, "nombre": clientes[k]} for i, k in ids.items()]
        usuario = json.dumps({"descripcion": mov.descripcion, "clientes": lista}, ensure_ascii=False)
        h = hashlib.sha256(usuario.encode()).hexdigest()[:12]
        try:
            texto = _llamar(cliente, modelo, SISTEMA_CLIENTE, usuario, 120)
            m = re.search(r"\{.*\}", texto, re.S)
            datos = json.loads(m.group(0)) if m else {}
            elegido = ids.get(datos.get("cliente_id"))
            log(uso="cliente", modelo=modelo, prompt_sha=h, movimiento=mov.id, respuesta=texto[:300],
                aceptado=elegido is not None)
            return elegido
        except Exception as e:
            log(uso="cliente", modelo=modelo, prompt_sha=h, movimiento=mov.id, error=str(e)[:200], aceptado=False)
            return None

    return sugeridor


def _numeros(texto: str) -> set:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", texto)}


def redactar_explicacion(hecho: str, bitacora=None, cliente=None, modelo: str | None = None) -> str:
    """Plain-language rewrite; falls back to `hecho` unless every number in the rewrite appears in `hecho`."""
    cliente = cliente or _cliente_anthropic()
    modelo = modelo or os.environ.get("CIERRE_LLM_MODEL", MODELO_DEFECTO)
    if cliente is None:
        return hecho
    try:
        texto = _llamar(cliente, modelo, SISTEMA_TEXTO, hecho, 220)
    except Exception as e:
        if bitacora:
            bitacora.registrar("llm_call", uso="texto", modelo=modelo, error=str(e)[:200], aceptado=False)
        return hecho
    ok = _numeros(texto) <= _numeros(hecho)
    if bitacora:
        bitacora.registrar("llm_call", uso="texto", modelo=modelo, prompt_sha=hashlib.sha256(hecho.encode()).hexdigest()[:12],
                           aceptado=ok, motivo_rechazo=None if ok else "the rewrite contains a number not in the facts")
    return texto if ok else hecho
