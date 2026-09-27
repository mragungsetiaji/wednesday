import { useEffect, useState } from "react";

import { fetchRisk, saveRisk, type RiskResponse, type RiskSettings } from "../api";
import { fmtMoney } from "../format";

const num = (v: string) => (v.trim() === "" ? null : Number(v));

/** Settings > Risk: the lot size shown on every setup and in alerts. */
export function RiskSettingsForm() {
  const [data, setData] = useState<RiskResponse | null>(null);
  const [form, setForm] = useState<RiskSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchRisk()
        .then((res) => {
          if (!alive) return;
          setData(res);
          setForm((f) => f ?? res.settings);
        })
        .catch(() => {});
    load();
    const id = setInterval(load, 20000); // the MT5 balance moves
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!data) return null;
  if (!data.editable || !form || !data.settings) {
    return (
      <section className="settings-form" aria-labelledby="risk-h">
        <div className="settings-intro">
          <h2 id="risk-h">Risk</h2>
          <p>Available when the screener runs with <code>--serve</code>.</p>
        </div>
      </section>
    );
  }

  const mt5 = form.use_mt5 ? data.mt5 : null;
  const dirty = JSON.stringify(form) !== JSON.stringify(data.settings);
  const set = (patch: Partial<RiskSettings>) => {
    setForm({ ...form, ...patch });
    setMessage(null);
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await saveRisk(form);
      setData(res);
      if (res.settings) setForm(res.settings);
      setMessage({ kind: "ok", text: "Risk settings saved. Sizes show from the next scan." });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const sizer = data.sizer;
  const summary = !data.settings.enabled
    ? "Off: setups show no lot size."
    : sizer
      ? `Risking ${fmtMoney(sizer.budget, sizer.currency)} per trade${sizer.balance ? ` of a ${fmtMoney(sizer.balance, sizer.currency)} balance` : ""}` +
        ` (${sizer.source === "mt5" ? "balance and lot rules from MT5" : "values below"}).`
      : "Needs a balance: enter one below, or connect MT5.";

  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="risk-h">
      <div className="settings-intro">
        <h2 id="risk-h">Risk</h2>
        <p>
          Shows the lot size for your risk on every setup and in Telegram alerts, from the setup's entry and stop. Lots round
          down to the broker's step, so the money at risk never goes over your budget.
        </p>
      </div>

      <fieldset className="fields" disabled={busy}>
        <legend className="sr-only">Position size switch</legend>
        <label className="switch">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          <span>Show position size</span>
        </label>
        <p className="field-note" role="status">{summary}</p>
      </fieldset>

      <fieldset className="fields-grid" disabled={busy || !form.enabled}>
        <legend>Risk per trade</legend>
        <label className="field">
          <span className="field-label">Type</span>
          <select value={form.mode} onChange={(e) => set({ mode: e.target.value as RiskSettings["mode"] })}>
            <option value="percent">% of balance</option>
            <option value="amount">Fixed amount</option>
          </select>
        </label>
        <label className="field">
          <span className="field-label">{form.mode === "percent" ? "Percent" : `Amount (${mt5?.currency ?? form.currency})`}</span>
          <input type="number" min={0} step="any" required value={form.value} onChange={(e) => set({ value: Number(e.target.value) })} />
        </label>
        <label className="field field-span">
          <span className="field-label">RISK OFF size</span>
          <input type="number" min={0} max={1} step={0.05} value={form.risk_off_multiplier}
            onChange={(e) => set({ risk_off_multiplier: Number(e.target.value) })} />
          <span className="field-hint">
            Share of the risk for setups against your bias, e.g. 0.5 for half size. 1 sizes them like the rest.
          </span>
        </label>
      </fieldset>

      <fieldset className="fields" disabled={busy || !form.enabled}>
        <legend>Account and symbol</legend>
        <label className="check">
          <input type="checkbox" checked={form.use_mt5} onChange={(e) => set({ use_mt5: e.target.checked })} />
          Read the balance and lot rules from MT5 when it is the data source
        </label>
        {form.use_mt5 && (
          data.mt5 ? (
            <dl className="facts">
              <dt>Balance</dt>
              <dd className="num">{fmtMoney(data.mt5.balance, data.mt5.currency)}</dd>
              <dt>1 lot per 1.00 move</dt>
              <dd className="num">{fmtMoney(data.mt5.per_point, data.mt5.currency)}</dd>
              <dt>Lots</dt>
              <dd className="num">min {data.mt5.min_lot}, step {data.mt5.lot_step}{data.mt5.max_lot ? `, max ${data.mt5.max_lot}` : ""}</dd>
            </dl>
          ) : (
            <p className="field-note">MT5 isn't connected, so the values below are used.</p>
          )
        )}
      </fieldset>

      <fieldset className="fields-grid" disabled={busy || !form.enabled || !!mt5}>
        <legend>{mt5 ? "Used when MT5 isn't connected" : "Without MT5"}</legend>
        <label className="field">
          <span className="field-label">Balance</span>
          <input type="number" min={0} step="any" value={form.balance ?? ""} placeholder="e.g. 10000"
            onChange={(e) => set({ balance: num(e.target.value) })} />
        </label>
        <label className="field">
          <span className="field-label">Currency</span>
          <input type="text" value={form.currency} maxLength={8} spellCheck={false}
            onChange={(e) => set({ currency: e.target.value.toUpperCase() })} />
        </label>
        <label className="field field-span">
          <span className="field-label">Contract size</span>
          <input type="number" min={0} step="any" value={form.contract_size} onChange={(e) => set({ contract_size: Number(e.target.value) })} />
          <span className="field-hint">
            Money one lot makes per 1.00 price move. XAUUSD is usually 100 (100 oz) on a USD account; check your broker's
            contract specification.
          </span>
        </label>
        <label className="field">
          <span className="field-label">Minimum lot</span>
          <input type="number" min={0} step="any" value={form.min_lot} onChange={(e) => set({ min_lot: Number(e.target.value) })} />
        </label>
        <label className="field">
          <span className="field-label">Lot step</span>
          <input type="number" min={0} step="any" value={form.lot_step} onChange={(e) => set({ lot_step: Number(e.target.value) })} />
        </label>
      </fieldset>

      <div className="form-actions">
        <button type="submit" className="button secondary" disabled={!dirty || busy}>
          {busy ? "Saving…" : "Save risk settings"}
        </button>
        <span className="form-status" role="status">
          {message ? <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span> : dirty ? "Unsaved changes" : ""}
        </span>
      </div>

      <p className="field-note">
        Sizes are arithmetic from your inputs, not advice. Check the lot and the money at risk in your broker's platform before
        every order.
      </p>
    </form>
  );
}
