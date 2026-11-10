"""Generic RFCs (XEXX010101000 foreign customers, XAXX010101000 general public) are shared by many customers:
the customer is identified by RFC + name, not by the RFC alone."""
from datetime import datetime

from conftest import D
from cierre_cxc.pipeline import ejecutar
from test_b3_ronda1 import banco, libro, mxn

XEXX = "XEXX010101000"
XAXX = "XAXX010101000"


def _usd0(folio, dia, total, cliente, rfc=XEXX, notas="Net 30"):
    return [folio, datetime(2026, 10, dia), rfc, cliente, "USD", 17.3, total, 0.0, 0.0, total,
            "PPD", "99", "I", None, "Vigente", notas]


def _dos_extranjeros(tmp_path, nombre="dos.xlsx", rfc=XEXX):
    facturas = [_usd0("P-1", 1, 1000, "Pacific Foods Inc", rfc), _usd0("N-1", 5, 1000, "Nordic Trade GmbH", rfc)]
    bancos = [banco(20, "SPEI RECIBIDO NORDIC TRADE GMBH", abono=1000, moneda="USD")]
    return ejecutar(libro(tmp_path / nombre, facturas, bancos, fix=17.3))


def test_cobro_sin_folio_va_al_extranjero_que_nombra_la_descripcion(tmp_path):
    s = _dos_extranjeros(tmp_path)
    assert [(a.folio, a.aplicado_doc) for a in s.res.aplicaciones] == [("N-1", D("1000"))]
    assert s.res.partidas["P-1"].saldo == D("1000") and s.res.partidas["N-1"].saldo == D(0)
    assert sorted(s.res.clientes.values()) == ["Nordic Trade GmbH", "Pacific Foods Inc"]
    assert s.controles_ok, [c for c in s.controles if not c[1]]


def test_publico_en_general_tambien_se_separa_por_nombre(tmp_path):
    s = _dos_extranjeros(tmp_path, "xaxx.xlsx", rfc=XAXX)
    assert [(a.folio, a.aplicado_doc) for a in s.res.aplicaciones] == [("N-1", D("1000"))]


def test_dos_extranjeros_con_el_mismo_importe_no_son_duplicado(tmp_path):
    facturas = [_usd0("P-1", 7, 500, "Pacific Foods Inc"), _usd0("N-1", 7, 500, "Nordic Trade GmbH")]
    s = ejecutar(libro(tmp_path / "dup.xlsx", facturas, [banco(28, "PAGO NOMINA", cargo=1)]))
    assert not [e for e in s.excepciones if e.categoria == "Possible duplicate"]


def test_plazo_de_un_extranjero_no_se_compara_con_otro(tmp_path):
    facturas = [_usd0("P-1", 1, 100, "Pacific Foods Inc"), _usd0("P-2", 2, 200, "Pacific Foods Inc"),
                _usd0("N-1", 3, 300, "Nordic Trade GmbH", notas="Net 60"),
                _usd0("N-2", 4, 400, "Nordic Trade GmbH", notas="Net 60")]
    s = ejecutar(libro(tmp_path / "plazo.xlsx", facturas, [banco(28, "PAGO NOMINA", cargo=1)]))
    assert not [a for a in s.res.ds.avisos if "credit terms" in a]


# previous behavior that must hold
def test_mismo_extranjero_en_varias_facturas_sigue_siendo_un_cliente(tmp_path):
    facturas = [_usd0("P-1", 1, 1000, "Pacific Foods Inc"), _usd0("P-2", 5, 1000, "Pacific Foods Inc")]
    s = ejecutar(libro(tmp_path / "uno.xlsx", facturas,
                       [banco(20, "SPEI RECIBIDO PACIFIC FOODS", abono=1000, moneda="USD")]))
    assert len(s.res.clientes) == 1
    assert {s.res.partidas[f].cliente_clave for f in ("P-1", "P-2")} == {next(iter(s.res.clientes))}


def test_rfc_nacional_sigue_identificando_aunque_cambie_el_nombre(tmp_path):
    facturas = [mxn("T-1", 1, 1000, cliente="Textiles SA"), mxn("T-2", 2, 1000, cliente="Textiles S.A. de C.V.")]
    s = ejecutar(libro(tmp_path / "nac.xlsx", facturas, [banco(28, "PAGO NOMINA", cargo=1)]))
    assert list(s.res.clientes) == ["TRI100101AA1"]
    assert s.res.partidas["T-1"].cliente_clave == s.res.partidas["T-2"].cliente_clave == "TRI100101AA1"
