"""The review UI loads a dataset, shows the same figures as the engine and records approvals."""
import json
import sys

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest   # noqa: E402

from conftest import RAIZ


def test_la_interfaz_muestra_cifras_y_registra_aprobacion(datos_caso, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(datos_caso), "--salida", str(tmp_path)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    assert not at.exception
    metricas = {m.label: m.value for m in at.metric}
    assert metricas["Closing AR (MXN)"] == "1,759,932.85"
    assert metricas["IVA cobrado (MXN)"] == "247,757.23"
    # approving requires a reviewer name
    at.sidebar.text_input[0].input("Diego").run()
    boton = next(b for b in at.button if b.key == "a_match:B03")
    boton.click().run()
    datos = json.loads((tmp_path / "revision.json").read_text(encoding="utf-8"))
    assert datos["items"]["match:B03"]["estado"] == "aprobado"
    assert datos["items"]["match:B03"]["usuario"] == "Diego"
    eventos = [json.loads(l)["evento"] for l in (tmp_path / "bitacora_auditoria.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "decision" in eventos


def test_la_interfaz_acepta_un_candidato_con_un_clic(datos_caso, tmp_path, monkeypatch):
    import openpyxl
    wb = openpyxl.load_workbook(datos_caso)
    wb["Bancos_Sep2026"].cell(3, 4).value = None          # B03 without description: only its amount fits A-1001
    ruta = tmp_path / "caso.xlsx"
    wb.save(ruta)
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(ruta), "--salida", str(tmp_path)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Closing AR (MXN)"] == "2,049,932.85"   # A-1001 still open
    at.sidebar.text_input[0].input("Diego").run()
    next(b for b in at.button if b.key == "acc_B03_A-1001").click().run()
    assert not at.exception
    datos = json.loads((tmp_path / "revision.json").read_text(encoding="utf-8"))
    assert datos["conciliacion"]["B03"]["folios"] == ["A-1001"] and datos["conciliacion"]["B03"]["usuario"] == "Diego"
    assert {m.label: m.value for m in at.metric}["Closing AR (MXN)"] == "1,759,932.85"   # applied after the click
