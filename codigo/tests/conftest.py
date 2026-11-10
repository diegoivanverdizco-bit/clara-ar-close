import sys
from decimal import Decimal
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

DATOS = RAIZ.parent / "material" / "2026-11-06 Case_Data_Sep2026.xlsx"


def D(x):
    return Decimal(str(x))


@pytest.fixture(scope="session")
def datos_caso():
    if not DATOS.exists():
        pytest.skip("case workbook not available")
    return DATOS


@pytest.fixture(scope="session")
def salida(datos_caso):
    from cierre_cxc.pipeline import ejecutar
    return ejecutar(datos_caso)
