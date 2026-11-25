import { Fragment, useEffect, useState, type ReactNode } from "react";

import { fetchAuthStatus, LOGGED_OUT_EVENT, logIn, setUp } from "../api";

type State = "checking" | "open" | "login" | "setup";

/**
 * The login screen, when the server asks for one (served on a network host, or --login). With no
 * account yet it offers Create account with the setup code from the server's log. Without a
 * login, or once logged in, the app shows; a 401 from any API call brings the screen back.
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [canSetUp, setCanSetUp] = useState(false);
  const [session, setSession] = useState(0); // remounts the app after each login so it reloads
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const check = () =>
      fetchAuthStatus()
        .then((s) => {
          setCanSetUp(s.setup);
          setState(!s.login || s.logged_in ? "open" : s.setup ? "setup" : "login");
        })
        .catch(() => setState("open")); // unreachable API: the app reports it as offline
    check();
    window.addEventListener(LOGGED_OUT_EVENT, check);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, check);
  }, []);

  if (state === "checking") return null;
  if (state === "open") return <Fragment key={session}>{children}</Fragment>;

  const creating = state === "setup";
  const mismatch = creating && again !== "" && again !== password;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError(null);
    try {
      if (creating) await setUp(code, username, password);
      else await logIn(username, password);
      setPassword("");
      setAgain("");
      setCode("");
      setSession((n) => n + 1);
      setState("open");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const typed = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) => {
    set(e.target.value);
    setError(null);
  };

  return (
    <div className="terms-page">
      <form className="terms-card login-card" onSubmit={submit} aria-labelledby="login-h">
        <h2 id="login-h">{creating ? "Create the account" : "Wednesday"}</h2>
        <p className="terms-lead">
          {creating
            ? "No account yet. Enter the setup code from the server's log, then choose a username and password."
            : "Log in to use this dashboard."}
        </p>
        {creating && (
          <label className="field">
            <span className="field-label">Setup code</span>
            <input type="text" autoComplete="one-time-code" autoFocus spellCheck={false} placeholder="XXXX-XXXX-XXXX"
              value={code} onChange={typed(setCode)} />
          </label>
        )}
        <label className="field">
          <span className="field-label">Username</span>
          <input type="text" autoComplete="username" autoFocus={!creating} spellCheck={false} autoCapitalize="none"
            value={username} onChange={typed(setUsername)} />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <input type="password" autoComplete={creating ? "new-password" : "current-password"} value={password}
            onChange={typed(setPassword)} />
          {creating && <span className="field-hint">8 to 256 characters.</span>}
        </label>
        {creating && (
          <label className="field">
            <span className="field-label">Password again</span>
            <input type="password" autoComplete="new-password" value={again} aria-invalid={mismatch}
              onChange={typed(setAgain)} />
            {mismatch && <span className="field-note text-error">The passwords don't match.</span>}
          </label>
        )}
        <div className="form-actions">
          <button type="submit" className="button primary"
            disabled={busy || !username || !password || (creating && (!code || again !== password))}>
            {busy ? "Working…" : creating ? "Create account" : "Log in"}
          </button>
          {canSetUp && (
            <button type="button" className="button quiet" disabled={busy}
              onClick={() => { setState(creating ? "login" : "setup"); setError(null); }}>
              {creating ? "I have an account" : "Create account"}
            </button>
          )}
        </div>
        <p className="form-status" role="status">{error && <span className="text-error">{error}</span>}</p>
      </form>
    </div>
  );
}
