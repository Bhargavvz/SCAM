import { useEffect, useState } from "react";
import { api, Config } from "./api";
import { AskView } from "./views/AskView";
import { DecideView } from "./views/DecideView";
import { DemoView } from "./views/DemoView";
import { QuestionView } from "./views/QuestionView";
import { ResultsView } from "./views/ResultsView";

const TABS = [
  { id: "decide", label: "🚨 Disruption decision" },
  { id: "ask", label: "💬 Ask the memory" },
  { id: "history", label: "🕰️ History question" },
  { id: "demo", label: "🎬 Demo walkthrough" },
  { id: "results", label: "📊 Results" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("decide");
  const [config, setConfig] = useState<Config | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setNotice("Backend not reachable — start it with scripts/run_ui.sh"));
  }, []);

  const reset = async () => {
    try {
      setNotice((await api.resetLive()).message);
    } catch (e) {
      setNotice(String((e as Error).message));
    }
  };

  return (
    <div className="layout">
      <aside className="sidebar">
        <div>
          <h1>📦 Supply Chain Memory Agent</h1>
          <div className="sub">Hindsight memory · as-of database · deterministic simulator · guarded LLM</div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
        {config && (
          <div className="meta">
            <div><b>LLM</b> {config.llm_provider} · {config.llm_model}{config.llm_compact ? " · compact" : ""}</div>
            <div><b>Memory bank</b> {config.bank_id}</div>
            <div><b>Hindsight</b> {config.hindsight_base_url.replace("https://", "")}</div>
            <div><b>Mental Models</b> {config.mental_models_in_reflect ? "on" : "off"}</div>
            <div><b>Default as-of</b> {config.default_as_of}</div>
          </div>
        )}
        <button className="btn ghost" style={{ color: "#c7d2fe", borderColor: "#374151" }} onClick={reset}>Reset logged decisions</button>
        {notice && <div className="alert info" style={{ fontSize: 12 }}>{notice}</div>}
        <div className="footnote">External systems (ERP, supplier portal, email) are never called — write-back actions are labelled mocks.</div>
      </aside>
      <main>
        {tab === "decide" && <DecideView />}
        {tab === "ask" && <AskView defaultAsOf={config?.default_as_of ?? "2025-12-31"} />}
        {tab === "history" && <QuestionView />}
        {tab === "demo" && <DemoView />}
        {tab === "results" && <ResultsView />}
      </main>
    </div>
  );
}
