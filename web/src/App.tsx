import { useEffect, useState } from "react";
import { api, Config } from "./api";
import { AskView } from "./views/AskView";
import { DecideView } from "./views/DecideView";
import { DemoView } from "./views/DemoView";
import { LoginView } from "./views/LoginView";
import { OverviewView } from "./views/OverviewView";
import { QuestionView } from "./views/QuestionView";
import { ResultsView } from "./views/ResultsView";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "decide", label: "Decide" },
  { id: "ask", label: "Ask" },
  { id: "history", label: "History" },
  { id: "demo", label: "Walkthrough" },
  { id: "results", label: "Results" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("overview");
  const [config, setConfig] = useState<Config | null>(null);
  const [offline, setOffline] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // null = still checking; {required:false} = open site; user = signed-in name
  const [auth, setAuth] = useState<{ required: boolean; user: string | null } | null>(null);

  useEffect(() => {
    api.session().then((s) => setAuth({ required: s.auth_required, user: s.user })).catch(() => setOffline(true));
    const onSignedOut = () => setAuth((a) => (a ? { ...a, user: null } : a));
    window.addEventListener("scm:signed-out", onSignedOut);
    return () => window.removeEventListener("scm:signed-out", onSignedOut);
  }, []);

  const signedIn = auth !== null && (!auth.required || auth.user !== null);
  useEffect(() => {
    if (signedIn) api.config().then(setConfig).catch(() => setOffline(true));
  }, [signedIn]);

  const signOut = async () => {
    await api.logout().catch(() => undefined);
    setAuth((a) => (a ? { ...a, user: null } : a));
  };

  if (auth === null && !offline) return <div className="login-page"><div className="muted" style={{ margin: "auto" }}>Loading…</div></div>;
  if (auth && auth.required && !auth.user) return <LoginView onSignedIn={(user) => setAuth({ required: true, user: user ?? "" })} />;

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
            {auth?.required && <button className="linkbtn" onClick={signOut}>sign out {auth.user}</button>}
          </div>
        </div>
      </header>
      <main>
        {offline && <div className="alert error"><span className="tag">Offline</span>The API is not reachable. Start it with scripts/run_ui.sh.</div>}
        {notice && <div className="alert info">{notice}</div>}
        {tab === "overview" && <OverviewView onOpen={setTab} />}
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
