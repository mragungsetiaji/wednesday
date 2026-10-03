import { useCallback, useEffect, useState } from "react";

import { addEntry, deleteEntry, fetchEntries, type FrozenMarket, type JournalEntry } from "../api";
import { fmtPrice } from "../format";
import { fmtStamp } from "./JournalSettings";

const KIND: Record<JournalEntry["kind"], string> = { note: "Note", review: "Session review", setup: "Setup" };
const MOODS = [1, 2, 3, 4, 5];
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/**
 * Notes, session reviews and setups journalled from the ladder (#14). Each keeps the market as it was
 * when written (price, bias, the nearest setups, structure, the next news), so a review later sees what
 * you saw then.
 */
export function JournalEntries({ journalId }: { journalId: string }) {
  const [entries, setEntries] = useState<JournalEntry[] | null>(null);
  const [kind, setKind] = useState<"note" | "review">("note");
  const [text, setText] = useState("");
  const [tags, setTags] = useState("");
  const [mood, setMood] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    fetchEntries(journalId).then((r) => setEntries(r.entries)).catch((e) => setError(errText(e)));
  }, [journalId]);
  useEffect(() => {
    load();
    const onAdded = () => load();
    window.addEventListener("wed:journal-entry", onAdded); // "Journal this" on the ladder
    return () => window.removeEventListener("wed:journal-entry", onAdded);
  }, [load]);

  return (
    <section className="journal-entries" aria-labelledby="entries-h">
      <div className="journal-section-head">
        <h3 id="entries-h">Notes and reviews</h3>
        <span className="meta">Each keeps the market as it was when you wrote it.</span>
      </div>
      <form className="journal-entry-form" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await addEntry(journalId, { kind, text, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), mood });
          setText("");
          setTags("");
          setMood(null);
          load();
        } catch (err) {
          setError(errText(err));
        } finally {
          setBusy(false);
        }
      }}>
        <div className="segmented" role="radiogroup" aria-label="Kind">
          {(["note", "review"] as const).map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} className="seg" onClick={() => setKind(k)}>
              {KIND[k]}
            </button>
          ))}
        </div>
        <label className="field">
          <span className="sr-only">{KIND[kind]}</span>
          <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)}
            placeholder={kind === "review" ? "How the session went: the plan, what price did, what you did…" : "What you see, what you're waiting for…"} />
        </label>
        <div className="journal-entry-meta">
          <label className="field">
            <span className="field-label">Tags</span>
            <input type="text" value={tags} placeholder="patience, a+ setup…" onChange={(e) => setTags(e.target.value)} />
          </label>
          <fieldset className="journal-mood">
            <legend className="field-label">Mood</legend>
            {MOODS.map((m) => (
              <label key={m} className="journal-mood-opt">
                <input type="radio" name="journal-mood" checked={mood === m} onChange={() => setMood(m)} />
                <span>{m}</span>
              </label>
            ))}
          </fieldset>
          <button type="submit" className="button primary" disabled={busy || !text.trim()}>{busy ? "Saving…" : `Add ${KIND[kind].toLowerCase()}`}</button>
        </div>
        {error && <p className="text-error" role="alert">{error}</p>}
      </form>
      {entries === null ? <p className="empty">Loading…</p> : entries.length === 0 ? (
        <p className="empty">No notes yet. Write one before a session and a review after it; &ldquo;Journal this&rdquo; on a ladder setup adds it here.</p>
      ) : (
        <ul className="journal-entry-list">
          {entries.map((e) => (
            <li key={e.id} className={`journal-entry is-${e.kind}`}>
              <div className="journal-entry-head">
                <span className="badge">{KIND[e.kind]}</span>
                <span className="meta num" title={e.updated_at ? `Edited ${fmtStamp(e.updated_at)}` : undefined}>{fmtStamp(e.created_at)}</span>
                {e.mood && <span className="meta" title="Mood, 1 to 5">mood {e.mood}/5</span>}
                {e.tags.map((t) => <span key={t} className="badge">{t}</span>)}
                <button type="button" className="link link-danger journal-entry-del"
                  onClick={() => window.confirm("Delete this entry?") && deleteEntry(journalId, e.id).then(load)}>
                  Delete<span className="sr-only"> this {KIND[e.kind].toLowerCase()}</span>
                </button>
              </div>
              {e.setup && (
                <p className="journal-entry-setup num">
                  <span className={`pill ${e.setup.side}`}>{e.setup.tag}</span> {e.setup.side === "sell" ? "Sell" : "Buy"} limit {e.setup.timeframe} at{" "}
                  {fmtPrice(e.setup.entry)}{e.setup.sl !== null && <> · SL {fmtPrice(e.setup.sl)}</>}
                </p>
              )}
              {e.text && <p className="journal-entry-text">{e.text}</p>}
              {e.market && <MarketThen market={e.market} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The frozen market, folded away until asked for. */
function MarketThen({ market: m }: { market: FrozenMarket }) {
  const setups = [...m.setups.sell.map((s) => ({ ...s, side: "Sell" })), ...m.setups.buy.map((s) => ({ ...s, side: "Buy" }))];
  return (
    <details className="journal-market">
      <summary>Market then: {m.price !== null ? <span className="num">{fmtPrice(m.price)}</span> : "—"}{m.bias ? `, bias ${m.bias.direction}` : ", no bias"}</summary>
      <dl className="journal-market-list">
        {m.structure.length > 0 && <div><dt>Structure</dt><dd>{m.structure.map((s) => `${s.timeframe} ${s.direction} ${s.event}`).join(" · ")}</dd></div>}
        {setups.length > 0 && (
          <div><dt>Setups</dt><dd className="num">
            {setups.map((s, i) => <span key={i}>{s.side} {s.timeframe} {s.entry !== null ? fmtPrice(s.entry) : "—"}{s.risk ? ` (risk ${s.risk})` : ""}{i < setups.length - 1 ? " · " : ""}</span>)}
          </dd></div>
        )}
        {m.news.length > 0 && <div><dt>Next news</dt><dd>{m.news.map((n) => `${fmtStamp(n.time)} ${n.currency} ${n.title}`).join(" · ")}</dd></div>}
        {m.bias?.note && <div><dt>Bias note</dt><dd>{m.bias.note}</dd></div>}
      </dl>
    </details>
  );
}
