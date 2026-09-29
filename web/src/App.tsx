import { useEffect, useState } from "react";
import { api, Config } from "./api";
import { AskView } from "./views/AskView";
import { DecideView } from "./views/DecideView";
import { DemoView } from "./views/DemoView";
import { QuestionView } from "./views/QuestionView";
import { ResultsView } from "./views/ResultsView";

const TABS = [
  { id: "decide", label: "Decide" },
  { id: "ask", label: "Ask" },
  { id: "history", label: "History" },
  { id: "demo", label: "Walkthrough" },
  { id: "results", label: "Results" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("decide");
  const [config, setConfig] = useState<Config | null>(null);
  const [offline, setOffline] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setOffline(true));
  }, []);

  const reset = async () => {
    try {
      setNotice((await api.resetLive()).message);
    } catch (e) {
      setNotice(String((e as Error).message));
    }
  };

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="mark" />
            Supply Chain Memory <small>decision desk</small>
          </div>
          <nav className="tabs">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>{t.label}</button>
            ))}
          </nav>
          <div className="status">
            {config ? (
              <>
                <span><span className="dot" />{config.bank_id}</span>
                <span className="mono">{config.llm_model}{config.llm_compact ? " (compact)" : ""}</span>
              </>
            ) : (
              <span><span className="dot off" />{offline ? "API offline" : "connecting"}</span>
            )}
            <button className="linkbtn" onClick={reset} title="Delete decisions logged from this UI">reset log</button>
          </div>
        </div>
      </header>
      <main>
        {offline && <div className="alert error"><span className="tag">Offline</span>The API is not reachable. Start it with scripts/run_ui.sh.</div>}
        {notice && <div className="alert info">{notice}</div>}
        {tab === "decide" && <DecideView />}
        {tab === "ask" && <AskView defaultAsOf={config?.default_as_of ?? "2025-12-31"} />}
        {tab === "history" && <QuestionView />}
        {tab === "demo" && <DemoView />}
        {tab === "results" && <ResultsView />}
        <div className="footnote">
          Memory: Hindsight bank {config?.bank_id ?? "-"} · mental models {config?.mental_models_in_reflect ? "on" : "off"} · default as-of {config?.default_as_of ?? "-"}.
          Costs and stockout days come only from the deterministic simulator. ERP, supplier portal and email actions are recorded as mocks, never sent.
        </div>
      </main>
    </>
  );
}
