"""D2 surprise exam: a receipt that names its invoice must not ask AR to confirm the application order."""
from test_b3_ronda1 import banco, libro, mxn
from cierre_cxc.pipeline import ejecutar


def _orden(s):
    return [e for e in s.excepciones if e.categoria == "Application order"]


def test_cobro_con_folio_no_pide_confirmar_el_orden(tmp_path):
    # Z-1 is older and still open; the payer names Z-2 in the description (SAT payments guide, numeral 3).
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 1, 1000), mxn("Z-2", 10, 2000)],
                 [banco(20, "SPEI RECIBIDO TEXTILES PAGO FACT Z-2", abono=2320)])
    s = ejecutar(ruta)
    assert [(a.folio, a.pago_monto) for a in s.res.aplicaciones] == [("Z-2", 2320)]
    assert _orden(s) == []
    assert s.res.partidas["Z-1"].saldo == 1160


def test_cobro_sin_folio_sigue_yendo_a_la_mas_antigua_y_avisa(tmp_path):
    # Criterion 1 is unchanged: no folio, amount equals the newer invoice -> oldest first and an exception.
    ruta = libro(tmp_path / "m.xlsx", [mxn("Z-1", 1, 1000), mxn("Z-2", 10, 2000)],
                 [banco(20, "SPEI RECIBIDO TEXTILES", abono=2320)])
    s = ejecutar(ruta)
    assert s.res.partidas["Z-1"].saldo == 0
    assert _orden(s)
