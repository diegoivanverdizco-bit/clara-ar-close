"""Three levels for customer receipts, and rules learned from reviewers' decisions.

1 Automatic: the description names the invoice, the customer or a learned payer, and the amount fits.
2 To confirm: only a clue (the amount, or a language model's suggestion). Held in 2101 with ranked candidates;
  nothing is applied until a reviewer accepts one.
3 Needs a person: no clue at all. Held in 2101.
"""
import json
from datetime import datetime

import openpyxl
import pytest

from conftest import D
from test_sabotaje import B, celda, huella
from cierre_cxc.aprendizaje import Reglas, tokens_distintivos
from cierre_cxc.cli import main
from cierre_cxc.exportar import conteo_niveles, filas_movimientos
from cierre_cxc.pipeline import ejecutar
from cierre_cxc.revision import Revision

ZETA = {"conciliacion": {"B08": {"accion": "asignar", "folios": ["A-1012"], "usuario": "Diego"}}}


@pytest.fixture
def libro(datos_caso, tmp_path):
    def hacer(*mutaciones, nombre="caso.xlsx"):
        wb = openpyxl.load_workbook(datos_caso)
        for m in mutaciones:
            m(wb)
        ruta = tmp_path / nombre
        wb.save(ruta)
        return ruta
    return hacer


def deposito(descripcion, monto, dia=30):
    return lambda wb: wb[B].append([datetime(2026, 9, dia), "MXN-001", f"SPEI-202609{dia}-0001", descripcion,
                                    None, monto, "MXN"])


def test_septiembre_conserva_sus_cifras_y_cada_cobro_tiene_nivel(salida):
    n = conteo_niveles(salida)
    assert n == {"1 Automatic": 9,"2 To confirm": 0, "3 Needs a person": 1, "Reviewer decided": 0}
    assert salida.aging["total"] == D("1759932.85")
    assert not [e for e in salida.excepciones if e.categoria == "Match to confirm"]
    b08 = next(f for f in filas_movimientos(salida) if f["id"] == "B08")
    assert b08["nivel"] == "3 Needs a person"


def test_solo_el_monto_no_aplica_el_cobro_lo_propone(libro, salida):
    s = ejecutar(libro(celda(B, 3, 4, None)))           # B03 loses its description: 290,000 fits only A-1001
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B03"]
    u = next(x for x in s.res.efectivo_no_identificado if x["mov_id"] == "B03")
    assert u["nivel"] == 2 and u["monto"] == D(290000)
    assert u["candidatos"][0]["folio"] == "A-1001" and u["candidatos"][0]["evidencia"].startswith("exact amount")
    e = next(e for e in s.excepciones if e.referencia == "B03")
    assert e.categoria == "Match to confirm" and "A-1001" in e.que and "accept B03" in e.accion
    assert s.aging["total"] == salida.aging["total"] + D(290000)      # the invoice stays open until confirmed
    assert s.controles_ok
    fila = next(f for f in filas_movimientos(s) if f["id"] == "B03")
    assert fila["nivel"] == "2 To confirm" and fila["facturas"] == "candidates: A-1001"


def test_aceptar_el_candidato_por_linea_de_comandos_da_el_cierre_limpio(libro, salida, tmp_path, capsys):
    ruta = libro(celda(B, 3, 4, None))
    out = str(tmp_path / "out")
    assert main(["run", "--datos", str(ruta), "--salida", out]) == 0
    assert "To confirm B03: [1] A-1001" in capsys.readouterr().out
    assert main(["accept", "B03", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"]) == 0
    assert "B03 assigned to A-1001" in capsys.readouterr().out
    rev = Revision(tmp_path / "out" / "revision.json")
    assert rev.datos["conciliacion"]["B03"]["folios"] == ["A-1001"]
    s = ejecutar(ruta, decisiones=rev.como_decisiones(), reglas=Reglas(tmp_path / "out" / "reglas_aprendidas.json"))
    assert huella(s) == huella(salida)
    # an empty description has nothing that identifies the payer: no rule, and the run says why
    assert not s.reglas and any("no word that identifies the payer" in a for a in s.res.avisos)


def test_accept_rechaza_un_candidato_que_no_existe(libro, tmp_path, capsys):
    ruta = libro(celda(B, 3, 4, None))
    out = str(tmp_path / "out")
    assert main(["accept", "B03", "--datos", str(ruta), "--salida", out, "--usuario", "Diego", "--candidato", "7"]) == 1
    assert "--candidato must be between 1 and" in capsys.readouterr().err
    assert main(["accept", "B07", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"]) == 1
    assert "has no proposed candidates" in capsys.readouterr().err


def test_la_decision_del_revisor_se_aprende_y_el_siguiente_deposito_cae_solo(libro, tmp_path):
    # a second deposit from the same payer, for what is left of A-1012 after B08 (300,000 - 34,800)
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA SA DE CV REF 99812", 265200))
    antes = ejecutar(ruta)
    assert {u["mov_id"] for u in antes.res.efectivo_no_identificado} == {"B08", "B16"}

    reglas = Reglas(tmp_path / "reglas.json")
    s = ejecutar(ruta, decisiones=ZETA, reglas=reglas)
    assert [r["tokens"] for r in s.reglas_nuevas] == [["COMERCIALIZADORA", "ZETA"]]
    assert s.reglas_nuevas[0]["cliente"].startswith("Constructora Pacifico")
    # the same run already uses the new rule, so what the reviewer approves does not move later
    ap = [a for a in s.res.aplicaciones if a.mov_id == "B16"]
    assert ap and ap[0].folio == "A-1012" and ap[0].tipo == "exacto" and ap[0].confianza == "media"
    assert "learned rule R01" in " ".join(ap[0].alertas)
    assert s.res.propuestas["B16"].modo == "aprendido" and s.res.propuestas["B16"].nivel == 1
    assert s.res.partidas["A-1012"].saldo == 0 and s.controles_ok
    # stored for next month, with who taught it
    guardado = json.loads((tmp_path / "reglas.json").read_text(encoding="utf-8"))["reglas"][0]
    assert guardado["usuario"] == "Diego" and guardado["movimiento"] == "B08" and guardado["activa"] is True
    # another run does not learn it twice
    assert ejecutar(ruta, decisiones=ZETA, reglas=Reglas(tmp_path / "reglas.json")).reglas_nuevas == []


def test_una_regla_nunca_aplica_si_el_monto_no_cabe(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 999999))
    s = ejecutar(ruta, decisiones=ZETA, reglas=Reglas(tmp_path / "reglas.json"))
    assert s.res.propuestas["B16"].modo == "aprendido"
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B16" and a.aplicado_doc > a.saldo_antes]
    assert s.controles_ok


def test_quitar_una_regla_regresa_el_deposito_y_reabre_aprobaciones(libro, tmp_path, capsys):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    out = tmp_path / "out"
    assert main(["override", "B08", "--asignar", "A-1012", "--datos", str(ruta), "--salida", str(out),
                 "--usuario", "Diego", "--comentario", "Zeta pays on behalf of Pacifico"]) == 0
    assert main(["approve", "--datos", str(ruta), "--salida", str(out), "--usuario", "Diego"]) == 0
    assert "Learned rule R01" in capsys.readouterr().out
    assert main(["rules", "--salida", str(out)]) == 0
    assert "R01 [active] 'COMERCIALIZADORA ZETA'" in capsys.readouterr().out
    assert main(["rules", "--salida", str(out), "--quitar", "R01", "--usuario", "Diego"]) == 0
    assert Revision(out / "revision.json").datos["items"] == {}      # approvals went back to pending
    reglas = Reglas(out / "reglas_aprendidas.json")
    assert reglas.activas() == [] and reglas.reglas[0]["quitada_por"] == "Diego"   # kept, marked inactive
    s = ejecutar(ruta, decisiones=Revision(out / "revision.json").como_decisiones(), reglas=reglas)
    assert s.res.propuestas["B16"].modo == "monto"
    # the override of B08 is still there, but a rule a person removed is not learned again from it
    assert s.reglas_nuevas == [] and reglas.activas() == []
    assert main(["rules", "--salida", str(out), "--quitar", "R09", "--usuario", "Diego"]) == 1
    assert "no learned rule R09" in capsys.readouterr().err


def test_si_el_revisor_corrige_al_cliente_la_regla_se_reemplaza(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 50000))
    reglas = Reglas(tmp_path / "reglas.json")
    ejecutar(ruta, decisiones=ZETA, reglas=reglas)
    otro = {"conciliacion": {"B08": {"accion": "asignar", "folios": ["A-1008"], "usuario": "Ana"}}}
    s = ejecutar(ruta, decisiones=otro, reglas=reglas)
    activas = reglas.activas()
    assert len(activas) == 1 and activas[0]["cliente"].startswith("Hotelera Riviera") and activas[0]["usuario"] == "Ana"
    assert reglas.reglas[0]["activa"] is False and reglas.reglas[0]["reemplazada_por"] == activas[0]["id"]
    assert [r["id"] for r in s.reglas] == [activas[0]["id"]]


def test_reglas_de_dos_clientes_que_chocan_no_se_usan(tmp_path):
    reglas = Reglas(tmp_path / "r.json")
    reglas.aprender([{"mov_id": "B1", "descripcion": "SPEI ZETA", "cliente_clave": "K1", "cliente": "Uno"},
                     {"mov_id": "B2", "descripcion": "SPEI COMERCIALIZADORA ZETA", "cliente_clave": "K2", "cliente": "Dos"}])
    assert reglas.buscar("SPEI RECIBIDO COMERCIALIZADORA ZETA") == (None, True)
    assert reglas.buscar("SPEI RECIBIDO ZETA")[0]["cliente_clave"] == "K1"


def test_palabras_de_banco_y_referencias_no_identifican_a_nadie():
    assert tokens_distintivos("SPEI RECIBIDO BBVA DEPOSITO EFECTIVO REF 4412 SEPTIEMBRE") == []
    assert tokens_distintivos("SPEI RECIBIDO COMERCIALIZADORA ZETA SA DE CV A1B2C3") == ["COMERCIALIZADORA", "ZETA"]


def test_archivo_de_reglas_danado_se_detiene_con_mensaje(libro, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "reglas_aprendidas.json").write_text("{no es json", encoding="utf-8")
    assert main(["run", "--datos", str(libro()), "--salida", str(out)]) == 1
    assert "reglas_aprendidas.json is damaged" in capsys.readouterr().err


def test_el_excel_muestra_el_nivel_y_las_reglas(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    out = tmp_path / "out"
    assert main(["override", "B08", "--asignar", "A-1012", "--datos", str(ruta), "--salida", str(out),
                 "--usuario", "Diego"]) == 0
    assert main(["run", "--datos", str(ruta), "--salida", str(out)]) == 0
    wb = openpyxl.load_workbook(next(out.glob("*_BORRADOR.xlsx")))
    textos = [c for fila in wb["Summary"].iter_rows(values_only=True) for c in fila if isinstance(c, str)]
    assert "Customer receipts by how they were placed" in textos and "   Reviewer decided" in textos
    assert "nivel" in [c.value for c in wb["A1_Bank_Movements"][1]]
    reglas = list(wb["Learned_Rules"].iter_rows(values_only=True))
    assert reglas[1][1] == "COMERCIALIZADORA ZETA" and reglas[1][3] == "Diego"


# ---- round 8 of the sabotage: learned rules -------------------------------------------------------------------
def _con_regla(ruta, tmp_path):
    """Teach R01 'COMERCIALIZADORA ZETA' -> Constructora Pacifico from the override of B08."""
    reglas = Reglas(tmp_path / "reglas.json")
    ejecutar(ruta, decisiones=ZETA, reglas=reglas)
    return reglas


def test_regla_sin_monto_exacto_no_aplica_queda_por_confirmar(libro, tmp_path):
    """Before: a rule alone placed a 50,000 partial payment at level 1."""
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 50000))
    s = ejecutar(ruta, decisiones=ZETA, reglas=_con_regla(ruta, tmp_path))
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B16"]
    u = next(x for x in s.res.efectivo_no_identificado if x["mov_id"] == "B16")
    assert u["nivel"] == 2 and u["candidatos"][0]["cliente"].startswith("Constructora Pacifico")
    assert "learned rule R01" in u["candidatos"][0]["evidencia"]


@pytest.mark.parametrize("descripcion", ["SPEI RECIBIDO COMERCIALIZADORA ZETA GAS SUR",
                                         "SPEI RECIBIDO JUAN COMERCIALIZADORA ZETA LOPEZ"])
def test_descripcion_con_palabras_que_la_regla_no_vio_no_se_asume(libro, tmp_path, descripcion):
    """Before: any description containing the rule's words was applied to the learned customer."""
    ruta = libro(deposito(descripcion, 265200))
    s = ejecutar(ruta, decisiones=ZETA, reglas=_con_regla(ruta, tmp_path))
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B16"]
    assert "which the rule never saw" in " ".join(s.res.propuestas["B16"].alertas)


def test_quitar_la_reasignacion_retira_la_regla(libro, tmp_path, capsys):
    """Before: removing the override left its rule active, and the rule placed the receipt again."""
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    out = str(tmp_path / "out")
    main(["override", "B08", "--asignar", "A-1012", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    main(["run", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    assert Reglas(tmp_path / "out" / "reglas_aprendidas.json").activas()
    assert main(["remove-override", "B08", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"]) == 0
    capsys.readouterr()
    main(["run", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    assert "was retired" in capsys.readouterr().out
    assert Reglas(tmp_path / "out" / "reglas_aprendidas.json").activas() == []
    s = ejecutar(ruta, decisiones=Revision(tmp_path / "out" / "revision.json").como_decisiones(),
                 reglas=Reglas(tmp_path / "out" / "reglas_aprendidas.json"))
    assert not [a for a in s.res.aplicaciones if a.mov_id in ("B08", "B16")]


def test_rechazar_la_conciliacion_retira_la_regla_y_no_la_reaprende(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    reglas = _con_regla(ruta, tmp_path)
    assert reglas.activas()
    rechazo = dict(ZETA, conciliaciones_rechazadas=["B08"])
    s = ejecutar(ruta, decisiones=rechazo, reglas=reglas)
    assert s.reglas_retiradas and reglas.activas() == [] and s.reglas_nuevas == []
    assert ejecutar(ruta, decisiones=rechazo, reglas=reglas).reglas_nuevas == []


def test_remove_override_de_un_movimiento_sin_reasignacion(libro, tmp_path, capsys):
    ruta = libro()
    assert main(["remove-override", "B13", "--datos", str(ruta), "--salida", str(tmp_path / "o"), "--usuario", "D"]) == 1
    assert "There is no override of B13" in capsys.readouterr().err


@pytest.mark.parametrize("editar,señal", [
    (lambda r: r.update(cliente_clave="SDO110101AA1", cliente="Servicios Digitales Omega"), "another customer"),
    (lambda r: r.update(tokens=["ZETA"]), "other words"),
])
def test_regla_editada_a_mano_no_se_usa(libro, tmp_path, editar, señal):
    """Before: a rule whose customer was edited by hand sent 127,600 to another customer silently."""
    from cierre_cxc.auditoria import Bitacora
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    bit = Bitacora(tmp_path / "log.jsonl")
    ejecutar(ruta, decisiones=ZETA, reglas=Reglas(tmp_path / "reglas.json", bit))
    d = json.loads((tmp_path / "reglas.json").read_text(encoding="utf-8"))
    editar(d["reglas"][0])
    (tmp_path / "reglas.json").write_text(json.dumps(d), encoding="utf-8")
    reglas = Reglas(tmp_path / "reglas.json", bit)
    assert reglas.activas() == [] and any(señal in x for x in reglas.discrepancias)
    s = ejecutar(ruta, decisiones=ZETA, reglas=reglas)
    assert any("Learned rules are NOT used" in a for a in s.res.avisos)


def test_regla_quitada_reactivada_a_mano_no_se_usa(libro, tmp_path):
    from cierre_cxc.auditoria import Bitacora
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    bit = Bitacora(tmp_path / "log.jsonl")
    reglas = Reglas(tmp_path / "reglas.json", bit)
    ejecutar(ruta, decisiones=ZETA, reglas=reglas)
    reglas.quitar("R01", "Diego")
    d = json.loads((tmp_path / "reglas.json").read_text(encoding="utf-8"))
    d["reglas"][0]["activa"] = True
    (tmp_path / "reglas.json").write_text(json.dumps(d), encoding="utf-8")
    assert Reglas(tmp_path / "reglas.json", bit).activas() == []


def test_regla_escrita_a_mano_sin_cliente(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps({"reglas": [{"id": "R01", "tokens": ["ZETA"], "cliente_clave": "X"}]}))
    from cierre_cxc.revision import RevisionError
    with pytest.raises(RevisionError, match="expected structure"):
        Reglas(tmp_path / "r.json")


# ---- round 9 of the sabotage ----------------------------------------------------------------------------------
def test_rechazar_la_conciliacion_por_linea_de_comandos_no_reaprende_la_regla(libro, tmp_path, capsys):
    """Before: the rejection was wiped when approvals were reopened, and the same run learned R02 again."""
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    out = str(tmp_path / "out")
    main(["override", "B08", "--asignar", "A-1012", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    main(["run", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    main(["approve", "--datos", str(ruta), "--salida", out, "--usuario", "Ana", "--items", "match:B08", "--estado", "rechazado"])
    for _ in range(2):
        main(["run", "--datos", str(ruta), "--salida", out, "--usuario", "Diego"])
    assert Reglas(tmp_path / "out" / "reglas_aprendidas.json").activas() == []
    assert Revision(tmp_path / "out" / "revision.json").estado("match:B08") == "rechazado"


@pytest.mark.parametrize("accion", ["no_identificado", "no_cxc"])
def test_cambiar_la_reasignacion_a_otra_accion_retira_la_regla(libro, tmp_path, accion):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 265200))
    reglas = _con_regla(ruta, tmp_path)
    s = ejecutar(ruta, decisiones={"conciliacion": {"B08": {"accion": accion, "folios": []}}}, reglas=reglas)
    assert s.reglas_retiradas and reglas.activas() == []


def test_regla_no_esconde_la_factura_de_otro_cliente_con_monto_exacto(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO COMERCIALIZADORA ZETA", 110200))     # exactly A-1017 (Hotelera Riviera)
    s = ejecutar(ruta, decisiones=ZETA, reglas=_con_regla(ruta, tmp_path))
    u = next(x for x in s.res.efectivo_no_identificado if x["mov_id"] == "B16")
    assert u["nivel"] == 2 and "A-1017" in [c["folio"] for c in u["candidatos"]]


def test_internet_en_las_notas_no_es_un_plazo(libro, salida):
    """Before: 'Internet 100 Mbps; Net 30' was read as 100-day terms and moved the due date."""
    s = ejecutar(libro(celda("CFDI_Emitidos", 17, 17, "Servicio de Internet 100 Mbps; Net 30")))
    assert s.res.partidas["A-1015"].vencimiento == salida.res.partidas["A-1015"].vencimiento
    assert not any("100 days" in a for a in s.res.avisos)


# ---- misspelled customer names (decided by Diego: level 2, a person picks from the list) ---------------------
# one exact distinctive word ("FARMACIAS") already identifies the customer (level 1); here every word has a typo
@pytest.mark.parametrize("descripcion", ["SPEI RECIBIDO FARMACAIS DEL VALE", "SPEI RECIBIDO FRAMACIAS DEL BALLE"])
def test_nombre_mal_escrito_queda_por_confirmar_con_el_cliente_parecido(libro, descripcion):
    s = ejecutar(libro(deposito(descripcion, 87000)))           # 87,000 = A-1011, Farmacias del Valle
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B16"]
    p = s.res.propuestas["B16"]
    assert p.nivel == 2 and p.candidatos[0]["folio"] == "A-1011"
    assert p.candidatos[0]["evidencia"].startswith("name similar to Farmacias del Valle")


def test_aceptar_el_nombre_parecido_aprende_esa_forma_de_escribirlo(libro, tmp_path):
    ruta = libro(deposito("SPEI RECIBIDO FARMACAIS DEL VALE", 87000))
    reglas = Reglas(tmp_path / "reglas.json")
    s = ejecutar(ruta, decisiones={"conciliacion": {"B16": {"accion": "asignar", "folios": ["A-1011"]}}}, reglas=reglas)
    assert [r["tokens"] for r in s.reglas_nuevas] == [["FARMACAIS", "VALE"]]


def test_parecido_y_distancia():
    from decimal import Decimal
    from cierre_cxc.conciliar import Contexto, _distancia, _tok_parecido
    assert _distancia("RZEOVER", "REZOVER") == 1          # two swapped letters are one typo
    assert not _tok_parecido("SOL", "SUR")                 # short words are names, not typos
    ctx = Contexto(None, None, {}, {"K1": "TECNOLOGIAS PAVEVAS SAPI DE CV", "K2": "TECNOLOGIAS FODAVER SA DE CV"})
    k, pares, _ = ctx.parecido("ABONO TECNOLOGIAS FODVER")
    assert k == "K2" and ("FODVER", "FODAVER") in pares
    assert ctx.parecido("ABONO TECNOLOGIAS XYZ") == (None, [], Decimal(0))


# ---- name known, invoice not: several open invoices of that customer (decided by Diego: level 2) -------------
@pytest.mark.parametrize("descripcion,monto,dia,folios", [
    ("SPEI RECIBIDO FARMACIAS DEL VALLE", 50000, 30, {"A-1010", "A-1011"}),        # partial, two open invoices
    ("SPEI RECIBIDO HOTELERA RIVIERA MAYA", 400000, 30, {"A-1008", "A-1017"}),     # more than both, not their sum
    ("SPEI RECIBIDO FARMACIAS DEL VALLE", 87000, 5, {"A-1010", "A-1011"}),         # equals two identical invoices
])
def test_nombre_sin_factura_con_varias_abiertas_queda_por_confirmar(libro, descripcion, monto, dia, folios):
    s = ejecutar(libro(deposito(descripcion, monto, dia)))
    assert not [a for a in s.res.aplicaciones if a.mov_id == "B16"]
    p = s.res.propuestas["B16"]
    assert p.nivel == 2 and folios <= {c["folio"] for c in p.candidatos}
    assert "is named in the description" in p.candidatos[0]["evidencia"]


def test_nombre_con_una_sola_factura_abierta_se_sigue_aplicando(salida):
    # B04 Retail Norte 'ABONO PARCIAL': one open invoice, so the partial payment is applied (level 1)
    ap = [a for a in salida.res.aplicaciones if a.mov_id == "B04"]
    assert [a.folio for a in ap] == ["A-1003"] and salida.res.propuestas["B04"].nivel == 1
