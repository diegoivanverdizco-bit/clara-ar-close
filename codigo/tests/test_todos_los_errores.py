"""Every input error in one run (docs/2026-11-17 Diseño corrección de errores de captura.md, delivery 1).

The load used to stop at the first error, so a file with five errors needed five runs. Now it collects them:
- structure (file, sheets, headers): every structure error is listed and no row is read;
- cells: every cell that cannot be read is listed; a cell that fails skips the checks that depend on it;
- cross checks (columns or sheets that must agree, repeated keys): the first one per row, because the later ones
  are usually the same wrong cell seen again.
A single error keeps the exact message it had before.
"""
import openpyxl
import pytest

from cierre_cxc.cargar import SchemaError, cargar
from cierre_cxc.cli import main

B, C, R, A, T = "Bancos_Sep2026", "CFDI_Emitidos", "REP_Emitidos", "AR_Apertura_31Ago", "TC_FIX_Diario"


@pytest.fixture
def saboteado(datos_caso, tmp_path):
    def hacer(*mutaciones):
        wb = openpyxl.load_workbook(datos_caso)
        for m in mutaciones:
            m(wb)
        ruta = tmp_path / "saboteado.xlsx"
        wb.save(ruta)
        return ruta
    return hacer


def celda(hoja, fila, col, valor):
    def m(wb):
        wb[hoja].cell(fila, col).value = valor
    return m


def repetir(hoja, n):
    return lambda wb: wb[hoja].append([c.value for c in wb[hoja][n]])


def problemas(ruta):
    with pytest.raises(SchemaError) as e:
        cargar(ruta)
    return e.value, e.value.problemas


def test_junta_los_errores_de_varias_hojas(saboteado):
    error, p = problemas(saboteado(celda(C, 2, 9, 16), celda(B, 3, 6, "290,000.00"), celda(C, 4, 6, "EUR"),
                                   celda(T, 40, 2, -18)))
    assert [(x.hoja, x.fila, x.celda) for x in p] == [(T, 40, "B40"), (C, 2, "I2"), (C, 4, "F4"), (B, 3, "F3")]
    assert [x.columna for x in p[1:]] == ["Tasa IVA", "Moneda", "Abono"]
    assert p[1].valor == 16 and p[3].valor == "290,000.00"
    texto = str(error)
    assert texto.startswith("The file has 4 errors.")
    for x in p:
        assert x.mensaje in texto


def test_cada_celda_de_una_misma_fila_cuenta(saboteado):
    _, p = problemas(saboteado(celda(C, 2, 9, 16), celda(C, 2, 6, "EUR"), celda(C, 2, 3, "no es fecha")))
    assert sorted(x.celda for x in p) == ["C2", "F2", "I2"]


@pytest.mark.parametrize("nombre,mutaciones,mensaje", [
    ("amount as text is not also negative or both", [celda(B, 3, 6, "290,000.00")], "is not a number"),
    ("cargo as text skips the cargo-and-abono check", [celda(B, 3, 5, "1,000")], "is not a number"),
    ("total as text skips the type check", [celda(C, 10, 11, "abc"), celda(C, 10, 14, "P")], "is not a number"),
    ("rate as text skips the MXN-with-rate check", [celda(C, 12, 7, "abc")], "is not a number"),
    ("wrong opening currency is one error, not three", [celda(A, 5, 4, "MXN")], "but its CFDI is in USD"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_una_celda_mala_no_repite_errores(saboteado, nombre, mutaciones, mensaje):
    error, p = problemas(saboteado(*mutaciones))
    assert len(p) == 1 and mensaje in p[0].mensaje
    assert str(error) == p[0].mensaje


def test_un_solo_error_conserva_el_mensaje_de_antes(saboteado):
    error, p = problemas(saboteado(celda(C, 2, 9, 16)))
    assert str(error) == ("CFDI_Emitidos!row 2, column 'Tasa IVA': IVA rate 16 looks like a percentage. "
                          "Type it as a fraction (0.16 for 16%).")


def test_la_estructura_se_detiene_sin_leer_filas(saboteado):
    error, p = problemas(saboteado(lambda wb: wb.remove(wb[A]), celda(B, 1, 6, "Monto entrada"), celda(C, 2, 9, 16)))
    assert len(p) == 2
    assert "missing columns: abono" in p[0].mensaje and "add the sheet with its headers only" in p[1].mensaje
    assert "looks like a percentage" not in str(error)


def test_cruces_y_repetidos_de_varias_filas(saboteado):
    _, p = problemas(saboteado(celda(C, 12, 7, 17.5), celda(A, 2, 7, 2900000), celda(A, 5, 4, "MXN"),
                               repetir(C, 2), repetir(A, 3)))
    textos = [x.mensaje for x in p]
    assert len(p) == 5
    assert any("is in MXN but has exchange rate" in t for t in textos)
    assert any("must equal its balance" in t for t in textos)
    assert any("but its CFDI is in USD" in t for t in textos)
    assert any("CFDI folio A-1001 appears twice" in t for t in textos)
    assert any("Opening AR folio" in t and "appears twice" in t for t in textos)


def test_fila_pegada_dos_veces_con_un_error_de_celda(saboteado):
    # the cell error shows in both copies, and the repeated row is listed in the same run (once, not also by UUID)
    _, p = problemas(saboteado(celda(C, 2, 9, 16), repetir(C, 2)))
    textos = [x.mensaje for x in p]
    assert len(p) == 3
    assert sum("looks like a percentage" in t for t in textos) == 2
    assert sum("appears twice" in t for t in textos) == 1 and "CFDI folio A-1001 appears twice" in textos[2]


def test_muchos_errores_se_resumen(saboteado, datos_caso):
    tasas = sum(isinstance(r[1].value, (int, float)) for r in openpyxl.load_workbook(datos_caso)[T].iter_rows(min_row=2))
    error, p = problemas(saboteado(lambda wb: [setattr(r[1], "value", 1 / r[1].value) for r in wb[T].iter_rows(min_row=2)
                                               if isinstance(r[1].value, (int, float))]))
    texto = str(error)
    assert len(p) == tasas > 20
    assert texto.startswith(f"The file has {tasas} errors.")
    assert "20. " in texto and "21. " not in texto and f"and {tasas - 20} more" in texto


def test_la_linea_de_comandos_muestra_todos(saboteado, tmp_path, capsys):
    ruta = saboteado(celda(C, 2, 9, 16), celda(B, 3, 6, "290,000.00"))
    assert main(["run", "--datos", str(ruta), "--salida", str(tmp_path / "salida")]) == 1
    err = capsys.readouterr().err
    assert "The file has 2 errors." in err and "looks like a percentage" in err and "'290,000.00' is not a number" in err
