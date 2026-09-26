import { useEffect, useMemo, useRef, useState } from "react";

import type { JournalStats } from "../api";
import { fmtPrice } from "../format";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MAX_BARS = 24;

const pct = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}%`;
const money = (v: number) => `${v >= 0 ? "+" : "−"}${fmtPrice(Math.abs(v))}`;
const short = (v: number) => {
  const a = Math.abs(v);
  const s = a >= 10_000 ? `${(a / 1000).toFixed(0)}k` : a >= 1000 ? `${(a / 1000).toFixed(1)}k` : a.toFixed(0);
  return `${v >= 0 ? "+" : "−"}${s}`;
};
const pips = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}`;
const monthName = (ym: string) => `${MONTHS[Number(ym.slice(5, 7)) - 1]} ${ym.slice(0, 4)}`;
const tone = (v: number) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Gain per month as bars from zero: green up, red down. Hover a month for money and pips. */
export function MonthlyBars({ stats, currency }: { stats: JournalStats; currency: string }) {
  const rows = useMemo(() => stats.monthly.filter((m) => m.gain !== null).slice(-MAX_BARS), [stats.monthly]);
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hot, setHot] = useState<number | null>(null);
  const H = 190;
  const top = 22;
  const values = rows.map((m) => m.gain as number);
  const bottom = Math.min(0, ...values) < 0 ? 40 : 24; // room for a label under a red bar, above the months
  const left = 8;
  const right = 52;
  const hi = Math.max(0, ...values);
  const lo = Math.min(0, ...values);
  const span = hi - lo || 1;
  const y = (v: number) => top + ((hi - v) / span) * (H - top - bottom);
  const plotW = Math.max(width - left - right, 1);
  const step = rows.length ? plotW / rows.length : plotW;
  const barW = Math.max(Math.min(step * 0.62, 44), 3);
  const labels = rows.length <= 12 && barW >= 22;
  const ticks = [hi, 0, lo].filter((v, i, a) => a.indexOf(v) === i);

  const bar = (i: number, v: number) => {
    const x = left + i * step + (step - barW) / 2;
    const y0 = y(0);
    const y1 = y(v);
    const h = Math.abs(y1 - y0);
    const r = Math.min(4, h, barW / 2);
    // rounded on the data end only, square on the baseline
    return v >= 0
      ? `M${x},${y0} V${y1 + r} Q${x},${y1} ${x + r},${y1} H${x + barW - r} Q${x + barW},${y1} ${x + barW},${y1 + r} V${y0} Z`
      : `M${x},${y0} V${y1 - r} Q${x},${y1} ${x + r},${y1} H${x + barW - r} Q${x + barW},${y1} ${x + barW},${y1 - r} V${y0} Z`;
  };

  const h = hot === null ? null : rows[hot];
  return (
    <section className="journal-monthly" aria-labelledby="monthly-h">
      <div className="journal-section-head">
        <h3 id="monthly-h">Monthly gain</h3>
        {h ? (
          <p className="meta num">
            <span>{monthName(h.month)}</span>
            <span className={tone(h.gain ?? 0)}>{pct(h.gain ?? 0)}</span>
            <span>{money(h.profit)} {currency}</span>
            <span>{pips(h.pips)} pips</span>
            <span>{h.trades} trades</span>
          </p>
        ) : (
          <p className="meta">Time-weighted, like the total. Hover a month for money and pips.</p>
        )}
      </div>
      <div ref={ref} className="journal-monthly-plot" onMouseLeave={() => setHot(null)}>
        {rows.length === 0 ? (
          <p className="empty">No closed month yet.</p>
        ) : width > 0 && (
          <svg width={width} height={H} role="img" aria-label="Gain per month">
            {ticks.map((v) => (
              <g key={v}>
                <line x1={left} x2={width - right} y1={y(v)} y2={y(v)} className={v === 0 ? "axis-zero" : "axis-grid"} />
                <text x={width - right + 6} y={y(v) + 4} className="axis-label">{v === 0 ? "0%" : `${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%`}</text>
              </g>
            ))}
            {rows.map((m, i) => {
              const v = m.gain as number;
              const x = left + i * step;
              const showMonth = rows.length <= 12 || i % Math.ceil(rows.length / 8) === 0;
              return (
                <g key={m.month} onMouseEnter={() => setHot(i)} onFocus={() => setHot(i)} tabIndex={0}
                  aria-label={`${monthName(m.month)}: ${pct(v)}, ${money(m.profit)} ${currency}, ${pips(m.pips)} pips`}>
                  <rect x={x} y={top - 4} width={step} height={H - top - bottom + 8} className="hit" />
                  {v !== 0 && <path d={bar(i, v)} className={`bar ${v > 0 ? "up" : "down"}${hot === i ? " is-hot" : ""}`} />}
                  {labels && (
                    <text x={x + step / 2} y={v >= 0 ? y(v) - 6 : y(v) + 13} textAnchor="middle" className="bar-label">
                      {v.toFixed(1)}%
                    </text>
                  )}
                  {showMonth && (
                    <text x={x + step / 2} y={H - 6} textAnchor="middle" className="axis-label">
                      {MONTHS[Number(m.month.slice(5, 7)) - 1]}{m.month.endsWith("-01") || i === 0 ? ` ’${m.month.slice(2, 4)}` : ""}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        )}
      </div>
    </section>
  );
}

/** A month of closed-trade results per day, tinted green or red by size, with a week total. */
export function PnlCalendar({ stats, currency }: { stats: JournalStats; currency: string }) {
  const byDay = useMemo(() => new Map(stats.daily.map((d) => [d.day, d])), [stats.daily]);
  const latest = stats.daily.length ? stats.daily[stats.daily.length - 1].day.slice(0, 7) : new Date().toISOString().slice(0, 7);
  const first = stats.daily.length ? stats.daily[0].day.slice(0, 7) : latest;
  const [month, setMonth] = useState(latest);
  useEffect(() => setMonth(latest), [latest]);

  const shift = (n: number) => {
    const d = new Date(`${month}-01T00:00:00Z`);
    d.setUTCMonth(d.getUTCMonth() + n);
    setMonth(d.toISOString().slice(0, 7));
  };

  const cells = useMemo(() => {
    const start = new Date(`${month}-01T00:00:00Z`);
    const lead = (start.getUTCDay() + 6) % 7; // Monday first
    const days = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate();
    const list: (string | null)[] = [...Array(lead).fill(null)];
    for (let i = 1; i <= days; i++) list.push(`${month}-${String(i).padStart(2, "0")}`);
    while (list.length % 7) list.push(null);
    return list;
  }, [month]);

  const inMonth = stats.daily.filter((d) => d.day.startsWith(month));
  const total = inMonth.reduce((a, d) => ({ profit: a.profit + d.profit, pips: a.pips + d.pips, trades: a.trades + d.trades }),
    { profit: 0, pips: 0, trades: 0 });
  const biggest = Math.max(1, ...inMonth.map((d) => Math.abs(d.profit)));
  const tint = (v: number) => {
    const share = Math.round(12 + 38 * Math.min(Math.abs(v) / biggest, 1));
    return { background: `color-mix(in srgb, var(${v > 0 ? "--bull" : "--bear"}) ${share}%, var(--surface))` };
  };
  const weeks = Array.from({ length: cells.length / 7 }, (_, i) => cells.slice(i * 7, i * 7 + 7));

  return (
    <section className="journal-calendar" aria-labelledby="cal-h">
      <div className="journal-section-head">
        <h3 id="cal-h">Calendar</h3>
        <div className="cal-nav">
          <button type="button" className="button quiet" aria-label="Previous month" disabled={month <= first} onClick={() => shift(-1)}>‹</button>
          <b>{monthName(month)}</b>
          <button type="button" className="button quiet" aria-label="Next month" disabled={month >= latest} onClick={() => shift(1)}>›</button>
        </div>
        <p className="meta num">
          <span className={tone(total.profit)}>{money(total.profit)} {currency}</span>
          <span>{pips(total.pips)} pips</span>
          <span>{total.trades} trades on {inMonth.length} days</span>
        </p>
      </div>
      <div className="cal-grid num" role="table" aria-label={`Results per day, ${monthName(month)}`}>
        <div className="cal-row cal-names" role="row">
          {DAYS.map((d) => <span key={d} role="columnheader">{d}</span>)}
          <span role="columnheader">Week</span>
        </div>
        {weeks.map((week, wi) => {
          const days = week.map((d) => (d ? byDay.get(d) : undefined)).filter((d) => d !== undefined);
          const wk = days.reduce((a, d) => ({ profit: a.profit + d!.profit, pips: a.pips + d!.pips }), { profit: 0, pips: 0 });
          return (
            <div key={wi} className="cal-row" role="row">
              {week.map((day, di) => {
                const d = day ? byDay.get(day) : undefined;
                return (
                  <div key={di} role="cell" className={`cal-cell${day ? "" : " is-out"}${d ? " has-trades" : ""}`}
                    style={d && d.profit !== 0 ? tint(d.profit) : undefined}
                    title={d ? `${day}: ${money(d.profit)} ${currency}, ${pips(d.pips)} pips, ${d.trades} trades (${d.won} won)` : undefined}>
                    {day && <span className="cal-day">{Number(day.slice(8))}</span>}
                    {d && (
                      <>
                        <b className="cal-money"><span className="cal-long">{money(d.profit)}</span><span className="cal-short">{short(d.profit)}</span></b>
                        <span className="cal-pips">{pips(d.pips)} pips</span>
                        <span className="cal-trades">{d.trades} {d.trades === 1 ? "trade" : "trades"}</span>
                      </>
                    )}
                  </div>
                );
              })}
              <div role="cell" className={`cal-cell cal-week${days.length ? " has-trades" : ""}`}>
                {days.length > 0 && (
                  <>
                    <b className={`cal-money ${tone(wk.profit)}`}><span className="cal-long">{money(wk.profit)}</span><span className="cal-short">{short(wk.profit)}</span></b>
                    <span className="cal-pips">{pips(wk.pips)} pips</span>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
