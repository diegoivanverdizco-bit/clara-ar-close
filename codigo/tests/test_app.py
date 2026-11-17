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


def test_la_interfaz_enlista_todos_los_errores_sin_leer_los_signos_de_pesos_como_formula(datos_caso, tmp_path, monkeypatch):
    import openpyxl
    wb = openpyxl.load_workbook(datos_caso)
    wb["Bancos_Sep2026"].cell(3, 6).value = "$290,000.00"
    wb["CFDI_Emitidos"].cell(2, 8).value = "$250,000.00"
    ruta = tmp_path / "caso.xlsx"
    wb.save(ruta)
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(ruta), "--salida", str(tmp_path)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    assert not at.exception
    texto = at.error[0].value
    assert "The file has 2 errors." in texto
    # two "$" in one Markdown text would be read as a math formula and hide the amounts between them
    assert texto.count("\\$") == 2 and "\\$290,000.00" in texto and "\\$250,000.00" in texto


def _app_borrador(datos_caso, tmp_path, monkeypatch, cliente):
    import cierre_cxc.asistente_llm as llm
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(datos_caso), "--salida", str(tmp_path)])
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: cliente)
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    at.sidebar.text_input[0].input("Diego").run()
    next(b for b in at.button if b.key == "ia_borrador").click().run()
    assert not at.exception
    return at


def test_borrador_con_ia_se_decide_aprende_y_queda_en_bitacora(datos_caso, tmp_path, monkeypatch):
    from test_asistente_llm import FakeCliente
    resp = json.dumps({"resumen": "La cartera cierra en 1,759,932.85 MXN.",
                       "decisiones": [{"id": "", "referencia": "A-1012", "que_pasa": "A-1012 lleva tasa 0 %.",
                                       "solucion_sugerida": "Fiscal confirma la tasa.", "pregunta": "¿Aplica el 0 %?",
                                       "opciones": ["Sí, se queda", "No, se emite de nuevo al 16 %"]}],
                       "correos": [{"para": "Fiscal", "asunto": "A-1012", "cuerpo": "Revisa A-1012."}]})
    fake = FakeCliente(resp)
    at = _app_borrador(datos_caso, tmp_path, monkeypatch, fake)
    assert any("Checked" in s.value for s in at.success)
    assert at.text_area(key="ia_resumen").value.startswith("La cartera cierra en 1,759,932.85")
    at.radio(key="ia_op_0").set_value("No, se emite de nuevo al 16 %").run()
    next(b for b in at.button if b.key == "ia_ok_0").click().run()
    eventos = [json.loads(l) for l in (tmp_path / "bitacora_auditoria.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["evento"] == "ai_decision" and e["aceptada"] and e["usuario"] == "Diego" for e in eventos)
    assert any(e["evento"] == "criterio_aprendido" for e in eventos)
    # the next draft carries the reviewer's decision to the model
    next(b for b in at.button if b.key == "ia_borrador").click().run()
    assert "se emite de nuevo al 16 %" in fake.llamadas[-1]["messages"][0]["content"]


def test_borrador_sin_clave_muestra_la_plantilla(datos_caso, tmp_path, monkeypatch):
    at = _app_borrador(datos_caso, tmp_path, monkeypatch, None)
    assert any("Template" in w.value for w in at.warning)
    assert "1,759,932.85" in at.text_area(key="ia_resumen").value


# ---- input errors: the table of problems and the approval of corrections on the screen ----

B, C = "Bancos_Sep2026", "CFDI_Emitidos"
LIMPIO = {"Closing AR (MXN)": "1,759,932.85", "IVA cobrado (MXN)": "247,757.23"}


def _app_con_errores(datos_caso, tmp_path, monkeypatch, *cambios, usuario="Diego"):
    import openpyxl
    wb = openpyxl.load_workbook(datos_caso)
    for hoja, fila, col, valor in cambios:
        wb[hoja].cell(fila, col).value = valor
    ruta = tmp_path / "caso.xlsx"
    wb.save(ruta)
    salida = tmp_path / "salida"
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(ruta), "--salida", str(salida)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    assert not at.exception
    if usuario:
        at.sidebar.text_input[0].input(usuario).run()
    return at, ruta, salida


def _eventos(salida, nombre):
    p = salida / "bitacora_auditoria.jsonl"
    lineas = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
    return [e for e in lineas if e["evento"] == nombre]


def _boton(at, clave):
    return next(b for b in at.button if b.key == clave)


DOS_SEGURAS = ((C, 2, 9, 16), (B, 3, 6, "290,000.00"))


def test_aprobar_las_seguras_en_la_pantalla_escribe_la_copia_y_cierra_con_las_cifras_del_archivo_limpio(
        datos_caso, tmp_path, monkeypatch):
    from cierre_cxc.auditoria import sha256_archivo
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, *DOS_SEGURAS)
    antes = sha256_archivo(ruta)
    tabla = at.dataframe[0].value
    assert list(tabla["P#"]) == ["P1", "P2"] and set(tabla["Label"]) == {"SAFE"}
    assert _boton(at, "fix_seguras").label == "Approve all SAFE (2)"
    _boton(at, "fix_seguras").click().run()
    _boton(at, "fix_escribir").click().run()
    assert not at.exception
    assert sha256_archivo(ruta) == antes                                    # the original is never written
    (copia,) = salida.glob("*corregido.xlsx")
    assert {m.label: m.value for m in at.metric if m.label in LIMPIO} == LIMPIO   # the close ran on the copy
    aprobadas = _eventos(salida, "input_fix_approved")
    assert [e["id"] for e in aprobadas] == ["P1", "P2"] and all(e["usuario"] == "Diego" for e in aprobadas)
    (proponer,) = _eventos(salida, "input_fix_proposed")
    assert proponer["sha256"] == antes and proponer["archivo"] == "caso.xlsx"
    (cop,) = _eventos(salida, "input_fix_copy")
    assert cop["copia"] == copia.name and cop["sha256_original"] == antes and cop["aplicadas"] == ["P1", "P2"]
    assert any("corrected copy" in i.value for i in at.info)               # the screen says which file it works on


def test_sin_nombre_de_revisor_los_botones_de_correccion_estan_deshabilitados(datos_caso, tmp_path, monkeypatch):
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, *DOS_SEGURAS, usuario="")
    assert _boton(at, "fix_seguras").disabled and _boton(at, "fix_escribir").disabled
    at.sidebar.text_input[0].input("Diego").run()
    assert not _boton(at, "fix_seguras").disabled and not _boton(at, "fix_escribir").disabled


def test_si_el_archivo_cambia_despues_de_dibujar_la_tabla_no_se_escribe_copia(datos_caso, tmp_path, monkeypatch):
    import openpyxl
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, *DOS_SEGURAS)
    _boton(at, "fix_seguras").click().run()
    wb = openpyxl.load_workbook(ruta)
    wb[B].cell(5, 6).value = "1,000.00"                                     # someone fixes or breaks a cell by hand
    wb.save(ruta)
    _boton(at, "fix_escribir").click().run()
    assert not at.exception
    assert any("The file changed since this list was shown. Review the new list." in e.value for e in at.error)
    assert list(salida.glob("*corregido.xlsx")) == []
    assert _eventos(salida, "input_fix_approved") == [] and _eventos(salida, "input_fix_copy") == []
    assert list(at.dataframe[0].value["P#"]) == ["P1", "P2", "P3"]           # the new list is on the screen


def test_un_error_por_confirmar_escribe_el_candidato_elegido(datos_caso, tmp_path, monkeypatch):
    import openpyxl
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 4, 6, "69,600"))
    assert at.dataframe[0].value["Label"].tolist() == ["TO CONFIRM"]
    sel = at.selectbox(key="fix_sel_P1")
    assert sel.options[0] == "Leave as is" and len(sel.options) == 3         # leave it, or candidate 1 or 2
    sel.set_value(sel.options[2]).run()
    _boton(at, "fix_escribir").click().run()
    assert not at.exception
    (copia,) = salida.glob("*corregido.xlsx")
    assert float(openpyxl.load_workbook(copia, data_only=True)[B]["F4"].value) == 69.6   # candidate 2, not 69600
    (ap,) = _eventos(salida, "input_fix_approved")
    assert ap["id"] == "P1" and ap["candidato"] == 2 and ap["usuario"] == "Diego"


def test_dejar_como_esta_no_escribe_nada(datos_caso, tmp_path, monkeypatch):
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 4, 6, "69,600"))
    _boton(at, "fix_escribir").click().run()
    assert any("Nothing approved" in w.value for w in at.warning)
    assert list(salida.glob("*corregido.xlsx")) == [] and _eventos(salida, "input_fix_approved") == []


def test_rechazar_queda_en_la_bitacora_y_no_escribe_la_copia(datos_caso, tmp_path, monkeypatch):
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 4, 6, "69,600"))
    at.checkbox(key="fix_rech_P1").check().run()
    at.text_input(key="fix_comentario").input("wrong period").run()
    _boton(at, "fix_escribir").click().run()
    (rech,) = _eventos(salida, "input_fix_rejected")
    assert rech["id"] == "P1" and rech["usuario"] == "Diego" and rech["comentario"] == "wrong period"
    assert list(salida.glob("*corregido.xlsx")) == []


def test_lo_que_no_se_corrige_solo_se_muestra_sin_controles(datos_caso, tmp_path, monkeypatch):
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 3, 7, "EUR"))
    tabla = at.dataframe[0].value
    assert tabla["Label"].tolist() == ["NOT FIXED"] and tabla["Cell"].tolist() == ["G3"]
    assert not [b for b in at.button if (b.key or "").startswith("fix_")]
    assert not at.selectbox and not [c for c in at.checkbox if (c.key or "").startswith("fix_")]


def test_si_la_copia_todavia_tiene_errores_se_muestra_su_tabla_nueva(datos_caso, tmp_path, monkeypatch):
    # "$17.50" in an MXN invoice: once it is a number, the invoice is MXN with a rate, which shows in the second round
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (C, 12, 7, "$17.50"))
    _boton(at, "fix_seguras").click().run()
    _boton(at, "fix_escribir").click().run()
    assert not at.exception
    assert any("corrected copy" in i.value for i in at.info)
    nueva = at.dataframe[0].value
    assert len(nueva) == 1 and nueva["Label"].tolist() != ["SAFE"]


def test_si_el_archivo_original_cambia_la_pantalla_deja_la_copia(datos_caso, tmp_path, monkeypatch):
    import openpyxl
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, *DOS_SEGURAS)
    _boton(at, "fix_seguras").click().run()
    _boton(at, "fix_escribir").click().run()
    assert any("corrected copy" in i.value for i in at.info)
    wb = openpyxl.load_workbook(ruta)
    wb[B].cell(5, 6).value = "1,000.00"
    wb.save(ruta)
    at.run()
    assert not any("corrected copy" in i.value for i in at.info)            # a different file: the copy does not apply
    assert list(at.dataframe[0].value["P#"]) == ["P1", "P2", "P3"]


def test_una_segunda_ronda_de_correcciones_sigue_sobre_la_copia_y_no_vuelve_al_original(
        datos_caso, tmp_path, monkeypatch):
    # The SAFE ones first; the copy still has the one to confirm. Approving it writes a second copy, and the screen
    # must keep working on the copies: the uploader still holds the original, whose SHA-256 is the one to compare.
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (C, 2, 9, 16), (B, 4, 6, "1,500"))
    _boton(at, "fix_seguras").click().run()
    _boton(at, "fix_escribir").click().run()
    assert list(at.dataframe[0].value["Label"]) == ["TO CONFIRM"]
    sel = next(s for s in at.selectbox if s.key == "fix_sel_P1")
    sel.set_value(sel.options[1]).run()
    _boton(at, "fix_escribir").click().run()
    assert not at.exception and not at.error
    assert len(list(salida.glob("*corregido.xlsx"))) == 2
    assert any("corrected copy" in i.value for i in at.info)
    assert "Closing AR (MXN)" in {m.label for m in at.metric}            # the close ran on the second copy


def test_la_ia_sugiere_una_opcion_pero_la_persona_elige_y_la_bitacora_lo_registra(datos_caso, tmp_path, monkeypatch):
    import cierre_cxc.asistente_llm as llm
    from test_asistente_llm import FakeCliente
    fake = FakeCliente(json.dumps({"sugerencias": [
        {"id": "P1", "opcion": 1, "porque": "Usa 1500: un abono de 1.5 pesos no tiene sentido."}]}))
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: fake)
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 4, 6, "1,500"))
    assert fake.llamadas == []                                               # only when the reviewer asks
    _boton(at, "fix_ia").click().run()
    assert any("AI suggests option 1" in i.value and "1.5 pesos" in i.value for i in at.info)   # visible, not grey
    sel = next(s for s in at.selectbox if s.key == "fix_sel_P1")
    assert sel.value == "Leave as is"                                        # the AI never preselects
    sel.set_value(sel.options[1]).run()
    _boton(at, "fix_escribir").click().run()
    assert not at.exception
    (aprobada,) = _eventos(salida, "input_fix_approved")
    assert aprobada["candidato"] == 1 and aprobada["ia_sugirio"] == 1 and aprobada["usuario"] == "Diego"
    assert [e["aceptadas"] for e in _eventos(salida, "llm_call") if e.get("uso") == "correcciones"] == [["P1"]]


def test_sin_clave_la_ia_no_sugiere_y_la_tabla_sigue_igual(datos_caso, tmp_path, monkeypatch):
    import cierre_cxc.asistente_llm as llm
    monkeypatch.setattr(llm, "_cliente_anthropic", lambda: None)
    at, ruta, salida = _app_con_errores(datos_caso, tmp_path, monkeypatch, (B, 4, 6, "1,500"))
    _boton(at, "fix_ia").click().run()
    assert not at.exception
    assert any("no suggestion" in c.value for c in at.caption)
    assert not any("AI suggests" in i.value for i in at.info)


def test_el_boton_de_netsuite_escribe_el_csv_en_borrador_y_lo_registra(datos_caso, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(datos_caso), "--salida", str(tmp_path)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    _boton(at, "netsuite").click().run()
    assert not at.exception
    assert (tmp_path / "2026-09_polizas_netsuite_BORRADOR.csv").exists()
    (e,) = _eventos(tmp_path, "export_netsuite")
    assert e["borrador"] is True and e["polizas"] == 27 and e["omitidas"] == []
    assert any("DRAFT" in w.value for w in at.warning)


def test_start_here_tambien_tiene_el_boton_de_netsuite(datos_caso, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app", "--datos", str(datos_caso), "--salida", str(tmp_path)])
    at = AppTest.from_file(str(RAIZ / "cierre_cxc" / "app.py"), default_timeout=90).run()
    _boton(at, "si_netsuite").click().run()
    assert not at.exception and (tmp_path / "2026-09_polizas_netsuite_BORRADOR.csv").exists()
