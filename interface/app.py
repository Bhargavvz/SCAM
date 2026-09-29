"""Web demo for the Supply Chain Memory & Decision Agent.

Run:  .venv/bin/python -m streamlit run interface/app.py

Tabs:
  1. Disruption decision - a holdout scenario or a free-text report -> decision card (simulated options, precedents,
     open commitments, guardrails, cited rationale, optional write-back)
  2. Ask the memory      - any question routed to one of the 7 capabilities (reflect + database ground truth)
  3. History question    - as-of QA over memory + database
  4. Demo walkthrough    - the scripted scenarios with expected vs actual checks
  5. Results             - eval scorecard, demo report and saved holdout cards
The agent is rebuilt per request: hindsight-client's sync API is not safe to share across Streamlit's threads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import agent_core  # noqa: E402
from agent.agent_core import AgentError  # noqa: E402
from agent.capabilities import ask  # noqa: E402
from agent.config import load_settings  # noqa: E402
from agent.corpus import load_index  # noqa: E402
from agent.llm import LLMRefusal, LLMUnavailable  # noqa: E402
from agent.scenarios import holdout_context, load_holdout  # noqa: E402
from agent.setup_data import ensure_db  # noqa: E402
from demo.run_demo import check_expectations, load_specs, run_spec  # noqa: E402

AGENT_ERRORS = (AgentError, LLMRefusal, LLMUnavailable, ValueError)
DEMO_QUESTIONS = [s["question"] for s in load_specs() if s["kind"] == "ask"]

st.set_page_config(page_title="Supply Chain Memory Agent", page_icon="📦", layout="wide")


# ----------------------------------------------------------------------------------------------- setup
@st.cache_resource
def _settings():
    s = load_settings()
    ensure_db(s)
    return s


@st.cache_data
def _holdout():
    return load_holdout(_settings())


def _agent(live_db: Path):
    return agent_core.build_agent(_settings(), live_db_path=live_db)


def _run(fn, *args, **kwargs):
    try:
        with st.spinner("Recalling memory, querying the database, simulating and reasoning..."):
            return fn(*args, **kwargs)
    except AGENT_ERRORS as ex:
        st.error(f"{type(ex).__name__}: {ex}")
    return None


def _df(rows, cols=None):
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    for c in df.columns:  # lists / dicts render poorly in st.dataframe
        if df[c].map(lambda v: isinstance(v, (list, dict))).any():
            df[c] = df[c].map(lambda v: ", ".join(map(str, v)) if isinstance(v, list) else
                              json.dumps(v) if isinstance(v, dict) else v)
    return df


def _show_table(title, rows, cols=None):
    df = _df(rows, cols)
    st.markdown(f"**{title}**")
    if df is None:
        st.caption("none")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def _show_docs(doc_ids, as_of, key):
    if not doc_ids:
        return
    index = load_index(_settings().corpus_path)
    with st.expander(f"Read cited memory documents ({len(doc_ids)})"):
        for d in doc_ids:
            doc = index.by_id.get(d)
            if doc is None or not as_of or not index.visible(doc, as_of):
                st.caption(f"{d}: not a corpus document visible as of {as_of}")
                continue
            st.markdown(f"**{d}** · {doc.date} · {doc.doc_type} · _{doc.context}_")
            later = index.superseding(d, as_of)
            if later:
                st.caption("superseded by " + ", ".join(f"{x.doc_id} ({x.date})" for x in later))
            st.code(doc.content, language=None)


# ----------------------------------------------------------------------------------------------- renderers
def render_card(card: dict):
    rec, best = card.get("recommended_action"), card.get("simulator_best_action")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Recommended action", rec or "none")
    c2.metric("Simulator best", best or "-")
    c3.metric("Confidence", card.get("confidence", "-"))
    u = card.get("usage") or {}
    c4.metric("Latency / LLM calls", f"{card.get('latency_s', 0)} s / {u.get('calls', 0)}")
    if rec and best and rec != best:
        st.warning(f"The agent deviates from the simulator's lowest risk-adjusted option ({best}); see rationale.")
    for g in card.get("guardrail_events") or []:
        st.error(f"🛡️ Guardrail: {g}")
    for w in card.get("warnings") or []:
        st.warning(w)
    if card.get("situation_summary"):
        st.info(card["situation_summary"])

    options = []
    for o in card.get("options") or []:
        mark = ("★ " if o["action"] == rec else "") + ("▲" if o["action"] == best else "")
        options.append({"": mark, "action": o["action"], "feasible": o.get("feasible"), "arrival": o.get("arrival"),
                        "stockout_days": o.get("stockout_days"), "cost_usd": o.get("cost"), "risk": o.get("risk"),
                        "score": o.get("score"), "service_impact_units": o.get("service_impact_units"),
                        "breaches": o.get("breaches_commitments"), "note": o.get("note")})
    _show_table("Candidate options - deterministic simulator (★ recommended, ▲ simulator best)", options)

    left, right = st.columns(2)
    with left:
        _show_table("Precedents (past decisions scored against today's options)", card.get("precedents"),
                    ["decision_id", "event_id", "decided_at", "decision_type", "outcome_label", "today_rank",
                     "applies_today", "source", "note"])
    with right:
        _show_table("Open commitments", card.get("open_commitments"),
                    ["commitment_id", "counterparty_id", "due_date", "commitment_text", "affected_by"])

    st.markdown("**Rationale**")
    st.markdown(card.get("rationale") or "_no rationale_")
    e1, e2 = st.columns(2)
    e1.markdown("**Cited documents:** " + (", ".join(card.get("cited_doc_ids") or []) or "-"))
    e2.markdown("**Cited records:** " + (", ".join(card.get("cited_record_ids") or []) or "-"))
    if card.get("unverifiable_citations"):
        st.caption("Unverifiable citations dropped: " + ", ".join(card["unverifiable_citations"]))
    if card.get("ungrounded_claims"):
        st.caption("Ungrounded claims flagged: " + "; ".join(card["ungrounded_claims"]))
    _show_docs(card.get("cited_doc_ids"), card.get("as_of"), key=card.get("run_id"))

    if card.get("writeback") or card.get("mock_actions"):
        st.success("Write-back: " + json.dumps(card.get("writeback") or {}))
        for m in card.get("mock_actions") or []:
            st.caption(m)
    st.caption(f"as_of {card.get('as_of')} · memory hits {card.get('memory_hits')} · dropped as future "
               f"{card.get('dropped_future_hits')} · reflect {card.get('reflect_mode')} · tokens in/out "
               f"{u.get('input_tokens')}/{u.get('output_tokens')} · cost ${u.get('cost_usd')}")
    if card.get("reflection"):
        with st.expander("Memory reflection"):
            st.markdown(card["reflection"])
    if card.get("trace"):
        with st.expander("Full trace"):
            st.dataframe(_df(card["trace"]), use_container_width=True, hide_index=True)


def render_capability(ans: dict):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Capability", ans["capability"])
    c2.metric("Reflect", f"{ans.get('reflect_mode')} ({ans.get('budget')})")
    c3.metric("Confidence", ans.get("confidence"))
    checked = {True: "checked ✅", False: "NOT checked ⚠️", None: "n/a"}[ans.get("commitments_checked")]
    c4.metric("Open commitments", checked)
    for w in ans.get("warnings") or []:
        st.warning(w)
    st.markdown(ans.get("answer") or "_no answer_")
    if ans.get("structured"):
        with st.expander("Structured output"):
            st.json(ans["structured"])
    for name, rows in (ans.get("ground_truth") or {}).items():
        _show_table(f"{name} (database, as of {ans['as_of']})", rows)
    st.markdown("**Cited documents:** " + (", ".join(ans.get("cited_doc_ids") or []) or "-") +
                " · **Cited records:** " + (", ".join(ans.get("cited_record_ids") or []) or "-"))
    if ans.get("unverifiable_citations"):
        st.caption("Unverifiable citations dropped: " + ", ".join(ans["unverifiable_citations"]))
    _show_docs(ans.get("cited_doc_ids"), ans["as_of"], key=ans["question"])


def render_answer(res: dict):
    st.markdown(res.get("answer") or "_no answer_")
    st.markdown("**Cited documents:** " + (", ".join(res.get("cited_doc_ids") or []) or "-") +
                " · **Cited records:** " + (", ".join(res.get("cited_record_ids") or []) or "-"))
    if res.get("unverifiable_citations"):
        st.caption("Unverifiable citations dropped: " + ", ".join(res["unverifiable_citations"]))
    st.caption(f"as_of {res['as_of']} · confidence {res.get('confidence')} · memory hits {res.get('memory_hits')} · "
               f"latency {res.get('latency_s')} s")
    _show_docs(res.get("cited_doc_ids"), res["as_of"], key=res["question"])


def render_result(result: dict):
    if result.get("error"):
        st.error(result["error"])
    if result.get("card"):
        render_card(result["card"])
    for i, c in enumerate(result.get("cards") or []):
        st.subheader(f"Step {i + 1}: {c.get('as_of')}")
        render_card(c)
    if result.get("answer"):
        render_answer(result["answer"])
    if result.get("capability"):
        render_capability(result["capability"])


# ----------------------------------------------------------------------------------------------- sidebar
def _live_db() -> Path:
    return ROOT / st.session_state.get("live_db", "runtime/ui_live.sqlite")


s = _settings()
with st.sidebar:
    st.title("📦 Supply Chain Memory Agent")
    st.caption("Hindsight memory + as-of database + deterministic simulator + guarded LLM")
    st.markdown(f"**LLM:** {s.llm_provider} · `{s.llm_model}`" + (" · compact mode" if s.llm_compact else ""))
    st.markdown(f"**Memory bank:** `{s.hindsight_bank_id}`")
    st.markdown(f"**Hindsight:** {s.hindsight_base_url}")
    st.markdown(f"**Mental Models in reflect:** {'on' if s.mental_models_in_reflect else 'off'}")
    st.text_input("Live DB (write-back target)", value="runtime/ui_live.sqlite", key="live_db")
    if st.button("Reset live DB"):
        _live_db().unlink(missing_ok=True)
        st.success("Live DB cleared - logged decisions removed (Hindsight retains are not deleted).")
    st.divider()
    st.caption("External systems (ERP, supplier portal, email) are never called; write-back actions are shown as "
               "labelled mocks.")

tab_decide, tab_ask, tab_qa, tab_demo, tab_results = st.tabs(
    ["🚨 Disruption decision", "💬 Ask the memory", "🕰️ History question", "🎬 Demo walkthrough", "📊 Results"])

# ----------------------------------------------------------------------------------------------- tab 1
with tab_decide:
    holdout = _holdout()
    source = st.radio("Input", ["Holdout scenario", "Free-text report"], horizontal=True)
    if source == "Holdout scenario":
        labels = {sid: f"{sid} · {sc['day0']} · {sc['entities']['supplier_id']} / {sc['entities']['rm_id']}"
                        + (" · TRAP" if sc["trap"] else "") for sid, sc in holdout.items()}
        sid = st.selectbox("Scenario", list(holdout), index=list(holdout).index("HS05"), format_func=labels.get)
        sc = holdout[sid]
        ctx = holdout_context(sc)
        report = st.text_area("Disruption report", sc["day0_report"], height=110)
        c1, c2, c3, c4 = st.columns(4)
        as_of = c1.text_input("As of", sc["day0"])
        rpo = c2.text_input("RPO", ctx["rpo_id"])
        runs = c3.text_input("Affected runs (first = needs the RM)", ",".join(ctx["run_ids"]))
        rm = c4.text_input("Raw material", ctx["rm_id"])
        if sc["trap"]:
            st.caption(f"Trap: the most similar precedent {sc['trap']['its_decision_id']} "
                       f"({sc['trap']['its_action']}, {sc['trap']['its_outcome']}) is misleading - gold answer: "
                       f"{sc['gold_best_action']}.")
    else:
        report = st.text_area("Disruption report (start with the report date, or fill 'As of')", height=110,
                              value="2025-01-13 - Supplier 169 (SUP0169) just moved RPO016183 (Elastic Film Laminate, "
                                    "RM0100) for Eastfield Plant (P03) to 2025-03-10 citing weather force majeure. Run "
                                    "PR0016522 is planned 2025-02-17 and needs this material. The buyer proposes "
                                    "cancelling RPO016183 and re-buying elsewhere. What should we do?")
        c1, c2, c3, c4 = st.columns(4)
        as_of = c1.text_input("As of", "2025-01-13")
        rpo = c2.text_input("RPO", "RPO016183")
        runs = c3.text_input("Affected runs (first = needs the RM)", "PR0016522")
        rm = c4.text_input("Raw material", "RM0100")
    writeback = st.checkbox("Initiate the response (log decision + commitment, retain summary in Hindsight)")
    if st.button("Decide", type="primary"):
        ctx = {k: v for k, v in {"rpo_id": rpo.strip() or None, "rm_id": rm.strip() or None,
                                 "run_ids": [r.strip() for r in runs.split(",") if r.strip()] or None}.items() if v}
        card = _run(lambda: _agent(_live_db()).run(report, as_of=as_of.strip() or None, context=ctx,
                                                   writeback=writeback))
        if card:
            st.session_state["last_card"] = card.to_dict()
    if st.session_state.get("last_card"):
        render_card(st.session_state["last_card"])

# ----------------------------------------------------------------------------------------------- tab 2
with tab_ask:
    choice = st.selectbox("Example questions (one per capability)", DEMO_QUESTIONS + ["Custom question..."])
    question = st.text_area("Question", "" if choice == "Custom question..." else choice, height=80)
    as_of_ask = st.text_input("As of", s.default_as_of, key="as_of_ask")
    if st.button("Ask", type="primary"):
        ans = _run(lambda: ask(_agent(_live_db()), question, as_of_ask.strip() or None))
        if ans:
            st.session_state["last_ask"] = ans.to_dict()
    if st.session_state.get("last_ask"):
        render_capability(st.session_state["last_ask"])

# ----------------------------------------------------------------------------------------------- tab 3
with tab_qa:
    st.caption("Only memory and records dated on or before 'As of' are visible - try the supersession example.")
    q = st.text_input("Question", "What is the latest ETA for RPO003179?")
    as_of_q = st.text_input("As of", "2023-06-11", key="as_of_qa")
    if st.button("Answer", type="primary"):
        res = _run(lambda: _agent(_live_db()).answer_question(q, as_of_q.strip()))
        if res:
            st.session_state["last_qa"] = res.to_dict()
    if st.session_state.get("last_qa"):
        render_answer(st.session_state["last_qa"])

# ----------------------------------------------------------------------------------------------- tab 4
with tab_demo:
    specs = load_specs()
    st.caption("Scripted walkthrough: routine decision, trap, commitment conflict, supersession, closing the loop, "
               "and one question per capability. Each run is checked against its expected behaviour.")
    spec = st.selectbox("Scenario", specs, format_func=lambda x: f"{x['id']} · {x['title']}")
    st.json(spec["expect"], expanded=False)
    demo_db = ROOT / "demo" / "out" / "ui_demo_live.sqlite"
    if st.button("Run scenario", type="primary"):
        if spec["kind"] == "chain":
            demo_db.unlink(missing_ok=True)  # the loop demo must start from an empty live DB
        result = _run(lambda: run_spec(_agent(demo_db), spec, _holdout()))
        if result:
            st.session_state["last_demo"] = (spec["id"], result)
    if st.session_state.get("last_demo") and st.session_state["last_demo"][0] == spec["id"]:
        result = st.session_state["last_demo"][1]
        checks = check_expectations(spec["expect"], result)
        ok = all(c["ok"] for c in checks)
        (st.success if ok else st.error)(f"{sum(c['ok'] for c in checks)}/{len(checks)} checks passed")
        st.dataframe(_df([{**c, "result": "PASS" if c["ok"] else "FAIL"} for c in checks],
                         ["check", "expected", "actual", "result"]), use_container_width=True, hide_index=True)
        render_result(result)

# ----------------------------------------------------------------------------------------------- tab 5
with tab_results:
    for label, path in (("Eval scorecard", ROOT / "eval" / "out" / "scorecard.md"),
                        ("Demo report", ROOT / "demo" / "out" / "demo_report.md")):
        st.subheader(label)
        st.markdown(path.read_text() if path.exists() else f"_not generated yet: {path.relative_to(ROOT)}_")
    cards = sorted((ROOT / "eval" / "out" / "cards").glob("*.json"))
    if cards:
        st.subheader("Saved holdout decision cards")
        pick = st.selectbox("Card", cards, format_func=lambda p: p.stem)
        render_card(json.loads(pick.read_text()))
