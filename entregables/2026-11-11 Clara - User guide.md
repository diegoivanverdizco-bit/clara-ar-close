# cierre_cxc: user guide

How to run the month-end AR close with the tool and review it. Setup and architecture are in `codigo/README.md`.

## Every month

1. **One output folder per close.** For example `salida/2026-09`. The review state, the learned rules and the audit
   log live there; rules carry over to the next month only if you copy `reglas_aprendidas.json` and the audit log.
2. **Run:** `python -m cierre_cxc run --datos <file.xlsx> --salida <folder> --usuario <your name>`.
   It prints the period it used. If it is not the month you expect, stop and check the file.
3. **Fix the input, not the output.** If the input has errors, the run lists all of them (sheet, row, column and
   value) and proposes a correction for each one. Approve them as shown below, or fix the cells in the input
   workbook and run again. Never edit the Excel the tool produces.
4. **Review** in the browser (`python -m cierre_cxc ui --datos <file.xlsx>`) or with the commands below.
5. The close is final only when the file is called `..._APROBADO.xlsx`.
6. **Journal entries for NetSuite:** `netsuite --datos <file.xlsx> --salida <folder>` or **Build NetSuite CSV** on
   the screen. Import only `YYYY-MM_polizas_netsuite.csv`; a `_BORRADOR` file means something is still pending.

## Input errors: approve the corrections

The code proposes every correction from the file itself; you approve it. The original file is never changed: the
approved corrections go to a copy (`<date> <name> corregido.xlsx`), the copy is checked again and, if it has no
errors left, the close runs on it. Every approval and rejection goes to the audit log with your name, the cell, the
value before and after.

| Label | Example | What you do |
|---|---|---|
| SAFE | VAT rate `16` where subtotal x 0.16 equals the VAT; amount typed as text `"290,000.00"` | Approve them all at once |
| TO CONFIRM | `"69,600"`: 69600 or 69.6? | Pick one option, or leave it as is |
| NOT FIXED | an empty date, a currency the tool does not handle | Fix it in the input file: nothing in the file says the right value |
| STRUCTURE | a missing sheet or column | Fix the file; nothing else is checked until then |

- **In the browser:** **Approve all SAFE**, one selector per TO CONFIRM, then **Write corrected copy and re-run**.
  **Ask AI which option fits** gives, for each TO CONFIRM, the option the AI finds more likely and why in one
  sentence (it sees the cell, the options and the range of the other values in that column). It is only a
  suggestion: it never writes a value or picks for you, and the log records what it suggested next to what you chose.
- **In the terminal:** `fix --datos <file.xlsx>` lists them as P1, P2...; then
  `fix --datos <file.xlsx> --usuario <your name> --seguras --elegir P2=1 --rechazar P3`. If the file changed after
  the list was shown, the numbers are refused: list again.

## The three levels of a receipt

| Level | What you do |
|---|---|
| 1 Automatic | Check and approve. The alert column says why it was placed there. |
| 2 To confirm | Pick one of the proposed invoices (`accept B08 --candidato 1`, or one click in the UI). Nothing is applied until you do. |
| 3 Needs a person | Find out who paid (bank, customer), then assign it (`override B08 --asignar A-1012`). |

When you place a receipt whose description named nobody, the tool learns that payer for next month. If it learned
something wrong: `rules --quitar R01`.

## Undoing a decision

Every command below also takes `--datos <file.xlsx> --salida <folder> --usuario <your name>`.

| You want to | Command |
|---|---|
| Change where a receipt goes | `override <B##> --asignar <folio,...>` (again, with the new invoices) |
| Undo an override | `remove-override <B##>` |
| Reject a conditional rate correction | `reject-correction <folio>` |
| Stop using a learned rule | `rules --quitar <R##>` |

Every one of them reopens all approvals, because one change can move other receipts.

## Do not

- Edit `revision.json`, `reglas_aprendidas.json` or `bitacora_auditoria.jsonl` by hand. The tool detects it and
  puts every approval back to pending.
- Reuse an output folder for another month or another file. The tool sets the earlier decisions aside, but a new
  folder keeps each close clean.
- Reinstall the environment on the day of a demo or a close: use the existing `codigo/.venv`.

## Notices that do not stop the run

They are listed in the Summary sheet under "Data notices". Read each one: if it reveals a typo, fix the input and run
again; if the data is right (e.g. a customer really has Net 45), no action is needed.
