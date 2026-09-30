import { FormEvent, useState } from "react";
import { api, Row } from "../lib/api";
import { Spinner } from "../components/ui";

export function LoginPage({ onSignedIn }: { onSignedIn: (user: string) => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<Row>("/login", { username: u, password: p });
      onSignedIn(r.user);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login">
      <div className="login-art">
        <div className="brand" style={{ padding: 0 }}>
          <div className="brand-mark"><svg width="16" height="16" viewBox="0 0 32 32"><path d="M6 24V8l10 9 10-9v16" stroke="#fff" strokeWidth="3.5" fill="none" strokeLinejoin="round" /></svg></div>
          <div>Meridian<small>Supply chain OS</small></div>
        </div>
        <div>
          <h2>Every order, shipment and supplier.<br />One live picture.</h2>
          <p style={{ maxWidth: 440, lineHeight: 1.6 }}>Procurement, inventory, production, fulfilment, logistics and returns in a single system — with AI that tells you what changed and what to do about it.</p>
        </div>
        <div className="small">Supplier → Procurement → Inventory → Production → Shipping → Customer</div>
      </div>
      <div className="login-form">
        <form onSubmit={submit}>
          <h2 style={{ margin: 0 }}>Sign in</h2>
          <p className="muted" style={{ margin: 0 }}>Use the credentials configured for this workspace.</p>
          <label className="field">Username<input className="input" value={u} onChange={(e) => setU(e.target.value)} autoFocus autoComplete="username" /></label>
          <label className="field">Password<input className="input" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" /></label>
          {err && <div className="error-box">{err}</div>}
          <button className="btn primary" disabled={busy || !u || !p} style={{ justifyContent: "center" }}>{busy && <Spinner />}Sign in</button>
        </form>
      </div>
    </div>
  );
}
