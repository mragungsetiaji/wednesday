import { useEffect, useState } from "react";

import { fetchDistribution, type DistLookback, type DistMeasure, type DistPeriod, type Distribution } from "../api";
import { fmtMove, percentileSentence, samplesNoun } from "../moveText";
import { usePref } from "../prefs";

const PERIODS: { id: DistPeriod; label: string }[] = [
  { id: "day", label: "Day" },
  { id: "week", label: "Week" },
  { id: "session", label: "Session" },
  { id: "q90", label: "90 min" },
];
const MEASURES: { id: DistMeasure; label: string }[] = [
  { id: "change", label: "Change %" },
  { id: "range_pct", label: "Range %" },
  { id: "range_atr", label: "Range in ATR" },
];
const LOOKBACKS: { id: DistLookback; label: string }[] = [
  { id: "1y", label: "1 year" },
  { id: "5y", label: "5 years" },
  { id: "all", label: "All" },
];

const W = 640;
const H = 170;
const PAD = { top: 26, bottom: 22, side: 8 };

/** Normal density at x, for the optional reference curve. */
const normal = (x: number, mean: number, std: number) => Math.exp(-0.5 * ((x - mean) / std) ** 2) / (std * Math.sqrt(2 * Math.PI));

function Histogram({ d, showNormal }: { d: Distribution; showNormal: boolean }) {
  const { edges, counts } = d.histogram;
  const lo = edges[0];
  const hi = edges[edges.length - 1];
  const max = Math.max(...counts, 1);
  const x = (v: number) => PAD.side + ((v - lo) / (hi - lo)) * (W - 2 * PAD.side);
  const y = (c: number) => H - PAD.bottom - (c / max) * (H - PAD.top - PAD.bottom);
  const cur = d.current?.value ?? null;
  const width = edges.length > 1 ? edges[1] - edges[0] : 1;
  let curve = "";
  if (showNormal && d.stats && d.stats.std > 0) {
    const pts = Array.from({ length: 121 }, (_, i) => lo + ((hi - lo) * i) / 120);
    curve = pts.map((v, i) => `${i ? "L" : "M"}${x(v).toFixed(1)},${y(normal(v, d.stats!.mean, d.stats!.std) * d.samples * width).toFixed(1)}`).join("");
  }
  const zeroIn = d.measure === "change" && lo < 0 && hi > 0;
  const curX = cur !== null ? x(cur) : null;
  const labelLeft = curX !== null && curX > W * 0.7;
  return (
    <svg className="dist-chart" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Histogram of ${samplesNoun(d)}${cur !== null ? `, the current one at ${fmtMove(cur, d.measure)}` : ""}`}>
      <line x1={PAD.side} x2={W - PAD.side} y1={H - PAD.bottom} y2={H - PAD.bottom} className="dist-axis" />
      {counts.map((c, i) => {
        const x0 = x(edges[i]);
        const x1 = x(edges[i + 1]);
        return (
          <rect key={i} x={x0 + 0.5} y={y(c)} width={Math.max(0.5, x1 - x0 - 1)} height={H - PAD.bottom - y(c)} className="dist-bar">
            <title>{`${fmtMove(edges[i], d.measure)} to ${fmtMove(edges[i + 1], d.measure)}: ${c}`}</title>
          </rect>
        );
      })}
      {curve && <path d={curve} className="dist-normal" />}
      {zeroIn && <line x1={x(0)} x2={x(0)} y1={PAD.top - 6} y2={H - PAD.bottom} className="dist-zero" />}
      <text x={PAD.side} y={H - 6} className="dist-tick">{fmtMove(lo, d.measure)}</text>
      <text x={W - PAD.side} y={H - 6} className="dist-tick" textAnchor="end">{fmtMove(hi, d.measure)}</text>
      {zeroIn && <text x={x(0)} y={H - 6} className="dist-tick" textAnchor="middle">0</text>}
      {curX !== null && cur !== null && (
        <g className="dist-marker">
          <line x1={curX} x2={curX} y1={PAD.top - 8} y2={H - PAD.bottom} />
          <text x={curX + (labelLeft ? -5 : 5)} y={PAD.top - 12} textAnchor={labelLeft ? "end" : "start"}>
            {`${d.current!.forming ? "Now (so far)" : "Latest"} ${fmtMove(cur, d.measure)}`}
          </text>
        </g>
      )}
    </svg>
  );
}

/**
 * How unusual the current move is: past closed periods as a histogram with the current one
 * marked, its percentile first and the z-score second. Context for the trader, not a signal.
 */
export function DistributionPanel({ version }: { version: number }) {
  const [period, setPeriod] = usePref<DistPeriod>("wed.distPeriod", "day");
  const [measure, setMeasure] = usePref<DistMeasure>("wed.distMeasure", "change");
  const [lookback, setLookback] = usePref<DistLookback>("wed.distLookback", "1y");
  const [weekday, setWeekday] = usePref("wed.distWeekday", false);
  const [session, setSession] = usePref("wed.distSession", false);
  const [showNormal, setShowNormal] = usePref("wed.distNormal", false);
  const [data, setData] = useState<Distribution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchDistribution({ period, measure, lookback, weekday, session })
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(null);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [period, measure, lookback, weekday, session, version]);

  const sentence = data ? percentileSentence(data) : null;
  const few = data && data.samples < data.min_samples;
  return (
    <section className="detail dist" aria-labelledby="dist-h">
      <div className="detail-head">
        <h2 id="dist-h">Distribution</h2>
        <span className="detail-note">How unusual the current move is, against past closed periods in New York time. Context, not a signal.</span>
      </div>

      <div className="dist-controls">
        <Segmented label="Period" value={period} options={PERIODS} onChange={setPeriod} />
        <Segmented label="Measure" value={measure} options={MEASURES} onChange={setMeasure} />
        <Segmented label="Lookback" value={lookback} options={LOOKBACKS} onChange={setLookback} />
        <label className="toggle">
          <input type="checkbox" checked={weekday} disabled={period === "week"} onChange={(e) => setWeekday(e.target.checked)} />
          Same weekday
        </label>
        <label className="toggle">
          <input type="checkbox" checked={session} disabled={period !== "session" && period !== "q90"} onChange={(e) => setSession(e.target.checked)} />
          Same session
        </label>
        <label className="toggle" title="A normal curve with the same mean and σ, for reference: gold's tails are fatter">
          <input type="checkbox" checked={showNormal} onChange={(e) => setShowNormal(e.target.checked)} />
          Normal curve
        </label>
      </div>

      {error && <p className="text-error" role="alert">{error}</p>}
      {data && (
        <>
          {sentence ? (
            <p className="dist-headline">{sentence}</p>
          ) : (
            data.current?.value != null && (
              <p className="dist-headline">
                {fmtMove(data.current.value, data.measure)}{data.current.forming ? " so far" : ""}
                {few && <span className="muted"> · only {samplesNoun(data)} to compare with; the percentile shows from {data.min_samples}.</span>}
              </p>
            )
          )}
          {data.stats && (
            <dl className="dist-stats num">
              {data.z !== null && <div><dt>z-score</dt><dd>{data.z > 0 ? "+" : data.z < 0 ? "−" : ""}{Math.abs(data.z).toFixed(1)}σ</dd></div>}
              <div><dt>Mean</dt><dd>{fmtMove(data.stats.mean, data.measure)}</dd></div>
              <div><dt>Median</dt><dd>{fmtMove(data.stats.median, data.measure)}</dd></div>
              <div><dt>σ</dt><dd>{fmtMove(data.stats.std, data.measure).replace(/^[+−]/, "")}</dd></div>
              <div><dt>Samples</dt><dd>{samplesNoun(data)}</dd></div>
              {data.coverage?.from && <div><dt>From</dt><dd>{data.coverage.from}</dd></div>}
            </dl>
          )}
          {data.histogram.counts.length > 0 && <Histogram d={data} showNormal={showNormal} />}
          {data.z !== null && (
            <p className="field-hint">
              The percentile counts past periods directly. The z-score read through a normal curve understates how often
              big moves happen, since gold's tails are fatter than normal.
            </p>
          )}
          {data.histogram.counts.length > 0 && (
            <details>
              <summary>As a table</summary>
              <div className="table-scroll">
                <table className="data compact">
                  <thead><tr><th scope="col">From</th><th scope="col">To</th><th scope="col" className="end">Count</th></tr></thead>
                  <tbody>
                    {data.histogram.counts.map((c, i) => (
                      <tr key={i}>
                        <td className="num">{fmtMove(data.histogram.edges[i], data.measure)}</td>
                        <td className="num">{fmtMove(data.histogram.edges[i + 1], data.measure)}</td>
                        <td className="end num">{c}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
          {data.like_this.length > 0 && (
            <details>
              <summary>Periods like this ({data.like_this.length})</summary>
              <p className="field-hint">Past periods that moved at least as far, newest first, and what the next one did. Facts, not a forecast.</p>
              <div className="table-scroll">
                <table className="data compact">
                  <thead>
                    <tr><th scope="col">Day</th><th scope="col">Period</th><th scope="col" className="end">Move</th><th scope="col" className="end">Next</th></tr>
                  </thead>
                  <tbody>
                    {data.like_this.map((r) => (
                      <tr key={`${r.day}|${r.label}`}>
                        <td className="num">{r.day}</td>
                        <td>{r.label}</td>
                        <td className="end num">{fmtMove(r.value, data.measure)}</td>
                        <td className="end num">{r.next === null ? "…" : fmtMove(r.next, data.measure)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}
    </section>
  );
}

function Segmented<T extends string>({ label, value, options, onChange }: {
  label: string;
  value: T;
  options: { id: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented dist-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" className="seg seg-text" aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
