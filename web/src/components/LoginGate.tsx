import { Fragment, useEffect, useState, type ReactNode } from "react";

import { fetchAuthStatus, logIn, LOGGED_OUT_EVENT } from "../api";

/**
 * The login screen, when the server has one (XAU_AUTH_PASSWORD). Without a login, or once logged
 * in, the app shows. A 401 from any API call (the session ended) brings the screen back.
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "open" | "login">("checking");
  const [session, setSession] = useState(0); // remounts the app after each login so it reloads
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAuthStatus()
      .then((s) => setState(s.enabled && !s.logged_in ? "login" : "open"))
      .catch(() => setState("open")); // unreachable API: the app reports it as offline
    const onLoggedOut = () => setState("login");
    window.addEventListener(LOGGED_OUT_EVENT, onLoggedOut);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, onLoggedOut);
  }, []);

  if (state === "checking") return null;
  if (state === "open") return <Fragment key={session}>{children}</Fragment>;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await logIn(password);
      setPassword("");
      setSession((n) => n + 1);
      setState("open");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="terms-page">
      <form className="terms-card login-card" onSubmit={submit} aria-labelledby="login-h">
        <h2 id="login-h">Wednesday</h2>
        <p className="terms-lead">This dashboard needs a password.</p>
        <label className="field">
          <span className="field-label">Password</span>
          <input type="password" autoComplete="current-password" autoFocus value={password}
            onChange={(e) => { setPassword(e.target.value); setError(null); }} />
        </label>
        <div className="form-actions">
          <button type="submit" className="button primary" disabled={!password || busy}>
            {busy ? "Logging in…" : "Log in"}
          </button>
          <span className="form-status" role="status">{error && <span className="text-error">{error}</span>}</span>
        </div>
      </form>
    </div>
  );
}
