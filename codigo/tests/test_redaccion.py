"""AI draft of the close write-up: the model writes, the code checks every number and folio against the close, and
anything it cannot verify falls back to the deterministic template. A person approves the draft."""
import json

from cierre_cxc.asistente_llm import hechos_cierre, redactar_cierre
from cierre_cxc.auditoria import Bitacora
from test_asistente_llm import FakeCliente


def _respuesta(resumen, explicacion="A-1008 sigue en cuentas por cobrar por 232,000.00.", cuerpo="Hola, revisa A-1008."):
    return json.dumps({"resumen": resumen,
                       "excepciones": [{"referencia": "A-1008", "explicacion": explicacion}],
                       "correos": [{"para": "Fiscal", "asunto": "A-1008", "cuerpo": cuerpo}]}, ensure_ascii=False)


def test_los_hechos_traen_cifras_y_excepciones_sin_uuid(salida):
    h = hechos_cierre(salida)
    assert h["cifras"]["cuentas_por_cobrar_mxn"] == "1759932.85" and h["cifras"]["iva_cobrado_mxn"] == "247757.23"
    assert any(e["referencia"] == "A-1008" and e["familia"] == "Papel del SAT" for e in h["excepciones"])
    texto = json.dumps(h, ensure_ascii=False)
    assert all(f.datos["uuid"] not in texto for f in salida.res.ds.cfdi if f.datos.get("uuid"))


def test_borrador_de_la_ia_se_acepta_si_todo_cuadra_y_queda_en_la_bitacora(salida, tmp_path):
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    r = redactar_cierre(salida, bit, FakeCliente(_respuesta("La cartera cierra en 1,759,932.85 MXN y A-1008 sigue abierta.")))
    assert r["origen"] == "ia" and r["aceptado"]
    assert "1,759,932.85" in r["resumen"] and r["verificados"]["numeros"] >= 1 and r["verificados"]["folios"] >= 1
    ev = [x for x in bit.leer() if x["evento"] == "llm_draft"]
    assert ev and ev[-1]["aceptado"] is True


def test_una_cifra_inventada_rechaza_el_borrador_y_usa_la_plantilla(salida, tmp_path):
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    r = redactar_cierre(salida, bit, FakeCliente(_respuesta("La cartera cierra en 1,799,932.85 MXN.")))
    assert r["origen"] == "plantilla" and not r["aceptado"]
    assert "1799932.85" in r["motivo"]
    assert "1,759,932.85" in r["resumen"]                       # the template carries the real figure
    assert [x for x in bit.leer() if x["evento"] == "llm_draft"][-1]["aceptado"] is False


def test_un_folio_inventado_rechaza_el_borrador(salida):
    r = redactar_cierre(salida, None, FakeCliente(_respuesta("Todo bien.", cuerpo="Revisa A-9999 por favor.")))
    assert r["origen"] == "plantilla" and "A-9999" in r["motivo"]


def test_respuesta_que_no_es_json_usa_la_plantilla(salida):
    r = redactar_cierre(salida, None, FakeCliente("Claro, aquí está el resumen..."))
    assert r["origen"] == "plantilla" and not r["aceptado"]


def test_sin_clave_api_usa_la_plantilla_con_correos_por_responsable(salida, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = redactar_cierre(salida)
    assert r["origen"] == "plantilla" and "sin clave" in r["motivo"].lower()
    assert "1,759,932.85" in r["resumen"]
    assert {c["para"] for c in r["correos"]} >= {"Fiscal", "Tesorería"}
    assert any(x["referencia"] == "A-1008" and x["explicacion"] for x in r["excepciones"])


class ClienteEstricto(FakeCliente):
    """Like the installed SDK with Claude Sonnet 5.5: sampling parameters are not accepted."""
    def _create(self, **kw):
        if "temperature" in kw:
            raise TypeError("Messages.create() got an unexpected keyword argument 'temperature'")
        return super()._create(**kw)


def test_la_llamada_no_manda_temperature_que_el_modelo_actual_rechaza(salida):
    r = redactar_cierre(salida, None, ClienteEstricto(_respuesta("La cartera cierra en 1,759,932.85 MXN.")))
    assert r["origen"] == "ia", r["motivo"]


def test_si_el_modelo_declina_se_usa_la_plantilla(salida):
    from types import SimpleNamespace
    f = FakeCliente("")
    f.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[], stop_reason="refusal"))
    r = redactar_cierre(salida, None, f)
    assert r["origen"] == "plantilla" and "1,759,932.85" in r["resumen"]
