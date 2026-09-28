import { useEffect, useState } from "react";

import { fetchLlmUsage, saveLlmBudget, saveLlmPrices, type LlmGroup, type LlmPrice, type LlmUsage } from "../api";

const usd = (v: number | null) => (v === null ? "no price" : v < 0.01 && v > 0 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
const tokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));

type PriceRow = { model: string } & Record<keyof LlmPrice, string>;
const toRows = (prices: Record<string, LlmPrice>): PriceRow[] =>
  Object.entries(prices).map(([model, p]) => ({
    model, input: String(p.input), output: String(p.output), cache_read: String(p.cache_read), cache_write: String(p.cache_write),
  }));

function GroupTable({ title, rows }: { title: string; rows: LlmGroup[] }) {
  return (
    <div className="table-scroll">
    <table className="data compact">
      <caption>{title}</caption>
      <thead>
        <tr>
          <th scope="col"><span className="sr-only">{title}</span></th>
          <th scope="col" className="end">Calls</th>
          <th scope="col" className="end">Input</th>
          <th scope="col" className="end">Cached</th>
          <th scope="col" className="end">Output</th>
          <th scope="col" className="end">Cost</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((g) => (
          <tr key={g.key}>
            <th scope="row">{g.key}</th>
            <td className="end num">{g.calls}</td>
            <td className="end num">{tokens(g.input_tokens)}</td>
            <td className="end num">{tokens(g.cache_read_tokens)}</td>
            <td className="end num">{tokens(g.output_tokens)}</td>
            <td className="end num">{g.unpriced === g.calls ? "no price" : usd(g.cost)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}

/** Cost per day over the last 30 days: one bar per day, one axis (dollars). */
function DailyBars({ daily }: { daily: [string, number][] }) {
  const max = Math.max(...daily.map(([, c]) => c), 0);
  const W = 600;
  const H = 120;
  const bar = W / daily.length;
  return (
    <figure className="llm-daily">
      <svg viewBox={`0 0 ${W} ${H + 16}`} role="img" aria-label="LLM cost per day, last 30 days" preserveAspectRatio="none">
        <line x1={0} x2={W} y1={H} y2={H} className="llm-axis" />
        {daily.map(([d, c], i) => {
          const h = max > 0 ? (c / max) * (H - 4) : 0;
          return (
            <rect key={d} x={i * bar + 1} y={H - h} width={bar - 2} height={h} className="llm-bar">
              <title>{d}: {usd(c)}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="muted num">
        {daily[0]?.[0]} → {daily[daily.length - 1]?.[0]} · highest day {usd(max)}
      </figcaption>
      <details>
        <summary>As a table</summary>
        <table className="data compact">
          <thead><tr><th scope="col">Day</th><th scope="col" className="end">Cost</th></tr></thead>
          <tbody>
            {daily.filter(([, c]) => c > 0).map(([d, c]) => (
              <tr key={d}><td className="num">{d}</td><td className="end num">{usd(c)}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

/** Settings > LLM usage: this month's spend, the budget that stops it, and the price table. */
export function LlmUsageSettings() {
  const [data, setData] = useState<LlmUsage | null>(null);
  const [budget, setBudget] = useState("");
  const [prices, setPrices] = useState<PriceRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const apply = (u: LlmUsage) => {
    setData(u);
    setBudget(u.budget.limit === null ? "" : String(u.budget.limit));
    setPrices(toRows(u.prices));
  };

  useEffect(() => {
    fetchLlmUsage().then(apply).catch(() => {
      /* the data source section already reports an unreachable API */
    });
  }, []);

  if (!data) return null;

  const run = async (job: () => Promise<LlmUsage>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      apply(await job());
      setMessage({ kind: "ok", text: done });
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const b = data.budget;
  const setPrice = (i: number, patch: Partial<PriceRow>) => setPrices(prices.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  const savePrices = () => run(() => saveLlmPrices(Object.fromEntries(prices.filter((p) => p.model.trim()).map((p) => [p.model.trim(), {
    input: Number(p.input), output: Number(p.output),
    cache_read: p.cache_read === "" ? undefined : Number(p.cache_read), cache_write: p.cache_write === "" ? undefined : Number(p.cache_write),
  }]))), "Prices saved.");

  return (
    <div className="settings-form">
      <div className="settings-intro">
        <h2>LLM usage</h2>
        <p>
          Every call to Claude or OpenAI (the news brief, and plugins' LLM features) with its tokens, as the provider
          reported them, and its cost from the price table below. Months are calendar months in UTC.
        </p>
      </div>

      <div>
        <h3 className="subhead">{data.month}</h3>
        <p className="llm-spent">
          <strong className="num">{usd(b.spent)}</strong>
          {b.limit !== null && <span className="muted num"> of {usd(b.limit)}</span>}
          <span className="muted"> · {b.calls} call{b.calls === 1 ? "" : "s"}</span>
        </p>
        {b.limit !== null && (
          <div className={`llm-meter is-${b.level}`} role="meter" aria-valuemin={0} aria-valuemax={b.limit} aria-valuenow={b.spent}
            aria-label="Spend against the monthly budget">
            <span style={{ width: `${Math.min(100, (b.share ?? 0) * 100)}%` }} />
          </div>
        )}
        {b.level === "warn" && <p className="notice">Past {Math.round(data.warn_at * 100)}% of the budget.</p>}
        {b.level === "over" && (
          <p className="notice text-error">Over the budget: scheduled LLM jobs are paused and a manual brief asks first.</p>
        )}
        {b.unpriced_calls > 0 && (
          <p className="field-hint">{b.unpriced_calls} call{b.unpriced_calls === 1 ? " has" : "s have"} no price, so the budget can't count {b.unpriced_calls === 1 ? "it" : "them"}: add the model below.</p>
        )}
      </div>

      <fieldset className="fields" disabled={busy}>
        <legend>Monthly budget</legend>
        <div className="input-with-button">
          <input type="number" min={0} step={1} value={budget} placeholder="No limit" aria-label="Monthly budget in US dollars"
            onChange={(e) => setBudget(e.target.value)} />
          <button type="button" className="button secondary"
            onClick={() => run(() => saveLlmBudget(budget === "" ? null : Number(budget)), "Budget saved.")}>
            Save budget
          </button>
        </div>
        <span className="field-hint">
          US dollars a month. A warning at {Math.round(data.warn_at * 100)}%; at 100% scheduled jobs stop and manual runs ask first.
        </span>
      </fieldset>

      {data.by_feature.length > 0 ? (
        <div className="llm-groups">
          <GroupTable title="By feature" rows={data.by_feature} />
          <GroupTable title="By model" rows={data.by_model} />
        </div>
      ) : (
        <p className="empty">No LLM calls this month.</p>
      )}

      <div>
        <h3 className="subhead">Last 30 days</h3>
        <DailyBars daily={data.daily} />
      </div>

      {data.top.length > 0 && (
        <div>
          <h3 className="subhead">Most expensive calls this month</h3>
          <div className="table-scroll">
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Feature</th>
                <th scope="col">Model</th>
                <th scope="col" className="end">Input</th>
                <th scope="col" className="end">Output</th>
                <th scope="col" className="end">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.top.map((c) => (
                <tr key={c.id}>
                  <td className="num">{new Date(c.created_at).toLocaleString()}</td>
                  <td>{c.feature}{c.scheduled && <span className="muted"> · scheduled</span>}</td>
                  <td>{c.model}</td>
                  <td className="end num">{tokens(c.input_tokens + c.cache_read_tokens)}{c.estimated && <span className="muted" title="The reply had no token counts"> est.</span>}</td>
                  <td className="end num">{tokens(c.output_tokens)}</td>
                  <td className="end num">{usd(c.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}

      <fieldset className="fields llm-price-fields" disabled={busy}>
        <legend>Prices (US dollars per million tokens)</legend>
        <p className="field-hint">Check them against your provider's pricing page. A dated model name uses the price of the name it starts with.</p>
        <div className="table-scroll">
          <table className="data compact llm-prices">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col" className="end">Input</th>
                <th scope="col" className="end">Output</th>
                <th scope="col" className="end">Cache read</th>
                <th scope="col" className="end">Cache write</th>
                <th scope="col"><span className="sr-only">Remove</span></th>
              </tr>
            </thead>
            <tbody>
              {prices.map((p, i) => (
                <tr key={i}>
                  <td><input type="text" value={p.model} aria-label="Model" spellCheck={false} onChange={(e) => setPrice(i, { model: e.target.value })} /></td>
                  {(["input", "output", "cache_read", "cache_write"] as const).map((k) => (
                    <td key={k} className="end">
                      <input type="number" min={0} step="any" value={p[k]} aria-label={`${p.model} ${k.replace("_", " ")}`}
                        onChange={(e) => setPrice(i, { [k]: e.target.value })} />
                    </td>
                  ))}
                  <td className="end">
                    <button type="button" className="button quiet" onClick={() => setPrices(prices.filter((_, j) => j !== i))}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="form-actions">
          <button type="button" className="button quiet"
            onClick={() => setPrices([...prices, { model: "", input: "", output: "", cache_read: "", cache_write: "" }])}>
            Add model
          </button>
          <button type="button" className="button secondary" onClick={savePrices}>Save prices</button>
        </div>
      </fieldset>

      {message && <p className={message.kind === "error" ? "text-error field-note" : "field-note"} role="status">{message.text}</p>}
    </div>
  );
}
