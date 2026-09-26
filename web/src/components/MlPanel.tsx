import { useState } from "react";

import { reviewBlock, type MlBlock } from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { CheckIcon, CrossIcon } from "../icons";
import { pct } from "../labPrimitive";
import type { MlState } from "../mlData";

const THRESHOLDS = [0, 0.3, 0.5, 0.7, 0.9]; // 0: each tag's own cut from training

/**
 * The active model's blocks on this timeframe, newest first, each with valid / invalid.
 * A review is saved as a label for the next training run, with the market's result beside it.
 */
export function MlPanel({ tf, ml, threshold, onThreshold, onReviewed, onHighlight, onOpenLab }: {
  tf: string;
  ml: MlState;
  threshold: number;
  onThreshold: (v: number) => void;
  onReviewed: () => void;
  onHighlight: (id: string | null) => void;
  onOpenLab: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const blocks = [...ml.blocks].reverse();
  const unreviewed = blocks.filter((b) => !b.verdict).length;

  const review = async (b: MlBlock, verdict: "valid" | "invalid") => {
    setBusy(b.id);
    setError(null);
    try {
      await reviewBlock(b, verdict);
      onReviewed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="detail detail-wide ml-panel" aria-labelledby="ml-h">
      <div className="detail-head">
        <h2 id="ml-h">Model on {tf}</h2>
        {ml.model ? (
          <p className="detail-note">
            {ml.model.name} · {blocks.length} block{blocks.length === 1 ? "" : "s"}, {unreviewed} to review ·{" "}
            <label className="ml-threshold">
              show
              <select value={threshold} onChange={(e) => onThreshold(Number(e.target.value))} aria-label="Minimum probability">
                {THRESHOLDS.map((t) => <option key={t} value={t}>{t ? `from ${pct(t)}` : "at the trained cut"}</option>)}
              </select>
            </label>
          </p>
        ) : null}
      </div>
      {!ml.model ? (
        <p className="empty">
          No active model. Train one or import a model file in the{" "}
          <a href="#lab/models" className="link" onClick={(e) => { e.preventDefault(); onOpenLab(); }}>Lab</a>.
        </p>
      ) : blocks.length === 0 ? (
        <p className="empty">Nothing on the last candles {threshold ? `at ${pct(threshold)} or more` : "above the trained cut"}.</p>
      ) : (
        <div className="table-scroll ml-scroll">
          <table className="data compact">
            <thead>
              <tr>
                <th scope="col">Candle</th>
                <th scope="col">Model says</th>
                <th scope="col" className="end">Price</th>
                <th scope="col" className="end">Win chance</th>
                <th scope="col" className="end">Valid?</th>
              </tr>
            </thead>
            <tbody>
              {blocks.map((b) => (
                <tr key={b.id} onMouseEnter={() => onHighlight(b.id)} onMouseLeave={() => onHighlight(null)}
                  onFocus={() => onHighlight(b.id)} onBlur={() => onHighlight(null)}>
                  <td className="num">{fmtUnix(b.time_unix)}</td>
                  <td>{b.title} <span className="muted num">{pct(b.prob)}</span></td>
                  <td className="end num">{b.top === b.bottom ? fmtPrice(b.top) : `${fmtPrice(b.bottom)} – ${fmtPrice(b.top)}`}</td>
                  <td className="end num">{b.outcome_prob !== null ? pct(b.outcome_prob) : "—"}</td>
                  <td className="end">
                    <span className="verdict">
                      <button type="button" className="verdict-btn" aria-pressed={b.verdict === "valid"} disabled={busy === b.id}
                        aria-label={`${b.title} at ${fmtUnix(b.time_unix)} is valid`} onClick={() => review(b, "valid")}>
                        <CheckIcon size={13} />
                      </button>
                      <button type="button" className="verdict-btn" aria-pressed={b.verdict === "invalid"} disabled={busy === b.id}
                        aria-label={`${b.title} at ${fmtUnix(b.time_unix)} is invalid`} onClick={() => review(b, "invalid")}>
                        <CrossIcon size={13} />
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {error && <p className="text-error" role="alert">{error}</p>}
    </section>
  );
}
