import { useEffect, useState } from "react";

import { fetchBrief, generateBriefAsking, saveBrief, type BriefResponse, type BriefSettings } from "../api";
import { SecretField } from "./SecretField";

/** Form state: URLs edited as one per line. */
type Form = Omit<BriefSettings, "urls"> & { urls: string };

const toForm = (s: BriefSettings): Form => ({ ...s, urls: s.urls.join("\n") });
const fromForm = (f: Form): BriefSettings => ({
  ...f,
  model: f.model?.trim() || null,
  prompt: f.prompt?.trim() || null,
  urls: f.urls.split("\n").map((u) => u.trim()).filter(Boolean),
});

/** LLM brief settings: provider, API key, model, prompt and the news pages it reads. */
export function BriefSettingsForm() {
  const [data, setData] = useState<BriefResponse | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState<"save" | "run" | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [forgetKey, setForgetKey] = useState(false);

  const running = !!data?.running;
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchBrief()
        .then((res) => {
          if (!alive) return;
          setData(res);
          setForm((f) => f ?? (res.settings ? toForm(res.settings) : null));
        })
        .catch(() => {});
    load();
    const id = setInterval(load, running ? 2000 : 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [running]);

  if (!data) return null;
  if (!data.editable || !form || !data.settings) {
    return (
      <section className="settings-form" aria-labelledby="brief-h">
        <div className="settings-intro">
          <h2 id="brief-h">News brief</h2>
          <p>Available when the screener runs with <code>--serve</code>.</p>
        </div>
      </section>
    );
  }

  const saved = toForm(data.settings);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved) || apiKey !== "" || forgetKey;
  const provider = data.providers?.find((p) => p.id === form.provider);
  const set = (patch: Partial<Form>) => {
    setForm({ ...form, ...patch });
    setMessage(null);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("save");
    try {
      const secret = provider?.secret;
      const res = await saveBrief(fromForm(form), secret ? {
        [secret]: apiKey || undefined,
        [`forget_${secret}`]: forgetKey || undefined,
      } : {});
      setData(res);
      if (res.settings) setForm(toForm(res.settings));
      setApiKey("");
      setForgetKey(false);
      setMessage({ kind: "ok", text: "Brief settings saved." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  };

  const run = async () => {
    setBusy("run");
    try {
      setData(await generateBriefAsking());
      setMessage({ kind: "ok", text: "Writing the brief. It shows up next to the chart in a minute." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  };

  const last = data.last;
  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="brief-h">
      <div className="settings-intro">
        <h2 id="brief-h">News brief</h2>
        <p>
          An LLM reads the news pages below with the current structure and writes a few points plus a suggested bias.
          The bias stays yours: the suggestion only applies when you click it.
        </p>
      </div>

      <fieldset className="choices" disabled={busy !== null}>
        <legend>Model provider</legend>
        {data.providers?.map((p) => (
          <label key={p.id} className={`choice${form.provider === p.id ? " is-checked" : ""}`}>
            <input type="radio" name="brief-provider" value={p.id} checked={form.provider === p.id}
              onChange={() => { set({ provider: p.id, model: null }); setApiKey(""); setForgetKey(false); }} />
            <span className="choice-body">
              <span className="choice-title">{p.title}</span>
              <span className="choice-desc">API key {p.key_set ? "set" : "not set"}.</span>
              {!p.installed && <span className="choice-warn">Install the SDK: uv sync --extra llm</span>}
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="fields" disabled={busy !== null}>
        <legend>Request</legend>
        <SecretField label={`${provider?.title ?? "Provider"} API key`} source={provider?.key_source ?? null} value={apiKey}
          forget={forgetKey} placeholder={form.provider === "anthropic" ? "sk-ant-…" : "sk-…"}
          onChange={(v) => { setApiKey(v); setForgetKey(false); setMessage(null); }}
          onForget={() => { setForgetKey(true); setApiKey(""); setMessage(null); }} />
        <label className="field">
          <span className="field-label">Model</span>
          <input type="text" value={form.model ?? ""} placeholder={provider?.default_model} spellCheck={false}
            onChange={(e) => set({ model: e.target.value })} />
          <span className="field-hint">Leave empty for {provider?.default_model}.</span>
        </label>
        <label className="field">
          <span className="field-label">News URLs</span>
          <textarea rows={5} value={form.urls} spellCheck={false}
            placeholder={"https://www.forexfactory.com/calendar\nhttps://www.reuters.com/markets/commodities/"}
            onChange={(e) => set({ urls: e.target.value })} />
          <span className="field-hint">One page per line, up to 12. Each is fetched as text when the brief is written.</span>
        </label>
        <label className="field">
          <span className="field-label">Prompt</span>
          <textarea rows={10} value={form.prompt ?? ""} placeholder={data.default_prompt}
            onChange={(e) => set({ prompt: e.target.value })} />
          <span className="field-hint">
            Leave empty for the default shown. Keep a last line like <code>BIAS: BEARISH</code> in the output so the dashboard can
            offer it as a suggestion.
          </span>
        </label>
        <label className="field field-narrow">
          <span className="field-label">Characters per page</span>
          <input type="number" min={1000} max={200000} step={1000} value={form.max_chars_per_source}
            onChange={(e) => set({ max_chars_per_source: Number(e.target.value) })} />
          <span className="field-hint">Longer pages are cut to this, and the brief says which ones were cut.</span>
        </label>
      </fieldset>

      <div className="form-actions">
        <button type="submit" className="button secondary" disabled={!dirty || busy !== null}>
          {busy === "save" ? "Saving…" : "Save brief settings"}
        </button>
        <button type="button" className="button quiet" disabled={dirty || running || busy !== null} onClick={run}
          title={dirty ? "Save first" : undefined}>
          {running ? "Writing…" : "Write brief now"}
        </button>
        <span className="form-status" role="status">
          {message ? <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span> : dirty ? "Unsaved changes" : ""}
        </span>
      </div>

      {data.error && !running && <p className="text-error field-note">Last brief failed: {data.error}</p>}

      {last && (
        <div>
          <h3 className="subhead">Last brief</h3>
          <p className="field-note">
            {new Date(last.created_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })} · {last.model}
            {last.suggested_bias && <> · suggested {last.suggested_bias}</>}
          </p>
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">Page</th>
                <th scope="col" className="end">Read</th>
              </tr>
            </thead>
            <tbody>
              {last.sources.map((s) => (
                <tr key={s.url}>
                  <td className="wrap">{s.url}</td>
                  <td className="end num">
                    {s.ok ? `${s.chars.toLocaleString()} chars${s.truncated ? ", cut" : ""}` : <span className="text-error">{s.error}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </form>
  );
}
