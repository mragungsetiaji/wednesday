import { useEffect, useRef, useState } from "react";

import {
  fetchBrief, generateBrief, saveBias,
  type BiasDirection, type BiasExpiry, type BriefResponse, type TradeBias,
} from "../api";
import { ArrowDown, ArrowUp } from "../icons";

const DIRECTIONS: { id: BiasDirection; label: string }[] = [
  { id: "bullish", label: "Bullish" },
  { id: "bearish", label: "Bearish" },
  { id: "neutral", label: "Neutral" },
];
const EXPIRY: { id: BiasExpiry; label: string }[] = [
  { id: "day", label: "Until NY close" },
  { id: "week", label: "Until Friday close" },
  { id: "none", label: "Until changed" },
];
const FOCUS: Record<BiasDirection, string> = {
  bearish: "Focus: sells at a lower high (risk on). Buys only on momentum (risk off).",
  bullish: "Focus: buys at a higher low (risk on). Sells only on momentum (risk off).",
  neutral: "No trading: every setup is marked no trade and alerts pause.",
};

const fmtWhen = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });

export function BiasIcon({ dir, size = 13 }: { dir: BiasDirection; size?: number }) {
  if (dir === "bullish") return <ArrowUp size={size} />;
  if (dir === "bearish") return <ArrowDown size={size} />;
  return <span className="bias-dash" aria-hidden="true" />;
}

/**
 * The trader's bias, set by hand, with the LLM brief as reading material. The
 * bias only labels setups (risk on / off / no trade); it never hides them.
 */
export function BiasPanel({ bias, onChanged, onOpenSettings }: {
  bias: TradeBias | null;
  onChanged: () => void;
  onOpenSettings: () => void;
}) {
  const [note, setNote] = useState(bias?.note ?? "");
  const [expiry, setExpiry] = useState<BiasExpiry>(bias?.expiry ?? "day");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [brief, setBrief] = useState<BriefResponse | null>(null);

  // Follow the server when the bias changes elsewhere (another tab, a restart).
  useEffect(() => {
    setNote(bias?.note ?? "");
    setExpiry(bias?.expiry ?? "day");
  }, [bias?.set_at, bias?.note, bias?.expiry]);

  // Brief status; poll faster while one is being generated.
  const running = !!brief?.running;
  const details = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (running && details.current) details.current.open = true; // show progress; closing stays up to the user
  }, [running]);
  useEffect(() => {
    let alive = true;
    const load = () => fetchBrief().then((b) => alive && setBrief(b)).catch(() => {});
    load();
    const id = setInterval(load, running ? 2000 : 30000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [running]);

  const save = async (direction: BiasDirection | null, patch: { note?: string; expiry?: BiasExpiry } = {}) => {
    setSaving(true);
    setError(null);
    try {
      await saveBias({ direction, note: patch.note ?? note, expiry: patch.expiry ?? expiry });
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const current = bias && !bias.expired ? bias.direction : null;
  const last = brief?.last ?? null;
  const suggestion = last?.suggested_bias ?? null;
  const provider = brief?.providers?.find((p) => p.id === brief.settings?.provider);
  const ready = !!(provider?.installed && provider.key_set && brief?.settings?.urls.length);

  const refresh = async () => {
    setError(null);
    try {
      setBrief(await generateBrief());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="bias" aria-labelledby="bias-h">
      <div className="bias-head">
        <h2 id="bias-h">Bias</h2>
        <span className={`bias-when${bias?.expired ? " is-expired" : ""}`}>
          {!bias ? "Not set" : bias.expired ? "Expired, set it again" : bias.expires_at ? `until ${fmtWhen(bias.expires_at)}` : "until changed"}
        </span>
      </div>

      <div className="segmented bias-choice" role="group" aria-label="Bias">
        {DIRECTIONS.map((d) => (
          <button key={d.id} type="button" className={`seg seg-text bias-${d.id}`} aria-pressed={current === d.id}
            disabled={saving} onClick={() => save(d.id)}>
            <BiasIcon dir={d.id} />
            {d.label}
          </button>
        ))}
      </div>

      <p className="bias-focus">{current ? FOCUS[current] : "Pick a direction to label the setups risk on or risk off."}</p>

      <div className="bias-fields">
        <input type="text" className="bias-note" value={note} maxLength={500} placeholder="Why? e.g. CPI hot, DXY breaking out"
          aria-label="Bias note" spellCheck={false}
          onChange={(e) => setNote(e.target.value)}
          onBlur={() => current && note !== (bias?.note ?? "") && save(current, { note })}
          onKeyDown={(e) => e.key === "Enter" && (e.currentTarget as HTMLInputElement).blur()} />
        <select className="bias-expiry" value={expiry} aria-label="Bias expires"
          onChange={(e) => {
            const v = e.target.value as BiasExpiry;
            setExpiry(v);
            if (current) save(current, { expiry: v });
          }}>
          {EXPIRY.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
      </div>

      <details className="brief" ref={details}>
        <summary>
          <span className="brief-title">News brief</span>
          <span className="brief-meta">
            {running ? "Writing…" : last ? `${fmtWhen(last.created_at)} · ${last.model}` : "None yet"}
          </span>
        </summary>
        {brief && !brief.editable ? (
          <p className="brief-empty">The brief needs the server running with --serve.</p>
        ) : !ready ? (
          <p className="brief-empty">
            Add an API key and news URLs to get a short read of the news here.{" "}
            <a href="#settings" onClick={(e) => { e.preventDefault(); onOpenSettings(); }}>Set up the brief</a>
          </p>
        ) : (
          <>
            {last ? <div className="brief-text">{last.text}</div> : <p className="brief-empty">No brief yet.</p>}
            {suggestion && (
              <p className="brief-suggest">
                Suggests <strong>{suggestion}</strong>
                {suggestion !== current && (
                  <button type="button" className="link" disabled={saving} onClick={() => save(suggestion)}>Use it</button>
                )}
              </p>
            )}
            {last && last.sources.some((s) => !s.ok || s.truncated) && (
              <ul className="brief-sources">
                {last.sources.filter((s) => !s.ok || s.truncated).map((s) => (
                  <li key={s.url}>{new URL(s.url).hostname}: {s.ok ? "long page, only the first part was read" : s.error}</li>
                ))}
              </ul>
            )}
            <button type="button" className="button quiet brief-refresh" disabled={running} onClick={refresh}>
              {running ? "Writing brief…" : last ? "Refresh brief" : "Write brief"}
            </button>
          </>
        )}
        {brief?.error && !running && <p className="text-error brief-error">{brief.error}</p>}
      </details>

      {error && <p className="text-error bias-error" role="alert">{error}</p>}
    </section>
  );
}
