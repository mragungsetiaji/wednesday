import { useEffect, useState } from "react";

import { activateLicence, fetchLicence, removeLicence, type LicenceStatus } from "../api";
import { usePlugins } from "../plugins";

const REPO = "https://github.com/mragungsetiaji/wednesday";
const fmtDate = (iso: string) => new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });

/**
 * Plan and licence. The paid features come from a plugin; the licence, when a plugin
 * handles one, decides which of them it turns on. Everything else is free.
 */
export function AccountSettings() {
  const { data: plugins, refresh } = usePlugins();
  const [licence, setLicence] = useState<LicenceStatus | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    fetchLicence().then(setLicence).catch(() => setLicence({ available: false }));
  }, []);

  const run = async (fn: () => Promise<LicenceStatus>, ok: string) => {
    setBusy(true);
    setMessage(null);
    try {
      setLicence(await fn());
      setKey("");
      setMessage({ kind: "ok", text: ok });
      refresh();
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!licence || !plugins) return null;
  const catalog = plugins.catalog ?? [];
  return (
    <section className="settings-form" aria-labelledby="account-h">
      <div className="settings-intro">
        <h2 id="account-h">Plan</h2>
        <p>
          The screener, alerts, the bias, the news card, the news brief with your own API key, and labelling and training
          your own models are free. The features below come with Wednesday EE and a licence.
        </p>
      </div>

      {licence.available ? (
        <div className="licence">
          {licence.plan ? (
            <dl className="facts">
              <div><dt>Plan</dt><dd>{licence.plan}{!licence.valid && <span className="text-error"> · not active</span>}</dd></div>
              {licence.licensee && <div><dt>Licensed to</dt><dd>{licence.licensee}</dd></div>}
              {licence.expires_at && <div><dt>Until</dt><dd>{fmtDate(licence.expires_at)}</dd></div>}
              {licence.key_hint && <div><dt>Key</dt><dd className="num">…{licence.key_hint}</dd></div>}
            </dl>
          ) : (
            <p className="field-note">No licence yet.</p>
          )}
          {licence.error && <p className="text-error field-note">{licence.error}</p>}
          <form className="licence-form" onSubmit={(e) => {
            e.preventDefault();
            run(() => activateLicence(key), "Licence activated.");
          }}>
            <label className="field">
              <span className="field-label">{licence.plan ? "Replace the licence key" : "Licence key"}</span>
              <input type="text" value={key} spellCheck={false} autoComplete="off" placeholder="WEDK-…" onChange={(e) => setKey(e.target.value)} />
            </label>
            <div className="form-actions">
              <button type="submit" className="button primary" disabled={busy || !key.trim()}>Activate</button>
              {licence.plan && (
                <button type="button" className="button quiet" disabled={busy}
                  onClick={() => window.confirm("Remove the licence? Paid features turn off.") && run(removeLicence, "Licence removed.")}>
                  Remove licence
                </button>
              )}
              <span className="form-status" role="status">
                {message && <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span>}
              </span>
            </div>
          </form>
        </div>
      ) : (
        <p className="field-note">
          Wednesday EE isn't installed. It's a plugin (a separate Python package); once it is, the licence is entered here.
        </p>
      )}

      <ul className="feature-list">
        {catalog.map((f) => (
          <li key={f.id} className={f.enabled ? "is-on" : undefined}>
            <span className="feature-title">
              {f.title}
              <span className={`badge${f.enabled ? " accent" : ""}`}>{f.enabled ? "In your plan" : "Locked"}</span>
            </span>
            {f.description && <span className="feature-desc">{f.description}</span>}
            {f.issue && !f.enabled && (
              <a className="link feature-link" href={`${REPO}/issues/${f.issue}`} target="_blank" rel="noreferrer">
                Planned in #{f.issue}
              </a>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
