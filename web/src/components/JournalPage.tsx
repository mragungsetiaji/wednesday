import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createJournal, deleteJournal, fetchJournal, fetchJournals, fetchTerminalAccount, importReport, journalCsvUrl, restoreSample, saveTradeNote,
  syncJournal, updateJournal, type Journal, type JournalStats, type JournalTrade, type JournalsResponse, type TerminalAccount,
  detachSnapshot,
  snapshotUrl,
} from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { CameraIcon, CheckIcon, CrossIcon, InfoIcon, TrashIcon } from "../icons";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { JournalChart, type JournalView } from "./JournalChart";
import { MonthlyBars, PnlCalendar } from "./JournalMonthly";

const PAGE = 25;
const VIEWS: { id: JournalView; title: string }[] = [
  { id: "growth", title: "Growth" },
  { id: "balance", title: "Balance" },
  { id: "drawdown", title: "Drawdown" },
];

/** What the new-journal form hands up: sync right away only when the terminal is on that account. */
type NewDraft = { name: string; login: string; sync: boolean };

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

  const run = async (fn: () => Promise<string | void>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const msg = await fn();
      if (msg) setNotice(msg);
      return true;
    } catch (e) {
      setError(errText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const refresh = async (id: string) => {
    await loadList();
    await loadStats(id);
  };

  // Create, then sync at once when the terminal is logged in to that account. If the sync fails the journal
  // stays, empty, and the banner says why.
  const create = (draft: NewDraft) => run(async () => {
    const j = await createJournal(draft.name, draft.login);
    setCurrent(j.id);
    await loadList();
    if (!draft.sync) return `Created ${j.name} for account ${j.login}. Sync it when the terminal is on that account, or import its history report.`;
    const r = await syncJournal(j.id);
    await refresh(j.id);
    return `Created ${j.name} and synced ${r.trades} trades and ${r.cash} deposits or withdrawals from account ${r.journal.login}.`;
  });

  const remove = (j: Journal) => run(async () => {
    await deleteJournal(j.id);
    if (j.id === selected?.id) setCurrent(null);
    await loadList();
    return j.sample ? "Deleted the sample portfolio. Journals, Show the sample brings it back." : `Removed ${j.name}.`;
  });

  const restore = () => run(async () => {
    const j = await restoreSample();
    setCurrent(j.id);
    await loadList();
  });

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
        <div className="settings-form journal-start">
          <div className="settings-intro">
            <h2>Start a journal</h2>
            <p>
              A journal follows one MT5 account: gain, drawdown, deposits and every trade. The drawdown is rebuilt from M1
              prices, so a trade that sat deep in loss before closing green still shows.
            </p>
          </div>
          <NewJournal journals={journals} busy={busy} onCreate={create} />
          <p className="field-note">
            To see a filled journal first:{" "}
            <button type="button" className="link-button" disabled={busy} onClick={restore}>show the sample portfolio</button>{" "}
            (made-up trades, deletable).
          </p>
          {(error || notice) && <p className={error ? "text-error" : "journal-notice"} role={error ? "alert" : "status"}>{error ?? notice}</p>}
        </div>
      </main>
    );
  }

  return (
    <main className="lab-page journal-page">
      <JournalHead journal={selected} journals={journals} multi={list.multi} busy={busy}
        onPick={setCurrent}
        onCreate={create}
        onRemove={remove}
        onRestore={restore}
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
          <div className="journal-periods">
            <MonthlyBars stats={stats} currency={selected.currency ?? ""} />
            <PnlCalendar stats={stats} currency={selected.currency ?? ""} />
          </div>
          <Trades journalId={selected.id} stats={stats} onSaved={() => loadStats(selected.id)} />
        </>
      )}
    </main>
  );
}

// ---- head: pick, sync, import, settings, the list of journals -------------------------------

function JournalHead({ journal, journals, multi, busy, onPick, onCreate, onRemove, onRestore, onSync, onImport, onUpdate }: {
  journal: Journal; journals: Journal[]; multi: boolean; busy: boolean;
  onPick: (id: string) => void; onCreate: (d: NewDraft) => Promise<boolean>; onRemove: (j: Journal) => Promise<boolean>;
  onRestore: () => void; onSync: () => void; onImport: (f: File) => void; onUpdate: (patch: { name?: string; time_offset?: number | null }) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [panel, setPanel] = useState<"journals" | "settings" | null>(null);
  const [rename, setRename] = useState(journal.name);
  const [offset, setOffset] = useState(journal.time_offset === null ? "" : String(journal.time_offset));
  useEffect(() => {
    setRename(journal.name);
    setOffset(journal.time_offset === null ? "" : String(journal.time_offset));
  }, [journal]);
  const toggle = (p: "journals" | "settings") => setPanel(panel === p ? null : p);
  const own = journals.filter((j) => !j.sample).length; // the sample doesn't count toward the free journal

  return (
    <div className="journal-head">
      <div className="journal-title">
        {journals.length > 1 ? (
          <select aria-label="Journal" value={journal.id} onChange={(e) => onPick(e.target.value)}>
            {journals.map((j) => <option key={j.id} value={j.id}>{j.sample ? `${j.name} (sample)` : j.name}</option>)}
          </select>
        ) : (
          <h2>{journal.name}</h2>
        )}
        <p className="meta">
          {journal.sample ? (
            <>
              <SampleBadge long />
              <span>Generated trades and prices, to show what the journal does</span>
            </>
          ) : (
            <>
              {journal.login ? <span className="num">#{journal.login}{journal.server ? ` · ${journal.server}` : ""}</span> : <span>No account yet</span>}
              <span>{syncedText(journal)}</span>
            </>
          )}
        </p>
      </div>
      <div className="journal-actions">
        {journal.sample ? (
          <button type="button" className="button secondary" disabled={busy}
            onClick={() => window.confirm("Delete the sample portfolio? It doesn't come back on its own; Journals, Show the sample restores it.") && onRemove(journal)}>
            Delete sample
          </button>
        ) : <>
        <button type="button" className="button primary" disabled={busy} onClick={onSync}
          title={journal.login ? `Read every deal of account ${journal.login} from the MT5 terminal` : "Read every deal from the MT5 terminal the scanner is connected to"}>
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
        </>}
        <a className="button quiet" href={journalCsvUrl(journal.id)} download>Export CSV</a>
        <button type="button" className="button quiet" aria-expanded={panel === "settings"} aria-controls="journal-panel"
          onClick={() => toggle("settings")}>Settings</button>
        <button type="button" className="button quiet" aria-expanded={panel === "journals"} aria-controls="journal-panel"
          onClick={() => toggle("journals")}>
          Journals <span className="num muted">{journals.length}</span>
        </button>
      </div>
      {panel === "journals" && (
        <div id="journal-panel" className="journal-panel journal-manage">
          <JournalList journals={journals} current={journal.id} busy={busy}
            onOpen={(id) => { onPick(id); setPanel(null); }} onRemove={onRemove} />
          {!journals.some((j) => j.sample) && (
            <p className="field-note">
              <button type="button" className="link-button" disabled={busy} onClick={() => { onRestore(); setPanel(null); }}>
                Show the sample
              </button>{" "}
              portfolio again: made-up trades that show every panel, and don't count as a journal.
            </p>
          )}
          <section className="journal-new" aria-labelledby="journal-new-h">
            <h3 id="journal-new-h">New journal</h3>
            {!multi && own >= 1 ? (
              <p className="field-note">
                One journal is free. More than one, say one per MT5 account, comes with a plan that includes{" "}
                <strong>Multiple journals</strong> (Settings, Plan).
              </p>
            ) : (
              <NewJournal journals={journals} busy={busy}
                onCreate={async (d) => {
                  const ok = await onCreate(d);
                  if (ok) setPanel(null);
                  return ok;
                }} />
            )}
          </section>
        </div>
      )}
      {panel === "settings" && (
        <form id="journal-panel" className="journal-panel fields-grid" onSubmit={(e) => {
          e.preventDefault();
          onUpdate({ name: rename, time_offset: offset.trim() === "" ? null : Number(offset) });
          setPanel(null);
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
            <span className="field-note">To remove this journal, open Journals.</span>
          </div>
        </form>
      )}
    </div>
  );
}

/** Marks the sample portfolio's numbers as made up. */
function SampleBadge({ long = false }: { long?: boolean }) {
  return (
    <span className="badge sample" title="Sample data: not a real account. Generated trades and prices, no results anyone got.">
      {long ? "Sample data: not a real account" : "Sample data"}
    </span>
  );
}

function syncedText(j: Journal) {
  if (!j.synced_at) return "not synced yet";
  return `${j.source === "mt5" ? "synced" : "imported"} ${new Date(j.synced_at).toLocaleString()}`;
}

/** Every journal with its account: open one, or remove it (its trades and notes go with it). */
function JournalList({ journals, current, busy, onOpen, onRemove }: {
  journals: Journal[]; current: string; busy: boolean; onOpen: (id: string) => void; onRemove: (j: Journal) => Promise<boolean>;
}) {
  return (
    <ul className="journal-list" aria-label="Journals">
      {journals.map((j) => (
        <li key={j.id} aria-current={j.id === current ? "true" : undefined}>
          <div className="journal-list-main">
            <b>{j.name}</b>
            <span className="meta">
              {j.sample ? <SampleBadge long /> : <>
                {j.login ? <span className="num">#{j.login}{j.server ? ` · ${j.server}` : ""}</span> : <span>No account yet</span>}
                <span>{syncedText(j)}</span>
              </>}
            </span>
          </div>
          <div className="journal-list-actions">
            {j.id === current
              ? <span className="badge">Open</span>
              : <button type="button" className="button quiet" onClick={() => onOpen(j.id)}>Open</button>}
            <button type="button" className="link link-danger" disabled={busy}
              onClick={() => window.confirm(j.sample
                ? "Delete the sample portfolio? It doesn't come back on its own; Show the sample restores it."
                : `Remove ${j.name}? Its trades and notes are deleted from Wednesday. Nothing changes in MT5.`) && onRemove(j)}>
              {j.sample ? "Delete" : "Remove"}<span className="sr-only"> {j.name}</span>
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * Name and MT5 account number, prefilled from the account the terminal is logged in to. When the terminal is on
 * that account the journal syncs as it's created; otherwise it starts empty and the form says why.
 */
function NewJournal({ journals, busy, onCreate }: { journals: Journal[]; busy: boolean; onCreate: (d: NewDraft) => Promise<boolean> }) {
  const [name, setName] = useState("");
  const [login, setLogin] = useState("");
  const [terminal, setTerminal] = useState<TerminalAccount | null>(null);
  useEffect(() => {
    let live = true;
    fetchTerminalAccount()
      .then((t) => {
        if (!live) return;
        setTerminal(t);
        if (t.connected && !journals.some((j) => j.login === t.login)) setLogin((cur) => cur || t.login);
      })
      .catch((e) => live && setTerminal({ connected: false, detail: errText(e) }));
    return () => { live = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const account = login.trim();
  const valid = /^\d+$/.test(account);
  const taken = journals.find((j) => j.login === account);
  const onTerminal = terminal?.connected === true && terminal.login === account;
  const ready = !busy && name.trim() !== "" && valid && !taken;

  return (
    <form className="journal-new-form" onSubmit={async (e) => {
      e.preventDefault();
      if (!ready) return;
      if (await onCreate({ name: name.trim(), login: account, sync: onTerminal })) {
        setName("");
        setLogin("");
      }
    }}>
      <p className="journal-terminal field-note" role="status">
        {terminal === null ? "Checking the MT5 terminal…"
          : terminal.connected ? (
            <>
              MT5 terminal is logged in to <span className="num">#{terminal.login}</span>{terminal.server ? ` · ${terminal.server}` : ""}.
              {account !== terminal.login && (
                <> <button type="button" className="link-button" onClick={() => setLogin(terminal.login)}>Use this account</button></>
              )}
            </>
          ) : <>{terminal.detail}. You can still create the journal and import the terminal's history report.</>}
      </p>
      <div className="fields-grid">
        <label className="field">
          <span className="field-label">Name</span>
          <input type="text" value={name} maxLength={120} placeholder="e.g. Prop firm 100k" onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">MT5 account number</span>
          <input type="text" inputMode="numeric" autoComplete="off" value={login} placeholder="e.g. 51234567"
            aria-invalid={account !== "" && (!valid || !!taken)} aria-describedby="journal-login-note"
            onChange={(e) => setLogin(e.target.value)} />
          <span id="journal-login-note" className={account !== "" && (!valid || taken) ? "field-note text-error" : "field-note"}>
            {account === "" ? "The login number shown in MT5, under Navigator, Accounts."
              : !valid ? "Digits only, as MT5 shows the login."
                : taken ? `${taken.name} already follows this account.`
                  : onTerminal ? "The terminal is on this account, so the journal syncs as it's created."
                    : terminal?.connected
                      ? `The terminal is on #${terminal.login}, so this journal starts empty. Log in to #${account} in MT5 and sync, or import its report.`
                      : "Starts empty. Sync once the terminal is on this account, or import its report."}
          </span>
        </label>
      </div>
      <div className="form-actions">
        <button type="submit" className="button primary" disabled={!ready}>
          {busy ? "Working…" : onTerminal ? "Create and sync" : "Create journal"}
        </button>
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
  const sample = stats.journal.sample;
  return (
    <section className="journal-stats" aria-label="Account">
      <div className="journal-hero">
        <div>
          <span className="stat-label">Gain</span>
          <b className={`stat-big num ${tone(s.gain) ?? ""}`}>{pct(s.gain)}</b>
          {sample && <SampleBadge />}
        </div>
        <div>
          <span className="stat-label">Drawdown</span>
          <b className="stat-big num">{s.drawdown.toFixed(2)}%</b>
          <span className={`badge${rebuilt ? " accent" : ""}`}
            title={rebuilt ? "Drawdown from 1-minute prices while trades were open" : "Drawdown from closed results: no prices for these trades"}>
            {rebuilt ? "from price" : "closed only"}
          </span>
          {sample && <SampleBadge />}
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

/** One neutral line on how the numbers were worked out, with the detail behind a disclosure.
 * Only real problems (deal prices outside their bars, a balance that doesn't match) get the
 * warning look. */
function Verification({ stats }: { stats: JournalStats }) {
  const v = stats.verification;
  const from = Object.entries(v.prices_from);
  const mismatched = v.mismatched.length > 0;
  const balanceOff = v.balance_reported !== undefined && v.balance_matches === false;
  const rebuilt = v.basis === "ohlc";
  return (
    <div className="journal-verify">
      <p className="journal-verify-line">
        <InfoIcon />
        <span>
          {rebuilt ? "Drawdown from 1-minute prices" : "Drawdown from closed results"}. Prices for{" "}
          <span className="num">{v.verified}</span> of <span className="num">{v.trades}</span> trades.
        </span>
      </p>
      {(mismatched || balanceOff) && (
        <ul className="journal-verify-problems">
          {mismatched && (
            <li>
              <CrossIcon />
              <span>
                <span className="num">{v.mismatched.length}</span> deal price{v.mismatched.length > 1 ? "s aren't" : " isn't"} inside
                its 1-minute bar: <span className="num">{v.mismatched.slice(0, 5).join(", ")}{v.mismatched.length > 5 ? "…" : ""}</span>.
                Those trades are left out of the drawdown. Check the report wasn't edited and the prices come from the same broker.
              </span>
            </li>
          )}
          {balanceOff && (
            <li>
              <CrossIcon />
              <span>
                The balance doesn't match the terminal's (<span className="num">{fmtPrice(v.balance_reported as number)}</span>).
                Set the terminal's History tab to all history, then sync again.
              </span>
            </li>
          )}
        </ul>
      )}
      <details className="journal-verify-details">
        <summary>Details</summary>
        <ul>
          <li>
            {rebuilt ? <CheckIcon /> : <InfoIcon />}
            <span>
              Drawdown rebuilt from 1-minute prices for <b className="num">{v.verified}</b> of <b className="num">{v.trades}</b> trades
              {v.coverage !== null && <> (<span className="num">{v.coverage.toFixed(0)}%</span> of the time in trades)</>}.
              {v.verified < v.trades && " The rest count at their closed result."}
            </span>
          </li>
          <li>
            {v.prices_checked > 0 && !mismatched ? <CheckIcon /> : mismatched ? <CrossIcon /> : <InfoIcon />}
            <span>
              {v.prices_checked > 0
                ? <><b className="num">{v.prices_ok}</b> of <b className="num">{v.prices_checked}</b> deal prices checked against 1-minute prices.</>
                : "No deal prices could be checked: there are no stored prices for these trades."}
            </span>
          </li>
          {v.balance_reported !== undefined && (
            <li>
              {v.balance_matches ? <CheckIcon /> : <CrossIcon />}
              <span>Balance {v.balance_matches ? "matches" : "doesn't match"} the terminal.</span>
            </li>
          )}
        </ul>
        <p className="field-note">
          {v.offset_hours !== null
            ? <>Broker time is {Math.abs(v.offset_hours)} h {v.offset_hours >= 0 ? "ahead of" : "behind"} the chart
              ({v.offset_detected ? "detected" : <>set in <a href="#settings/journal">journal settings</a></>}). </>
            : "The trade times didn't line up with the stored prices at any offset. "}
          {from.length > 0 && <>Prices: {from.map(([sym, src]) => `${sym} from ${src}`).join(", ")}. </>}
          {v.no_prices.length > 0 && <>No stored prices for {v.no_prices.join(", ")}: run the scanner on that symbol with MT5 to check them. </>}
        </p>
      </details>
    </div>
  );
}

// ---- trades ---------------------------------------------------------------------------------

function Trades({ journalId, stats, onSaved }: { journalId: string; stats: JournalStats; onSaved: () => void }) {
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const rows = useMemo(
    () => [...stats.trades].sort((a, b) => (b.close_time ?? Infinity) - (a.close_time ?? Infinity) || b.open_time - a.open_time),
    [stats.trades]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const current = Math.min(page, pages - 1);
  const go = (n: number) => {
    setPage(Math.max(0, Math.min(n, pages - 1)));
    setOpen(null);
  };
  const from = rows.length ? current * PAGE + 1 : 0;
  const to = Math.min(rows.length, (current + 1) * PAGE);
  const shown = rows.slice(current * PAGE, current * PAGE + PAGE);
  // Columns that would be all dashes on this page stay out.
  const hasWorst = shown.some((t) => t.mae !== null);
  const hasChecked = shown.some((t) => t.verified || t.price_ok === false);
  const hasNote = shown.some((t) => t.note || t.tags.length > 0 || t.images.length > 0);
  const cols = 7 + Number(hasWorst) + Number(hasChecked) + Number(hasNote);
  // The Closed cell is the row's button; focus goes back to it when the note closes.
  const triggers = useRef(new Map<string, HTMLButtonElement>());
  const close = (id: string) => {
    setOpen(null);
    requestAnimationFrame(() => triggers.current.get(id)?.focus());
  };
  const toggleRow = (id: string) => (open === id ? close(id) : setOpen(id));
  return (
    <section className="journal-trades" aria-labelledby="trades-h">
      <div className="journal-section-head">
        <h3 id="trades-h">Trades</h3>
        <span className="pager-range num">{from}–{to} of {rows.length}</span>
      </div>
      {shown.length > 0 && !hasWorst && !hasChecked && (
        <p className="field-note">No prices stored for these trades: drawdown is from closed results.</p>
      )}
      <div className="table-scroll">
        <table className="data journal-table num">
          <thead>
            <tr>
              <th scope="col">Closed</th>
              <th scope="col">Symbol</th>
              <th scope="col" className="journal-wide">Side</th>
              <th scope="col" className="end">Result</th>
              <th scope="col" className="end">Pips</th>
              <th scope="col" className="end journal-wide">Lots</th>
              <th scope="col" className="end journal-wide">Open → close</th>
              {hasWorst && <th scope="col" className="end journal-wide" title="Worst floating result while open, from 1-minute prices">Worst</th>}
              {hasChecked && <th scope="col" className="journal-wide">Checked</th>}
              {hasNote && <th scope="col" className="journal-wide">Note</th>}
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => [
              <tr key={t.id} className={open === t.id ? "is-selected" : undefined} onClick={() => toggleRow(t.id)}>
                <td>
                  <button type="button" className="journal-row-button" aria-expanded={open === t.id} aria-controls={`note-${t.id}`}
                    title={open === t.id ? "Close the note" : "Open the note, tags and charts"}
                    ref={(el) => { if (el) triggers.current.set(t.id, el); else triggers.current.delete(t.id); }}
                    onClick={(e) => { e.stopPropagation(); toggleRow(t.id); }}>
                    {t.close_time ? fmtUnix(t.close_time) : <span className="badge accent">open</span>}
                  </button>
                </td>
                <td>{t.symbol}</td>
                <td className="journal-wide">{t.side === "buy" ? "↑ Buy" : "↓ Sell"}</td>
                <td className={`end ${tone(t.close_time ? t.net : t.floating) ?? ""}`}>{signed(t.close_time ? t.net : t.floating)}</td>
                <td className="end">{t.pips === null ? "—" : `${t.pips >= 0 ? "+" : "−"}${Math.abs(t.pips).toFixed(1)}`}</td>
                <td className="end journal-wide">{t.volume.toFixed(2)}</td>
                <td className="end journal-wide">{fmtPrice(t.open_price)} → {t.close_price === null ? "—" : fmtPrice(t.close_price)}</td>
                {hasWorst && <td className="end journal-wide">{t.mae === null ? "—" : signed(t.mae)}</td>}
                {hasChecked && (
                  <td className="journal-wide">
                    {t.verified ? <span className="pos" title="Prices checked and floating rebuilt"><CheckIcon /></span>
                      : t.price_ok === false ? <span className="neg" title="Its prices aren't inside the 1-minute bars"><CrossIcon /></span>
                        : <span className="muted" title="No prices for this trade">—</span>}
                  </td>
                )}
                {hasNote && (
                  <td className="journal-note-cell journal-wide">
                    {t.tags.map((g) => <span key={g} className="badge">{g}</span>)} {t.note}
                    {t.images.length > 0 && (
                      <span className="muted journal-shot-count" title={`${t.images.length} chart snapshot${t.images.length > 1 ? "s" : ""}`}>
                        <CameraIcon size={12} /> {t.images.length}
                      </span>
                    )}
                  </td>
                )}
              </tr>,
              open === t.id && (
                <tr key={`${t.id}-note`} id={`note-${t.id}`} className="lab-scores-row">
                  <td colSpan={cols}>
                    <NoteEditor journalId={journalId} trade={t} onClose={() => close(t.id)}
                      onSaved={() => { close(t.id); onSaved(); }} onChanged={onSaved} />
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      </div>
      {pages > 1 && <div className="journal-pager-foot"><Pager page={current} pages={pages} onGo={go} /></div>}
    </section>
  );
}

/** Page numbers around the current one, with the first and last always there. */
function pageList(page: number, pages: number): (number | null)[] {
  const keep = new Set([0, pages - 1, page - 1, page, page + 1]);
  const out: (number | null)[] = [];
  for (let i = 0; i < pages; i++) {
    if (keep.has(i)) out.push(i);
    else if (out[out.length - 1] !== null) out.push(null);
  }
  return out;
}

/** Page buttons under the table; the range ("1–25 of 165") sits in the section header. */
function Pager({ page, pages, onGo }: { page: number; pages: number; onGo: (n: number) => void }) {
  return (
    <nav className="pager num" aria-label="Trade pages">
      <button type="button" className="pager-btn" aria-label="Previous page" disabled={page === 0} onClick={() => onGo(page - 1)}>‹</button>
      {pageList(page, pages).map((n, i) =>
        n === null ? <span key={`gap${i}`} className="pager-gap">…</span> : (
          <button key={n} type="button" className="pager-btn" aria-current={n === page ? "page" : undefined} onClick={() => onGo(n)}>
            {n + 1}
          </button>
        ))}
      <button type="button" className="pager-btn" aria-label="Next page" disabled={page === pages - 1} onClick={() => onGo(page + 1)}>›</button>
    </nav>
  );
}

/** A trade's chart snapshots: open one full size, or take it off (its file goes too). */
function Shots({ journalId, trade, onChanged }: { journalId: string; trade: JournalTrade; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="field">
      <span className="field-label">Charts</span>
      <div className="journal-shots">
        {trade.images.map((id) => (
          <div key={id} className="journal-shot">
            <a href={snapshotUrl(id)} target="_blank" rel="noreferrer" title="Open full size">
              <img src={snapshotUrl(id)} alt="Chart snapshot" loading="lazy" />
            </a>
            <button type="button" className="icon-button" aria-label="Remove this chart" title="Remove this chart"
              onClick={() => window.confirm("Remove this chart from the trade? The image is deleted.") &&
                detachSnapshot(journalId, trade.id, id).then(onChanged).catch((e) => setError(errText(e)))}>
              <TrashIcon size={13} />
            </button>
          </div>
        ))}
      </div>
      {error && <span className="text-error">{error}</span>}
      <span className="field-note">Add one from a chart: the camera under it, then Attach to a journal trade.</span>
    </div>
  );
}

function NoteEditor({ journalId, trade, onSaved, onChanged, onClose }: { journalId: string; trade: JournalTrade; onSaved: () => void;
  onChanged: () => void; onClose: () => void }) {
  const [note, setNote] = useState(trade.note);
  const [tags, setTags] = useState(trade.tags.join(", "));
  const [error, setError] = useState<string | null>(null);
  return (
    <form className="journal-note" onKeyDown={(e) => { if (e.key === "Escape") onClose(); }} onSubmit={async (e) => {
      e.preventDefault();
      try {
        await saveTradeNote(journalId, trade.id, note, tags.split(",").map((x) => x.trim()).filter(Boolean));
        onSaved();
      } catch (err) {
        setError(errText(err));
      }
    }}>
      <p className="field-note">
        {trade.side === "buy" ? "Buy" : "Sell"} {trade.volume.toFixed(2)} lots, {fmtPrice(trade.open_price)} →{" "}
        {trade.close_price === null ? "—" : fmtPrice(trade.close_price)}
        {trade.mae !== null && <> · worst floating {signed(trade.mae)}</>}
        <br />
        Opened {fmtUnix(trade.open_time)} · position {trade.position} · commission {money(trade.commission)} · swap {money(trade.swap)}
        {trade.mfe !== null && <> · best floating {signed(trade.mfe)}</>}
      </p>
      {trade.images.length > 0 && <Shots journalId={journalId} trade={trade} onChanged={onChanged} />}
      <label className="field">
        <span className="field-label">Note</span>
        <textarea rows={2} autoFocus value={note} placeholder="Why you took it, what you'd do again" onChange={(e) => setNote(e.target.value)} />
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
