import { useEffect, useState } from "react";

import { changePassword, endDevice, fetchDevices, logOut, type AuthUser, type Device } from "../api";

/** A session's time (UTC, from the server) in this computer's clock. */
const fmtWhen = (iso: string | null) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–");

/**
 * Settings > Login, shown when the server asks for a login: the devices logged in to the account
 * (each can be logged out), a new password (which logs every other device out) and Log out.
 */
export function LoginSettings({ user, onLoggedOut }: { user: AuthUser | null; onLoggedOut: () => void }) {
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  useEffect(() => {
    fetchDevices()
      .then((res) => setDevices(res.sessions))
      .catch((e) => setMessage({ kind: "error", text: `Can't load the devices: ${e instanceof Error ? e.message : e}` }));
  }, []);

  const run = async (work: () => Promise<{ sessions: Device[] }>, done: string): Promise<boolean> => {
    setBusy(true);
    setMessage(null);
    try {
      setDevices((await work()).sessions);
      setMessage({ kind: "ok", text: done });
      return true;
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const mismatch = again !== "" && again !== next;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    const ok = await run(() => changePassword(current, next), "Changed the password. Every other device is logged out.");
    if (ok) {
      setCurrent("");
      setNext("");
      setAgain("");
    }
  };

  const leave = async () => {
    setBusy(true);
    try {
      await logOut();
      onLoggedOut();
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      setBusy(false);
    }
  };

  return (
    <section className="settings-form" aria-labelledby="login-settings-h">
      <div className="settings-intro">
        <h2 id="login-settings-h">Login</h2>
        <p>
          {user ? <>Logged in as <strong>{user.username}</strong>. </> : null}
          The devices logged in to this account stay logged in for 30 days after they were last used.
        </p>
      </div>

      {message && <p className={message.kind === "error" ? "text-error" : "muted"} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}

      {devices && (
        <table className="data compact">
          <thead>
            <tr>
              <th scope="col">Device</th>
              <th scope="col">Logged in</th>
              <th scope="col">Last used</th>
              <th scope="col"><span className="sr-only">Log out</span></th>
            </tr>
          </thead>
          <tbody>
            {devices.map((d) => (
              <tr key={d.id}>
                <td>{d.device ?? "Unknown device"}{d.current && <span className="muted"> (this one)</span>}</td>
                <td>{fmtWhen(d.created_at)}</td>
                <td>{fmtWhen(d.last_seen_at)}</td>
                <td className="end">
                  {d.current ? (
                    <button type="button" className="button quiet" disabled={busy} onClick={leave}>Log out</button>
                  ) : (
                    <button type="button" className="button quiet" disabled={busy}
                      onClick={() => run(() => endDevice(d.id), `Logged out ${d.device ?? "the device"}.`)}>Log out</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form className="fields login-password" onSubmit={submit} aria-labelledby="password-h">
        <h3 id="password-h">Change the password</h3>
        <input type="text" autoComplete="username" value={user?.username ?? ""} readOnly hidden />
        <label className="field">
          <span className="field-label">Current password</span>
          <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">New password</span>
          <input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
          <span className="field-hint">8 to 256 characters. Every other device is logged out.</span>
        </label>
        <label className="field">
          <span className="field-label">New password again</span>
          <input type="password" autoComplete="new-password" value={again} aria-invalid={mismatch}
            onChange={(e) => setAgain(e.target.value)} />
          {mismatch && <span className="field-note text-error">The passwords don't match.</span>}
        </label>
        <div className="form-actions">
          <button type="submit" className="button primary" disabled={busy || !current || !next || again !== next}>
            {busy ? "Working…" : "Change password"}
          </button>
        </div>
      </form>
    </section>
  );
}
