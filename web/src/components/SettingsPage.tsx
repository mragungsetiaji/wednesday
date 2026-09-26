import { useEffect, useState } from "react";

import { fetchSettings, saveSettings, type DataSettings, type SettingsResponse } from "../api";
import { fmtFeedTime } from "../format";
import { usePlugins } from "../plugins";
import { AccountSettings } from "./AccountSettings";
import { AlertsSettings } from "./AlertsSettings";
import { BriefSettingsForm } from "./BriefSettingsForm";
import { CalendarSettingsForm } from "./CalendarSettingsForm";

const EMPTY: DataSettings = { source: "yfinance", symbol: null, csv_path: null, mt5_login: null, mt5_server: null, mt5_path: null, clock: null };

const same = (a: DataSettings, b: DataSettings) => JSON.stringify(a) === JSON.stringify(b);
const orNull = (v: string) => (v.trim() === "" ? null : v.trim());

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/** Data source settings. Saving restarts the feed; the page keeps polling so the result shows up here. */
export function SettingsPage() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<DataSettings>(EMPTY);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let alive = true;
    let first = true;
    const load = () =>
      fetchSettings()
        .then((res) => {
          if (!alive) return;
          setData(res);
          setLoadError(null);
          if (first && res.settings) setForm(res.settings);
          first = false;
        })
        .catch((e) => alive && setLoadError(e instanceof Error ? e.message : String(e)));
    load();
    const id = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!data) {
    return (
      <main className="settings-page">
        <p className="empty">{loadError ? `Can't load settings: ${loadError}` : "Loading settings…"}</p>
      </main>
    );
  }

  const current = data.settings ?? EMPTY;
  const dirty = !same(form, current);
  const source = data.sources.find((s) => s.id === form.source);
  const running = data.running;
  const runningSource = data.sources.find((s) => s.id === running.source);
  const set = (patch: Partial<DataSettings>) => {
    setForm({ ...form, ...patch });
    setSaved(false);
    setSaveError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const res = await saveSettings(form);
      setData(res);
      if (res.settings) setForm(res.settings);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const short = running.bars_loaded < running.bars_needed;

  return (
    <main className="settings-page">
      <div className="settings-main">
        <form className="settings-form" onSubmit={submit} aria-labelledby="settings-h">
          <div className="settings-intro">
            <h2 id="settings-h">Data source</h2>
            <p>Where the screener gets 1-minute bars. Saving restarts the feed; the chart reloads after the first scan.</p>
          </div>

          {!data.editable && (
            <p className="notice">These settings are read-only because the server was started without <code>--serve</code>.</p>
          )}

          <fieldset className="choices" disabled={!data.editable || saving}>
            <legend>Source</legend>
            {data.sources.map((s) => (
              <label key={s.id} className={`choice${form.source === s.id ? " is-checked" : ""}${s.available ? "" : " is-disabled"}`}>
                <input type="radio" name="source" value={s.id} checked={form.source === s.id} disabled={!s.available}
                  onChange={() => set({ source: s.id, symbol: null, clock: null })} />
                <span className="choice-body">
                  <span className="choice-title">
                    {s.title}
                    {s.id === "yfinance" && <span className="badge">Default</span>}
                  </span>
                  <span className="choice-desc">{s.description}</span>
                  {!s.available && <span className="choice-warn">{s.unavailable_reason}</span>}
                </span>
              </label>
            ))}
          </fieldset>

          <fieldset className="fields" disabled={!data.editable || saving}>
            <legend>Connection</legend>
            <Field label="Symbol" hint={`Leave empty for ${source?.default_symbol ?? "the default"}.`}>
              <input type="text" value={form.symbol ?? ""} placeholder={source?.default_symbol} spellCheck={false}
                onChange={(e) => set({ symbol: orNull(e.target.value) })} />
            </Field>
            <Field label="Feed clock"
              hint={`The time zone of the bar times, for the quarterly view. Leave empty for ${source?.default_clock ?? "UTC"}. NY+7 is New York time plus 7 hours, the server time of most MT5 gold brokers.`}>
              <input type="text" list="clock-options" value={form.clock ?? ""} placeholder={source?.default_clock} spellCheck={false}
                onChange={(e) => set({ clock: orNull(e.target.value) })} />
              <datalist id="clock-options">
                <option value="UTC" />
                <option value="NY+7" />
                <option value="UTC+2" />
                <option value="UTC+3" />
                <option value="Europe/London" />
                <option value="Asia/Jakarta" />
              </datalist>
            </Field>

            {form.source === "csv" && (
              <Field label="CSV file" hint="Columns: time, open, high, low, close, and optionally volume.">
                <input type="text" value={form.csv_path ?? ""} placeholder="data/xauusd_m1.csv" spellCheck={false} required
                  onChange={(e) => set({ csv_path: orNull(e.target.value) })} />
              </Field>
            )}

            {form.source === "mt5" && (
              <>
                <p className="field-note">
                  If the terminal is already open and logged in, leave these empty. The password is read from{" "}
                  <code>MT5_PASSWORD</code> in <code>.env</code> and never stored ({data.mt5_password_set ? "currently set" : "currently not set"}).
                </p>
                <div className="field-row">
                  <Field label="Login">
                    <input type="text" inputMode="numeric" value={form.mt5_login ?? ""} placeholder="12345678"
                      onChange={(e) => {
                        const v = e.target.value.replace(/\D/g, "");
                        set({ mt5_login: v ? Number(v) : null });
                      }} />
                  </Field>
                  <Field label="Server">
                    <input type="text" value={form.mt5_server ?? ""} placeholder="Broker-Server" spellCheck={false}
                      onChange={(e) => set({ mt5_server: orNull(e.target.value) })} />
                  </Field>
                </div>
                <Field label="Terminal path" hint="Lets the screener start the terminal itself when it isn't running.">
                  <input type="text" value={form.mt5_path ?? ""} placeholder="C:\Program Files\MetaTrader 5\terminal64.exe" spellCheck={false}
                    onChange={(e) => set({ mt5_path: orNull(e.target.value) })} />
                </Field>
              </>
            )}
          </fieldset>

          {data.editable && (
            <div className="form-actions">
              <button type="submit" className="button primary" disabled={!dirty || saving}>
                {saving ? "Restarting feed…" : "Save and restart feed"}
              </button>
              <button type="button" className="button quiet" disabled={!dirty || saving} onClick={() => set(current)}>
                Discard changes
              </button>
              <span className="form-status" role="status">
                {saveError ? <span className="text-error">{saveError}</span> : saved && !dirty ? "Saved. The feed restarted." : dirty ? "Unsaved changes" : ""}
              </span>
            </div>
          )}
        </form>
        <AlertsSettings />
        <CalendarSettingsForm />
        <BriefSettingsForm />
        <AccountSettings />
      </div>

      <aside className="settings-side">
        <section aria-labelledby="running-h">
          <h3 id="running-h">Running now</h3>
          <dl className="facts">
            <div><dt>Source</dt><dd>{runningSource?.title ?? running.source}</dd></div>
            <div><dt>Symbol</dt><dd>{running.symbol}</dd></div>
            <div>
              <dt>Status</dt>
              <dd>
                {running.error ? (
                  <span className="text-error">{running.error}</span>
                ) : running.version > 0 ? (
                  `Scanning · last scan ${running.scanned_at ? new Date(running.scanned_at).toLocaleTimeString() : ""}`
                ) : (
                  "Connecting…"
                )}
              </dd>
            </div>
            <div>
              <dt>History</dt>
              <dd className="num">
                {running.bars_loaded.toLocaleString()} of {running.bars_needed.toLocaleString()} M1 bars
                {running.first_bar && running.last_bar && (
                  <span className="muted"> · {fmtFeedTime(running.first_bar)} to {fmtFeedTime(running.last_bar)}</span>
                )}
              </dd>
            </div>
          </dl>
          {short && running.version > 0 && (
            <p className="field-note">
              Higher timeframes see fewer candles until more history is stored. The screener saves every bar it
              fetches, so this fills up over time.
            </p>
          )}
        </section>

        <section aria-labelledby="storage-h">
          <h3 id="storage-h">Storage</h3>
          {data.storage ? (
            <>
              <dl className="facts">
                <div><dt>Database</dt><dd>{data.storage.backend === "sqlite" ? "SQLite" : data.storage.backend}</dd></div>
                <div><dt>Location</dt><dd><code className="wrap">{data.storage.url}</code></dd></div>
              </dl>
              {data.storage.series.length > 0 ? (
                <table className="data compact">
                  <thead>
                    <tr>
                      <th scope="col">Series</th>
                      <th scope="col" className="end">Bars</th>
                      <th scope="col" className="end">Latest</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.storage.series.map((s) => (
                      <tr key={`${s.source}-${s.symbol}`}>
                        <td>{s.source} · {s.symbol}</td>
                        <td className="end num">{s.bars.toLocaleString()}</td>
                        <td className="end num muted">{s.last ? fmtFeedTime(s.last) : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="empty">No bars stored yet. Demo data is never stored.</p>
              )}
              <p className="field-note">
                To use PostgreSQL, set <code>XAU_DB_URL=postgresql+psycopg://user:pass@host/db</code> in <code>.env</code>, run{" "}
                <code>uv sync --extra postgres</code> and restart.
              </p>
            </>
          ) : (
            <p className="empty">Storage is off (started with <code>--db none</code>).</p>
          )}
        </section>

        <PluginsSection />
      </aside>
    </main>
  );
}

/** Installed plugins: what loaded, what didn't and why. */
function PluginsSection() {
  const { data } = usePlugins();
  if (!data) return null;
  return (
    <section aria-labelledby="plugins-h">
      <h3 id="plugins-h">Plugins</h3>
      {data.plugins.length === 0 ? (
        <p className="empty">None installed. Plugins are Python packages that add features; see docs/plugins.md.</p>
      ) : (
        <table className="data compact">
          <thead>
            <tr>
              <th scope="col">Plugin</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.plugins.map((p) => (
              <tr key={p.name}>
                <td>{p.name}{p.version && <span className="muted num"> {p.version}</span>}</td>
                <td>
                  {p.loaded ? (p.features.length ? p.features.join(", ") : "Loaded") : <span className="text-error">{p.error}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
