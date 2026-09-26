import { useEffect, useState } from "react";

import {
  fetchAlerts, fetchTelegramChats, saveAlerts, testAlert, type AlertSettings, type AlertsResponse, type TelegramChat,
} from "../api";
import { fmtPrice } from "../format";
import { SecretField } from "./SecretField";

const TFS = ["4H", "1H", "30M", "15M", "5M"];
const PRIORITIES = [
  { id: "extreme", label: "Extreme", hint: "the candle the move started from" },
  { id: "middle", label: "Mid", hint: "continuation OBs inside the move" },
];

const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

/** Telegram alert settings: which order blocks alert, a test button, and the alert log. */
export function AlertsSettings() {
  const [data, setData] = useState<AlertsResponse | null>(null);
  const [form, setForm] = useState<AlertSettings | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | "chats" | null>(null);
  const [token, setToken] = useState("");
  const [forgetToken, setForgetToken] = useState(false);
  const [chats, setChats] = useState<TelegramChat[] | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    let first = true;
    const load = () =>
      fetchAlerts()
        .then((res) => {
          if (!alive) return;
          setData(res);
          if (first && res.settings) setForm(res.settings);
          first = false;
        })
        .catch(() => {
          /* the data source section already reports an unreachable API */
        });
    load();
    const id = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!data || !form) return null;
  const dirty = JSON.stringify(form) !== JSON.stringify(data.settings) || token !== "" || forgetToken;
  const set = (patch: Partial<AlertSettings>) => {
    setForm({ ...form, ...patch });
    setMessage(null);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("save");
    try {
      const res = await saveAlerts(form, {
        telegram_bot_token: token || undefined,
        forget_telegram_bot_token: forgetToken || undefined,
      });
      setData(res);
      if (res.settings) setForm(res.settings);
      setToken("");
      setForgetToken(false);
      setMessage({ kind: "ok", text: "Alert settings saved." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  };

  const sendTest = async () => {
    setBusy("test");
    try {
      await testAlert();
      setMessage({ kind: "ok", text: "Test message sent. Check Telegram." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  };

  const findChats = async () => {
    setBusy("chats");
    setMessage(null);
    try {
      const res = await fetchTelegramChats();
      setChats(res.chats);
      if (res.chats.length === 0) setMessage({ kind: "error", text: "No chats yet. Send your bot any message in Telegram, then try again." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(null);
    }
  };

  const locked = !data.editable || busy !== null;

  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="alerts-h">
      <div className="settings-intro">
        <h2 id="alerts-h">Telegram alerts</h2>
        <p>A message when price trades into an active order block, with the limit entry and stop. Each order block alerts once.</p>
      </div>

      <fieldset className="fields" disabled={locked}>
        <legend>Bot</legend>
        {!data.configured && (
          <ol className="steps">
            <li className={data.token_set ? "done" : undefined}>
              Create a bot with <code>@BotFather</code> in Telegram and paste its token below.
            </li>
            <li className={data.chat_id_set ? "done" : undefined}>
              Send your bot any message, save, then press <strong>Find my chat</strong> and pick it.
            </li>
          </ol>
        )}
        <SecretField label="Bot token" source={forgetToken ? null : data.token_source} value={token} forget={forgetToken}
          placeholder="123456789:AA…"
          onChange={(v) => { setToken(v); setForgetToken(false); setMessage(null); }}
          onForget={() => { setForgetToken(true); setToken(""); setMessage(null); }} />
        <div className="field">
          <label className="field-label" htmlFor="telegram-chat">Chat id</label>
          <div className="input-with-button">
            <input id="telegram-chat" type="text" value={form.chat_id ?? ""} placeholder="123456789" spellCheck={false}
              onChange={(e) => set({ chat_id: e.target.value.trim() || null })} />
            <button type="button" className="button secondary" disabled={!data.token_set || dirty || busy !== null}
              title={!data.token_set ? "Save the bot token first" : dirty ? "Save first" : undefined} onClick={findChats}>
              {busy === "chats" ? "Looking…" : "Find my chat"}
            </button>
          </div>
          {chats && chats.length > 0 && (
            <select aria-label="Chats that messaged the bot" value="" onChange={(e) => e.target.value && set({ chat_id: e.target.value })}>
              <option value="">Pick a chat that messaged the bot…</option>
              {chats.map((c) => <option key={c.id} value={String(c.id)}>{c.name} ({c.type}, {c.id})</option>)}
            </select>
          )}
          <span className="field-hint">Where alerts go: your own chat with the bot, a group or a channel.</span>
        </div>
      </fieldset>

      <fieldset className="fields" disabled={locked}>
        <legend className="sr-only">Alert switch</legend>
        <label className="switch">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          <span>Send alerts</span>
        </label>
      </fieldset>

      <fieldset className="fields" disabled={locked || !form.enabled}>
        <legend>Timeframes</legend>
        <div className="check-row">
          {TFS.map((tf) => (
            <label key={tf} className="check">
              <input type="checkbox" checked={form.timeframes.includes(tf)} onChange={() => set({ timeframes: toggle(form.timeframes, tf) })} />
              {tf}
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="fields" disabled={locked || !form.enabled}>
        <legend>Order blocks</legend>
        <div className="check-row">
          {PRIORITIES.map((p) => (
            <label key={p.id} className="check" title={p.hint}>
              <input type="checkbox" checked={form.priorities.includes(p.id)} onChange={() => set({ priorities: toggle(form.priorities, p.id) })} />
              {p.label} <span className="muted">· {p.hint}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="fields" disabled={locked || !form.enabled}>
        <legend>Bias</legend>
        <label className="check">
          <input type="checkbox" checked={form.neutral_alerts} onChange={(e) => set({ neutral_alerts: e.target.checked })} />
          Alert with a neutral bias too <span className="muted">· off: no alerts while you're not trading</span>
        </label>
        <p className="field-hint">Every alert says RISK ON or RISK OFF against the bias you set on the chart page.</p>
      </fieldset>

      {data.editable && (
        <div className="form-actions">
          <button type="submit" className="button secondary" disabled={!dirty || busy !== null}>
            {busy === "save" ? "Saving…" : "Save alert settings"}
          </button>
          <button type="button" className="button quiet" disabled={!data.configured || busy !== null} onClick={sendTest}>
            {busy === "test" ? "Sending…" : "Send test message"}
          </button>
          <span className="form-status" role="status">
            {message ? <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span> : dirty ? "Unsaved changes" : ""}
          </span>
        </div>
      )}

      {data.last_error && <p className="text-error field-note">Last alert failed: {data.last_error}</p>}

      <div>
        <h3 className="subhead">Recent alerts</h3>
        {data.recent.length === 0 ? (
          <p className="empty">None yet. Alerts appear here as they are sent.</p>
        ) : (
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">Order block</th>
                <th scope="col" className="end">Entry</th>
                <th scope="col" className="end">Price</th>
                <th scope="col" className="end">Sent</th>
              </tr>
            </thead>
            <tbody>
              {data.recent.map((a) => (
                <tr key={a.key}>
                  <td>
                    {a.timeframe} {a.kind === "bullish" ? "bull" : "bear"} OB <span className="muted">· {a.priority === "extreme" ? "extreme" : "mid"}</span>
                    {a.status === "failed" && <span className="text-error"> · not delivered</span>}
                  </td>
                  <td className="end num">{fmtPrice(a.entry)}</td>
                  <td className="end num">{fmtPrice(a.price)}</td>
                  <td className="end num muted">{new Date(a.sent_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </form>
  );
}
