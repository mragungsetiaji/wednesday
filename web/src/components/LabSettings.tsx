import { useEffect, useState } from "react";

import { fetchTrust, saveTrust, type TrustedKey, type TrustSettings } from "../api";

/** Lab settings: whose signed model files load without asking, and whether unsigned ones load at all. */
export function LabSettings() {
  const [saved, setSaved] = useState<TrustSettings | null>(null);
  const [form, setForm] = useState<TrustSettings | null>(null);
  const [name, setName] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchTrust()
      .then((t) => {
        if (!alive) return;
        setSaved(t);
        setForm(t);
      })
      .catch((err) => {
        if (alive) setLoadError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (loadError) {
    return (
      <div className="settings-form">
        <div className="settings-intro">
          <h2>Lab</h2>
          <p className="text-error">{loadError}</p>
        </div>
      </div>
    );
  }
  if (!saved || !form) return null;
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const set = (patch: Partial<TrustSettings>) => {
    setForm({ ...form, ...patch });
    setMessage(null);
  };

  const add = () => {
    if (!name.trim() || !publicKey.trim()) return;
    set({ keys: [...form.keys, { name: name.trim(), public_key: publicKey.trim() }] });
    setName("");
    setPublicKey("");
  };
  const remove = (k: TrustedKey) => set({ keys: form.keys.filter((x) => x !== k) });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const t = await saveTrust(form);
      setSaved(t);
      setForm(t);
      setMessage({ kind: "ok", text: "Lab settings saved." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="lab-h">
      <div className="settings-intro">
        <h2 id="lab-h">Lab</h2>
        <p>
          Model files can be signed, so you know who made one before it runs on your machine. Files signed by a key
          below load straight away; anything else asks first.
        </p>
      </div>

      <fieldset className="fields" disabled={busy}>
        <legend>Trusted keys</legend>
        <table className="data compact">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Key id</th>
              <th scope="col"><span className="sr-only">Remove</span></th>
            </tr>
          </thead>
          <tbody>
            {form.keys.map((k, i) => (
              <tr key={`${k.public_key}-${i}`}>
                <td>{k.name}{k.builtin && <span className="muted"> · built in</span>}</td>
                <td className="num">{k.key_id ?? "saved on save"}</td>
                <td className="end">
                  {!k.builtin && (
                    <button type="button" className="button quiet" onClick={() => remove(k)}>Remove</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="field">
          <label className="field-label" htmlFor="trust-name">Name</label>
          <input id="trust-name" type="text" value={name} placeholder="A colleague" maxLength={60}
            onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="trust-key">Public key</label>
          <textarea id="trust-key" rows={2} value={publicKey} spellCheck={false}
            placeholder="44 characters of base64, or a -----BEGIN PUBLIC KEY----- block"
            onChange={(e) => setPublicKey(e.target.value)} />
          <span className="field-hint">
            The person signing prints theirs with <code>wednesday --new-signing-key PATH</code>. Never paste a private key here.
          </span>
        </div>
        <div className="form-actions">
          <button type="button" className="button quiet" disabled={!name.trim() || !publicKey.trim()} onClick={add}>
            Add key
          </button>
        </div>
      </fieldset>

      <fieldset className="fields" disabled={busy}>
        <legend className="sr-only">Signed models only</legend>
        <label className="switch">
          <input type="checkbox" checked={form.only_signed} onChange={(e) => set({ only_signed: e.target.checked })} />
          <span>Only load models signed by a trusted key</span>
        </label>
        <p className="field-hint">Unsigned files and files from other keys are refused instead of asking.</p>
      </fieldset>

      <div className="form-actions">
        <button type="submit" className="button secondary" disabled={!dirty || busy}>
          {busy ? "Saving…" : "Save lab settings"}
        </button>
        <span className="form-status" role="status">
          {message ? <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span> : dirty ? "Unsaved changes" : ""}
        </span>
      </div>
    </form>
  );
}
