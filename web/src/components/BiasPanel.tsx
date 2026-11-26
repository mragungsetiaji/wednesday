import { useEffect, useState } from "react";

import { saveBias, type BiasDirection, type BiasExpiry, type TradeBias } from "../api";
import { ArrowDown, ArrowUp, ChevronUp } from "../icons";
import { usePref } from "../prefs";

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
 * The trader's bias, set by hand. It only labels setups (risk on / off / no trade);
 * it never hides them. Folded by default to its heading (direction and expiry); the heading opens it.
 */
export function BiasPanel({ bias, onChanged }: {
  bias: TradeBias | null;
  onChanged: () => void;
}) {
  const [note, setNote] = useState(bias?.note ?? "");
  const [expiry, setExpiry] = useState<BiasExpiry>(bias?.expiry ?? "day");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = usePref<boolean>("wed.biasOpen", false);

  // Follow the server when the bias changes elsewhere (another tab, a restart).
  useEffect(() => {
    setNote(bias?.note ?? "");
    setExpiry(bias?.expiry ?? "day");
  }, [bias?.set_at, bias?.note, bias?.expiry]);

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

  return (
    <section className={`bias${open ? "" : " is-folded"}`} aria-labelledby="bias-h">
      <h2 id="bias-h" className="bias-head">
        <button type="button" className="bias-toggle" aria-expanded={open} aria-controls="bias-body"
          onClick={() => setOpen(!open)}>
          <span className="bias-title">
            Bias
            {current && !open && (
              <span className={`bias-now bias-${current}`}>
                <BiasIcon dir={current} size={12} />
                {DIRECTIONS.find((d) => d.id === current)?.label}
              </span>
            )}
          </span>
          <span className={`bias-when${bias?.expired ? " is-expired" : ""}`}>
            {!bias ? "Not set" : bias.expired ? "Expired, set it again" : bias.expires_at ? `until ${fmtWhen(bias.expires_at)}` : "until changed"}
          </span>
          <span className="bias-chevron" aria-hidden="true"><ChevronUp size={12} /></span>
        </button>
      </h2>

      {open && (
        <div className="bias-body" id="bias-body">
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

          {error && <p className="text-error bias-error" role="alert">{error}</p>}
        </div>
      )}
    </section>
  );
}
