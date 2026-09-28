"""Seeded template grammar for the narrative corpus.

A template is a string with `{slot}` placeholders and `[a|b|c]` alternations
(no nesting). Each doc type is rendered from a FRAME (ordered list of sentence
pools); every pool is a list of templates. The number of distinct surface
variants of a doc type = product over frames of pool sizes, summed over frames,
and is reported by `variant_counts()` (each doc type has >= 15 variants).
"""
from __future__ import annotations

import re

import numpy as np

ALT = re.compile(r"\[([^\[\]]+)\]")


class SafeDict(dict):
    def __missing__(self, k):
        raise KeyError(f"missing slot {k!r}")


def render(tpl: str, rng: np.random.Generator, slots: dict) -> str:
    s = ALT.sub(lambda m: m.group(1).split("|")[int(rng.integers(len(m.group(1).split("|"))))], tpl)
    return s.format_map(SafeDict(slots))


# ----------------------------------------------------------------------------- pools
OPEN = {
    "po_exception_note": [
        "[Heads-up|FYI|Exception logged|PO exception] - {sup} [called|emailed|flagged through the portal] on {date}: {rpo} ({rm}) [will not make|is going to miss|slips past] its {old} [ETA|delivery date|dock date].",
        "{sup} [confirmed|let us know|informed the buyer desk] today ({date}) that {rpo} for {plant} [is delayed|has slipped|is running late].",
        "Logging a [slip|delay|date change] on {rpo}: {sup} now [shows|quotes|confirms] {new} instead of {old}.",
        "[Buyer note|Exception note|Desk note], {date}: [late delivery|delivery slip] on {rpo} ({rm}) from {sup}.",
        "Per [today's call|this morning's email|the supplier portal] ({date}), {rpo} from {sup} moves out from {old} to {new}.",
    ],
    "shift_handover": [
        "[Shift handover|Handover|End-of-shift notes] {plant_short}, {date}.",
        "[Day|Morning|Afternoon] shift handover - {plant}, {date}.",
        "Handover to [next shift|night shift|incoming planner] ({date}, {plant_short}).",
        "[Planner handover|Materials handover] for {date} at {plant}.",
        "{date} handover, {plant}: [main points|key items|what you need to know].",
    ],
    "supplier_call_summary": [
        "[Call summary|Notes from call|Supplier call] with {sup}, {date}.",
        "{date} - [spoke with|call with|Teams call with] {sup} ([account manager|sales lead|operations director]).",
        "Summary of today's ({date}) [call|conversation|review call] with {sup}.",
        "[Supplier call notes|Call recap] - {sup} - {date}.",
        "Had a [quick|30-minute|long] call with {sup} on {date}.",
    ],
    "escalation_email": [
        "Subject: [ESCALATION|Escalation|Urgent] - {topic}\n[Hi|Hello] {to},",
        "Subject: {topic} [needs a decision|at risk|- escalating]\n{to} -",
        "Subject: [Escalating|Flagging] {topic}\n[Team|{to}],",
        "Subject: [ACTION NEEDED|Heads-up]: {topic}\n{to},",
        "Subject: {topic}\n[Hi|Morning|Afternoon] {to}, escalating this [as discussed|before it gets worse|so we can decide today].",
    ],
    "decision_memo": [
        "[Decision memo|DECISION|Decision record] {dec} - {date}. Owner: {role}.",
        "{role} decision, {date} ({dec}): {title}.",
        "Memo {dec} ({date}) - {title}. [Decided by|Owner:] {role}.",
        "[Decision|Call made] on {date} by {role} ({dec}): {title}.",
        "{dec} | {date} | {role} | {title}",
    ],
    "post_mortem": [
        "[Post-mortem|Lessons learned|After-action review] for {dec} ({title}), [written|closed] {date}.",
        "{date} - [look-back|post-mortem|review] on {dec}: {title}.",
        "[Closing the loop|Retro] on {dec} ({date}).",
        "Post-mortem {dec} - {title} - {date}.",
        "[Review|Outcome review] of decision {dec} taken on {decided}, [done|written up] {date}.",
    ],
    "supplier_qbr_notes": [
        "[QBR|Quarterly business review] - {sup} - {quarter}.",
        "{quarter} [QBR notes|business review] with {sup} ({date}).",
        "Notes from the {quarter} [QBR|review] with {sup}, held {date}.",
        "[Supplier review|QBR] {quarter}: {sup}.",
        "{sup} {quarter} [scorecard review|QBR] ({date}).",
    ],
    "commitment_confirmation": [
        "[Confirming|Confirmation|For the record]: {who} committed on {date} to {what}.",
        "{date} - [commitment logged|confirmed in writing|agreed]: {what} ({who}).",
        "[Written confirmation|Confirmation email] received {date} from {who}: {what}.",
        "Logging commitment ({date}): {who} will {what_verb}.",
        "{who} [confirmed|re-confirmed|put in writing] on {date}: {what}.",
    ],
    "warehouse_ops_note": [
        "[DC ops note|Warehouse note|Ops update] - {wh} - {date}.",
        "{wh}, {date}: [receiving|inventory|ops] update.",
        "[Inbound|Dock|Floor] note from {wh} ({date}).",
        "{date} {wh} [ops log|daily ops note].",
        "[Warehouse|DC] update {date} ({wh}).",
    ],
    "forecast_review_minutes": [
        "[Forecast review|Demand review|S&OP demand review] minutes - {month}.",
        "{month} [demand|forecast] review, minutes ({date}).",
        "Minutes: [monthly|] forecast review {month}.",
        "[S&OP|Demand planning] - {month} review notes ({date}).",
        "Forecast accuracy review for {month}, held {date}.",
    ],
    "state_update": [
        "[UPDATE|Update|Status change] ({date}): {what_changed}.",
        "[Superseding|Replacing] earlier info - {what_changed} ({date}).",
        "{date} - [revised|new] information: {what_changed}.",
        "[Correction|Update] {date}: {what_changed}.",
        "Status update {date}: {what_changed}.",
    ],
}

CLOSE = {
    "po_exception_note": ["[Will chase daily.|Chasing again tomorrow.|Buyer to follow up.]", "[Planner informed.|Copying planning.|Flagged to planning.]",
                          "[No penalty waiver given.|Reminded them of the late-delivery clause.|Contract credit applies.]", "[Next check-in: {next}.|Follow-up set for {next}.]", ""],
    "shift_handover": ["[Nothing else open.|Rest is routine.|No other issues.]", "[Call me if anything changes.|Reach me on the planner phone.]", "[Next shift please watch this.|Keep an eye on it.]", ""],
    "supplier_call_summary": ["[Next call {next}.|Follow-up call booked for {next}.]", "[Actions logged in the tracker.|Notes shared with planning.]", "[They will confirm in writing.|Waiting for written confirmation.]", ""],
    "escalation_email": ["[Thanks|Regards|Best],\n{frm}", "[Need a call today.|Can we decide by EOD?]\n{frm}", "[Happy to walk through it.|Details in the tracker.]\n- {frm}", "{frm}"],
    "decision_memo": ["[Review date: {next}.|Outcome check scheduled for {next}.]", "[Filed in the decision log.|Logged.]", "[Stakeholders informed.|Planning and procurement aligned.]", ""],
    "post_mortem": ["[Filed to the lessons library.|Added to lessons learned.]", "[Shared with the S&OP team.|Circulated to planning.]", "[Closed.|Case closed.]", ""],
    "supplier_qbr_notes": ["[Next QBR in {next}.|Next review {next}.]", "[Actions tracked by Category Management.|Owner: category manager.]", "[Scorecard attached.|See scorecard for detail.]", ""],
    "commitment_confirmation": ["[Tracking against due date {due}.|Due {due}.]", "[Logged in the commitments tracker.|Added to the tracker.]", "[Penalty/credit: {penalty}.|Consequence if missed: {penalty}.]", ""],
    "warehouse_ops_note": ["[Nothing else to report.|Rest of the dock is normal.]", "[Updated in WMS.|System updated.]", "[Planning informed.|Told the planner.]", ""],
    "forecast_review_minutes": ["[Next review {next}.|Meeting closed.]", "[Actions with demand planning.|Owner: demand planner.]", "[Numbers in the S&OP deck.|Deck on the share drive.]", ""],
    "state_update": ["[Please use this date going forward.|Earlier figure is superseded.]", "[Planning updated.|MRP updated.]", "[Previous note no longer valid.|Disregard the earlier value.]", ""],
}


def variant_counts() -> dict:
    """Surface-variant count per doc type from opener / closer pools and body frames (see BODY_FRAMES in gen_narratives)."""
    def n_alts(t):
        n = 1
        for m in ALT.finditer(t):
            n *= len(m.group(1).split("|"))
        return n
    out = {}
    for k in OPEN:
        o = sum(n_alts(t) for t in OPEN[k])
        c = sum(max(1, n_alts(t)) for t in CLOSE[k])
        out[k] = {"openers": len(OPEN[k]), "opener_variants": o, "closer_variants": c}
    return out
