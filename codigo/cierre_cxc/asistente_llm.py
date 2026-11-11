"""Optional language-model assistance, fenced in so it can never change an amount.

What the model may do:
  1. Suggest which customer a vague bank description refers to (only for receipts the deterministic
     matcher left unidentified). The answer must be one of the known customer keys, otherwise it is ignored.
     The suggested customer only narrows the candidate invoices; amounts are then matched and computed in code,
     the result is capped at 'media' confidence and a reviewer must approve it.
  2. Rewrite an exception explanation in plainer language. Every number in the rewrite must already appear in
     the facts it was given, otherwise the original deterministic text is kept.

  3. Draft the close write-up (summary for the Controller, each high exception in plain words, and one email per
     owner) from the facts the engine already computed. Every number and every folio in the draft must appear in
     those facts, otherwise the whole draft is replaced by the deterministic template. It is a draft: a person
     approves it.

What it never receives: bank account numbers, bank references or UUIDs (uses 1 and 2 never receive invoice amounts;
use 3 receives the computed figures it has to write about). What it never does: compute, round, post or approve
anything. Every call is written to the audit log.
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


def _llamar(cliente, modelo, sistema, usuario, max_tokens=200, esquema=None) -> str:
    # No sampling parameters: current models reject `temperature`. Determinism comes from the fences (a known id,
    # numbers checked against the facts), not from the sampler. Low effort: short drafting, answered fast.
    r = cliente.messages.create(model=modelo, max_tokens=max_tokens, system=sistema,
                                output_config={"effort": "low", **({"format": {"type": "json_schema", "schema": esquema}}
                                                                   if esquema else {})},
                                messages=[{"role": "user", "content": usuario}])
    if getattr(r, "stop_reason", None) == "refusal":
        raise ValueError("the model declined the request")
    return "".join(b.text for b in r.content if getattr(b, "type", "text") == "text" and hasattr(b, "text")).strip()


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


# ---------------------------------------------------------------------------------------------------------------
# 3. Draft of the close write-up, fenced by a number-and-folio check against the computed facts
# ---------------------------------------------------------------------------------------------------------------
SISTEMA_BORRADOR = (
    "Eres analista de cuentas por cobrar en México. Recibes en JSON los hechos de un cierre mensual, ya calculados "
    "por el sistema. Redacta en español neutral y profesional. Devuelve SOLO un objeto JSON con esta forma: "
    "{\"resumen\": texto de máximo 120 palabras para el Controller, "
    "\"excepciones\": [{\"id\": id de la excepción, \"referencia\": su referencia, \"explicacion\": máximo dos "
    "frases simples: qué pasa y qué se hace}] solo para las de severidad alta, "
    "\"correos\": [{\"para\": el responsable tal como viene en los hechos, \"asunto\": texto, \"cuerpo\": texto}] "
    "un correo por responsable con sus acciones y fechas límite}. "
    "Reglas: usa solo cifras, fechas y folios que aparecen en los hechos, escritos igual (montos con comas y dos "
    "decimales, como 1,234.56). No calcules, no sumes, no redondees. No cites artículos ni reglas fiscales. Escribe "
    "las cantidades pequeñas con letra (dos, tres). Si no sabes algo, no lo menciones."
)

_TEXTO = {"type": "string"}
ESQUEMA_BORRADOR = {
    "type": "object", "additionalProperties": False, "required": ["resumen", "excepciones", "correos"],
    "properties": {
        "resumen": _TEXTO,
        "excepciones": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "referencia", "explicacion"],
            "properties": {"id": _TEXTO, "referencia": _TEXTO, "explicacion": _TEXTO}}},
        "correos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["para", "asunto", "cuerpo"],
            "properties": {"para": _TEXTO, "asunto": _TEXTO, "cuerpo": _TEXTO}}},
    },
}

_FOLIO = re.compile(r"\b(?:[A-Z]{1,3}-\d{2,6}|B\d{2,4}|E\d{2,3}|P\d{2,3})\b")
_NUMERO = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _valores(texto: str) -> set:
    from decimal import Decimal, InvalidOperation
    out = set()
    for n in _NUMERO.findall(texto):
        try:
            out.add(Decimal(n.replace(",", "")).normalize())
        except InvalidOperation:
            pass
    return out


def hechos_cierre(s) -> dict:
    """The computed facts the draft may talk about. No UUIDs, bank accounts or bank references."""
    from .familias import FAMILIA, RESPONSABLE
    from .exportar import conteo_niveles
    res = s.res
    altas = [e for e in s.excepciones if e.severidad == "alta"]
    return {
        "periodo": {"inicio": str(res.inicio), "cierre": str(res.cierre)},
        "cifras": {"cuentas_por_cobrar_mxn": str(s.aging["total"]), "iva_cobrado_mxn": str(s.iva["cobrado"]),
                   "iva_por_trasladar_cierre_mxn": str(s.iva["cierre"])},
        "controles": "todos cuadran" if s.controles_ok else "fallan: " + ", ".join(n for n, ok, _ in s.controles if not ok),
        "excepciones_total": len(s.excepciones), "excepciones_altas": len(altas),
        "cobros_por_nivel": dict(conteo_niveles(s)),
        "plazos": {"REP": "día 5 del mes siguiente al pago", "declaracion_IVA": "día 17 del mes siguiente"},
        "tasa_general_iva_pct": "16",
        "cobros_sin_aplicar": [{"movimiento": u["mov_id"], "fecha": str(u["fecha"]), "monto": str(u["monto"]),
                                "moneda": u["moneda"], "descripcion": u["descripcion"], "nivel": u.get("nivel"),
                                "candidatas": [c["folio"] for c in u.get("candidatos", [])]}
                               for u in res.efectivo_no_identificado],
        "polizas_condicionadas": [{"id": pz.id, "concepto": pz.concepto} for pz in s.polizas if pz.estado == "condicionada"],
        "excepciones": [{"id": e.id, "severidad": e.severidad, "categoria": e.categoria,
                         "familia": FAMILIA.get(e.categoria, "otra"), "referencia": e.referencia,
                         "monto_mxn": str(e.monto_mxn), "responsable": RESPONSABLE.get(e.responsable, e.responsable),
                         "fecha_limite": str(e.fecha_limite), "que_pasa": e.que, "accion": e.accion}
                        for e in s.excepciones],
    }


def _m(x) -> str:
    from decimal import Decimal
    return f"{Decimal(x):,.2f}"


def borrador_plantilla(s, h=None) -> dict:
    """Deterministic draft, used without an API key or when the model's draft fails the check."""
    from .familias import FRASE, PORQUE
    h = h or hechos_cierre(s)
    altas = [e for e in h["excepciones"] if e["severidad"] == "alta"]
    expl = []
    for e in altas:
        frase = FRASE.get(e["categoria"], e["que_pasa"]).format(refs=e["referencia"], monto=_m(e["monto_mxn"]))
        expl.append({"id": e["id"], "referencia": e["referencia"], "familia": e["familia"],
                     "explicacion": frase + (" Importa porque " + PORQUE[e["categoria"]] if e["categoria"] in PORQUE else "")})
    principales = sorted(altas, key=lambda e: -float(e["monto_mxn"]))[:3]
    resumen = (f"Cierre del {h['periodo']['inicio']} al {h['periodo']['cierre']}: cuentas por cobrar de "
               f"{_m(h['cifras']['cuentas_por_cobrar_mxn'])} MXN e IVA cobrado de {_m(h['cifras']['iva_cobrado_mxn'])} "
               f"MXN. Controles: {h['controles']}. Hay {h['excepciones_total']} excepciones, {h['excepciones_altas']} de "
               "prioridad alta" + (": " + "; ".join(f"{e['categoria']} ({e['referencia']}, {_m(e['monto_mxn'])})"
                                                   for e in principales) if principales else "") + ".")
    por_resp = {}
    for e, x in zip(altas, expl):
        por_resp.setdefault(e["responsable"], []).append(f"- {x['explicacion'].split(' Importa porque')[0]} "
                                                         f"Fecha límite: {e['fecha_limite']}.")
    correos = [{"para": r, "asunto": f"Pendientes del cierre al {h['periodo']['cierre']} ({len(ls)})",
                "cuerpo": "Hola,\n\nDel cierre de cuentas por cobrar quedan estos pendientes a tu cargo:\n"
                          + "\n".join(ls) + "\n\nGracias."}
               for r, ls in por_resp.items()]
    return {"resumen": resumen, "excepciones": expl, "correos": correos}


def _verificar(borrador: dict, h: dict):
    """Every folio and number in the draft must appear in the facts. Returns (ok, reason, n_numbers, n_folios)."""
    textos = [borrador.get("resumen", "")]
    textos += [x.get("explicacion", "") for x in borrador.get("excepciones", [])]
    textos += [c.get(k, "") for c in borrador.get("correos", []) for k in ("para", "asunto", "cuerpo")]
    texto = "\n".join(str(x) for x in textos)
    hechos_txt = json.dumps(h, ensure_ascii=False)
    folios = set(_FOLIO.findall(texto))
    ajenos = sorted(f for f in folios if f not in hechos_txt)
    if ajenos:
        return False, "folio(s) not in the close: " + ", ".join(ajenos), 0, len(folios)
    sin_folios = _FOLIO.sub(" ", texto)
    nums = _valores(sin_folios)
    permitidos = _valores(_FOLIO.sub(" ", hechos_txt)) | _valores(hechos_txt)
    inventados = sorted(nums - permitidos)
    if inventados:
        return False, "number(s) not in the close: " + ", ".join(str(n) for n in inventados), len(nums), len(folios)
    return True, "", len(nums), len(folios)


def redactar_cierre(s, bitacora=None, cliente=None, modelo: str | None = None) -> dict:
    """Draft of the close write-up. The model writes; the code checks; a person approves."""
    h = hechos_cierre(s)
    cliente = cliente or _cliente_anthropic()
    modelo = modelo or os.environ.get("CIERRE_LLM_MODEL", MODELO_DEFECTO)

    def salida(origen, aceptado, motivo, b, nn=0, nf=0):
        if bitacora:
            bitacora.registrar("llm_draft", modelo=modelo if cliente else None, origen=origen, aceptado=aceptado,
                               motivo=motivo or None, numeros=nn, folios=nf,
                               prompt_sha=hashlib.sha256(json.dumps(h, ensure_ascii=False).encode()).hexdigest()[:12])
        return {**b, "origen": origen, "aceptado": aceptado, "motivo": motivo,
                "verificados": {"numeros": nn, "folios": nf}, "modelo": modelo if cliente else None}

    if cliente is None:
        return salida("plantilla", False, "Sin clave de API: se usó la plantilla.", borrador_plantilla(s, h))
    try:
        texto = _llamar(cliente, modelo, SISTEMA_BORRADOR, json.dumps(h, ensure_ascii=False), 8000, ESQUEMA_BORRADOR)
        m = re.search(r"\{.*\}", texto, re.S)
        b = json.loads(m.group(0)) if m else None
        if not isinstance(b, dict) or not isinstance(b.get("resumen"), str):
            raise ValueError("the reply is not the expected JSON")
    except Exception as e:
        return salida("plantilla", False, f"The model's reply could not be used ({str(e)[:120]}).",
                      borrador_plantilla(s, h))
    ok, motivo, nn, nf = _verificar(b, h)
    if not ok:
        return salida("plantilla", False, "Draft rejected: " + motivo, borrador_plantilla(s, h), nn, nf)
    familia = {e["referencia"]: e["familia"] for e in h["excepciones"]}
    b["excepciones"] = [{**x, "familia": familia.get(x.get("referencia"), "")} for x in b.get("excepciones", [])]
    b.setdefault("correos", [])
    return salida("ia", True, "", b, nn, nf)
