"""The language model is fenced: it can suggest, never decide, and every call is logged."""
import json
from types import SimpleNamespace

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
