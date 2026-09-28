import { useEffect, useState } from "react";

import {
  createApiToken, fetchApiTokens, fetchAuthStatus, logOut, LOGGED_OUT_EVENT, revokeApiToken, type ApiToken, type AuthStatus,
} from "../api";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

/** The dashboard login: whether it's on, logging out, and API tokens for scripts. */
export function AccessSettings() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [name, setName] = useState("");
  const [made, setMade] = useState<{ name: string; token: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    fetchAuthStatus()
      .then((s) => {
        setStatus(s);
        if (s.enabled) fetchApiTokens().then((r) => setTokens(r.tokens)).catch(() => {});
      })
      .catch(() => {
        /* the data source section already reports an unreachable API */
      });
  }, []);

  if (!status) return null;

  const run = async (job: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await job();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const create = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const t = await createApiToken(name);
      setTokens([...tokens, t]);
      setMade({ name: t.name, token: t.token });
      setName("");
    });
  };

  return (
    <div className="settings-form">
      <div className="settings-intro">
        <h2>Access</h2>
        {status.enabled ? (
          <p>The dashboard asks for the password set in <code>.env</code>. Scripts use an API token instead.</p>
        ) : (
          <p>
            No login: anyone who can reach this port controls Wednesday, so it only listens on this computer. To open it
            from a phone or another machine, set <code>XAU_AUTH_PASSWORD</code> in <code>.env</code> (or the hash from{" "}
            <code>wednesday --hash-password</code>) and put it behind HTTPS; see <code>docs/deploy-windows.md</code>.
          </p>
        )}
      </div>

      {status.enabled && (
        <>
          <div className="form-actions">
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => run(async () => {
                await logOut();
                window.dispatchEvent(new Event(LOGGED_OUT_EVENT));
              })}>
              Log out
            </button>
          </div>

          <div>
            <h3 className="subhead">API tokens</h3>
            <p className="field-hint">
              Send one as <code>Authorization: Bearer TOKEN</code>. Only a fingerprint is stored, so a token shows once.
            </p>
            {tokens.length === 0 ? (
              <p className="empty">None yet.</p>
            ) : (
              <table className="data compact">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Made</th>
                    <th scope="col">Last used</th>
                    <th scope="col"><span className="sr-only">Revoke</span></th>
                  </tr>
                </thead>
                <tbody>
                  {tokens.map((t) => (
                    <tr key={t.id}>
                      <td>{t.name}</td>
                      <td>{when(t.created_at)}</td>
                      <td>{when(t.last_used_at)}</td>
                      <td className="end">
                        <button type="button" className="button quiet" disabled={busy}
                          onClick={() => run(async () => {
                            setTokens((await revokeApiToken(t.id)).tokens);
                            if (made?.name === t.name) setMade(null);
                          })}>
                          Revoke
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {made && (
              <div className="notice token-once" role="status">
                <p>Copy the token for <strong>{made.name}</strong> now; it won't show again.</p>
                <input type="text" readOnly value={made.token} className="num" onFocus={(e) => e.target.select()} />
              </div>
            )}
            <form className="input-with-button token-new" onSubmit={create}>
              <input type="text" value={name} placeholder="What uses it, e.g. backup script" maxLength={120}
                aria-label="Token name" onChange={(e) => setName(e.target.value)} />
              <button type="submit" className="button secondary" disabled={!name.trim() || busy}>New token</button>
            </form>
          </div>
        </>
      )}
      {message && <p className={message.kind === "error" ? "text-error field-note" : "field-note"}>{message.text}</p>}
    </div>
  );
}
