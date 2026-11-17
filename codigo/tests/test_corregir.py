"""`fix`: approve proposed corrections into a corrected copy (docs/2026-11-17 Diseño corrección de errores de captura.md).

Rules: the original is never written; the copy differs from it only in the approved cells; every approval and
rejection goes to the audit log with who made it; after correcting, the copy is checked again and, with no errors
left, the whole close runs on it.
"""
import json
from datetime import datetime

import openpyxl
import pytest

from cierre_cxc.auditoria import sha256_archivo
from cierre_cxc.cli import main
from cierre_cxc.pipeline import ejecutar

B, C, R, A, T = "Bancos_Sep2026", "CFDI_Emitidos", "REP_Emitidos", "AR_Apertura_31Ago", "TC_FIX_Diario"


@pytest.fixture
def saboteado(datos_caso, tmp_path):
    def hacer(*mutaciones, nombre="entrada.xlsx"):
        wb = openpyxl.load_workbook(datos_caso)
        for m in mutaciones:
            m(wb)
        ruta = tmp_path / nombre
        wb.save(ruta)
        return ruta
    return hacer


def celda(hoja, fila, col, valor):
    def m(wb):
        wb[hoja].cell(fila, col).value = valor
    return m


def fix(ruta, salida, *extra, usuario="Diego", listar=True):
    """Run fix. Choosing by number (--elegir, --rechazar) needs the list of that same file shown first, as a
    reviewer would see it: the helper shows it unless `listar` is False."""
    args = ["fix", "--datos", str(ruta), "--salida", str(salida)]
    if listar and ({"--elegir", "--rechazar"} & set(extra)):
        main(args)
    return main(args + (["--usuario", usuario] if usuario else []) + list(extra))


def eventos(salida, nombre=None):
    if not (salida / "bitacora_auditoria.jsonl").exists():
        return []
    lineas = [json.loads(l) for l in (salida / "bitacora_auditoria.jsonl").read_text(encoding="utf-8").splitlines()]
    return [e for e in lineas if nombre is None or e["evento"] == nombre]


def copias(salida):
    return sorted(salida.glob("*corregido.xlsx"))


def valores(ruta):
    wb = openpyxl.load_workbook(ruta, data_only=True)
    return {(ws.title, c.coordinate): c.value for ws in wb for fila in ws.iter_rows() for c in fila if c.value is not None}


def huella(s):
    return (s.res.cierre, s.aging["total"], s.iva["cobrado"], s.iva["cierre"], len(s.res.aplicaciones),
            len(s.res.efectivo_no_identificado), len(s.polizas), s.controles_ok, s.res.apertura_libro)


DOS_SEGURAS = (celda(C, 2, 9, 16), celda(B, 3, 6, "290,000.00"))


def test_sin_aprobar_solo_enlista_y_no_escribe(saboteado, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "23.200,00"))
    antes = sha256_archivo(ruta)
    assert fix(ruta, tmp_path / "s", usuario=None) == 1
    out = capsys.readouterr().out
    assert "P1" in out and "P2" in out and "P3" in out and "SAFE" in out and "NOT FIXED" in out
    assert "TO CONFIRM" not in out and "candidate number" not in out
    assert "--seguras" in out and "--elegir" in out
    assert copias(tmp_path / "s") == [] and sha256_archivo(ruta) == antes
    (prop,) = eventos(tmp_path / "s", "input_fix_proposed")
    assert prop["sha256"] == antes and prop["por_categoria"] == {"a": 2, "c": 1}


def test_corregir_las_seguras_da_el_mismo_cierre_que_el_archivo_limpio(saboteado, datos_caso, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS)
    antes = sha256_archivo(ruta)
    assert fix(ruta, tmp_path / "s", "--seguras") == 0
    (copia,) = copias(tmp_path / "s")
    assert sha256_archivo(ruta) == antes                                   # the original is never written
    assert huella(ejecutar(copia)) == huella(ejecutar(datos_caso))          # corrected = clean data
    out = capsys.readouterr().out
    assert "Checks: ALL PASS" in out                                        # the close ran on the copy
    (run,) = eventos(tmp_path / "s", "run")
    assert run["archivo"] == copia.name and run["sha256"] == sha256_archivo(copia)


def test_la_copia_solo_cambia_las_celdas_aprobadas(saboteado, tmp_path):
    ruta = saboteado(*DOS_SEGURAS)
    fix(ruta, tmp_path / "s", "--seguras")
    original, copia = valores(ruta), valores(copias(tmp_path / "s")[0])
    distintas = {k for k in original.keys() | copia.keys() if original.get(k) != copia.get(k)}
    assert distintas == {(C, "I2"), (B, "F3")}
    assert copia[(C, "I2")] == 0.16 and copia[(B, "F3")] == 290000


def test_cada_aprobacion_queda_en_la_bitacora_con_quien_la_aprobo(saboteado, tmp_path):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "69,600"))
    antes = sha256_archivo(ruta)
    assert fix(ruta, tmp_path / "s", "--elegir", "P1,P3=1") == 1     # one by one: P2 stays, so the copy is not clean
    aprobadas = eventos(tmp_path / "s", "input_fix_approved")
    assert [e["id"] for e in aprobadas] == ["P1", "P3"]
    tasa = aprobadas[0]
    assert tasa["usuario"] == "Diego" and tasa["categoria"] == "a" and tasa["sha256"] == antes
    assert tasa["cambios"] == [{"hoja": C, "celda": "I2", "antes": "16", "despues": "0.16"}]
    assert aprobadas[1]["categoria"] == "a" and aprobadas[1]["candidato"] == 1
    assert aprobadas[1]["cambios"] == [{"hoja": B, "celda": "F4", "antes": "69,600", "despues": "69600"}]
    assert all("ia_sugirio" not in e for e in aprobadas)
    (copia,) = eventos(tmp_path / "s", "input_fix_copy")
    assert copia["sha256_original"] == antes and copia["sha256"] == sha256_archivo(copias(tmp_path / "s")[0])
    assert copia["aplicadas"] == ["P1", "P3"]


def test_seguras_no_alcanza_a_las_que_no_se_corrigen(saboteado, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "23.200,00"))
    assert fix(ruta, tmp_path / "s", "--seguras") == 1
    out = capsys.readouterr().out
    assert "still has 1 error" in out and "23.200,00" in out                # the NOT FIXED one is listed again, in the copy
    assert len(eventos(tmp_path / "s", "input_fix_approved")) == 2
    assert eventos(tmp_path / "s", "run") == []                             # the close does not run with errors


@pytest.mark.parametrize("extra,mensaje", [
    (["--elegir", "P3=5"], "P3 has 1 candidate"),
    (["--elegir", "P9=1"], "P9 is not in the list"),
    (["--elegir", "P1=1", "--rechazar", "P1"], "P1 is both approved and rejected"),
], ids=["out of range", "unknown", "approved and rejected"])
def test_aprobacion_invalida_no_escribe_nada(saboteado, tmp_path, capsys, extra, mensaje):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "69,600"))
    assert fix(ruta, tmp_path / "s", *extra) == 1
    assert mensaje in capsys.readouterr().err
    assert copias(tmp_path / "s") == [] and eventos(tmp_path / "s", "input_fix_approved") == []


def test_lo_que_no_se_corrige_no_se_puede_aprobar(saboteado, tmp_path, capsys):
    ruta = saboteado(celda(B, 3, 7, "EUR"))
    assert fix(ruta, tmp_path / "s", "--elegir", "P1=1") == 1
    assert "P1 has no proposed correction" in capsys.readouterr().err and copias(tmp_path / "s") == []


def test_aprobar_exige_el_nombre_de_quien_aprueba(saboteado, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS)
    assert fix(ruta, tmp_path / "s", "--seguras", usuario=None) == 1
    assert "--usuario" in capsys.readouterr().err and copias(tmp_path / "s") == []


def test_rechazar_queda_en_la_bitacora_y_el_error_sigue(saboteado, tmp_path):
    ruta = saboteado(*DOS_SEGURAS)
    assert fix(ruta, tmp_path / "s", "--elegir", "P1=1", "--rechazar", "P2", "--comentario", "lo reviso con tesoreria") == 1
    (rech,) = eventos(tmp_path / "s", "input_fix_rejected")
    assert rech["id"] == "P2" and rech["usuario"] == "Diego" and rech["comentario"] == "lo reviso con tesoreria"
    assert len(copias(tmp_path / "s")) == 1                                  # P1 applied; P2 still in the copy


def test_corregir_dos_veces_no_pisa_la_primera_copia(saboteado, tmp_path):
    ruta = saboteado(*DOS_SEGURAS)
    fix(ruta, tmp_path / "s", "--seguras")
    primera = copias(tmp_path / "s")[0]
    contenido = primera.read_bytes()
    fix(ruta, tmp_path / "s", "--seguras")
    assert len(copias(tmp_path / "s")) == 2 and primera.read_bytes() == contenido


def test_si_el_archivo_cambio_desde_la_lista_no_se_escribe(saboteado, tmp_path, monkeypatch, capsys):
    # the copy re-reads every cell and refuses to write if it is no longer what was proposed
    import cierre_cxc.correcciones as cor
    ruta = saboteado(*DOS_SEGURAS)
    real = cor.escribir_copia

    def con_cambio(origen, destino, aprobadas):
        wb = openpyxl.load_workbook(origen)
        wb[C]["I2"].value = 8
        wb.save(origen)
        return real(origen, destino, aprobadas)
    monkeypatch.setattr(cor, "escribir_copia", con_cambio)
    assert fix(ruta, tmp_path / "s", "--seguras") == 1
    assert "changed since it was checked" in capsys.readouterr().err and copias(tmp_path / "s") == []


def test_elegir_por_numero_exige_haber_visto_la_lista_de_ese_archivo(saboteado, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "69,600"))
    assert fix(ruta, tmp_path / "s", "--elegir", "P3=1", listar=False) == 1
    assert "List the corrections first" in capsys.readouterr().err and copias(tmp_path / "s") == []


def test_si_el_archivo_cambio_despues_de_la_lista_los_numeros_no_valen(saboteado, tmp_path, capsys):
    # P2 was the abono of B3; after the reviewer corrects the IVA rate by hand, P2 is another error
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "69,600"))
    main(["fix", "--datos", str(ruta), "--salida", str(tmp_path / "s")])
    wb = openpyxl.load_workbook(ruta)
    wb[C]["I2"].value = 0.16
    wb.save(ruta)
    assert fix(ruta, tmp_path / "s", "--elegir", "P2=1", listar=False) == 1
    assert "changed since its list was shown" in capsys.readouterr().err and copias(tmp_path / "s") == []


def test_la_lista_de_la_copia_sirve_para_elegir_en_la_copia(saboteado, tmp_path, capsys):
    ruta = saboteado(*DOS_SEGURAS, celda(B, 4, 6, "69,600"))
    fix(ruta, tmp_path / "s", "--elegir", "P1,P2")             # the copy keeps the third one, and lists it
    (copia,) = copias(tmp_path / "s")
    assert fix(copia, tmp_path / "s", "--elegir", "P1=1", listar=False) == 0


def test_una_correccion_puede_destapar_otro_error(saboteado, tmp_path, capsys):
    # "$17.50" in an MXN invoice: once it is a number, the invoice is MXN with a rate, which shows in the second round
    ruta = saboteado(celda(C, 12, 7, "$17.50"))
    assert fix(ruta, tmp_path / "s", "--seguras") == 1
    out = capsys.readouterr().out
    assert "still has 1 error" in out and "is in MXN but has exchange rate" in out


def test_la_fila_pegada_dos_veces_se_quita_en_la_copia(saboteado, tmp_path):
    ruta = saboteado(lambda wb: wb[C].append([c.value for c in wb[C][2]]))
    assert fix(ruta, tmp_path / "s", "--seguras") == 0
    wb = openpyxl.load_workbook(copias(tmp_path / "s")[0], data_only=True)
    assert sum(1 for r in wb[C].iter_rows(min_row=2) if r[1].value == "A-1001") == 1
    assert sum(1 for r in openpyxl.load_workbook(ruta)[C].iter_rows(min_row=2) if r[1].value == "A-1001") == 2


def test_lo_que_ya_no_se_adivina_se_avisa_sin_copia(saboteado, tmp_path, capsys):
    # a balance line, a totals row and a cargo-and-abono row used to get proposals; now they are only reported
    ruta = saboteado(celda(B, 3, 5, 1000),
                     lambda wb: wb[B].append([datetime(2026, 9, 1), "MXN-001", "", "SALDO INICIAL", None, 1500000, "MXN"]))
    assert fix(ruta, tmp_path / "s", "--seguras") == 1
    out = capsys.readouterr().out
    assert out.count("NOT FIXED") == 2 and "Nothing approved" in out and copias(tmp_path / "s") == []


def test_una_entrada_xlsm_da_una_copia_xlsx(saboteado, tmp_path):
    ruta = saboteado(*DOS_SEGURAS, nombre="entrada.xlsm")
    assert fix(ruta, tmp_path / "s", "--seguras") == 0
    (copia,) = copias(tmp_path / "s")
    assert copia.suffix == ".xlsx" and huella(ejecutar(copia))


def test_listar_o_rechazar_no_reabre_las_aprobaciones_del_cierre(datos_caso, saboteado, tmp_path, capsys):
    salida = tmp_path / "s"
    assert main(["run", "--datos", str(datos_caso), "--salida", str(salida)]) in (0, 2)
    assert main(["approve", "--datos", str(datos_caso), "--salida", str(salida), "--usuario", "Diego"]) == 0
    roto = saboteado(*DOS_SEGURAS)
    fix(roto, salida, usuario=None)
    fix(roto, salida, "--rechazar", "P1,P2")
    capsys.readouterr()
    main(["run", "--datos", str(datos_caso), "--salida", str(salida)])
    assert "Review status: APPROVED" in capsys.readouterr().out


def test_sin_errores_no_hay_nada_que_corregir(datos_caso, tmp_path, capsys):
    assert fix(datos_caso, tmp_path / "s") == 0
    assert "No input errors" in capsys.readouterr().out and copias(tmp_path / "s") == []
