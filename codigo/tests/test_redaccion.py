"""AI draft of the close write-up: the model writes, the code checks every number and folio against the close, and
anything it cannot verify falls back to the deterministic template. A person approves the draft."""
import json

from cierre_cxc.asistente_llm import hechos_cierre, redactar_cierre
from cierre_cxc.auditoria import Bitacora
from test_asistente_llm import FakeCliente


def _respuesta(resumen, que="A-1008 sigue en cuentas por cobrar por 232,000.00.", cuerpo="Hola, revisa A-1008."):
    return json.dumps({"resumen": resumen,
                       "decisiones": [{"id": "", "referencia": "A-1008", "que_pasa": que,
                                       "solucion_sugerida": "Fiscal la sustituye por PPD.", "pregunta": "", "opciones": []}],
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
    assert any(x["referencia"] == "A-1008" and x["que_pasa"] and x["solucion_sugerida"] for x in r["decisiones"])


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


def test_las_decisiones_traen_monto_y_responsable_del_cierre(salida):
    r = redactar_cierre(salida, None, FakeCliente(_respuesta("La cartera cierra en 1,759,932.85 MXN.")))
    d = r["decisiones"][0]
    assert d["referencia"] == "A-1008" and d["monto_mxn"] == "232000.00" and d["responsable"] == "Fiscal"


def test_los_criterios_del_revisor_llegan_a_la_ia(salida):
    f = FakeCliente(_respuesta("La cartera cierra en 1,759,932.85 MXN."))
    redactar_cierre(salida, None, f, criterios=["A-1012: la tasa 0 % no es exento."])
    assert "la tasa 0 % no es exento" in f.llamadas[0]["messages"][0]["content"]


def test_las_pistas_avisan_prestamo_y_rep_del_nivel_2(tmp_path):
    from datetime import datetime
    from cierre_cxc.pipeline import ejecutar
    from test_b3_ronda1 import banco, libro, mxn
    import uuid
    facturas = [mxn("H-1", 1, 50000, cliente="Hotelera Riviera Maya SA de CV", rfc="HRM100101AA1"),
                mxn("H-2", 5, 30000, cliente="Hotelera Riviera Maya SA de CV", rfc="HRM100101AA1")]
    bancos = [banco(16, "SPEI RECIBIDO HOTELERA RIVIERA MAYA ABONO", abono=20000),
              banco(19, "ABONO DISPOSICION CREDITO SIMPLE BBVA 4421", abono=500000)]
    rep = ["R1", datetime(2026, 10, 16), str(uuid.uuid5(uuid.UUID("12345678-1234-5678-1234-567812345678"), "H-1")).upper(),
           "H-1", 1, 58000, 20000, 38000, "MXN", 1]
    s = ejecutar(libro(tmp_path / "p.xlsx", facturas, bancos, reps=[rep]))
    pistas = " ".join(hechos_cierre(s)["pistas"])
    assert "préstamo" in pistas and "B03" in pistas
    assert "REP de H-1 corresponde al cobro por confirmar" in pistas


def test_criterios_se_guardan_sin_repetir_y_se_pueden_quitar(tmp_path):
    from cierre_cxc.criterios import Criterios
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    k = Criterios(tmp_path / "c.json", bit)
    k.aprender("IVA rate", "A-1012: la tasa 0 % no es exento.", "decision", "Diego")
    k.aprender("IVA rate", "A-1012: la tasa 0 % no es exento.", "decision", "Diego")
    assert Criterios(tmp_path / "c.json").textos() == ["A-1012: la tasa 0 % no es exento."]
    assert k.quitar("K01", "Diego") and Criterios(tmp_path / "c.json").textos() == []
    assert [e["evento"] for e in bit.leer()] == ["criterio_aprendido", "criterio_retirado"]
