import { Fragment, useCallback, useEffect, useState, type ReactNode } from "react";

import {
  fetchAuthStatus, fetchPrefs, importMine, LOGGED_OUT_EVENT, logIn, savePrefs, setUp, signUp, startPreview,
  type AuthStatus,
} from "../api";
import { CrossIcon } from "../icons";
import { applyPrefs, clearLocal, hasLocalData, localData, prefsDiff, prefsOf, setPreview } from "../localData";

type State = "checking" | "open" | "preview" | "login" | "signup" | "setup";

/** How often a logged-in browser saves its changed preferences to the account. */
const PREFS_EVERY_MS = 5000;

/**
 * The login, when the server asks for one (served on a network host, or --login). Without a
 * session, a browser first gets a 24-hour preview: the app runs and keeps its drawings, templates
 * and workspaces in this browser (localData.ts). When the preview ends, the wall: log in, or sign
 * up for an account the admin approves. Logging in moves what the browser kept into the account.
 * With no account yet it offers Create account with the setup code from the server's log.
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>("checking");
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [form, setForm] = useState(false); // the login form over the preview
  const [banner, setBanner] = useState(() => !bannerClosed()); // the preview's banner, until closed
  const [formMode, setFormMode] = useState<"login" | "signup" | "setup">("login");
  const [session, setSession] = useState(0); // remounts the app after each login so it reloads
  const [code, setCode] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const check = useCallback(async () => {
    let s: AuthStatus;
    try {
      s = await fetchAuthStatus();
    } catch {
      setPreview(false);
      setState("open"); // unreachable API: the app reports it as offline
      return;
    }
    if (s.login && !s.logged_in && !s.guest && !s.setup && s.guest_available) {
      try {
        s = await startPreview();
      } catch {
        /* no preview from here: the wall */
      }
    }
    setStatus(s);
    if (!s.login) {
      setPreview(false);
      setState("open");
    } else if (s.logged_in) {
      setPreview(false);
      await loadPrefs();
      setState("open");
    } else if (s.guest) {
      setPreview(true);
      setState("preview");
    } else {
      setPreview(false);
      setState(s.setup ? "setup" : "login");
    }
  }, []);

  useEffect(() => {
    void check();
    const again = () => void check();
    window.addEventListener(LOGGED_OUT_EVENT, again);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, again);
  }, [check]);

  // The preview ends on time: back to the server, which shows the wall.
  useEffect(() => {
    if (state !== "preview" || !status?.guest) return;
    const left = Date.parse(status.guest.until) - Date.now();
    const t = window.setTimeout(() => void check(), Math.max(1000, Math.min(left + 1000, 2 ** 31 - 1)));
    return () => window.clearTimeout(t);
  }, [state, status, check]);

  // Logged in: the preferences follow the account.
  useEffect(() => {
    if (state !== "open" || !status?.login || !status.logged_in) return;
    return syncPrefs();
  }, [state, status]);

  const setMode = (m: "login" | "signup" | "setup") => {
    setError(null);
    setNotice(null);
    if (state === "preview") setFormMode(m);
    else setState(m);
  };

  if (state === "checking") return null;
  const app = <Fragment key={session}>{children}</Fragment>;
  if (state === "open") return app;
  if (state === "preview" && !form) {
    return (
      <>
        {banner ? (
          <PreviewBanner until={status?.guest?.until ?? null}
            onLogin={() => { setForm(true); setMode("login"); }}
            onSignup={() => { setForm(true); setMode("signup"); }}
            onClose={() => { closeBanner(); setBanner(false); }} />
        ) : (
          <div className="preview-banner is-small">
            <button type="button" className="button quiet" onClick={() => { setForm(true); setMode("login"); }}>Log in</button>
          </div>
        )}
        {app}
      </>
    );
  }

  const mode: "login" | "signup" | "setup" = state === "preview" ? formMode : state;

  const creating = mode !== "login";
  const mismatch = creating && again !== "" && again !== password;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signup") {
        await signUp(username, password, email);
        setPassword("");
        setAgain("");
        setNotice("Account created. You can log in once the admin approves it.");
        setMode("login");
        return;
      }
      const s = mode === "setup" ? await setUp(code, username, password) : await logIn(username, password);
      setPassword("");
      setAgain("");
      setCode("");
      setPreview(false);
      await moveIntoAccount();
      await loadPrefs();
      setStatus(s);
      setForm(false);
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

  const ended = !status?.guest && status?.login && !status.setup;
  const lead = mode === "setup"
    ? "No account yet. Enter the setup code from the server's log, then choose a username and password."
    : mode === "signup"
      ? "Choose a username and password. The admin approves new accounts before they can log in."
      : form
        ? "Log in to keep your drawings, templates and layout in your account, on every device."
        : ended
          ? "The 24-hour preview has ended. Log in, or sign up for an account."
          : "Log in to use this dashboard.";
  const title = mode === "setup" ? "Create the account" : mode === "signup" ? "Sign up" : "Wednesday";

  return (
    <div className="terms-page">
      <form className="terms-card login-card" onSubmit={submit} aria-labelledby="login-h">
        <h2 id="login-h">{title}</h2>
        <p className="terms-lead">{lead}</p>
        {mode === "setup" && (
          <label className="field">
            <span className="field-label">Setup code</span>
            <input type="text" autoComplete="one-time-code" autoFocus spellCheck={false} placeholder="XXXX-XXXX-XXXX"
              value={code} onChange={typed(setCode)} />
          </label>
        )}
        <label className="field">
          <span className="field-label">Username</span>
          <input type="text" autoComplete="username" autoFocus={mode !== "setup"} spellCheck={false} autoCapitalize="none"
            value={username} onChange={typed(setUsername)} />
        </label>
        {mode === "signup" && (
          <label className="field">
            <span className="field-label">Email (optional)</span>
            <input type="email" autoComplete="email" spellCheck={false} value={email} onChange={typed(setEmail)} />
          </label>
        )}
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
            disabled={busy || !username || !password || (creating && again !== password) || (mode === "setup" && !code)}>
            {busy ? "Working…" : mode === "setup" ? "Create account" : mode === "signup" ? "Sign up" : "Log in"}
          </button>
          {mode === "login" && status?.signup && (
            <button type="button" className="button quiet" disabled={busy} onClick={() => setMode("signup")}>
              Sign up
            </button>
          )}
          {mode !== "login" && (
            <button type="button" className="button quiet" disabled={busy} onClick={() => setMode("login")}>
              I have an account
            </button>
          )}
          {mode === "login" && status?.setup && (
            <button type="button" className="button quiet" disabled={busy} onClick={() => setMode("setup")}>
              Create account
            </button>
          )}
          {form && (
            <button type="button" className="button quiet" disabled={busy}
              onClick={() => { setForm(false); setError(null); setNotice(null); }}>
              Back to the preview
            </button>
          )}
        </div>
        <p className="form-status" role="status">
          {error && <span className="text-error">{error}</span>}
          {!error && notice}
        </p>
      </form>
    </div>
  );
}

/** Closing the preview's banner leaves a small Log in; it holds for this tab (sessionStorage). */
const BANNER_KEY = "wedlocal.bannerClosed";
function bannerClosed(): boolean {
  try {
    return sessionStorage.getItem(BANNER_KEY) === "1";
  } catch {
    return false;
  }
}
function closeBanner(): void {
  try {
    sessionStorage.setItem(BANNER_KEY, "1");
  } catch {
    /* closed until the next load */
  }
}

function PreviewBanner({ until, onLogin, onSignup, onClose }: {
  until: string | null; onLogin: () => void; onSignup: () => void; onClose: () => void;
}) {
  const ends = until ? new Date(until) : null;
  const when = ends ? ends.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <div className="preview-banner" role="status">
      <span>
        Preview until {when}. Your drawings, templates and layout stay in this browser; log in to keep them in an account.
      </span>
      <button type="button" className="button quiet" onClick={onLogin}>Log in</button>
      <button type="button" className="button primary" onClick={onSignup}>Sign up</button>
      <button type="button" className="preview-close" aria-label="Close" title="Close" onClick={onClose}>
        <CrossIcon size={12} />
      </button>
    </div>
  );
}

/** The preview's drawings, templates, workspaces and preferences, into the account just logged in. */
async function moveIntoAccount(): Promise<void> {
  const data = localData();
  if (!hasLocalData(data) && Object.keys(data.prefs).length === 0) return;
  try {
    await importMine(data);
    clearLocal();
  } catch {
    /* kept in this browser: the next login tries again */
  }
}

/** The account's preferences, into this browser (what another device changed). */
async function loadPrefs(): Promise<void> {
  try {
    applyPrefs((await fetchPrefs()).prefs);
  } catch {
    /* this browser's own preferences stay */
  }
}

/** Save the preferences that change in this browser to the account; returns the stop. */
function syncPrefs(): () => void {
  let last = prefsOf();
  const flush = (keepalive: boolean) => {
    const now = prefsOf();
    const diff = prefsDiff(last, now);
    if (!diff) return;
    last = now;
    if (keepalive) {
      void fetch("/api/prefs", { method: "PUT", keepalive: true, headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify(diff) }).catch(() => undefined);
    } else {
      savePrefs(diff).catch(() => undefined);
    }
  };
  const t = window.setInterval(() => flush(false), PREFS_EVERY_MS);
  const leaving = () => flush(true);
  window.addEventListener("pagehide", leaving);
  return () => {
    window.clearInterval(t);
    window.removeEventListener("pagehide", leaving);
    flush(false);
  };
}
