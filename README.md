# Clara · Mexico Accounting Case Study (September 2026 AR close)

| Part | What | Where |
|---|---|---|
| A | AR close workbook (cash application, exceptions, journal entries, aging, IVA) | `entregables/2026-11-11 Clara - Part A AR close Sep 2026.xlsx` |
| A | One-page memo to the Controller | `entregables/2026-11-11 Clara - Part A memo to the Controller.pdf` |
| B | `cierre_cxc`, the tool that repeats the close on any dataset with the same schema | [`codigo/README.md`](codigo/README.md) |
| B | Day-to-day user guide | [`entregables/2026-11-11 Clara - User guide.md`](entregables/2026-11-11%20Clara%20-%20User%20guide.md) |

Quick start (Python 3.11+):

```
cd codigo
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip
.venv\Scripts\python -m cierre_cxc run --datos "../material/2026-11-06 Case_Data_Sep2026.xlsx" --usuario "Your name"
.venv\Scripts\python -m pytest -q
```

All data in `material/` is fictional (provided with the case).
