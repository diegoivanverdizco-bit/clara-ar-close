"""Start here tab: only what needs the reviewer's judgment, and each button acts through the same Revision methods."""
import json
import sys

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest   # noqa: E402

from conftest import RAIZ, DATOS

EXAMEN = RAIZ.parent / "material" / "2026-11-10 Practica" / "4 Examen sorpresa - corregido.xlsx"


def _app(datos, tmp_path, monkeypatch, cliente=None):
    import cierre_cxc.asistente_llm as llm
    if not datos.exists():
        pytest.skip("dataset not available")
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(datos), "--salida", str(tmp_path)])
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: cliente)
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=120).run()
    at.sidebar.text_input[0].input("Diego").run()
    assert not at.exception
    return at


def _clic(at, key):
    """Click the button with this key; a key ending in * matches by prefix."""
    next(b for b in at.button if b.key == key or (key.endswith("*") and (b.key or "").startswith(key[:-1]))).click().run()
    assert not at.exception
    return at


def _rev(tmp_path):
    return json.loads((tmp_path / "revision.json").read_text(encoding="utf-8"))


def _eventos(tmp_path):
    return [json.loads(l) for l in (tmp_path / "bitacora_auditoria.jsonl").read_text(encoding="utf-8").splitlines()]


def test_cobro_por_confirmar_dice_cual_sugiere_y_por_que(tmp_path, monkeypatch):
    at = _app(EXAMEN, tmp_path, monkeypatch)
    assert any("Suggested: A-1017" in m.value and "REP for exactly 50,000.00" in m.value for m in at.markdown)
    assert any("A-1017" in m.value and "oldest" in m.value and "REP for 50,000.00" in m.value for m in at.markdown)
    assert any("A-1021" in m.value and "no REP for this amount" in m.value for m in at.markdown)


def test_cobro_por_confirmar_se_aplica_a_la_candidata_elegida(tmp_path, monkeypatch):
    at = _clic(_app(EXAMEN, tmp_path, monkeypatch), "si_acc_B08_A-1017")
    assert _rev(tmp_path)["conciliacion"]["B08"]["folios"] == ["A-1017"]
    assert any(e["evento"] == "override" and e["movimiento"] == "B08" for e in _eventos(tmp_path))
    assert not any(b.key == "si_acc_B08_A-1017" for b in at.button)          # the card is gone once applied


def test_dinero_sin_dueno_se_deja_en_2101(tmp_path, monkeypatch):
    at = _clic(_app(EXAMEN, tmp_path, monkeypatch), "si_2101_B13")
    items = _rev(tmp_path)["items"]
    assert items["match:B13"]["estado"] == "aprobado"
    assert items["exc:Unidentified cash:B13"]["estado"] == "aprobado"
    assert not any(b.key == "si_2101_B13" for b in at.button)
    assert any("Money with no owner" in m.value and "aprobado" in m.value for m in at.markdown)


def test_no_es_de_clientes_pide_motivo_y_marca_no_cxc(tmp_path, monkeypatch):
    at = _app(EXAMEN, tmp_path, monkeypatch)
    assert any("loan drawdown" in m.value for m in at.markdown)              # B09 reads like a bank loan
    _clic(at, "si_nocxc_B09")
    assert any("Write the reason" in e.value for e in at.error)
    assert not (tmp_path / "revision.json").exists() or "B09" not in _rev(tmp_path)["conciliacion"]
    at.text_input(key="si_c3_B09").input("loan drawdown from BBVA").run()
    _clic(at, "si_nocxc_B09")
    c = _rev(tmp_path)["conciliacion"]["B09"]
    assert c["accion"] == "no_cxc" and c["comentario"] == "loan drawdown from BBVA"


def test_poliza_de_reversa_se_rechaza_como_decision(tmp_path, monkeypatch):
    _clic(_app(EXAMEN, tmp_path, monkeypatch), "si_pr_pol:Correction:Reversal of cancelled CFDI A-1033")
    pol = {k: v for k, v in _rev(tmp_path)["items"].items() if k.startswith("pol:") and "A-1033" in k}
    assert [v["estado"] for v in pol.values()] == ["rechazado"]


def test_correccion_de_tipo_de_cambio_se_rechaza_y_recalcula(tmp_path, monkeypatch):
    at = _clic(_app(DATOS, tmp_path, monkeypatch), "si_pr_pol:Correction:Rate correction A-1013*")
    assert _rev(tmp_path)["rechazar_correcciones"] == ["A-1013"]
    assert any(e["evento"] == "correction_rejected" and e["folio"] == "A-1013" for e in _eventos(tmp_path))
    assert not any("A-1013" in (b.key or "") and b.key.startswith("si_pr_") for b in at.button)                  # the correction entry is gone


def test_poliza_condicionada_se_aprueba(tmp_path, monkeypatch):
    _clic(_app(DATOS, tmp_path, monkeypatch), "si_pa_pol:Correction:Rate correction A-1013*")
    pol = {k: v for k, v in _rev(tmp_path)["items"].items() if k.startswith("pol:") and "A-1013" in k}
    assert [v["estado"] for v in pol.values()] == ["aprobado"]


def test_excepcion_alta_se_aprueba_y_rechazar_pide_motivo(tmp_path, monkeypatch):
    at = _clic(_app(DATOS, tmp_path, monkeypatch), "si_ea_exc:IVA rate:A-1012")
    assert _rev(tmp_path)["items"]["exc:IVA rate:A-1012"]["estado"] == "aprobado"
    clave = "exc:PUE not collected:A-1008"
    _clic(at, "si_er_" + clave)
    assert clave not in _rev(tmp_path)["items"]
    at.text_input(key="si_ce_" + clave).input("customer already paid, wait for bank").run()
    _clic(at, "si_er_" + clave)
    assert _rev(tmp_path)["items"][clave]["estado"] == "rechazado"


def test_aprobar_lo_demas_deja_las_tarjetas_pendientes(tmp_path, monkeypatch):
    at = _clic(_app(DATOS, tmp_path, monkeypatch), "si_resto")
    items = _rev(tmp_path)["items"]
    assert items["match:B03"]["estado"] == "aprobado"                         # routine receipt
    assert "exc:IVA rate:A-1012" not in items and "match:B08" not in items    # cards wait for the reviewer
    assert any("**0** other items pending" in m.value for m in at.markdown)
    _clic(at, "si_excel")
    assert any(e["evento"] == "export" and e["borrador"] for e in _eventos(tmp_path))


def test_mejorar_con_ia_reescribe_las_tarjetas(tmp_path, monkeypatch):
    from test_asistente_llm import FakeCliente
    resp = json.dumps({"resumen": "La cartera cierra en 1,759,932.85 MXN.",
                       "decisiones": [{"id": "", "referencia": "A-1012", "que_pasa": "A-1012 se facturó con tasa 0 %.",
                                       "solucion_sugerida": "Fiscal confirma la tasa.", "pregunta": "", "opciones": []}],
                       "correos": []})
    at = _clic(_app(DATOS, tmp_path, monkeypatch, FakeCliente(resp)), "si_ia")
    assert any("AI wording checked" in m.value for m in at.success)
    assert any("A-1012 se facturó con tasa 0 %." in m.value for m in at.markdown)
    assert any("La cartera cierra en 1,759,932.85 MXN." in m.value for m in at.markdown)


def test_sin_clave_se_queda_el_texto_fijo(tmp_path, monkeypatch):
    at = _clic(_app(DATOS, tmp_path, monkeypatch), "si_ia")
    assert any("Fixed wording kept" in w.value for w in at.warning)
    assert any("si_ea_exc:IVA rate:A-1012" == b.key for b in at.button)
