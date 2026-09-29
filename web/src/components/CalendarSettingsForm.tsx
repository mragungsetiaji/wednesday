import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchCalendar, fetchNewsReactions, importCalendar, saveCalendar, type CalendarHistory, type CalendarResponse, type CalendarSettings,
  type NewsReaction,
} from "../api";

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
      <NewsReactions />
    </form>
  );
}

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString([], { dateStyle: "medium" }) : "–");
const px = (v: number | null) => (v === null ? "–" : v.toFixed(2));
const atr = (v: number | null) => (v === null ? "" : ` · ${v.toFixed(1)} ATR`);
const signed = (v: number | null) => (v === null ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}`);

/**
 * How gold moved after past releases of each event type, from the calendar history (every fetched
 * week, plus imported CSVs) and the stored M1 bars. Only releases with bars around them count.
 */
function NewsReactions() {
  const [types, setTypes] = useState<NewsReaction[] | null>(null);
  const [history, setHistory] = useState<CalendarHistory | null>(null);
  const [zone, setZone] = useState("UTC");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => fetchNewsReactions().then((r) => {
    setTypes(r.available ? r.types : null);
    setHistory(r.history);
  }).catch(() => {}), []);
  useEffect(() => {
    load();
  }, [load]);

  const upload = async (file: File) => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await importCalendar(file, zone);
      setMessage({ kind: "ok", text: `Added ${r.imported} release${r.imported === 1 ? "" : "s"} to the history.` });
      await load();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  if (types === null) return null;
  const measured = types.filter((t) => t.count > 0);
  return (
    <div className="news-reactions">
      <h3 className="subhead">Past reactions</h3>
      <p className="field-note">
        Gold's move after earlier releases of each event, from the stored M1 bars: the size of the move 5, 15 and 60
        minutes after, the range of the first 15 minutes, and how often the first move had reversed by the hour. They
        describe what happened, not what will. The risk-time card, the news lines and the brief show them too.
      </p>
      <p className="field-note num">
        {history && history.stored > 0
          ? <>Calendar history: {history.stored} releases, {day(history.first)} to {day(history.last)}. </>
          : "No calendar history yet: every fetched week is kept from now on. "}
        Older weeks can be imported from CSV.
      </p>
      <div className="news-import">
        <label className="field">
          <span className="field-label">Times in the file without an offset are</span>
          <select value={zone} onChange={(e) => setZone(e.target.value)} disabled={busy}>
            <option value="UTC">UTC</option>
            <option value="America/New_York">New York time</option>
          </select>
        </label>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) upload(f);
          e.target.value = "";
        }} />
        <button type="button" className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}
          title="Columns: currency (or country), title (or event), datetime (or date and time); optional impact, actual, forecast, previous">
          {busy ? "Importing…" : "Import calendar CSV"}
        </button>
        <span className="form-status" role="status">
          {message && <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span>}
        </span>
      </div>
      {measured.length === 0 ? (
        <p className="empty">
          {types.length ? "No stored release has M1 bars around it yet." : "No release matching the currencies and impact above is stored yet."}
        </p>
      ) : (
        <div className="table-scroll">
          <table className="data compact news-reaction-table">
            <thead>
              <tr>
                <th scope="col">Event</th>
                <th scope="col" className="end" title="Releases with M1 bars around them, of those stored">Used</th>
                <th scope="col" className="end" title="Median high-low of the first 15 minutes">15m range</th>
                <th scope="col" className="end" title="Median size of the move from the last price before the release">Move 5m / 15m / 60m</th>
                <th scope="col" className="end" title="The 60-minute move on the other side of the price before the release from the 5-minute one">Reversed</th>
                <th scope="col" className="end wrap" title="Median 15-minute move when the actual came in above or below the forecast">Above / below forecast</th>
              </tr>
            </thead>
            <tbody>
              {measured.map((t) => (
                <tr key={`${t.currency} ${t.title}`}>
                  <td>{t.currency} {t.title}</td>
                  <td className="end num">{t.count}<span className="muted"> / {t.stored}</span></td>
                  <td className="end num">{px(t.range15)}<span className="muted">{atr(t.range15_atr)}</span></td>
                  <td className="end num" title={t.move_atr["15"] === null ? undefined
                    : `In ATRs: ${[t.move_atr["5"], t.move_atr["15"], t.move_atr["60"]].map((v) => (v === null ? "–" : v.toFixed(1))).join(" / ")}`}>
                    {px(t.move["5"])} / {px(t.move["15"])} / {px(t.move["60"])}
                  </td>
                  <td className="end num">{t.reversed} of {t.count}</td>
                  <td className="end num wrap">
                    {t.surprise.above.count || t.surprise.below.count
                      ? <>{signed(t.surprise.above.move15)} <span className="muted">({t.surprise.above.count})</span> / {signed(t.surprise.below.move15)} <span className="muted">({t.surprise.below.count})</span></>
                      : <span className="muted">no actuals</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
