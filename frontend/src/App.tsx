import {
  BarChart3, Bell, Boxes, Building2, CalendarDays, ClipboardList, Database, Factory, LayoutDashboard, LineChart, Menu, Moon,
  Package, PanelLeftClose, RotateCcw, Search, ShoppingCart, Sparkles, Sun, Truck, Undo2, Users, Warehouse,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Copilot } from "./components/Copilot";
import { ToastProvider } from "./components/ui";
import { api, fmt, routeFor, Row, useDebounced } from "./lib/api";
import { AlertsPage } from "./pages/Alerts";
import { Dashboard } from "./pages/Dashboard";
import { Forecasting } from "./pages/Forecasting";
import { InsightsPage } from "./pages/Insights";
import { InventoryPage } from "./pages/Inventory";
import { LoginPage } from "./pages/Login";
import { LogisticsPage, ShipmentDetail } from "./pages/Logistics";
import { ProductDetail, ProductsPage } from "./pages/Products";
import { ProductionPage, RunDetail } from "./pages/Production";
import { PODetail, PurchasingPage } from "./pages/Purchasing";
import { ReportsPage } from "./pages/Reports";
import { ReturnDetail, ReturnsPage } from "./pages/Returns";
import { CustomerDetail, CustomersPage, OrderDetail, SalesPage } from "./pages/Sales";
import { SupplierDetail, SuppliersPage } from "./pages/Suppliers";
import { SystemPage } from "./pages/System";
import { WarehouseDetail, WarehousesPage } from "./pages/Warehouses";

const NAV = [
  { label: "Overview", items: [["/", "Dashboard", LayoutDashboard], ["/insights", "AI Insights", Sparkles], ["/alerts", "Alerts", Bell]] },
  { label: "Source", items: [["/suppliers", "Suppliers", Building2], ["/purchasing", "Purchasing", ClipboardList]] },
  { label: "Store", items: [["/products", "Products", Package], ["/inventory", "Inventory", Boxes], ["/warehouses", "Warehouses", Warehouse]] },
  { label: "Make", items: [["/forecasting", "Demand forecast", LineChart], ["/production", "Production", Factory]] },
  { label: "Deliver", items: [["/sales", "Sales orders", ShoppingCart], ["/sales/customers", "Customers", Users], ["/logistics", "Logistics", Truck], ["/returns", "Returns", Undo2]] },
  { label: "Analyze", items: [["/reports", "Reports", BarChart3], ["/system", "Data & sync", Database]] },
] as const;

function GlobalSearch() {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<Row[]>([]);
  const [sel, setSel] = useState(0);
  const dq = useDebounced(q, 180);
  const nav = useNavigate();
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);
  useEffect(() => {
    if (dq.trim().length < 2) return setHits([]);
    api.get<Row[]>("/search", { q: dq }).then((r) => { setHits(r); setSel(0); }).catch(() => setHits([]));
  }, [dq]);
  const go = (h: Row) => {
    const to = h.type === "brand" ? null : routeFor(h.id);
    if (to) nav(to);
    setQ("");
    setOpen(false);
    ref.current?.blur();
  };
  return (
    <div className="search">
      <Search size={15} className="lead" />
      <input ref={ref} placeholder="Search products, orders, suppliers, shipments, customers…" value={q}
        onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)} onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setSel((s) => Math.min(s + 1, hits.length - 1));
          if (e.key === "ArrowUp") setSel((s) => Math.max(s - 1, 0));
          if (e.key === "Enter" && hits[sel]) go(hits[sel]);
          if (e.key === "Escape") ref.current?.blur();
        }} />
      <kbd>⌘K</kbd>
      {open && hits.length > 0 && (
        <div className="search-menu">
          {hits.map((h, i) => (
            <button key={`${h.type}-${h.id}`} className={i === sel ? "sel" : ""} onMouseDown={() => go(h)}>
              <span className="mono">{h.id}</span><span className="clip">{h.label}</span><i>{fmt.label(h.type)}</i>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Shell({ user, authRequired, onSignOut }: { user: string; authRequired: boolean; onSignOut: () => void }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [copilot, setCopilot] = useState(false);
  const [health, setHealth] = useState<Row | null>(null);
  const [counts, setCounts] = useState<Row>({});
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem("meridian.theme") ?? "light";
    } catch {
      return "light";
    }
  });
  const loc = useLocation();
  const nav = useNavigate();
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("meridian.theme", theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  useEffect(() => setMobile(false), [loc.pathname]);
  useEffect(() => {
    const load = () => {
      api.get("/health").then(setHealth).catch(() => setHealth(null));
      api.get<Row>("/alerts", { limit: 1 }).then((a) => setCounts({ critical: a.summary.by_severity.critical ?? 0, open: a.total })).catch(() => undefined);
    };
    load();
    const t = setInterval(load, 30000);
    window.addEventListener("meridian:changed", load);
    return () => {
      clearInterval(t);
      window.removeEventListener("meridian:changed", load);
    };
  }, []);

  return (
    <div className={`app ${collapsed ? "collapsed" : ""} ${mobile ? "mobile-open" : ""}`}>
      <nav className="nav">
        <div className="brand">
          <div className="brand-mark"><svg width="16" height="16" viewBox="0 0 32 32"><path d="M6 24V8l10 9 10-9v16" stroke="#fff" strokeWidth="3.5" fill="none" strokeLinejoin="round" /></svg></div>
          <div className="brand-text">Meridian<small>Supply chain OS</small></div>
        </div>
        {NAV.map((g) => (
          <div key={g.label} className="nav-group">
            <div className="nav-label">{g.label}</div>
            {g.items.map(([to, label, Icon]) => (
              <NavLink key={to} to={to} end={to === "/" || to === "/sales"} title={label}>
                <Icon size={16} /><span>{label}</span>
                {to === "/alerts" && counts.critical > 0 && <span className="count">{counts.critical}</span>}
                {to === "/insights" && <span className="count ai">AI</span>}
              </NavLink>
            ))}
          </div>
        ))}
        <div className="nav-foot">
          <div><span className={`dot ${health?.ok ? "" : "off"}`} />{health?.ok ? "Database online" : "Connecting…"}</div>
          <div><span className={`dot ${health?.ai ? "" : "off"}`} />{health?.ai ? `AI · ${String(health.model).split("/").pop()}` : "AI offline"}</div>
          {authRequired && <button className="btn ghost sm" style={{ color: "var(--nav-text)", padding: 0, justifyContent: "flex-start" }} onClick={onSignOut}>Sign out {user}</button>}
        </div>
      </nav>
      <div className="main">
        <header className="topbar">
          <button className="iconbtn" onClick={() => (window.innerWidth < 980 ? setMobile((m) => !m) : setCollapsed((c) => !c))} aria-label="Toggle navigation">
            {collapsed ? <Menu size={17} /> : <PanelLeftClose size={17} />}
          </button>
          <GlobalSearch />
          <div className="grow" />
          <span className="bizdate" title="All operations post on this business date"><CalendarDays size={14} />Business date <b>{health?.business_date ?? "…"}</b></span>
          <button className="btn ai sm" onClick={() => setCopilot(true)}><Sparkles size={14} />Ask Meridian</button>
          <button className="iconbtn" onClick={() => nav("/alerts")} aria-label="Alerts">
            <Bell size={17} />{counts.critical > 0 && <span className="badge-dot">{counts.critical > 99 ? "99+" : counts.critical}</span>}
          </button>
          <button className="iconbtn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Theme">
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
        </header>
        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/insights" element={<InsightsPage />} />
            <Route path="/alerts" element={<AlertsPage />} />
            <Route path="/products" element={<ProductsPage />} />
            <Route path="/products/:id" element={<ProductDetail />} />
            <Route path="/inventory" element={<InventoryPage />} />
            <Route path="/warehouses" element={<WarehousesPage />} />
            <Route path="/warehouses/:id" element={<WarehouseDetail />} />
            <Route path="/suppliers" element={<SuppliersPage />} />
            <Route path="/suppliers/:id" element={<SupplierDetail />} />
            <Route path="/purchasing" element={<PurchasingPage />} />
            <Route path="/purchasing/:id" element={<PODetail />} />
            <Route path="/sales" element={<SalesPage />} />
            <Route path="/sales/orders/:id" element={<OrderDetail />} />
            <Route path="/sales/customers" element={<CustomersPage />} />
            <Route path="/sales/customers/:id" element={<CustomerDetail />} />
            <Route path="/forecasting" element={<Forecasting />} />
            <Route path="/logistics" element={<LogisticsPage />} />
            <Route path="/logistics/:id" element={<ShipmentDetail />} />
            <Route path="/production" element={<ProductionPage />} />
            <Route path="/production/:id" element={<RunDetail />} />
            <Route path="/returns" element={<ReturnsPage />} />
            <Route path="/returns/:id" element={<ReturnDetail />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/system" element={<SystemPage />} />
            <Route path="*" element={<div className="empty"><RotateCcw size={18} /> Page not found.</div>} />
          </Routes>
        </main>
      </div>
      <Copilot open={copilot} onClose={() => setCopilot(false)} />
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<{ required: boolean; user: string | null } | null>(null);
  useEffect(() => {
    api.get<Row>("/session").then((s) => setAuth({ required: s.auth_required, user: s.user })).catch(() => setAuth({ required: false, user: "planner" }));
    const out = () => setAuth((a) => (a ? { ...a, user: null } : a));
    window.addEventListener("meridian:signed-out", out);
    return () => window.removeEventListener("meridian:signed-out", out);
  }, []);
  if (!auth) return null;
  return (
    <ToastProvider>
      <BrowserRouter>
        {auth.required && !auth.user ? (
          <LoginPage onSignedIn={(u) => setAuth({ required: true, user: u })} />
        ) : (
          <Shell user={auth.user ?? ""} authRequired={auth.required}
            onSignOut={async () => { await api.post("/logout").catch(() => undefined); setAuth({ required: true, user: null }); }} />
        )}
      </BrowserRouter>
    </ToastProvider>
  );
}
