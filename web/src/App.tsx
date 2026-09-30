import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { AsOfCtx, go, hrefFor, useRoute } from "./nav";
import { AppInfo, scm } from "./scm";
import { DecideView } from "./views/DecideView";
import { DemoView } from "./views/DemoView";
import { LoginView } from "./views/LoginView";
import { OverviewView } from "./views/OverviewView";
import { QuestionView } from "./views/QuestionView";
import { ResultsView } from "./views/ResultsView";
import { AskMemory } from "./views/scm/AskMemory";
import { Dashboard } from "./views/scm/Dashboard";
import { DisruptionDetail, DisruptionList } from "./views/scm/Disruptions";
import { Commitments, DecisionDetail, Decisions, Demand, MaterialDetail, MaterialList, Production } from "./views/scm/Operations";
import { PoDetail, PoList } from "./views/scm/PurchaseOrders";
import { SupplierDetail, SupplierList } from "./views/scm/Suppliers";

const NAV: { section: string; items: [string, string][] }[] = [
  { section: "Operate", items: [["", "Control tower"], ["disruptions", "Disruptions"], ["pos", "Purchase orders"], ["production", "Production"]] },
  { section: "Plan", items: [["materials", "Materials"], ["suppliers", "Suppliers"], ["demand", "Demand"]] },
  { section: "Govern", items: [["commitments", "Commitments"], ["decisions", "Decision log"]] },
  { section: "Intelligence", items: [["ask", "Ask memory"]] },
  { section: "Evaluation", items: [["eval", "Agent scorecard"], ["eval/walkthrough", "Walkthrough"], ["eval/sandbox", "Decision sandbox"], ["eval/history", "History QA"], ["eval/results", "Full results"]] },
];

const ASOF_KEY = "scm.asOf";

function Search({ asOf }: { asOf: string }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ id: string; type: string; label: string }[]>([]);
  const [open, setOpen] = useState(false);
  const t = useRef<number | null>(null);
  useEffect(() => {
    if (t.current) window.clearTimeout(t.current);
    if (q.trim().length < 2) {
      setHits([]);
      return;
    }
    t.current = window.setTimeout(() => {
      scm.search(q, asOf).then((r) => setHits(r.results)).catch(() => setHits([]));
    }, 180);
  }, [q, asOf]);
  const pick = (id: string) => {
    const h = hrefFor(id);
    if (h) window.location.hash = h.slice(1);
    setQ("");
    setOpen(false);
  };
  return (
    <div className="gsearch">
      <input placeholder="Search suppliers, materials, orders, events…" value={q} onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)} onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && hits[0]) pick(hits[0].id); }} />
      {open && hits.length > 0 && (
        <div className="gsearch-menu">
          {hits.map((h) => (
            <button key={h.id} onMouseDown={() => pick(h.id)}>
              <span className="mono">{h.id}</span><span className="clip">{h.label}</span><i>{h.type}</i>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const route = useRoute();
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [offline, setOffline] = useState(false);
  const [auth, setAuth] = useState<{ required: boolean; user: string | null } | null>(null);
  const [asOf, setAsOfState] = useState<string>(() => {
    try {
      return localStorage.getItem(ASOF_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const setAsOf = (d: string) => {
    setAsOfState(d);
    try {
      localStorage.setItem(ASOF_KEY, d);
    } catch {
      /* storage unavailable */
    }
  };

  useEffect(() => {
    api.session().then((s) => setAuth({ required: s.auth_required, user: s.user })).catch(() => setOffline(true));
    const onOut = () => setAuth((a) => (a ? { ...a, user: null } : a));
    window.addEventListener("scm:signed-out", onOut);
    return () => window.removeEventListener("scm:signed-out", onOut);
  }, []);
  const signedIn = auth !== null && (!auth.required || auth.user !== null);
  useEffect(() => {
    if (signedIn) scm.app().then((i) => {
      setInfo(i);
      setAsOfState((cur) => cur || i.today);
    }).catch(() => setOffline(true));
  }, [signedIn]);

  if (auth === null && !offline) return <div className="login-page"><div className="muted" style={{ margin: "auto" }}>Loading…</div></div>;
  if (auth && auth.required && !auth.user) return <LoginView onSignedIn={(user) => setAuth({ required: true, user: user ?? "" })} />;

  const today = info?.today ?? "2025-10-14";
  const effective = asOf || today;
  const [p0 = "", p1] = route.path;
  const active = p0 === "eval" ? `eval${p1 ? `/${p1}` : ""}` : p0;

  const shift = (days: number) => {
    const d = new Date(`${effective}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    const s = d.toISOString().slice(0, 10);
    if (info && (s < info.data_start || s > info.data_end)) return;
    setAsOf(s);
  };

  let page: React.ReactNode;
  switch (p0) {
    case "": page = <Dashboard />; break;
    case "disruptions": page = p1 ? <DisruptionDetail id={p1} /> : <DisruptionList route={route} />; break;
    case "pos": page = p1 ? <PoDetail id={p1} /> : <PoList route={route} />; break;
    case "suppliers": page = p1 ? <SupplierDetail id={p1} /> : <SupplierList />; break;
    case "materials": page = p1 ? <MaterialDetail id={p1} /> : <MaterialList />; break;
    case "production": page = <Production route={route} />; break;
    case "demand": page = <Demand />; break;
    case "commitments": page = <Commitments key={route.query.toString()} route={route} />; break;
    case "decisions": page = p1 ? <DecisionDetail id={p1} /> : <Decisions route={route} />; break;
    case "ask": page = <AskMemory />; break;
    case "eval":
      page = p1 === "walkthrough" ? <DemoView /> : p1 === "sandbox" ? <DecideView /> : p1 === "history" ? <QuestionView />
        : p1 === "results" ? <ResultsView />
        : <OverviewView onOpen={(t) => go(t === "decide" ? "/eval/sandbox" : t === "demo" ? "/eval/walkthrough" : "/eval/results")} />;
      break;
    default: page = <div className="empty">Page not found. <a href="#/">Back to the control tower</a></div>;
  }

  return (
    <AsOfCtx.Provider value={{ asOf: effective, setAsOf, today }}>
      <div className="shell">
        <aside className="sidebar">
          <a className="brand" href="#/"><span className="mark" />Meridian<small>supply chain</small></a>
          {NAV.map((g) => (
            <nav key={g.section}>
              <div className="nav-section">{g.section}</div>
              {g.items.map(([to, label]) => (
                <a key={to} href={`#/${to}`} className={active === to ? "active" : ""}>{label}</a>
              ))}
            </nav>
          ))}
          <div className="sidebar-foot">
            {info ? (
              <>
                <div><span className="dot" />memory · {info.bank_id}</div>
                <div className="mono">{info.llm}{info.compact ? " · compact" : ""}</div>
              </>
            ) : (
              <div><span className="dot off" />{offline ? "API offline" : "connecting"}</div>
            )}
            {auth?.required && <button className="linkbtn" onClick={async () => { await api.logout().catch(() => undefined); setAuth((a) => (a ? { ...a, user: null } : a)); }}>sign out {auth.user}</button>}
          </div>
        </aside>
        <div className="content">
          <header className="appbar">
            <Search asOf={effective} />
            <div className="asof">
              <span className="asof-label">Business date</span>
              <button className="btn ghost sm" onClick={() => shift(-7)} title="one week earlier">−7d</button>
              <input type="date" value={effective} min={info?.data_start} max={info?.data_end} onChange={(e) => e.target.value && setAsOf(e.target.value)} />
              <button className="btn ghost sm" onClick={() => shift(7)} title="one week later">+7d</button>
              {effective !== today && <button className="linkbtn" onClick={() => setAsOf(today)}>back to today</button>}
            </div>
          </header>
          {effective !== today && (
            <div className="timetravel">Viewing the business as it was on <b>{effective}</b>. Nothing after this date is visible — not in the database, not in memory.</div>
          )}
          <main>
            {offline && <div className="alert error"><span className="tag">Offline</span>The API is not reachable.</div>}
            {page}
            <div className="footnote">
              Costs and stockout days come only from the deterministic simulator. ERP, supplier portal and email actions are recorded as mocks and never sent.
            </div>
          </main>
        </div>
      </div>
    </AsOfCtx.Provider>
  );
}
