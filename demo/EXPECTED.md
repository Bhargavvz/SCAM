# Demo - what each scenario should show

Run `.venv/bin/python -m demo.run_demo`. Actual results land in:
- `demo/out/demo_report.md`: a PASS/FAIL table per check;
- `demo/out/demo_console.txt`: the rendered cards.

## Decision agent (D1-D5)

**D1 - HS01: SUP0192 moves RPO023175 (RM0079, P04) to 2025-10-30**
- Expected:
  - `switch_supplier` (spot lot from SUP0177, risk-adjusted score 36, 0 stockout days).
  - accept_delay would cost $25,386 and 9 stockout days.
  - Open plant commitments CMT00786 and CMT00802 are flagged.
- Where to look: the options table (★▲) and Open commitments.

**D2 - HS05: SUP0247 moves RPO023030 (RM0083, P01) to 2025-12-07**
- Expected (trap):
  - The most similar precedent, DEC00353 (accept_delay, 2025-08-28), succeeded.
  - The new ETA falls in Nov-Dec, when SUP0247 historically slips another 7-14 days.
  - accept_delay is high risk (score 31,826) against 2 for `switch_supplier`.
  - The card marks DEC00353 "no - misleading if copied".
- Where to look: the Precedents table and the rationale.

**D3 - EVT01987: SUP0169 moves RPO016183 (RM0100, P03) to 2025-03-10; the buyer proposes cancelling**
- Expected (commitment conflict):
  - CMT00560 (a SUP0169 volume commitment, open until 2025-12-30) blocks the proposed cancel_po in the pre-check.
  - It blocks cancel_po again if the model submits it.
  - The recommendation is `switch_supplier` or `reallocate_stock`.
- Where to look: the Guardrails panel, and "Affected by: cancel_po" under Open commitments.

**D4 - Q0020: "What is the latest ETA for RPO003179?" as of 2023-06-11**
- Expected (supersession):
  - DOC000248 said 2023-06-22; DOC000254 supersedes it with an expedite ETA of 2023-06-08.
  - The answer is 2023-06-08 and cites DOC000254.
- Where to look: the Answer panel.

**D5 - HS05 with write-back, then HS08**
- Expected (closing the loop):
  - HS05 writes DECL00001 + CMTL00001 to `demo/out/live_demo.sqlite` and retains a summary.
  - The ERP / portal steps print as `[MOCK - no external call made]`.
  - HS08 lists DECL00001 as a `live` precedent.
- Where to look: the Write-back panel, and the second card's Precedents.

## The seven capability queries (D6-D12)

**D6 - Supplier reliability: SUP0247 in Nov-Dec**
- Expected: a delay-by-promised-month table showing Nov 13.5 and Dec 13.8 days against ~4-5 in other months; an answer that flags the pattern, with citations.

**D7 - Commitments with SUP0091**
- Expected: open commitments and contracts listed; `commitments_checked = true`; record-id citations.

**D8 - 35% supply reduction, "primary supplier" not named**
- Expected: `commitments_checked = false`, with a warning to name the supplier. The answer either cites real precedents or says there is no record.

**D9 - Partial-shipment exceptions**
- Expected: not in the corpus. A correct answer says there is no record, or cites what does exist.

**D10 - Corrosion / humidity**
- Expected: not in the corpus. A correct answer says there is no record.

**D11 - SUP0179 negotiations**
- Expected: negotiation rows plus a lead-time-by-month table (a rising trend); levers, with citations.

**D12 - Switch-supplier prediction accuracy**
- Expected: accuracy rows for switch_supplier (18 decisions, actual cost ≈1.63× expected); the answer cites DEC ids.

## Side effects

D1-D4 and D6-D12 are read-only. D5 writes only to `demo/out/live_demo.sqlite`, plus one Hindsight retain per run (skip it with `--no-retain`). Eval runs use their own empty live DB, so demo decisions never reach eval.
