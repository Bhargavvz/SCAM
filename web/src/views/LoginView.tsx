import { FormEvent, useState } from "react";
import { api } from "../api";

export function LoginView({ onSignedIn }: { onSignedIn: (user: string | null) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.login(username.trim(), password);
      onSignedIn(r.user);
    } catch (err) {
      setError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-side">
        <div className="brand"><span className="mark" />Supply Chain Memory <small>decision desk</small></div>
        <h1>Decisions that remember what happened last time.</h1>
        <ul>
          <li>Recalls three years of supplier history from memory</li>
          <li>Checks every open commitment before it recommends</li>
          <li>Prices each response with a deterministic simulator</li>
          <li>Cites the documents and records behind every call</li>
        </ul>
        <div className="muted">Access is limited to invited reviewers.</div>
      </div>
      <form className="login-card" onSubmit={submit}>
        <h2>Sign in</h2>
        <p className="muted">Use the credentials you were given.</p>
        <label>
          Username
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <div className="alert error"><span className="tag">Error</span>{error}</div>}
        <button className="btn" type="submit" disabled={busy || !username || !password}>
          {busy && <span className="spinner" />}
          {busy ? "Signing in" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
