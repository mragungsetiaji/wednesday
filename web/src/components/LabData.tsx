import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  confirmBars, fetchLabData, saveLabHistory, stageBars, startBackfill, stopBackfill,
  type BarsImported, type BarsPreview, type LabData,
} from "../api";
import { fmtPrice } from "../format";

const FULL_DAY = 1380; // M1 bars in a full gold trading day (23 hours)
const FORMATS: Record<BarsPreview["format"], string> = {
  mt5: "MT5 export", dukascopy: "Dukascopy", histdata: "HistData", csv: "CSV",
};

const day = (unix: number) => new Date(unix * 1000).toISOString().slice(0, 10);
const monthsOf = (bars: number) => Math.round(bars / (FULL_DAY * 5 * 4.33));

interface Month {
  key: string; // YYYY-MM
  days: (number | null)[]; // bars per day of the month; null on weekends without bars
  bars: number;
}

/** Stored bars per day, grouped by month, newest month first. */
function months(days: [number, number][]): Month[] {
  if (!days.length) return [];
  const byDay = new Map(days.map(([d, n]) => [day(d), n]));
  const first = new Date(days[0][0] * 1000);
  const last = new Date(days[days.length - 1][0] * 1000);
  const out: Month[] = [];
  for (let y = first.getUTCFullYear(), m = first.getUTCMonth(); y < last.getUTCFullYear() || (y === last.getUTCFullYear() && m <= last.getUTCMonth());) {
    const len = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
    const cells: (number | null)[] = [];
    let bars = 0;
    for (let d = 1; d <= len; d++) {
      const date = new Date(Date.UTC(y, m, d));
      const n = byDay.get(date.toISOString().slice(0, 10)) ?? 0;
      const weekend = date.getUTCDay() === 0 || date.getUTCDay() === 6;
      cells.push(weekend && !n ? null : n);
      bars += n;
    }
    out.push({ key: `${y}-${String(m + 1).padStart(2, "0")}`, days: cells, bars });
    m += 1;
    if (m === 12) {
      m = 0;
      y += 1;
    }
  }
  return out.reverse();
}

function Coverage({ data }: { data: LabData }) {
  const rows = useMemo(() => months(data.days), [data.days]);
  if (!rows.length) return <p className="empty">No stored bars for {data.source} {data.symbol} yet.</p>;
  return (
    <div className="coverage" role="table" aria-label="Stored M1 bars per day">
      {rows.map((m) => (
        <div key={m.key} className="coverage-row" role="row">
          <span className="coverage-month num" role="rowheader">{m.key}</span>
          <span className="coverage-days" role="cell">
            {m.days.map((n, i) => (
              <span key={i} className={`coverage-day${n === null ? " is-weekend" : ""}`}
                style={n ? { opacity: 0.25 + 0.75 * Math.min(1, n / FULL_DAY) } : undefined}
                data-empty={n === 0 ? "" : undefined}
                title={`${m.key}-${String(i + 1).padStart(2, "0")}: ${n ?? 0} bars`} />
            ))}
          </span>
          <span className="coverage-count num" role="cell">{m.bars.toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

/** Lab > Data: how much M1 history is stored, and ways to add more. */
export function LabData({ onChanged }: { onChanged: () => void }) {
  const [data, setData] = useState<LabData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchLabData()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const running = !!data?.backfill.running;
  useEffect(() => {
    load();
    const id = setInterval(load, running ? 1500 : 30000);
    return () => clearInterval(id);
  }, [load, running]);

  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) onChanged(); // the Lab's history size changed
    wasRunning.current = running;
  }, [running, onChanged]);

  if (!data) return <p className="empty">{error ? `Can't load: ${error}` : "Loading…"}</p>;
  const s = data.stored;

  return (
    <div className="lab-data">
      <section className="settings-form" aria-labelledby="coverage-h">
        <div className="settings-intro">
          <h2 id="coverage-h">Stored history</h2>
          <p>
            {s.bars
              ? <><span className="num">{s.bars.toLocaleString()}</span> M1 bars of {data.symbol} ({data.source}),{" "}
                <span className="num">{day(s.first_unix!)}</span> to <span className="num">{day(s.last_unix!)}</span>, in the feed clock{" "}
                ({data.clock}). Each square is a day; paler means fewer bars, grey is a weekend.</>
              : data.can_import
                ? "Nothing stored yet. Bars are stored as the screener runs; import a file or backfill from MT5 to go further back."
                : "Demo data isn't stored. Switch to Yahoo Finance, MT5 or CSV in Settings to keep and add history."}
          </p>
        </div>
        <Coverage data={data} />
      </section>

      {data.can_backfill && <Backfill data={data} onData={setData} />}
      {data.can_import && <Import data={data} onData={setData} onChanged={onChanged} />}
      <HistorySize data={data} onData={setData} onChanged={onChanged} />
    </div>
  );
}

function Backfill({ data, onData }: { data: LabData; onData: (d: LabData) => void }) {
  const b = data.backfill;
  const [start, setStart] = useState(() => {
    const d = new Date();
    d.setUTCFullYear(d.getUTCFullYear() - 1);
    return d.toISOString().slice(0, 10);
  });
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<LabData>) => {
    try {
      onData(await fn());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="settings-form" aria-labelledby="backfill-h">
      <div className="settings-intro">
        <h2 id="backfill-h">Backfill from MT5</h2>
        <p>
          Pulls older M1 bars from the terminal, a few days at a time between scans, back to the date you pick. How far it goes
          depends on the history the terminal has: raise <em>Tools &gt; Options &gt; Charts &gt; Max bars in chart</em> to get more.
        </p>
      </div>
      <div className="form-actions">
        <label className="field field-inline">
          <span className="field-label">Back to</span>
          <input type="date" value={start} max={new Date().toISOString().slice(0, 10)} disabled={b.running}
            onChange={(e) => setStart(e.target.value)} />
        </label>
        {b.running ? (
          <button type="button" className="button secondary" onClick={() => run(stopBackfill)}>Stop</button>
        ) : (
          <button type="button" className="button secondary" disabled={!start} onClick={() => run(() => startBackfill(start))}>
            Backfill
          </button>
        )}
        <span className="form-status" role="status">
          {error ? <span className="text-error">{error}</span>
            : b.running ? `Pulling… reached ${b.reached?.slice(0, 10) ?? "–"}, ${b.added.toLocaleString()} bars added`
              : b.finished_at ? `${b.error ? "Failed" : "Done"}: ${b.added.toLocaleString()} bars added${b.reached ? `, back to ${b.reached.slice(0, 10)}` : ""}.` : ""}
        </span>
      </div>
      {b.error && <p className="text-error field-note">{b.error}</p>}
      {b.note && !b.running && <p className="import-warn">{b.note}</p>}
    </section>
  );
}

function Import({ data, onData, onChanged }: { data: LabData; onData: (d: LabData) => void; onChanged: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [staged, setStaged] = useState<BarsPreview | null>(null);
  const [clock, setClock] = useState("UTC");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<BarsImported | null>(null);

  const choose = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const p = await stageBars(file);
      setStaged(p);
      setClock(p.suggested_clock ?? data.clock);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  const confirm = async () => {
    if (!staged) return;
    setBusy(true);
    try {
      const res = await confirmBars(staged.token, clock.trim());
      setDone(res.imported);
      setStaged(null);
      onData(res);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const clocks = [...new Set([staged?.suggested_clock ?? data.clock, "UTC", "UTC-5", "NY+7", data.clock])];
  return (
    <section className="settings-form" aria-labelledby="import-bars-h">
      <div className="settings-intro">
        <h2 id="import-bars-h">Import a file</h2>
        <p>
          M1 bars from an MT5 export, Dukascopy, HistData or any CSV with time, open, high, low and close. They go into{" "}
          {data.source} {data.symbol}; bars already stored are replaced, not doubled.
        </p>
      </div>
      <div className="form-actions">
        <input ref={fileRef} type="file" accept=".csv,.txt,text/csv,text/plain" hidden onChange={(e) => choose(e.target.files?.[0])} />
        <button type="button" className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}>
          {busy && !staged ? "Reading…" : "Choose a file"}
        </button>
        <span className="form-status" role="status">
          {error ? <span className="text-error">{error}</span>
            : done ? `Imported: ${done.added.toLocaleString()} new of ${done.read.toLocaleString()} bars (${done.first.slice(0, 10)} to ${done.last.slice(0, 10)}).` : ""}
        </span>
      </div>

      {staged && (
        <div className="import-card" role="dialog" aria-labelledby="bars-preview-h">
          <h3 id="bars-preview-h">{staged.bars.toLocaleString()} M1 bars</h3>
          <dl className="import-facts">
            <dt>Format</dt><dd>{FORMATS[staged.format]}</dd>
            <dt>Range</dt><dd className="num">{staged.first.replace("T", " ")} to {staged.last.replace("T", " ")} (file clock)</dd>
          </dl>
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col" className="end">Open</th>
                <th scope="col" className="end">High</th>
                <th scope="col" className="end">Low</th>
                <th scope="col" className="end">Close</th>
              </tr>
            </thead>
            <tbody>
              {staged.sample.map((r) => (
                <tr key={r.time}>
                  <td className="num">{r.time.replace("T", " ")}</td>
                  <td className="end num">{fmtPrice(r.open)}</td>
                  <td className="end num">{fmtPrice(r.high)}</td>
                  <td className="end num">{fmtPrice(r.low)}</td>
                  <td className="end num">{fmtPrice(r.close)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <label className="field">
            <span className="field-label">The file's clock</span>
            <input type="text" list="bar-clocks" value={clock} spellCheck={false} onChange={(e) => setClock(e.target.value)} />
            <datalist id="bar-clocks">{clocks.map((c) => <option key={c} value={c} />)}</datalist>
            <span className="field-hint">
              Times are moved from this clock to the feed's ({data.clock}). Dukascopy is UTC, HistData UTC-5, an MT5 export is
              the broker's server time (usually NY+7).
            </span>
          </label>
          <p className="import-warn">
            Use prices of the same market as the feed: MT5 and most CFD brokers quote spot gold, Yahoo Finance uses COMEX futures,
            which trade a few dollars apart.
          </p>
          <div className="form-actions">
            <button type="button" className="button primary" disabled={busy || !clock.trim()} onClick={confirm}>
              {busy ? "Importing…" : `Import ${staged.bars.toLocaleString()} bars`}
            </button>
            <button type="button" className="button quiet" disabled={busy} onClick={() => setStaged(null)}>Cancel</button>
          </div>
        </div>
      )}
    </section>
  );
}

function HistorySize({ data, onData, onChanged }: { data: LabData; onData: (d: LabData) => void; onChanged: () => void }) {
  const [bars, setBars] = useState(data.history_bars);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      onData(await saveLabHistory(bars));
      setMessage({ kind: "ok", text: "Saved." });
      onChanged();
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof Error ? err.message : String(err) });
    }
  };
  return (
    <form className="settings-form" onSubmit={save} aria-labelledby="history-size-h">
      <div className="settings-intro">
        <h2 id="history-size-h">History the Lab uses</h2>
        <p>The latest stored M1 bars loaded for labelling, training and the dataset. More history means more examples, more memory.</p>
      </div>
      <label className="field field-narrow">
        <span className="field-label">M1 bars</span>
        <input type="number" min={10000} max={data.history_bars_max} step={10000} value={bars}
          onChange={(e) => setBars(Number(e.target.value))} />
        <span className="field-hint">
          About {monthsOf(bars)} months of trading, ~{Math.round((bars * 48) / 1e6)} MB in memory. Up to{" "}
          {data.history_bars_max.toLocaleString()}.
        </span>
      </label>
      <div className="form-actions">
        <button type="submit" className="button secondary" disabled={bars === data.history_bars}>Save</button>
        <span className="form-status" role="status">
          {message && <span className={message.kind === "error" ? "text-error" : undefined}>{message.text}</span>}
        </span>
      </div>
    </form>
  );
}
