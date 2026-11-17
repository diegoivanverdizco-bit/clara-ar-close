"""Journal entries as a CSV for NetSuite's CSV Import Assistant: the same entries as the Excel, never recomputed, every
entry balances or is left out with its reason, and the file is final only when the close is approved."""
import csv
import json
import re
from collections import defaultdict
from decimal import Decimal

import pytest

from cierre_cxc import netsuite
from cierre_cxc.cli import main
from cierre_cxc.exportar import lineas_polizas
from cierre_cxc.pipeline import ejecutar

COLUMNAS = ["External ID", "Date", "Memo", "Reversal Date", "Journal Entry - Line: Account",
            "Journal Entry - Line: Debit", "Journal Entry - Line: Credit", "Journal Entry - Line: Memo"]
DEBE, HABER, CUENTA = "Journal Entry - Line: Debit", "Journal Entry - Line: Credit", "Journal Entry - Line: Account"


def _d(x):
    return Decimal(x) if x else Decimal(0)


def test_las_polizas_salen_cuadradas_y_con_las_mismas_cifras_del_excel(salida):
    filas, omitidas = netsuite.filas(salida)
    assert omitidas == []
    assert list(filas[0]) == COLUMNAS
    por_poliza = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
    for f in filas:
        assert re.fullmatch(r"(\d+\.\d{2})?", f[DEBE]) and re.fullmatch(r"(\d+\.\d{2})?", f[HABER])
        assert bool(f[DEBE]) != bool(f[HABER])                   # one side per line, never both, never empty
        t = por_poliza[f["External ID"]]
        t[0] += _d(f[DEBE]); t[1] += _d(f[HABER]); t[2] += 1
    assert all(d == h and n >= 2 for d, h, n in por_poliza.values())
    excel = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for l in lineas_polizas(salida):
        excel[f"CXC-202609-{l['poliza']}"][0] += l["debe"]
        excel[f"CXC-202609-{l['poliza']}"][1] += l["haber"]
    assert {k: v[:2] for k, v in por_poliza.items()} == {k: v for k, v in excel.items()}
    assert len(por_poliza) == len(salida.polizas) == 27


def test_fecha_cuenta_y_reversa_de_la_revaluacion(salida):
    filas, _ = netsuite.filas(salida)
    p02 = [f for f in filas if f["External ID"] == "CXC-202609-P02"]
    assert [(f[CUENTA], f[DEBE], f[HABER]) for f in p02] == [("1101 Bancos MXN", "290000.00", ""),
                                                             ("1201 Clientes (CxC)", "", "290000.00")]
    assert p02[0]["Date"] == "10/09/2026" and p02[0]["Reversal Date"] == ""
    assert p02[0]["Memo"].startswith("Collection A-1001")
    # unrealized FX: NetSuite reverses it on the first day of the next period, as the entry says
    reval = {f["External ID"]: f["Reversal Date"] for f in filas if "Revaluation" in f["Memo"]}
    assert reval == {"CXC-202609-P23": "01/10/2026", "CXC-202609-P24": "01/10/2026", "CXC-202609-P25": "01/10/2026"}


def test_una_poliza_que_no_cuadra_no_se_exporta_y_se_dice_por_que(datos_caso):
    s = ejecutar(datos_caso)
    s.polizas[1].lineas[0].debe += Decimal("1")
    filas, omitidas = netsuite.filas(s)
    assert "CXC-202609-P02" not in {f["External ID"] for f in filas}
    assert omitidas == [("P02", "does not balance: debits 290,001.00, credits 290,000.00")]


def test_el_mapa_de_cuentas_cambia_el_nombre_y_una_cuenta_sin_mapa_detiene_todo(salida, tmp_path):
    cuentas = sorted({l.cuenta for p in salida.polizas for l in p.lineas})
    mapa = tmp_path / "cuentas.csv"
    mapa.write_text("cuenta,netsuite\n" + "".join(f"{c},NS {c}\n" for c in cuentas), encoding="utf-8")
    filas, _ = netsuite.filas(salida, netsuite.leer_mapa(mapa))
    assert {f[CUENTA] for f in filas} == {f"NS {c}" for c in cuentas}
    mapa.write_text("cuenta,netsuite\n" + "".join(f"{c},NS {c}\n" for c in cuentas if c != "7103"), encoding="utf-8")
    with pytest.raises(ValueError, match="7103"):
        netsuite.filas(salida, netsuite.leer_mapa(mapa))


def test_la_subsidiaria_solo_sale_si_se_da(salida):
    filas, _ = netsuite.filas(salida, subsidiaria="Clara Mexico")
    assert list(filas[0])[:2] == ["External ID", "Subsidiary"] and {f["Subsidiary"] for f in filas} == {"Clara Mexico"}


def _csv(ruta):
    with open(ruta, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _eventos(salida):
    return [json.loads(l) for l in (salida / "bitacora_auditoria.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(l)["evento"] == "export_netsuite"]


def test_el_comando_escribe_borrador_hasta_que_todo_esta_aprobado(datos_caso, tmp_path, capsys):
    out = tmp_path / "s"
    assert main(["netsuite", "--datos", str(datos_caso), "--salida", str(out), "--usuario", "Diego"]) == 0
    borrador = out / "2026-09_polizas_netsuite_BORRADOR.csv"
    assert borrador.exists() and not (out / "2026-09_polizas_netsuite.csv").exists()
    assert "DRAFT" in capsys.readouterr().out
    assert len({f["External ID"] for f in _csv(borrador)}) == 27
    assert main(["approve", "--datos", str(datos_caso), "--salida", str(out), "--items", "all",
                 "--usuario", "Diego"]) == 0
    assert main(["netsuite", "--datos", str(datos_caso), "--salida", str(out), "--usuario", "Diego"]) == 0
    final = out / "2026-09_polizas_netsuite.csv"
    assert final.exists() and _csv(final) == _csv(borrador)
    e1, e2 = _eventos(out)
    assert (e1["borrador"], e2["borrador"]) == (True, False)
    assert e2["archivo"] == final.name and e2["polizas"] == 27 and e2["omitidas"] == [] and len(e2["sha256"]) == 64


def test_el_comando_sin_mapa_completo_no_escribe_nada(datos_caso, tmp_path, capsys):
    out = tmp_path / "s"
    mapa = tmp_path / "cuentas.csv"
    mapa.write_text("cuenta,netsuite\n1101,Banco\n", encoding="utf-8")
    assert main(["netsuite", "--datos", str(datos_caso), "--salida", str(out), "--cuentas", str(mapa)]) == 1
    assert "1102" in capsys.readouterr().err
    assert list(out.glob("*netsuite*.csv")) == []


@pytest.mark.parametrize("debe,haber,motivo", [
    ("-5", "0", "a line with a negative amount (1101)"),
    ("0", "0", "a line with no amount (1101)"),
    ("5", "5", "a line with both a debit and a credit (1101)"),
])
def test_una_linea_que_netsuite_rechazaria_deja_fuera_su_poliza(datos_caso, debe, haber, motivo):
    from cierre_cxc.modelos import LineaPoliza
    s = ejecutar(datos_caso)
    s.polizas[1].lineas.append(LineaPoliza("1101", "Bancos MXN", Decimal(debe), Decimal(haber)))
    s.polizas[1].lineas.append(LineaPoliza("1101", "Bancos MXN", Decimal(haber), Decimal(debe)))   # still balances
    filas, omitidas = netsuite.filas(s)
    assert omitidas == [("P02", motivo)] and "CXC-202609-P02" not in {f["External ID"] for f in filas}


def test_un_salto_de_linea_en_la_nota_no_parte_la_fila(datos_caso):
    s = ejecutar(datos_caso)
    s.polizas[1].nota = "first line\nsecond line"
    filas, _ = netsuite.filas(s)
    assert {f["Memo"] for f in filas if f["External ID"] == "CXC-202609-P02"} == {
        "Collection A-1001 - Grupo Alfa Logistica SA de CV (exacto). first line second line"}
