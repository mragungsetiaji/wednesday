import { useEffect, useState } from "react";

import {
  fetchMt5Terminals, fetchSettings, reconnectFeed, saveSettings, type DataSettings, type Mt5Terminal, type SettingsResponse,
} from "../api";
import { useDesktopApi } from "../desktop";
import { fmtFeedTime } from "../format";
import { usePlugins } from "../plugins";
import { AccessSettings } from "./AccessSettings";
import { AccountSettings } from "./AccountSettings";
import { RiskSettingsForm } from "./RiskSettingsForm";
import { AlertsSettings } from "./AlertsSettings";
import { BriefSettingsForm } from "./BriefSettingsForm";
import { CalendarSettingsForm } from "./CalendarSettingsForm";
import { JournalSettings } from "./JournalSettings";
import { LabSettings } from "./LabSettings";
import { LlmUsageSettings } from "./LlmUsageSettings";

const EMPTY: DataSettings = { source: "yfinance", symbol: null, csv_path: null, mt5_login: null, mt5_server: null, mt5_path: null, clock: null, tick_seconds: null };

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

const fmtTick = (s: number) => (s === 0 || s >= 60 ? "once a minute, with each scan" : `${s}s`);

type Section = "data" | "risk" | "alerts" | "calendar" | "brief" | "llm" | "lab" | "journal" | "access" | "plan" | "plugins";
const SECTIONS: { id: Section; title: string }[] = [
  { id: "data", title: "Data source" },
  { id: "risk", title: "Risk" },
  { id: "alerts", title: "Telegram alerts" },
  { id: "calendar", title: "News calendar" },
  { id: "brief", title: "News brief" },
  { id: "llm", title: "LLM usage" },
  { id: "lab", title: "Lab" },
  { id: "journal", title: "Journal" },
  { id: "access", title: "Access" },
  { id: "plan", title: "Plan" },
  { id: "plugins", title: "Plugins" },
];
const sectionFromHash = (): Section => {
  const s = window.location.hash.split("/")[1];
  return SECTIONS.find((x) => x.id === s)?.id ?? "data";
};
const sectionHash = (s: Section) => (s === "data" ? "#settings" : `#settings/${s}`);

/**
 * Settings, one section at a time with a menu on the left. Every section stays mounted
 * (only hidden), so unsaved edits survive switching between them.
 */
export function SettingsPage() {
  const [section, setSection] = useState<Section>(sectionFromHash);

  useEffect(() => {
    const onHash = () => setSection(sectionFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (s: Section) => {
    window.location.hash = sectionHash(s);
    setSection(s);
    window.scrollTo(0, 0);
  };

  return (
    <main className="settings-page">
      <nav className="settings-nav" aria-label="Settings">
        {SECTIONS.map((s) => (
          <a key={s.id} href={sectionHash(s.id)} className="settings-nav-item" aria-current={section === s.id ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); go(s.id); }}>
            {s.title}
          </a>
        ))}
      </nav>
      <div className="settings-content">
        <div hidden={section !== "data"}><DataSourceSettings /></div>
        <div hidden={section !== "risk"}><RiskSettingsForm /></div>
        <div hidden={section !== "alerts"}><AlertsSettings /></div>
        <div hidden={section !== "calendar"}><CalendarSettingsForm /></div>
        <div hidden={section !== "brief"}><BriefSettingsForm /></div>
        <div hidden={section !== "llm"}><LlmUsageSettings /></div>
        <div hidden={section !== "lab"}><LabSettings /></div>
        <div hidden={section !== "journal"}><JournalSettings /></div>
        <div hidden={section !== "access"}><AccessSettings /></div>
        <div hidden={section !== "plan"}><AccountSettings /></div>
        <div hidden={section !== "plugins"}><PluginsSection /></div>
      </div>
    </main>
  );
}

type Running = SettingsResponse["running"];

/** What the feed is doing, in words. Only "connected" with a scan counts as done. */
function connText(running: Running, sourceTitle: string): string {
  switch (running.conn) {
    case "connecting":
      return running.attempt > 0 ? `Connecting to ${sourceTitle}, trying again next minute…` : `Connecting to ${sourceTitle}…`;
    case "reconnecting":
      return running.max_attempts
        ? `Connection lost. Trying to reconnect (attempt ${running.attempt} of ${running.max_attempts})…`
        : "Connection lost. Trying again next minute…";
    case "failed":
      return "Not connected. The feed stopped trying.";
    default:
      return `Scanning · last scan ${running.scanned_at ? new Date(running.scanned_at).toLocaleTimeString() : ""}`;
  }
}

const PASSWORD_SOURCE: Record<string, string> = {
  saved: "Saved in Windows Credential Manager for this login.",
  session: "Kept until the app closes (no credential store on this PC).",
  env: "Read from MT5_PASSWORD in .env.",
};

/** Data source settings. Saving restarts the feed; the page keeps polling so the result shows up here. */
function DataSourceSettings() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<DataSettings>(EMPTY);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [password, setPassword] = useState("");
  const [forgetPassword, setForgetPassword] = useState(false);
  const [terminals, setTerminals] = useState<Mt5Terminal[]>([]);
  const [reconnecting, setReconnecting] = useState(false);
  const desktop = useDesktopApi();

  useEffect(() => {
    if (form.source !== "mt5") return;
    fetchMt5Terminals().then((r) => setTerminals(r.terminals)).catch(() => setTerminals([]));
  }, [form.source]);

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
      <p className="empty">{loadError ? `Can't load settings: ${loadError}` : "Loading settings…"}</p>
    );
  }

  const current = data.settings ?? EMPTY;
  const dirty = !same(form, current) || password !== "" || forgetPassword;
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
      const res = await saveSettings(form, {
        mt5_password: password || undefined,
        forget_mt5_password: forgetPassword || undefined,
      });
      setData(res);
      if (res.settings) setForm(res.settings);
      setPassword("");
      setForgetPassword(false);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const reconnect = async () => {
    setReconnecting(true);
    try {
      setData(await reconnectFeed());
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setReconnecting(false);
    }
  };

  const browse = async () => {
    const picked = await desktop?.pick_terminal?.();
    if (picked) set({ mt5_path: picked });
  };

  const short = running.bars_loaded < running.bars_needed;
  const runningTitle = runningSource?.title ?? running.source;
  const knownTerminal = terminals.some((t) => t.path === form.mt5_path);

  return (
    <div className="settings-data">
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
                  onChange={() => set({ source: s.id, symbol: null, clock: null, tick_seconds: null })} />
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
            {source && source.min_tick > 0 && (
              <Field label="Live price every (seconds)"
                hint={`Between the minute scans, the price and the forming candle update this often. Leave empty for ${fmtTick(source.default_tick)}; 0 turns it off. ${source.min_tick}s at the fastest${form.source === "yfinance" ? ", as each update is a request to Yahoo" : ""}.`}>
                <input type="number" inputMode="decimal" min={0} max={60} step={0.5} value={form.tick_seconds ?? ""}
                  placeholder={String(source.default_tick)}
                  onChange={(e) => set({ tick_seconds: e.target.value === "" ? null : Number(e.target.value) })} />
              </Field>
            )}

            {form.source === "csv" && (
              <Field label="CSV file" hint="Columns: time, open, high, low, close, and optionally volume.">
                <input type="text" value={form.csv_path ?? ""} placeholder="data/xauusd_m1.csv" spellCheck={false} required
                  onChange={(e) => set({ csv_path: orNull(e.target.value) })} />
              </Field>
            )}

            {form.source === "mt5" && (
              <>
                <div className="field">
                  <label className="field-label" htmlFor="mt5-terminal">Terminal</label>
                  {terminals.length > 0 && (
                    <select aria-label="Installed MT5 terminals" value={knownTerminal ? form.mt5_path ?? "" : ""}
                      onChange={(e) => set({ mt5_path: e.target.value || null })}>
                      <option value="">{knownTerminal ? "Any open terminal" : "Choose an installed terminal…"}</option>
                      {terminals.map((t) => (
                        <option key={t.path} value={t.path}>{t.name}{t.running ? " (open)" : ""}</option>
                      ))}
                    </select>
                  )}
                  <div className="input-with-button">
                    <input id="mt5-terminal" type="text" value={form.mt5_path ?? ""} spellCheck={false}
                      placeholder="C:\Program Files\MetaTrader 5\terminal64.exe"
                      onChange={(e) => set({ mt5_path: orNull(e.target.value) })} />
                    {desktop?.pick_terminal && (
                      <button type="button" className="button secondary" onClick={browse}>Browse…</button>
                    )}
                  </div>
                  <span className="field-hint">
                    With several MT5 terminals (MetaQuotes and your brokers'), pick the one logged in to this account. It is
                    started once when the feed starts; if it can't connect, the feed stops and waits for Reconnect instead of
                    opening it again. Empty = the terminal that is open.
                  </span>
                </div>
                <div className="field-row">
                  <Field label="Login">
                    <input type="text" inputMode="numeric" autoComplete="off" value={form.mt5_login ?? ""} placeholder="12345678"
                      onChange={(e) => {
                        const v = e.target.value.replace(/\D/g, "");
                        set({ mt5_login: v ? Number(v) : null });
                      }} />
                  </Field>
                  <Field label="Server">
                    <input type="text" autoComplete="off" value={form.mt5_server ?? ""} placeholder="Broker-Server" spellCheck={false}
                      onChange={(e) => set({ mt5_server: orNull(e.target.value) })} />
                  </Field>
                </div>
                <Field label="Password"
                  hint={forgetPassword ? "The saved password will be removed when you save."
                    : data.mt5_password ? PASSWORD_SOURCE[data.mt5_password] : "Kept in Windows Credential Manager, never in the database."}>
                  <input type="password" autoComplete="new-password" value={password}
                    placeholder={data.mt5_password && !forgetPassword ? "•••••••• (leave empty to keep)" : "MT5 password"}
                    onChange={(e) => { setPassword(e.target.value); setForgetPassword(false); setSaved(false); setSaveError(null); }} />
                </Field>
                {data.mt5_password && data.mt5_password !== "env" && !forgetPassword && (
                  <p className="field-note">
                    <button type="button" className="link-button" onClick={() => { setForgetPassword(true); setPassword(""); setSaved(false); }}>
                      Forget the saved password
                    </button>
                  </p>
                )}
                <p className="field-note">
                  If the terminal is already open and logged in, login, server and password can stay empty.
                </p>
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
                {saveError ? (
                  <span className="text-error">{saveError}</span>
                ) : saved && !dirty ? (
                  running.conn === "connected" ? "Saved. Connected." : running.conn === "failed" ? (
                    <span className="text-error">Saved, but not connected: {running.error}</span>
                  ) : `Saved. ${connText(running, runningTitle)}`
                ) : dirty ? "Unsaved changes" : ""}
              </span>
            </div>
          )}
        </form>
      </div>

      <aside className="settings-side">
        <section aria-labelledby="running-h">
          <h3 id="running-h">Running now</h3>
          <dl className="facts">
            <div><dt>Source</dt><dd>{runningTitle}</dd></div>
            <div><dt>Symbol</dt><dd>{running.symbol}</dd></div>
            <div>
              <dt>Status</dt>
              <dd>
                {running.conn === "connected" ? connText(running, runningTitle) : (
                  <>
                    <span className={running.conn === "failed" ? "text-error" : undefined}>{connText(running, runningTitle)}</span>
                    {running.error && <span className="text-error"> {running.error}</span>}
                  </>
                )}
                {data.editable && running.conn === "failed" && (
                  <div className="status-actions">
                    <button type="button" className="button secondary" disabled={reconnecting} onClick={reconnect}>
                      {reconnecting ? "Reconnecting…" : "Reconnect"}
                    </button>
                  </div>
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
            </>
          ) : (
            <p className="empty">Storage is off (started with <code>--db none</code>).</p>
          )}
        </section>
      </aside>
    </div>
  );
}

/** Installed plugins: what loaded, what didn't and why. */
function PluginsSection() {
  const { data } = usePlugins();
  if (!data) return null;
  return (
    <section className="settings-form" aria-labelledby="plugins-h">
      <div className="settings-intro">
        <h2 id="plugins-h">Plugins</h2>
        <p>Python packages that add features. What loaded, what didn't and why.</p>
      </div>
      {data.plugins.length === 0 ? (
        <p className="empty">None installed. See docs/plugins.md to write one.</p>
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
