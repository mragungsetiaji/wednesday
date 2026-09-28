import { BaselineSeries, ColorType, CrosshairMode, createChart, type IChartApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import {
  backtestUrl, runBacktest,
  type BacktestFilter, type BacktestParams, type BacktestResult, type BacktestSummary, type LabStatus,
} from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { usePref } from "../prefs";
import { withAlpha, type ChartPalette } from "../theme";

const FONT = getComputedStyle(document.documentElement).getPropertyValue("--font-ui").trim() || "system-ui, sans-serif";
const TIMEFRAMES = ["4H", "1H", "30M", "15M", "5M"];
const SPLIT_TITLES: Record<string, string> = {
  priority: "OB priority", swing: "Swing", session: "Session (New York)", weekday: "Weekday", direction: "Direction",
};
const KEY_TITLES: Record<string, string> = { extreme: "Extreme", middle: "Mid", none: "—", bullish: "Buy", bearish: "Sell" };
const TRADES_SHOWN = 200;

const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);
const r2 = (v: number | null) => (v === null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}R`);
const day = (unix: number | null) => (unix ? new Date(unix * 1000).toISOString().slice(0, 10) : "");
const toUnix = (d: string) => (d ? Math.floor(Date.parse(`${d}T00:00:00Z`) / 1000) : null);

/** Cumulative R at each trade's exit, one axis. */
function EquityChart({ points, palette }: { points: [number, number][]; palette: ChartPalette }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    const chart = createChart(ref.current!, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Magnet },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } },
      handleScroll: { vertTouchDrag: false },
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      // No logo on the chart: the TradingView notice and link are in the status bar instead.
      layout: { background: { type: ColorType.Solid, color: palette.surface }, textColor: palette.text, fontFamily: FONT, fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderColor: palette.grid },
    });
    const series = chart.addSeries(BaselineSeries, {
      baseValue: { type: "price", price: 0 },
      topLineColor: palette.bull, topFillColor1: withAlpha(palette.bull, 0.2), topFillColor2: withAlpha(palette.bull, 0.02),
      bottomLineColor: palette.bear, bottomFillColor1: withAlpha(palette.bear, 0.02), bottomFillColor2: withAlpha(palette.bear, 0.2),
      lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
      priceFormat: { type: "custom", formatter: (v: number) => r2(v), minMove: 0.01 },
    });
    // The chart needs strictly increasing times: trades closing in the same minute keep the last total.
    const data: { time: UTCTimestamp; value: number }[] = [];
    for (const [t, v] of points) {
      if (data.length && data[data.length - 1].time >= t) data[data.length - 1].value = v;
      else data.push({ time: t as UTCTimestamp, value: v });
    }
    series.setData(data);
    chart.timeScale().fitContent();
    return () => chart.removeSeries(series);
  }, [points, palette]);

  return (
    <div className="backtest-chart">
      <div ref={ref} className="chart" role="img" aria-label="Cumulative result in R over time" />
    </div>
  );
}

function Stats({ s }: { s: BacktestSummary }) {
  return (
    <dl className="backtest-stats">
      <div><dt>Trades</dt><dd className="num">{s.trades}</dd></div>
      <div><dt>Fill rate</dt><dd className="num">{pct(s.fill_rate)}</dd></div>
      <div><dt>Win rate</dt><dd className="num">{pct(s.win_rate)}</dd></div>
      <div><dt>Avg R</dt><dd className="num">{r2(s.avg_r)}</dd></div>
      <div><dt>Total</dt><dd className="num">{r2(s.total_r)}</dd></div>
      <div><dt>Max drawdown</dt><dd className="num">{s.max_dd_r.toFixed(2)}R</dd></div>
    </dl>
  );
}

function SplitTable({ name, rows }: { name: string; rows: (BacktestSummary & { key: string })[] }) {
  return (
    <table className="data compact backtest-split">
      <caption>{SPLIT_TITLES[name] ?? name}</caption>
      <thead>
        <tr>
          <th scope="col"><span className="sr-only">{SPLIT_TITLES[name]}</span></th>
          <th scope="col" className="end">Trades</th>
          <th scope="col" className="end">Filled</th>
          <th scope="col" className="end">Win rate</th>
          <th scope="col" className="end">Avg R</th>
          <th scope="col" className="end">Total</th>
          <th scope="col" className="end">Max DD</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((g) => (
          <tr key={g.key}>
            <th scope="row">{KEY_TITLES[g.key] ?? g.key}</th>
            <td className="end num">{g.trades}</td>
            <td className="end num">{pct(g.fill_rate)}</td>
            <td className="end num">{pct(g.win_rate)}</td>
            <td className="end num">{r2(g.avg_r)}</td>
            <td className="end num">{r2(g.total_r)}</td>
            <td className="end num">{g.max_dd_r.toFixed(2)}R</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Lab > Backtest: every order block the detector finds, traded with the limit plan the Lab trains on,
 * split by priority, swing, session and weekday. A trade starts at the close of the break candle.
 */
export function LabBacktest({ status, palette }: { status: LabStatus; palette: ChartPalette }) {
  const hasModel = !!status.active && status.available;
  const [tf, setTf] = usePref("wed.btTf", "15M");
  const [rr, setRr] = usePref("wed.btRr", 2);
  const [horizon, setHorizon] = usePref("wed.btHorizon", 72);
  const [filter, setFilter] = usePref<BacktestFilter>("wed.btFilter", "all");
  const [minWin, setMinWin] = usePref("wed.btMinWin", 0.5);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const params: BacktestParams = { tf, from: toUnix(from), to: toUnix(to), rr, horizon, filter, min_win: minWin };
  const run = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await runBacktest(params));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const trades = result?.trades ?? [];
  const shown = [...trades].sort((a, b) => b.start_unix - a.start_unix).slice(0, TRADES_SHOWN);
  const h = status.history;

  return (
    <div className="lab-backtest">
      <form className="settings-form" onSubmit={run} aria-labelledby="bt-h">
        <div className="settings-intro">
          <h2 id="bt-h">Backtest</h2>
          <p>
            Every order block the detector finds on the stored history, traded with the Lab's limit plan: entry on the body,
            stop at the far side (capped at the max stop{result ? `, ${fmtPrice(result.params.max_sl)}` : ""}), target in R. A trade starts at the close of the
            candle that broke structure, never earlier. Past results don't predict future ones.
          </p>
        </div>
        <fieldset className="fields backtest-form" disabled={busy}>
          <legend className="sr-only">Backtest settings</legend>
          <label className="field">
            <span className="field-label">Timeframe</span>
            <select value={tf} onChange={(e) => setTf(e.target.value)}>
              {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
            </select>
          </label>
          <label className="field">
            <span className="field-label">From</span>
            <input type="date" value={from} min={h ? day(h.first_unix) : undefined} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">To</span>
            <input type="date" value={to} max={h ? day(h.last_unix) : undefined} onChange={(e) => setTo(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Target (R)</span>
            <input type="number" min={0.5} max={10} step={0.5} value={rr} onChange={(e) => setRr(Number(e.target.value))} />
          </label>
          <label className="field">
            <span className="field-label">Horizon (hours)</span>
            <input type="number" min={1} max={720} step={1} value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} />
          </label>
          <label className="field">
            <span className="field-label">Order blocks</span>
            <select value={filter} onChange={(e) => setFilter(e.target.value as BacktestFilter)}>
              <option value="all">All the detector finds</option>
              <option value="model" disabled={!hasModel}>Model's win chance at least…</option>
              <option value="labels">Only ones I labelled</option>
            </select>
          </label>
          {filter === "model" && (
            <label className="field">
              <span className="field-label">Win chance ≥</span>
              <input type="number" min={0} max={1} step={0.05} value={minWin} onChange={(e) => setMinWin(Number(e.target.value))} />
            </label>
          )}
        </fieldset>
        <div className="form-actions">
          <button type="submit" className="button primary" disabled={busy}>{busy ? "Running…" : "Run backtest"}</button>
          {result && (
            <a className="button quiet" href={backtestUrl(params, "csv")} download>Download trades (CSV)</a>
          )}
          <span className="form-status" role="status">{error && <span className="text-error">{error}</span>}</span>
        </div>
      </form>

      {result && (
        <section className="backtest-result" aria-label="Backtest result">
          <Stats s={result.summary} />
          {result.equity.length > 0 ? (
            <EquityChart points={result.equity} palette={palette} />
          ) : (
            <p className="empty">No finished trades to draw.</p>
          )}
          <div className="backtest-splits">
            {Object.entries(result.splits).map(([name, rows]) => <SplitTable key={name} name={name} rows={rows} />)}
          </div>
          <details className="backtest-trades">
            <summary>
              Trades{trades.length > TRADES_SHOWN ? ` (newest ${TRADES_SHOWN} of ${trades.length}; the CSV has all)` : ` (${trades.length})`}
            </summary>
            <div className="table-scroll">
              <table className="data compact">
                <thead>
                  <tr>
                    <th scope="col">Start</th>
                    <th scope="col">Side</th>
                    <th scope="col">OB</th>
                    <th scope="col">Session</th>
                    <th scope="col" className="end">Entry</th>
                    <th scope="col" className="end">Stop</th>
                    <th scope="col" className="end">Target</th>
                    {filter === "model" && <th scope="col" className="end">Win chance</th>}
                    <th scope="col">Result</th>
                    <th scope="col" className="end">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((t) => (
                    <tr key={t.id}>
                      <td className="num">{fmtUnix(t.start_unix)}</td>
                      <td>{t.direction === "bullish" ? "Buy" : "Sell"}</td>
                      <td>{KEY_TITLES[t.priority] ?? t.priority}{t.swing !== "none" && ` · ${t.swing}`}</td>
                      <td>{t.session} · {t.weekday}</td>
                      <td className="end num">{fmtPrice(t.entry)}</td>
                      <td className="end num">{fmtPrice(t.stop)}</td>
                      <td className="end num">{fmtPrice(t.target)}</td>
                      {filter === "model" && <td className="end num">{pct(t.win_prob)}</td>}
                      <td>{t.outcome === "win" || t.outcome === "loss" ? `${t.outcome} ${r2(t.r)}` : t.outcome}</td>
                      <td className="end num">{t.equity_r === undefined ? "" : r2(t.equity_r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      )}
    </div>
  );
}
