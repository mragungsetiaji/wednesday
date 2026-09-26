import { useEffect, useState } from "react";

import { fetchCalendar, saveCalendar, type CalendarResponse, type CalendarSettings } from "../api";

const CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF"];
const IMPACTS = ["High", "Medium", "Low"];
const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

/** Which news the risk-time card warns about, and this week's matching events. */
export function CalendarSettingsForm() {
  const [data, setData] = useState<CalendarResponse | null>(null);
  const [form, setForm] = useState<CalendarSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchCalendar()
        .then((res) => {
          if (!alive) return;
          setData(res);
          setForm((f) => f ?? res.settings ?? null);
        })
        .catch(() => {});
    load();
    const id = setInterval(load, 20000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!data) return null;
  if (!data.editable || !form || !data.settings) {
    return (
      <section className="settings-form" aria-labelledby="calendar-h">
        <div className="settings-intro">
          <h2 id="calendar-h">News calendar</h2>
          <p>Available when the screener runs with <code>--serve</code>.</p>
        </div>
      </section>
    );
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(data.settings);
  const set = (patch: Partial<CalendarSettings>) => {
    setForm({ ...form, ...patch });
    setMessage(null);
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await saveCalendar(form);
      setData(res);
      if (res.settings) setForm(res.settings);
      setMessage({ kind: "ok", text: "Calendar settings saved." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="calendar-h">
      <div className="settings-intro">
        <h2 id="calendar-h">News calendar</h2>
        <p>
          A card in the bottom right warns an hour before matching news and glows in the last 30 minutes. The calendar is fetched at
          most once an hour.
        </p>
      </div>

      <fieldset className="fields" disabled={busy}>
        <legend className="sr-only">Calendar switch</legend>
        <label className="switch">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          <span>Warn before news</span>
        </label>
      </fieldset>

      <fieldset className="fields" disabled={busy || !form.enabled}>
        <legend>Currencies</legend>
        <div className="check-row">
          {CURRENCIES.map((c) => (
            <label key={c} className="check">
              <input type="checkbox" checked={form.currencies.includes(c)} onChange={() => set({ currencies: toggle(form.currencies, c) })} />
              {c}
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="fields" disabled={busy || !form.enabled}>
        <legend>Impact</legend>
        <div className="check-row">
          {IMPACTS.map((i) => (
            <label key={i} className="check">
              <input type="checkbox" checked={form.impacts.includes(i)} onChange={() => set({ impacts: toggle(form.impacts, i) })} />
              {i}
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="fields" disabled={busy}>
        <legend className="sr-only">Source</legend>
        <label className="field">
          <span className="field-label">Source</span>
          <input type="text" value={form.url} spellCheck={false} onChange={(e) => set({ url: e.target.value })} />
          <span className="field-hint">ForexFactory's weekly JSON by default. Any URL returning the same format works.</span>
        </label>
      </fieldset>

      <div className="form-actions">
        <button type="submit" className="button secondary" disabled={!dirty || busy}>
          {busy ? "Saving…" : "Save calendar settings"}
        </button>
        <span className="form-status" role="status">
          {message ? <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span> : dirty ? "Unsaved changes" : ""}
        </span>
      </div>

      {data.error && <p className="text-error field-note">Last fetch failed: {data.error}{data.fetched_at && " Showing the last good copy."}</p>}

      <div>
        <h3 className="subhead">Coming up</h3>
        {data.events.length === 0 ? (
          <p className="empty">
            {data.loading ? "Fetching the calendar…" : data.fetched_at ? "Nothing matching for the rest of this week." : "No calendar yet."}
          </p>
        ) : (
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Event</th>
                <th scope="col" className="end">Forecast</th>
                <th scope="col" className="end">Previous</th>
              </tr>
            </thead>
            <tbody>
              {data.events.map((e) => (
                <tr key={`${e.time}-${e.title}`}>
                  <td className="num muted">{new Date(e.time).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}</td>
                  <td>{e.currency} {e.title} <span className="muted">· {e.impact}</span></td>
                  <td className="end num">{e.forecast ?? "–"}</td>
                  <td className="end num muted">{e.previous ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data.fetched_at && <p className="field-note">Updated {new Date(data.fetched_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}.</p>}
      </div>
    </form>
  );
}
