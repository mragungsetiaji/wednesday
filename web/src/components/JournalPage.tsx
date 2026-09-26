import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createJournal, deleteJournal, fetchJournal, fetchJournals, importReport, journalCsvUrl, saveTradeNote, syncJournal,
  updateJournal, type Journal, type JournalStats, type JournalTrade, type JournalsResponse,
} from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { CheckIcon, CrossIcon } from "../icons";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { JournalChart, type JournalView } from "./JournalChart";

const PAGE = 50;
const VIEWS: { id: JournalView; title: string }[] = [
  { id: "growth", title: "Growth" },
  { id: "balance", title: "Balance" },
  { id: "drawdown", title: "Drawdown" },
];

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}%`);
const money = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))}`);
const signed = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${fmtPrice(Math.abs(v))}`);
const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? undefined : v > 0 ? "pos" : "neg");

function fmtHold(s: number | null) {
  if (s === null) return "—";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
}

/**
 * The journal: an MT5 account's gain, drawdown and trades. Numbers on the left, the curve on the right,
 * like a public track record, except the drawdown is rebuilt from the price instead of the closed results.
 */
export function JournalPage({ palette }: { palette: ChartPalette }) {
  const [list, setList] = useState<JournalsResponse | null>(null);
  const [current, setCurrent] = usePref<string | null>("wed.journal", null);
  const [stats, setStats] = useState<JournalStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = usePref<JournalView>("wed.journalView", "growth");

  const loadList = useCallback(async () => {
    try {
      const res = await fetchJournals();
      setList(res);
      return res;
    } catch (e) {
      setError(errText(e));
      return null;
    }
  }, []);

  const journals = useMemo(() => list?.journals ?? [], [list]);
  const selected = journals.find((j) => j.id === current) ?? journals[0] ?? null;

  const loadStats = useCallback(async (id: string) => {
    try {
      setStats(await fetchJournal(id));
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    setStats(null);
    if (!selected) return;
    loadStats(selected.id);
    const id = setInterval(() => loadStats(selected.id), 60_000);
    return () => clearInterval(id);
  }, [selected?.id, loadStats]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const msg = await fn();
      if (msg) setNotice(msg);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const refresh = async (id: string) => {
    await loadList();
    await loadStats(id);
  };

  if (!list) {
    return <main className="lab-page"><p className="empty">{error ? `Can't load the journal: ${error}` : "Loading the journal…"}</p></main>;
  }
  if (!list.available) {
    return (
      <main className="lab-page">
        <div className="settings-intro">
          <h2>Journal</h2>
          <p>The journal needs the server running with --serve and a database.</p>
        </div>
      </main>
    );
  }
  if (!selected) {
    return (
      <main className="lab-page">
        <NewJournal first busy={busy} error={error}
          onCreate={(name) => run(async () => {
            const j = await createJournal(name);
            setCurrent(j.id);
            await loadList();
          })} />
      </main>
    );
  }

  return (
    <main className="lab-page journal-page">
      <JournalHead journal={selected} journals={journals} multi={list.multi} busy={busy}
        onPick={setCurrent}
        onCreate={(name) => run(async () => {
          const j = await createJournal(name);
          setCurrent(j.id);
          await loadList();
        })}
        onSync={() => run(async () => {
          const r = await syncJournal(selected.id);
          await refresh(selected.id);
          return `Synced ${r.trades} trades and ${r.cash} deposits or withdrawals from account ${r.journal.login}.`;
        })}
        onImport={(file) => run(async () => {
          const r = await importReport(selected.id, file);
          await refresh(selected.id);
          return `Imported ${r.trades} trades and ${r.cash} deposits or withdrawals.`;
        })}
        onUpdate={(patch) => run(async () => {
          await updateJournal(selected.id, patch);
          await refresh(selected.id);
        })}
        onDelete={() => run(async () => {
          await deleteJournal(selected.id);
          setCurrent(null);
          await loadList();
        })} />
      {(error || notice) && (
        <p className={error ? "banner" : "journal-notice"} role={error ? "alert" : "status"}>{error ?? notice}</p>
      )}
      {!stats ? (
        <p className="empty">Working out the numbers…</p>
      ) : stats.trades.length === 0 && stats.cash.length === 0 ? (
        <EmptyJournal />
      ) : (
        <>
          <div className="journal-grid">
            <Summary stats={stats} />
            <section className="journal-curve" aria-label="Curve">
              <div className="tabs" role="tablist" aria-label="Curve">
                {VIEWS.map((v) => (
                  <button key={v.id} type="button" role="tab" className="tab" aria-selected={view === v.id} onClick={() => setView(v.id)}>
                    {v.title}
                  </button>
                ))}
              </div>
              <JournalChart stats={stats} view={view} palette={palette} currency={selected.currency ?? ""} />
              <Verification stats={stats} />
            </section>
          </div>
          <Trades journalId={selected.id} stats={stats} onSaved={() => loadStats(selected.id)} />
        </>
      )}
    </main>
  );
}

// ---- head: pick, create, sync, import, settings ----------------------------------------------

function JournalHead({ journal, journals, multi, busy, onPick, onCreate, onSync, onImport, onUpdate, onDelete }: {
  journal: Journal; journals: Journal[]; multi: boolean; busy: boolean;
  onPick: (id: string) => void; onCreate: (name: string) => void; onSync: () => void; onImport: (f: File) => void;
  onUpdate: (patch: { name?: string; time_offset?: number | null }) => void; onDelete: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [rename, setRename] = useState(journal.name);
  const [offset, setOffset] = useState(journal.time_offset === null ? "" : String(journal.time_offset));
  useEffect(() => {
    setRename(journal.name);
    setOffset(journal.time_offset === null ? "" : String(journal.time_offset));
  }, [journal]);
  const locked = !multi && journals.length >= 1;

  return (
    <div className="journal-head">
      <div className="journal-title">
        {journals.length > 1 ? (
          <select aria-label="Journal" value={journal.id} onChange={(e) => onPick(e.target.value)}>
            {journals.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
          </select>
        ) : (
          <h2>{journal.name}</h2>
        )}
        <p className="meta">
          {journal.login ? <span className="num">#{journal.login}{journal.server ? ` · ${journal.server}` : ""}</span> : <span>No account yet</span>}
          {journal.synced_at && <span>{journal.source === "mt5" ? "synced" : "imported"} {new Date(journal.synced_at).toLocaleString()}</span>}
        </p>
      </div>
      <div className="journal-actions">
        <button type="button" className="button primary" disabled={busy} onClick={onSync}
          title="Read every deal from the MT5 terminal the scanner is connected to">
          {busy ? "Working…" : "Sync from MT5"}
        </button>
        <input ref={fileRef} type="file" accept=".html,.htm,text/html" hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onImport(f);
            e.target.value = "";
          }} />
        <button type="button" className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}
          title="The terminal's history report: History tab, right click, Report, HTML">
          Import report
        </button>
        <a className="button quiet" href={journalCsvUrl(journal.id)} download>Export CSV</a>
        <button type="button" className="button quiet" aria-expanded={editing} onClick={() => setEditing(!editing)}>Settings</button>
        <button type="button" className="button quiet" aria-expanded={adding} onClick={() => setAdding(!adding)}>
          New journal{locked && <span className="badge journal-lock">Paid</span>}
        </button>
      </div>
      {adding && (
        locked ? (
          <p className="journal-panel field-note">
            One journal is free. More than one, say one per MT5 account, comes with a plan that includes{" "}
            <strong>Multiple journals</strong> (Settings, Plan).
          </p>
        ) : (
          <form className="journal-panel journal-inline" onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim()) return;
            onCreate(name);
            setName("");
            setAdding(false);
          }}>
            <input type="text" aria-label="Name of the new journal" placeholder="Name, e.g. Prop firm 100k" value={name} onChange={(e) => setName(e.target.value)} />
            <button type="submit" className="button secondary" disabled={busy || !name.trim()}>Create</button>
          </form>
        )
      )}
      {editing && (
        <form className="journal-panel fields-grid" onSubmit={(e) => {
          e.preventDefault();
          onUpdate({ name: rename, time_offset: offset.trim() === "" ? null : Number(offset) });
          setEditing(false);
        }}>
          <label className="field">
            <span className="field-label">Name</span>
            <input type="text" value={rename} onChange={(e) => setRename(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">Deal clock minus price clock (hours)</span>
            <input type="number" min={-14} max={14} step={0.5} placeholder="detect" value={offset} onChange={(e) => setOffset(e.target.value)} />
            <span className="field-note">Leave empty to detect it from the deal prices.</span>
          </label>
          <div className="form-actions field-span">
            <button type="submit" className="button secondary" disabled={busy}>Save</button>
            <button type="button" className="link link-danger" disabled={busy}
              onClick={() => window.confirm(`Delete ${journal.name} and its trades and notes?`) && onDelete()}>
              Delete journal
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function NewJournal({ first, busy, error, onCreate }: { first: boolean; busy: boolean; error: string | null; onCreate: (name: string) => void }) {
  const [name, setName] = useState("");
  return (
    <form className="settings-form journal-start" onSubmit={(e) => {
      e.preventDefault();
      if (name.trim()) onCreate(name);
    }}>
      <div className="settings-intro">
        <h2>{first ? "Start a journal" : "New journal"}</h2>
        <p>
          A journal follows one MT5 account: gain, drawdown, deposits and every trade. Sync it from the terminal the scanner
          is connected to, or import the terminal's history report. The drawdown is rebuilt from M1 prices, so a trade that
          sat deep in loss before closing green still shows.
        </p>
      </div>
      <label className="field field-narrow">
        <span className="field-label">Name</span>
        <input type="text" value={name} placeholder="e.g. Main account" onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="form-actions">
        <button type="submit" className="button primary" disabled={busy || !name.trim()}>Create journal</button>
        {error && <span className="text-error">{error}</span>}
      </div>
    </form>
  );
}

function EmptyJournal() {
  return (
    <div className="journal-empty">
      <h3>No trades yet</h3>
      <ol>
        <li><strong>Sync from MT5</strong> reads every deal from the terminal, when the data source is MT5 (Settings).</li>
        <li>
          Or in the terminal: <em>History</em> tab, right click, <em>Report</em>, <em>HTML</em>, with <em>All history</em>
          selected. Then <strong>Import report</strong>.
        </li>
      </ol>
      <p className="field-note">Either way replaces what the journal had, so sync or import the full history.</p>
    </div>
  );
}

// ---- left column: the numbers ------------------------------------------------------------------

function Summary({ stats }: { stats: JournalStats }) {
  const s = stats.summary;
  const t = stats.trading;
  const cur = stats.journal.currency ?? "";
  const rebuilt = stats.verification.basis === "ohlc";
  return (
    <section className="journal-stats" aria-label="Account">
      <div className="journal-hero">
        <div>
          <span className="stat-label">Gain</span>
          <b className={`stat-big num ${tone(s.gain) ?? ""}`}>{pct(s.gain)}</b>
        </div>
        <div>
          <span className="stat-label">Drawdown</span>
          <b className="stat-big num">{s.drawdown.toFixed(2)}%</b>
          <span className={`badge${rebuilt ? " accent" : ""}`}
            title={rebuilt ? "Rebuilt from the price every minute a trade was open" : "From closed results only: no prices for these trades"}>
            {rebuilt ? "from price" : "closed only"}
          </span>
        </div>
      </div>
      <dl className="stat-list num">
        <div><dt>Abs. gain</dt><dd className={tone(s.abs_gain)}>{pct(s.abs_gain)}</dd></div>
        <div><dt>Daily</dt><dd>{pct(s.daily)}</dd></div>
        <div><dt>Monthly</dt><dd>{pct(s.monthly)}</dd></div>
        <div className="sep"><dt>Balance</dt><dd>{money(s.balance)} {cur}</dd></div>
        <div><dt>Equity</dt><dd>{money(s.equity)} {cur}</dd></div>
        {s.floating !== 0 && <div><dt>Floating</dt><dd className={tone(s.floating)}>{signed(s.floating)}</dd></div>}
        <div><dt>Profit</dt><dd className={tone(s.profit)}>{signed(s.profit)}</dd></div>
        <div><dt>Deposits</dt><dd>{money(s.deposits)}</dd></div>
        <div><dt>Withdrawals</dt><dd>{money(s.withdrawals)}</dd></div>
        {s.credit !== 0 && <div><dt>Credit</dt><dd>{money(s.credit)}</dd></div>}
        {s.max_floating_loss !== null && <div><dt>Worst floating</dt><dd className={tone(s.max_floating_loss)}>{signed(s.max_floating_loss)}</dd></div>}
        {s.drawdown_at !== null && s.drawdown > 0 && <div><dt>Deepest at</dt><dd>{fmtUnix(s.drawdown_at)}</dd></div>}
        <div className="sep"><dt>Trades</dt><dd>{t.trades}{t.open ? ` · ${t.open} open` : ""}</dd></div>
        <div><dt>Won</dt><dd>{t.win_rate === null ? "—" : `${t.win_rate.toFixed(1)}%`} <span className="muted">({t.won}/{t.trades})</span></dd></div>
        <div><dt>Profit factor</dt><dd>{t.profit_factor?.toFixed(2) ?? "—"}</dd></div>
        <div><dt>Avg win / loss</dt><dd>{money(t.avg_win)} / {money(t.avg_loss)}</dd></div>
        <div><dt>Best / worst</dt><dd>{signed(t.best)} / {signed(t.worst)}</dd></div>
        <div><dt>Lots</dt><dd>{t.lots.toFixed(2)}</dd></div>
        <div><dt>Commission / swap</dt><dd>{money(t.commission)} / {money(t.swap)}</dd></div>
        <div><dt>Avg hold</dt><dd>{fmtHold(t.avg_hold)}</dd></div>
      </dl>
    </section>
  );
}

function Verification({ stats }: { stats: JournalStats }) {
  const v = stats.verification;
  const from = Object.entries(v.prices_from);
  return (
    <div className="journal-verify">
      <h3>How far these numbers are checked</h3>
      <ul>
        <li>
          {v.basis === "ohlc" ? <CheckIcon /> : <CrossIcon />}
          <span>
            Drawdown rebuilt from M1 prices for <b className="num">{v.verified}</b> of <b className="num">{v.trades}</b> trades
            {v.coverage !== null && <> (<span className="num">{v.coverage.toFixed(0)}%</span> of the time in trades)</>}.
            {v.verified < v.trades && " The rest count at their closed result."}
          </span>
        </li>
        <li>
          {v.prices_checked > 0 && v.prices_ok === v.prices_checked ? <CheckIcon /> : <CrossIcon />}
          <span>
            {v.prices_checked > 0
              ? <><b className="num">{v.prices_ok}</b> of <b className="num">{v.prices_checked}</b> deal prices sit inside their M1 bar.</>
              : "No deal price could be checked against the bars."}
            {v.mismatched.length > 0 && <> Not matching: <span className="num">{v.mismatched.slice(0, 5).join(", ")}{v.mismatched.length > 5 ? "…" : ""}</span>.</>}
          </span>
        </li>
        {v.balance_reported !== undefined && (
          <li>
            {v.balance_matches ? <CheckIcon /> : <CrossIcon />}
            <span>
              Balance {v.balance_matches ? "matches" : "doesn't match"} the terminal
              {!v.balance_matches && <> (<span className="num">{fmtPrice(v.balance_reported)}</span>; is the terminal showing all history?)</>}.
            </span>
          </li>
        )}
      </ul>
      <p className="field-note">
        {v.offset_hours !== null
          ? <>Deal clock {v.offset_hours >= 0 ? "+" : "−"}{Math.abs(v.offset_hours)} h from the price clock ({v.offset_detected ? "detected" : "set in Settings"}). </>
          : "The deal times didn't line up with the stored prices at any offset. "}
        {from.length > 0 && <>Prices: {from.map(([sym, src]) => `${sym} from ${src}`).join(", ")}. </>}
        {v.no_prices.length > 0 && <>No stored prices for {v.no_prices.join(", ")}: run the scanner on that symbol with MT5 to check them. </>}
      </p>
    </div>
  );
}

// ---- trades ---------------------------------------------------------------------------------

function Trades({ journalId, stats, onSaved }: { journalId: string; stats: JournalStats; onSaved: () => void }) {
  const [shown, setShown] = useState(PAGE);
  const [open, setOpen] = useState<string | null>(null);
  const rows = useMemo(
    () => [...stats.trades].sort((a, b) => (b.close_time ?? Infinity) - (a.close_time ?? Infinity) || b.open_time - a.open_time),
    [stats.trades]);
  return (
    <section className="journal-trades" aria-labelledby="trades-h">
      <h3 id="trades-h">Trades</h3>
      <div className="table-scroll">
        <table className="data journal-table num">
          <thead>
            <tr>
              <th scope="col">Closed</th>
              <th scope="col">Symbol</th>
              <th scope="col">Side</th>
              <th scope="col" className="end">Lots</th>
              <th scope="col" className="end">Open → close</th>
              <th scope="col" className="end">Result</th>
              <th scope="col" className="end" title="Worst floating result while open, from M1 prices">Worst</th>
              <th scope="col">Checked</th>
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, shown).map((t) => [
              <tr key={t.id} className={open === t.id ? "is-selected" : undefined} onClick={() => setOpen(open === t.id ? null : t.id)}>
                <td>{t.close_time ? fmtUnix(t.close_time) : <span className="badge accent">open</span>}</td>
                <td>{t.symbol}</td>
                <td className={t.side === "buy" ? "pos" : "neg"}>{t.side}</td>
                <td className="end">{t.volume.toFixed(2)}</td>
                <td className="end">{fmtPrice(t.open_price)} → {t.close_price === null ? "—" : fmtPrice(t.close_price)}</td>
                <td className={`end ${tone(t.close_time ? t.net : t.floating) ?? ""}`}>{signed(t.close_time ? t.net : t.floating)}</td>
                <td className="end">{t.mae === null ? "—" : signed(t.mae)}</td>
                <td>
                  {t.verified ? <span className="pos" title="Prices checked and floating rebuilt"><CheckIcon /></span>
                    : t.price_ok === false ? <span className="neg" title="Its prices aren't inside the M1 bars"><CrossIcon /></span>
                      : <span className="muted" title="No prices for this trade">—</span>}
                </td>
                <td className="journal-note-cell">
                  {t.tags.map((g) => <span key={g} className="badge">{g}</span>)} {t.note}
                </td>
              </tr>,
              open === t.id && (
                <tr key={`${t.id}-note`} className="lab-scores-row">
                  <td colSpan={9}>
                    <NoteEditor journalId={journalId} trade={t} onSaved={() => { setOpen(null); onSaved(); }} />
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      </div>
      {rows.length > shown && (
        <button type="button" className="button quiet" onClick={() => setShown(shown + PAGE)}>
          {rows.length - shown <= PAGE ? `Show the last ${rows.length - shown}` : `Show ${PAGE} more of ${rows.length - shown}`}
        </button>
      )}
    </section>
  );
}

function NoteEditor({ journalId, trade, onSaved }: { journalId: string; trade: JournalTrade; onSaved: () => void }) {
  const [note, setNote] = useState(trade.note);
  const [tags, setTags] = useState(trade.tags.join(", "));
  const [error, setError] = useState<string | null>(null);
  return (
    <form className="journal-note" onSubmit={async (e) => {
      e.preventDefault();
      try {
        await saveTradeNote(journalId, trade.id, note, tags.split(",").map((x) => x.trim()).filter(Boolean));
        onSaved();
      } catch (err) {
        setError(errText(err));
      }
    }}>
      <p className="field-note">
        Opened {fmtUnix(trade.open_time)} · position {trade.position} · commission {money(trade.commission)} · swap {money(trade.swap)}
        {trade.mfe !== null && <> · best floating {signed(trade.mfe)}</>}
      </p>
      <label className="field">
        <span className="field-label">Note</span>
        <textarea rows={2} value={note} placeholder="Why you took it, what you'd do again" onChange={(e) => setNote(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">Tags</span>
        <input type="text" value={tags} placeholder="a+ setup, fomo, moved stop" onChange={(e) => setTags(e.target.value)} />
      </label>
      <div className="journal-inline">
        <button type="submit" className="button secondary">Save note</button>
        {error && <span className="text-error">{error}</span>}
      </div>
    </form>
  );
}
