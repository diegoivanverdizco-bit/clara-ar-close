"""The language model is fenced: it can suggest, never decide, and every call is logged."""
import json
from types import SimpleNamespace
import pytest

from conftest import D
from cierre_cxc.asistente_llm import crear_sugeridor, redactar_explicacion
from cierre_cxc.auditoria import Bitacora
from cierre_cxc.pipeline import ejecutar
from dataset_sintetico import crear


class FakeCliente:
    """Mimics anthropic.Anthropic().messages.create returning a fixed text."""
    def __init__(self, texto):
        self.texto = texto
        self.llamadas = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.llamadas.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.texto)])


def _datos_con_recibo_vago(tmp_path):
    import openpyxl
    ruta = crear(tmp_path / "x.xlsx")
    wb = openpyxl.load_workbook(ruta)
    ws = wb["Bancos_Oct2026"]
    for fila in ws.iter_rows(min_row=2):
        if fila[3].value == "SPEI RECIBIDO PAGO":      # vague description, amount equals nothing open
            fila[3].value = "SPEI RECIBIDO CLIENTE DEL SUR"
            fila[5].value = 100000                    # matches no open balance: needs the customer to be placed
    wb.save(ruta)
    return ruta


def test_sin_clave_api_el_sugeridor_no_hace_nada_y_lo_registra(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    sug = crear_sugeridor(bit)
    assert sug(SimpleNamespace(id="B1", descripcion="x"), {"k": "Cliente"}) is None
    assert bit.leer()[0]["disponible"] is False


def test_el_modelo_solo_puede_elegir_un_cliente_conocido(tmp_path):
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    inventado = FakeCliente(json.dumps({"cliente_id": "C99", "motivo": "x"}))
    sug = crear_sugeridor(bit, cliente=inventado)
    assert sug(SimpleNamespace(id="B1", descripcion="algo"), {"k1": "Cliente Uno"}) is None
    assert bit.leer()[-1]["aceptado"] is False


def test_respuesta_valida_se_acepta_y_se_registra(tmp_path):
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    cli = FakeCliente(json.dumps({"cliente_id": "C1", "motivo": "same name"}))
    sug = crear_sugeridor(bit, cliente=cli)
    assert sug(SimpleNamespace(id="B1", descripcion="algo"), {"k1": "Cliente Uno"}) == "k1"
    ev = bit.leer()[-1]
    assert ev["evento"] == "llm_call" and ev["aceptado"] is True
    # privacy: the prompt carries the description and names only
    enviado = cli.llamadas[0]["messages"][0]["content"]
    assert "algo" in enviado and "Cliente Uno" in enviado


def test_sugerencia_del_modelo_queda_como_propuesta_y_no_se_aplica(tmp_path):
    ruta = _datos_con_recibo_vago(tmp_path)
    sin_llm = ejecutar(ruta)
    assert any(u["mov_id"] == "B09" for u in sin_llm.res.efectivo_no_identificado)
    cli = FakeCliente(json.dumps({"cliente_id": "C4", "motivo": "customer is Mercado Verde"}))
    # C4 is the 4th customer in the order the engine lists them; resolve it dynamically
    claves = list(sin_llm.res.clientes)
    mv = claves.index(next(k for k, n in sin_llm.res.clientes.items() if n.startswith("Mercado")))
    cli.texto = json.dumps({"cliente_id": f"C{mv + 1}", "motivo": "test"})
    con_llm = ejecutar(ruta, sugeridor=crear_sugeridor(None, cliente=cli))
    # the model's suggestion never moves money: B09 stays in 2101 as a level-2 proposal
    assert not [a for a in con_llm.res.aplicaciones if a.mov_id == "B09"]
    u = next(x for x in con_llm.res.efectivo_no_identificado if x["mov_id"] == "B09")
    assert u["nivel"] == 2 and u["monto"] == D(100000)
    assert u["candidatos"][0]["folio"] == "B-2008" and "language model" in u["candidatos"][0]["evidencia"]
    assert "language model" in " ".join(u["alertas"])
    assert any(e.categoria == "Match to confirm" and e.referencia == "B09" for e in con_llm.excepciones)
    assert con_llm.controles_ok
    # a reviewer accepts it: only then the engine applies it, computing the amounts itself
    aceptado = ejecutar(ruta, decisiones={"conciliacion": {"B09": {"accion": "asignar", "folios": ["B-2008"],
                                                                  "usuario": "Diego"}}})
    ap = [a for a in aceptado.res.aplicaciones if a.mov_id == "B09"]
    assert ap and ap[0].folio == "B-2008" and ap[0].aplicado_doc == D(100000)
    assert aceptado.controles_ok


def test_texto_con_numero_inventado_se_descarta(tmp_path):
    bit = Bitacora(tmp_path / "b.jsonl", "t")
    hecho = "Receipt of 34,800.00 MXN has no invoice."
    malo = FakeCliente("A receipt of 43,800.00 MXN is unmatched.")
    assert redactar_explicacion(hecho, bit, cliente=malo) == hecho
    assert bit.leer()[-1]["aceptado"] is False
    bueno = FakeCliente("We received 34,800.00 MXN that matches no invoice.")
    assert redactar_explicacion(hecho, bit, cliente=bueno) == "We received 34,800.00 MXN that matches no invoice."


# ---- 4. Which option fits an input error to confirm: the model picks one the code computed, never a value ----

def _problemas(datos_caso, tmp_path, *cambios):
    import openpyxl
    from cierre_cxc.cargar import SchemaError, cargar
    from cierre_cxc.correcciones import numerar
    wb = openpyxl.load_workbook(datos_caso)
    for hoja, fila, col, valor in cambios:
        wb[hoja].cell(fila, col).value = valor
    ruta = tmp_path / "caso.xlsx"
    wb.save(ruta)
    with pytest.raises(SchemaError) as e:
        cargar(ruta)
    return numerar(e.value.problemas)


# P1 SAFE (tasa 16), P2 TO CONFIRM ("1,500": 1500 or 1.5)
UNA_DUDOSA = (("CFDI_Emitidos", 2, 9, 16), ("Bancos_Sep2026", 4, 6, "1,500"))


def _resp(*sugerencias):
    return json.dumps({"sugerencias": [dict(zip(("id", "opcion", "porque"), s)) for s in sugerencias]})


def _ia(numerados, texto, tmp_path):
    from cierre_cxc.asistente_llm import sugerir_correcciones
    from cierre_cxc.auditoria import Bitacora
    bit = Bitacora(tmp_path / "b.jsonl", "Diego")
    cli = FakeCliente(texto)
    return sugerir_correcciones(numerados, bit, cli), cli, [e for e in bit.leer() if e["evento"] == "llm_call"]


def test_la_ia_recomienda_una_opcion_del_codigo_con_un_porque_simple(datos_caso, tmp_path):
    import copy
    numerados = _problemas(datos_caso, tmp_path, *UNA_DUDOSA)
    antes = copy.deepcopy({pid: p.candidatos for pid, p in numerados.items()})
    sug, cli, ev = _ia(numerados, _resp(("P2", 1, "Usa 1500: un abono de 1.5 pesos no tiene sentido.")), tmp_path)
    assert sug == {"P2": {"opcion": 1, "porque": "Usa 1500: un abono de 1.5 pesos no tiene sentido."}}
    assert {pid: p.candidatos for pid, p in numerados.items()} == antes           # the candidates do not change
    enviado = cli.llamadas[0]["messages"][0]["content"]
    assert '"P2"' in enviado and '"P1"' not in enviado                          # only the ones to confirm go out
    (e,) = ev
    assert e["uso"] == "correcciones" and e["aceptadas"] == ["P2"] and e["descartadas"] == {}


def test_un_porque_con_una_cifra_que_no_esta_en_el_problema_se_descarta(datos_caso, tmp_path):
    numerados = _problemas(datos_caso, tmp_path, *UNA_DUDOSA)
    sug, _, (e,) = _ia(numerados, _resp(("P2", 1, "Usa 1500: el cliente siempre paga 2,750.00.")), tmp_path)
    assert sug == {}
    assert "2750" in e["descartadas"]["P2"]


def test_una_opcion_que_no_existe_o_un_id_ajeno_se_descartan(datos_caso, tmp_path):
    numerados = _problemas(datos_caso, tmp_path, *UNA_DUDOSA)
    sug, _, (e,) = _ia(numerados, _resp(("P2", 3, "La tercera."), ("P1", 1, "Segura."), ("P9", 1, "Otra.")), tmp_path)
    assert sug == {}
    assert set(e["descartadas"]) == {"P2", "P1", "P9"}


def test_la_opcion_cero_es_no_recomendar_y_no_se_muestra(datos_caso, tmp_path):
    numerados = _problemas(datos_caso, tmp_path, *UNA_DUDOSA)
    sug, _, (e,) = _ia(numerados, _resp(("P2", 0, "No se puede saber con lo que hay.")), tmp_path)
    assert sug == {} and e["descartadas"] == {}


def test_sin_clave_o_si_la_llamada_falla_no_hay_sugerencias(datos_caso, tmp_path, monkeypatch):
    import cierre_cxc.asistente_llm as llm
    from cierre_cxc.auditoria import Bitacora
    numerados = _problemas(datos_caso, tmp_path, *UNA_DUDOSA)
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: None)
    bit = Bitacora(tmp_path / "b.jsonl", "Diego")
    assert llm.sugerir_correcciones(numerados, bit) == {}
    falla = FakeCliente("")
    falla.messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("timeout")))
    assert llm.sugerir_correcciones(numerados, bit, falla) == {}
    ev = [e for e in bit.leer() if e["evento"] == "llm_call"]
    assert ev[0]["disponible"] is False and "timeout" in ev[1]["error"]


def test_sin_errores_por_confirmar_no_se_llama_a_la_ia(datos_caso, tmp_path):
    numerados = _problemas(datos_caso, tmp_path, ("CFDI_Emitidos", 2, 9, 16))
    sug, cli, ev = _ia(numerados, _resp(), tmp_path)
    assert sug == {} and cli.llamadas == [] and ev == []


def test_con_el_archivo_la_ia_recibe_el_rango_de_la_columna_y_puede_citarlo(datos_caso, tmp_path):
    # Practical context computed by the code: the other values of the same column (without the cell itself), so the
    # model can say "the other deposits go from X to Y" instead of "cannot tell"
    from cierre_cxc.asistente_llm import sugerir_correcciones
    numerados = _problemas(datos_caso, tmp_path, ("Bancos_Sep2026", 4, 6, "69,600"))
    cli = FakeCliente(_resp(("P1", 1, "Usa 69600: los demás abonos van de 15,000.00 a 556,800.00.")))
    sug = sugerir_correcciones(numerados, None, cli, ruta=tmp_path / "caso.xlsx")
    enviado = json.loads(cli.llamadas[0]["messages"][0]["content"])["errores"][0]
    assert enviado["otros_valores_de_la_columna"] == {"cuantos": 9, "minimo": "15,000.00", "mediana": "86,850.00",
                                                      "maximo": "556,800.00"}
    assert sug == {"P1": {"opcion": 1, "porque": "Usa 69600: los demás abonos van de 15,000.00 a 556,800.00."}}
