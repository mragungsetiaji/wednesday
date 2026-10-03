import { useCallback, useEffect, useRef, useState } from "react";

import {
  createJournal, deleteJournal, fetchJournals, fetchTerminalAccount, importReport, journalCsvUrl, restoreSample, syncJournal,
  updateJournal, type Journal, type JournalPatch, type JournalsResponse, type TerminalAccount,
} from "../api";
import { usePref } from "../prefs";

/** What the new-journal form hands up: sync right away only when the terminal is on that account. */
export type NewDraft = { name: string; login: string; sync: boolean };

export const JOURNAL_PREF = "wed.journal"; // the journal open on the journal page and in its settings
export const JOURNAL_SETTINGS = "#settings/journal";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const pad = (n: number) => String(n).padStart(2, "0");

/** "2026-09-28 20:58" in this computer's time, like every other date on the journal page. */
export function fmtStamp(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "just now", "5 min ago", "3 h ago", "2 days ago". */
export function fmtRelative(iso: string, now = Date.now()): string {
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} days ago`;
}

/** The journal's sync in a few words, for the header and the settings: synced, syncing, waiting, failed or off. */
export function syncText(j: Journal, now = Date.now()): { text: string; title?: string; problem: boolean } {
  const s = j.sync;
  const last = s.at ? `${j.source === "report" ? "Imported" : "Synced"} ${fmtRelative(s.at, now)}` : "Not synced yet";
  const title = s.at ? fmtStamp(s.at) : undefined;
  switch (s.state) {
    case "syncing":
      return { text: "Syncing…", title, problem: false };
    case "waiting":
      return { text: s.error ?? "Waiting for the terminal", title, problem: false };
    case "error":
      return { text: `Last sync failed: ${s.error ?? "unknown error"}`, title, problem: true };
    case "off":
      return { text: j.source === "report" || !j.login ? last : `${last} · auto-sync off`, title, problem: false };
    default:
      return { text: last, title, problem: false };
  }
}

/** Marks the sample portfolio's numbers as made up. */
export function SampleBadge({ long = false }: { long?: boolean }) {
  return (
    <span className="badge sample" title="Sample data: not a real account. Generated trades and prices, no results anyone got.">
      {long ? "Sample data: not a real account" : "Sample data"}
    </span>
  );
}

/**
 * Settings > Journal: the open journal's name, account, sync, display and clock offset, import and export,
 * and every journal with New journal. The journal page only links here.
 */
export function JournalSettings() {
  const [list, setList] = useState<JournalsResponse | null>(null);
  const [current, setCurrent] = usePref<string | null>(JOURNAL_PREF, null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setList(await fetchJournals());
    } catch (e) {
      setMessage({ kind: "error", text: errText(e) });
    }
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 15_000); // auto-sync status changes on its own
    return () => clearInterval(id);
  }, [load]);

  const run = async (fn: () => Promise<string | void>): Promise<boolean> => {
    setBusy(true);
    setMessage(null);
    try {
      const text = await fn();
      if (text) setMessage({ kind: "ok", text });
      await load();
      return true;
    } catch (e) {
      setMessage({ kind: "error", text: errText(e) });
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!list) return null;
  if (!list.available) {
    return (
      <div className="settings-form">
        <div className="settings-intro">
          <h2>Journal</h2>
          <p>The journal needs the server running with --serve and a database.</p>
        </div>
      </div>
    );
  }
  const journals = list.journals;
  const j = journals.find((x) => x.id === current) ?? journals[0] ?? null;
  const own = journals.filter((x) => !x.sample).length; // the sample doesn't count toward the free journal
  const patch = (p: JournalPatch, done?: string) => j && run(async () => {
    await updateJournal(j.id, p);
    return done;
  });

  const create = (draft: NewDraft) => run(async () => {
    const made = await createJournal(draft.name, draft.login, draft.sync);
    setCurrent(made.id);
    if (!draft.sync) return `Created ${made.name} for account ${made.login}. It syncs once the terminal is on that account, or import its history report.`;
    const r = await syncJournal(made.id);
    return `Created ${made.name} and synced ${r.trades} trades and ${r.cash} deposits or withdrawals from account ${r.journal.login}.`;
  });

  const remove = (x: Journal) => run(async () => {
    await deleteJournal(x.id);
    if (x.id === j?.id) setCurrent(null);
    return x.sample ? "Deleted the sample portfolio. Show the sample, under Journals, brings it back." : `Removed ${x.name}.`;
  });

  return (
    <div className="settings-form journal-settings">
      <div className="settings-intro">
        <h2>Journal</h2>
        <p>A journal follows one MT5 account: gain, drawdown, deposits and every trade. Open it from Journal in the menu.</p>
      </div>

      {message && <p className={message.kind === "error" ? "text-error" : "journal-notice"} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}

      {j && <ThisJournal key={j.id} journal={j} journals={journals} busy={busy} onPick={setCurrent} onPatch={patch}
        onSync={() => run(async () => {
          const r = await syncJournal(j.id);
          return `Synced ${r.trades} trades and ${r.cash} deposits or withdrawals from account ${r.journal.login}.`;
        })}
        onImport={(file) => run(async () => {
          const r = await importReport(j.id, file);
          return `Imported ${r.trades} trades and ${r.cash} deposits or withdrawals.`;
        })} />}

      <fieldset className="fields">
        <legend>Journals</legend>
        <JournalList journals={journals} current={j?.id ?? ""} busy={busy} onOpen={setCurrent} onRemove={remove} />
        {!journals.some((x) => x.sample) && (
          <p className="field-note">
            <button type="button" className="link-button" disabled={busy} onClick={() => run(async () => {
              const s = await restoreSample();
              setCurrent(s.id);
            })}>
              Show the sample
            </button>{" "}
            portfolio again: made-up trades that show every panel, and don't count as a journal.
          </p>
        )}
        <section className="journal-new" aria-labelledby="journal-new-h">
          <h3 id="journal-new-h">New journal</h3>
          {!list.multi && own >= 1 ? (
            <p className="field-note">
              One journal is free. More than one, say one per MT5 account, comes with a plan that includes{" "}
              <strong>Multiple journals</strong> (<a href="#settings/plan">Settings, Plan</a>).
            </p>
          ) : (
            <NewJournal journals={journals} busy={busy} onCreate={create} />
          )}
        </section>
      </fieldset>
    </div>
  );
}

/** The open journal's own settings, saved per field. */
function ThisJournal({ journal: j, journals, busy, onPick, onPatch, onSync, onImport }: {
  journal: Journal; journals: Journal[]; busy: boolean; onPick: (id: string) => void;
  onPatch: (p: JournalPatch, done?: string) => void; onSync: () => void; onImport: (f: File) => void;
}) {
  const [name, setName] = useState(j.name);
  const [offset, setOffset] = useState(j.time_offset === null ? "" : String(j.time_offset));
  const fileRef = useRef<HTMLInputElement>(null);
  const status = syncText(j);
  const savedOffset = j.time_offset === null ? "" : String(j.time_offset);

  return (
    <>
      <fieldset className="fields">
        <legend>This journal</legend>
        {journals.length > 1 && (
          <label className="field">
            <span className="field-label">Journal</span>
            <select value={j.id} onChange={(e) => onPick(e.target.value)}>
              {journals.map((x) => <option key={x.id} value={x.id}>{x.sample ? `${x.name} (sample)` : x.name}</option>)}
            </select>
          </label>
        )}
        {j.sample && <p className="field-note"><SampleBadge long /> Generated trades and prices, to show what the journal does.</p>}
        <form className="field" onSubmit={(e) => { e.preventDefault(); onPatch({ name }, "Saved the name."); }}>
          <label className="field-label" htmlFor="journal-name">Name</label>
          <div className="input-with-button">
            <input id="journal-name" type="text" maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
            <button type="submit" className="button secondary" disabled={busy || name.trim() === "" || name === j.name}>Save</button>
          </div>
        </form>
        {!j.sample && (
          <dl className="journal-facts">
            <div><dt>MT5 account</dt><dd className="num">{j.login ? `#${j.login}` : "Not set"}</dd></div>
            <div><dt>Server</dt><dd>{j.server ?? "—"}</dd></div>
            <div><dt>Company</dt><dd>{j.company ?? "—"}</dd></div>
            <div><dt>Currency</dt><dd>{j.currency ?? "—"}</dd></div>
          </dl>
        )}
        {!j.sample && <p className="field-note">Account, server, company and currency come from the last sync or import.</p>}
      </fieldset>

      {!j.sample && (
        <fieldset className="fields" disabled={busy}>
          <legend>Sync</legend>
          <label className="switch">
            <input type="checkbox" checked={j.auto_sync} disabled={!j.login}
              onChange={(e) => onPatch({ auto_sync: e.target.checked }, e.target.checked ? "Auto-sync is on." : "Auto-sync is off.")} />
            <span>Sync from MT5 automatically</span>
          </label>
          <p className={status.problem ? "field-note text-error" : "field-note"} title={status.title}>
            {!j.login ? "Needs the MT5 account number: import a report or make the journal from the terminal's account. " : null}
            {j.auto_sync && "After each scan, when the terminal is logged in to this account and something changed. "}
            {status.text}{status.title && <> ({status.title})</>}.{" "}
            <button type="button" className="link-button" onClick={onSync}>Sync now</button>
          </p>
          <div className="field">
            <span className="field-label">Import report</span>
            <input ref={fileRef} type="file" accept=".html,.htm,text/html" hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onImport(f);
                e.target.value = "";
              }} />
            <div>
              <button type="button" className="button secondary" onClick={() => fileRef.current?.click()}>Choose report…</button>
            </div>
            <span className="field-note">
              In the terminal: <em>History</em> tab, right click, <em>Report</em>, <em>HTML</em>, with <em>All history</em> selected.
              It replaces what the journal had.
            </span>
          </div>
        </fieldset>
      )}

      <fieldset className="fields" disabled={busy}>
        <legend>Display</legend>
        <label className="switch">
          <input type="checkbox" checked={j.show_weekends} onChange={(e) => onPatch({ show_weekends: e.target.checked })} />
          <span>Show Saturday and Sunday in the calendar</span>
        </label>
        <p className="field-note">Off: weekend results still count in the week and month totals.</p>
      </fieldset>

      <fieldset className="fields" disabled={busy}>
        <legend>Advanced</legend>
        <form className="field" onSubmit={(e) => {
          e.preventDefault();
          onPatch({ time_offset: offset.trim() === "" ? null : Number(offset) }, "Saved the broker time offset.");
        }}>
          <label className="field-label" htmlFor="journal-offset">Broker time offset (hours)</label>
          <div className="input-with-button">
            <input id="journal-offset" type="number" min={-14} max={14} step={0.5} placeholder="detect" value={offset}
              onChange={(e) => setOffset(e.target.value)} />
            <button type="submit" className="button secondary" disabled={offset === savedOffset}>Save</button>
          </div>
          <span className="field-note">How far the broker's trade times are from the chart's prices. Leave empty to detect it from the deal prices.</span>
        </form>
      </fieldset>

      <fieldset className="fields">
        <legend>Data</legend>
        <div>
          <a className="button secondary" href={journalCsvUrl(j.id)} download>Export trades (CSV)</a>
        </div>
        <p className="field-note">Every trade with its note and tags.</p>
      </fieldset>
    </>
  );
}

/** Every journal with its account: open one, or remove it (its trades and notes go with it). */
function JournalList({ journals, current, busy, onOpen, onRemove }: {
  journals: Journal[]; current: string; busy: boolean; onOpen: (id: string) => void; onRemove: (j: Journal) => Promise<boolean>;
}) {
  return (
    <ul className="journal-list" aria-label="Journals">
      {journals.map((j) => {
        const s = syncText(j);
        return (
          <li key={j.id} aria-current={j.id === current ? "true" : undefined}>
            <div className="journal-list-main">
              <b>{j.name}</b>
              <span className="meta">
                {j.sample ? <SampleBadge long /> : <>
                  {j.login ? <span className="num">#{j.login}{j.server ? ` · ${j.server}` : ""}</span> : <span>No account yet</span>}
                  <span title={s.title}>{s.text}</span>
                </>}
              </span>
            </div>
            <div className="journal-list-actions">
              {j.id === current
                ? <span className="muted">Current</span>
                : <button type="button" className="button quiet" onClick={() => onOpen(j.id)}>Open</button>}
              <button type="button" className="button danger" disabled={busy}
                onClick={() => window.confirm(j.sample
                  ? "Delete the sample portfolio? It doesn't come back on its own; Show the sample restores it."
                  : `Remove ${j.name}? Its trades and notes are deleted from Wednesday. Nothing changes in MT5.`) && onRemove(j)}>
                {j.sample ? "Delete…" : "Remove journal…"}<span className="sr-only"> {j.name}</span>
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Name and MT5 account number, prefilled from the account the terminal is logged in to. When the terminal is on
 * that account the journal syncs as it's created and keeps syncing; otherwise it starts empty and the form says why.
 */
export function NewJournal({ journals, busy, onCreate }: { journals: Journal[]; busy: boolean; onCreate: (d: NewDraft) => Promise<boolean> }) {
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
                  : onTerminal ? "The terminal is on this account, so the journal syncs as it's created and keeps syncing."
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
