# cierre_cxc: Mexican AR month-end close

A tool a teammate can run at every month-end. It takes the four data tabs (CFDI, bank, REP, FX), plus the opening
AR sub-ledger and the chart of accounts. With them it:

1. applies every bank receipt to its invoice(s) (cash application),
2. flags exceptions in the CFDI, REP and bank data,
3. proposes the September-style journal entries,
4. produces the corrected aging with a roll-forward, and
5. produces the IVA summary.

Nothing is final until a named reviewer approves it, and every number links back to the rows it came from.

## Setup (Windows, macOS or Linux, Python 3.11+)

```bash
cd codigo
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# 1. run the close: writes an Excel (DRAFT until approved), CSVs and the audit log into ./salida
python -m cierre_cxc run --datos "path/to/Case_Data.xlsx" --usuario "Diego"

# 2. review in the browser (approve / reject with comments, reassign a receipt, reject a conditional entry)
python -m cierre_cxc ui --datos "path/to/Case_Data.xlsx"

# the same review from the command line
python -m cierre_cxc accept B08 --datos "path/to/Case_Data.xlsx" --candidato 1 --usuario "Diego"   # take a proposed candidate
python -m cierre_cxc rules                                                     # learned rules; --quitar R01 to stop using one
python -m cierre_cxc remove-override B13 --datos "path/to/Case_Data.xlsx" --usuario "Diego"
python -m cierre_cxc override B13 --datos "path/to/Case_Data.xlsx" --asignar A-1015 --comentario "customer indicated A-1015" --usuario "Diego"
python -m cierre_cxc reject-correction A-1013 --datos "path/to/Case_Data.xlsx" --comentario "tax team: keep the stamped rate" --usuario "Diego"
python -m cierre_cxc approve --datos "path/to/Case_Data.xlsx" --items all --usuario "Diego"
python -m cierre_cxc run --datos "path/to/Case_Data.xlsx" --usuario "Diego"   # -> ..._APROBADO.xlsx

# tests
python -m pytest -q
```

Options: `--cierre YYYY-MM-DD` sets the period end (by default, the month with the most bank rows, so one row pasted
from another month cannot move the close); `--salida` sets the output folder; `--llm` turns on the language-model
assistant (see below).

## Input checks (human errors)

The workbook is filled in by people, so the loader assumes it can be wrong. Every error ends in one of three states:

- **Prevented:** read correctly anyway. Examples: headers in another order, case or spelling (`Depósito`, `Ingreso`),
  a title row above the header, UUIDs in lower case or in braces, folios stored as `1001.0`, `Pesos` / `Ingreso`
  written out.
- **Stopped with a message** that names the sheet, row and column and says what to fix. Examples: amounts typed as
  text (`290,000.00`), negative amounts, cargo and abono in the same row, an IVA rate typed as `16`, currencies other
  than MXN/USD, rows pasted twice, an old month's sheet left in the file, an MXN invoice with an exchange rate,
  opening balances that do not tie (MXN balance vs. balance × rate, or a currency different from the CFDI).
- **Flagged** as a notice or an exception in the output. Examples: bank rows dated outside the period (not processed),
  a duplicated bank row, text dates that could be day/month or month/day, an exchange rate 10 % away from the rest,
  a payer who typed another customer's invoice number, a receipt with a not-AR keyword that mentions an invoice.

`tests/test_sabotaje.py` keeps every one of these as a permanent test.

## Receipts the rules cannot place: three levels

Every customer receipt ends in one of three levels (column `nivel` in `A1_Bank_Movements`, counts in `Summary`):

| Level | When | What the tool does |
|---|---|---|
| 1 Automatic | The description names the invoice, the customer, or a payer a reviewer placed before, and the amount fits | Applies it |
| 2 To confirm | Only a clue: the amount fits an open invoice but the description names nobody, the name is misspelled ("FARMACAIS DEL VALE" is one or two letters away from a customer), the name is clear but the customer has several open invoices and the amount fits none exactly (partial, overpayment, or two identical invoices), or a language model suggested the customer | Holds it in 2101 and lists up to 3 candidates, best first, each with its evidence (exact amount or slightly short, due date, currency). Nothing is applied until a reviewer accepts one (`accept`, or one click in the UI) |
| 3 Needs a person | No clue at all | Holds it in 2101 with an action for treasury and AR |

An amount alone is never enough to apply a receipt: a coincidence would relieve another customer's invoice and the
error would hide inside a close that ties.

**Which invoice, when the payer does not say.** If the description names the customer but no invoice, the receipt
goes to that customer's oldest open invoice first, even when the amount matches a newer one exactly. This is the
order set by the SAT guide for the payments complement 2.0 (pp. 4-5): the payer may indicate the invoice within 5
calendar days of the payment (numeral 3); if it does not, the receiver applies the payment to the oldest pending CFDI
(numeral 4). When the amount points to a newer invoice, an `Application order` exception asks AR to confirm with the
customer by that 5th day; if the customer names the newer invoice, `override <movement> --asignar <folio>` reassigns
it before the REP is issued. Only invoices in the matched invoice's currency take part (assumption: the guide does
not mix currencies).

**Learned rules.** When a reviewer places a level-2 or level-3 receipt, and all of it goes to one customer, the tool
learns "a deposit whose description has these words is from this customer" (`reglas_aprendidas.json` next to the
output, sheet `Learned_Rules`). Next month the same payer is level 1. Safeguards:

- Only words that identify a payer are learned: bank names, transfer jargon, months and references are dropped. If
  nothing is left, no rule is learned and the run says why.
- A rule only names the customer. It is applied (level 1, "medium" confidence, with an alert that names the rule and
  who taught it) only when the amount matches one invoice exactly; anything else (partial, short, several invoices,
  overpayment) becomes level 2 with that customer's invoices as candidates.
- If the description has identifying words the rule never saw ("ZETA GAS SUR" for a rule "ZETA"), the payer is not
  assumed: level 2.
- If two rules point to different customers, neither is used. If a reviewer later places the same payer with
  another customer, the newer decision replaces the rule.
- `rules --quitar R01` stops using a rule. Nothing is deleted: it is marked inactive, it is not learned again from
  the same decision, and every approval goes back to pending. Every rule learned, replaced or removed is in the
  audit log.
- A run that learns a rule recomputes at once, so what the reviewer sees and approves already includes it.
- If the override that taught a rule is removed (`remove-override B08`) or its match is rejected, the rule is retired
  and every approval goes back to pending.
- `reglas_aprendidas.json` is cross-checked against the audit log: a rule edited or switched back on by hand makes the
  run use no rule at all, and says why.

## What it produces

| Output | Content |
|---|---|
| `Summary` | Key figures, the internal checks (PASS/FAIL), exception counts, data notices, review status |
| `A1_Bank_Movements` | Every bank row: classification (AR, not AR, bank charge), invoice(s), match type, confidence, reason, alerts, source row |
| `A1_By_Invoice` | Per invoice: cash received, AR relieved, realized FX, open balance and an explanation of any difference |
| `A2_Exceptions` | What is wrong, why it matters, amount at stake, certain vs. to confirm with tax, action, owner, deadline |
| `A3_Journal_Entries` / `A3_Not_Posted` | Proposed entries (conditional ones marked) and the items deliberately not posted, with the reason |
| `A4_Aging` / `A4_Aging_Summary` / `A4_Roll_Forward` | Corrected aging in MXN (Current / 1-30 / 31-60 / 60+) and the bridge from the opening balance |
| `A5_IVA_Collected` / `A5_IVA_Roll_Forward` | IVA cobrado at the FIX of the collection date and the IVA por trasladar bridge |
| `Learned_Rules` | Payers learned from reviewers' decisions: words, customer, who taught it and from which receipt |
| `Audit_Log` | Runs (with the SHA-256 of the input file), decisions, overrides, learned rules, exports and language-model calls |

## Architecture

```
cargar.py        reads the workbook, maps headers (case/accents/synonyms), validates the schema, keeps the source row of every record
fx.py            FIX lookup (falls back to the previous date and says so)
motor.py         builds the open items and replays the month in date order: credit notes, then bank rows
conciliar.py     matching rules (deterministic): invoice number in the description -> customer name (weighted fuzzy tokens)
                 -> learned payer -> exact amount -> several invoices in one receipt -> short payment (<= 2%) -> partial
                 -> overpayment -> unidentified. Cross-currency receipts are converted at the FIX of the payment date
                 (0.5% tolerance). Ranked candidates for receipts that name nobody (level 2). A customer is its RFC;
                 a generic RFC (XAXX/XEXX) is shared by many customers, so there it is RFC + name.
aprendizaje.py   learned rules: payer words -> customer, taught by reviewers' decisions
excepciones.py   independent rules over CFDI, REP and bank data
contabilidad.py  journal entries, aging, roll-forward, IVA
pipeline.py      runs everything and the internal checks
revision.py      reviewer decisions (approve / reject / override), stored in salida/revision.json and tied to the
                 SHA-256 of the input: if the input changes, earlier decisions are set aside and the review restarts.
                 An override or a rejected correction reopens every approval, because it can move other items.
                 It is cross-checked against the audit log: an older copy put back or a hand edit makes every
                 approval pending until the log and the file agree again.
auditoria.py     append-only JSON-lines audit log
asistente_llm.py optional language-model assistant, fenced (see below)
exportar.py      Excel and CSV
cli.py, app.py   command line and the Streamlit review UI
```

All money is `Decimal`, rounded only at the cent. Overrides never take amounts: the reviewer says *which invoices*,
and the engine recomputes everything.

## Internal checks (each run)

The tool runs these checks every time. They are shown as PASS/FAIL in `Summary`, and the process exits with code 2
if any check fails.

- Every journal entry balances.
- The roll-forward closing balance equals the corrected aging total.
- The IVA por trasladar roll-forward equals the sum of the open items.
- Every AR bank credit is either applied or held as unidentified.
- No application exceeds the open balance.
- No invoice has a negative balance.

## Where the language model is used, and how it is fenced

The model writes and suggests; the code computes and checks; a person approves. It needs `ANTHROPIC_API_KEY`
(the model is set with `CIERRE_LLM_MODEL`, default `claude-sonnet-5-5`). Without a key every feature below falls
back to the deterministic tool.

- **AI draft of the close write-up** (tab *AI draft*, one click, one call per close, 20 to 30 seconds). It
  prepares the reviewer's work so the reviewer only decides: (1) a summary for the Controller, (2) one card per high
  exception with what happens, a suggested solution and, when the case needs judgment, a question with two or three
  options, and (3) one email per owner, ready to copy (nothing is sent). The model receives the figures the tool
  already computed (folios, customer names, amounts, owners, deadlines), never UUIDs, bank accounts or bank
  references; plus the verified wording of each exception, the reviewer's past decisions, and the connections the
  code already found (a loan drawdown, a REP that belongs to a receipt waiting for confirmation, a receipt applied
  to the oldest invoice whose amount points to another). The reply is structured JSON (guaranteed by the API).
  **Every number and every folio in the draft must exist in the close**; if one does not, the whole draft is
  discarded and the deterministic template is shown, with the reason. The check covers figures and folios, not
  judgment: that is why the reviewer accepts or rejects each card. Each decision goes to the audit log, and the
  reviewer's choices and reasons are kept in `criterios_aprendidos.json` so the next draft follows them (they can be
  removed; they only steer wording, never amounts).
- **Customer suggestion** (`--llm` or the sidebar checkbox) for receipts the rules left unidentified. The model sees only the bank description and
  the customer names, never amounts, accounts or UUIDs. Its answer must be one of the given ids, otherwise it is
  ignored. The suggestion only narrows the candidate invoices: matching and amounts are still done in code. The
  result is a level-2 proposal: it is never applied until a reviewer accepts it.
- **Plain-language rewrite** of an exception. Any rewrite containing a number that is not in the original text is
  discarded.

Every call is written to the audit log (model, prompt hash, response, accepted or not). Cost: one short call per
unidentified receipt (usually zero to three per month) and one call per close for the draft; cents per month. No
sampling parameters are sent (current models reject `temperature`): determinism comes from the fences, not from
the sampler.

How model errors are tested: `tests/test_asistente_llm.py` uses a fake client to prove that

- an invented customer id is rejected,
- a valid suggestion still goes through the deterministic matcher and stays a proposal until a reviewer accepts it,
- a rewrite with an invented number is dropped, and
- every call is logged.

`tests/test_redaccion.py` does the same for the draft: an invented figure or folio, a reply that is not JSON and a
declined request all fall back to the template; a valid draft is accepted and logged; the call sends no
`temperature`.

## Tests

`python -m pytest -q` runs 328 tests:

- `test_caso_septiembre.py`: the control answers of the September case, worked out by hand.
- `test_sabotaje.py`: human errors injected into the case workbook (see "Input checks").
- `test_niveles.py`: the three levels, accepting a candidate, and learned rules (learning, replacing, removing,
  conflicts, a damaged rules file).
- `test_dataset_nuevo.py`: a synthetic October dataset with other customers, other header spellings and missing
  FX days. It also covers patterns the case does not have: one receipt for two invoices, an overpayment, and a
  cross-currency receipt with a small rate difference.
- `test_asistente_llm.py`: the model guards.
- `test_app.py`: the review UI, including the AI draft tab.
- `test_redaccion.py`: the AI draft and its number-and-folio check.
- `test_rfc_generico.py`: customers that share a generic RFC (XEXX010101000 foreign, XAXX010101000 general
  public) are told apart by RFC + name.

## Known limitations

How to use it day to day (one output folder per close, what not to edit by hand, how to undo a decision):
[`entregables/2026-11-11 Clara - User guide.md`](../entregables/2026-11-11%20Clara%20-%20User%20guide.md).

Accepted risks (approved by Diego, 2026-11-09). Each has a notice or a person in the loop; none moves money silently:

- A notice that turns out to be right (e.g. a customer's real Net 45) cannot be marked as reviewed: it shows again
  on every run.
- "Deposit typed as a withdrawal?" can flag a genuine refund or a supplier with a similar name (medium severity;
  the text says so).
- If `revision.json` is lost after an override, the way out is to redo the override and then remove it.
- Learned rules created before the rule file carried the input hash (only in test folders) are checked by customer
  name, not key.

- Only MXN and USD.
- One receipt is matched against at most 4 invoices at a time.
- The customer-name match depends on the description carrying the name, the invoice number or a learned payer.
  Anything else is proposed (level 2) or left for a person (level 3), never applied on a guess.
- Learned rules match whole words. A payer that writes its name differently each month needs one rule per spelling.
- Short-payment and cross-currency tolerances (2% and 0.5%) are settings in `config.py`, not learned.
- Tax judgments, like a 0% rate or a CFDI rate that is not the FIX, are flagged
  "to confirm with tax". The tool does not decide them. Rate corrections and the reversal of cancelled CFDIs are
  proposed as *conditional* entries.
- IVA por trasladar (2202) of USD invoices not yet collected stays at the booked rate: IVA cobrado is measured at
  the FIX of the collection date (case convention; CFF art. 20), so 2202 is settled then. `A5_IVA_Roll_Forward`
  shows, not booked, what it would be at the closing FIX and the difference, in case the Controller prefers to
  revalue it.
- The reviewer state is a JSON file next to the output. It is fine for one team, but it has no login.
- The opening AR sheet is required (with its headers only if the opening balance is really zero).
- **Accepted risk (approved by Diego, 2026-11-09):** someone with access to the output folder could edit
  `revision.json` and the audit log together, consistently. Editing one of them alone, putting back an older copy,
  changing the file hash or damaging log lines is detected, but anything local can be recomputed by whoever runs
  the tool. The real fix is separation of duties: approvals stored where the preparer cannot write (a database with
  SSO, item 2 of "What I would build next").
- It reads Excel, not the SAT web services or the bank's API.

## What I would build next

1. Read CFDI/REP XML directly (or download them from SAT) and the bank statement from the bank's API.
2. Store review state and the audit log in a database with SSO, so approvals are tied to real identities.
3. Generate the missing REPs as drafts for the tax team.
4. Learn more from reviewers: per-customer payment habits (who pays net of fees, who pays FIFO) to rank candidates.
5. Post approved entries to the ERP through its API, with the entry id written back to the audit log.

## Security

No secrets in the repository: the API key is read from the environment, and `.env` and `salida/` are ignored by
git. The data in `material/` is fictional.
