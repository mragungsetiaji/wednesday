import { useEffect, useState } from "react";

import { fetchScorecard, reviewBlock, type MlBlock, type Scorecard } from "../api";
import { fmtPrice, fmtUnix, fmtWhy } from "../format";
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
  const [hovered, setHovered] = useState<string | null>(null);
  const card = useScorecard(ml.model?.id ?? null, ml.blocks);
  const blocks = [...ml.blocks].reverse();
  const hot = blocks.find((b) => b.id === hovered);
  const hover = (id: string | null) => {
    setHovered(id);
    onHighlight(id);
  };
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
    <section className="detail ml-panel" aria-labelledby="ml-h">
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
                <tr key={b.id} onMouseEnter={() => hover(b.id)} onMouseLeave={() => hover(null)}
                  onFocus={() => hover(b.id)} onBlur={() => hover(null)}>
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
      {hot?.why?.length ? (
        <p className="ml-why" aria-live="polite">
          <strong>{hot.title} at {fmtUnix(hot.time_unix)}</strong>, why: {fmtWhy(hot.why)}
        </p>
      ) : null}
      {card && <ScorecardLine card={card} />}
      {error && <p className="text-error" role="alert">{error}</p>}
    </section>
  );
}

/** The active model's scorecard, refetched when its blocks change (the server caches it). */
function useScorecard(modelId: string | null, blocks: MlBlock[]) {
  const [card, setCard] = useState<Scorecard | null>(null);
  const reviewed = blocks.filter((b) => b.verdict).length;
  useEffect(() => {
    if (!modelId) return setCard(null);
    let alive = true;
    fetchScorecard()
      .then((r) => alive && setCard(r.scorecard))
      .catch(() => alive && setCard(null));
    return () => {
      alive = false;
    };
  }, [modelId, blocks.length, reviewed]);
  return card;
}

const pctOrDash = (v: number | null) => (v === null ? "—" : pct(v));
const signedR = (v: number | null) => (v === null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}R`);

/** One line: reviews and traded result since the model went on, then any drift warning. */
export function ScorecardLine({ card }: { card: Scorecard }) {
  const r = card.reviews;
  const m = card.market;
  return (
    <div className="ml-scorecard">
      <p>
        Since {new Date(card.since).toLocaleDateString(undefined, { dateStyle: "medium" })}:{" "}
        {r.n ? <>{r.n} reviewed, {pctOrDash(r.rate)} valid (held-out {pctOrDash(r.expected)})</> : "nothing reviewed yet"}
        {" · "}
        {m.finished ? <>{m.finished} finished trade{m.finished === 1 ? "" : "s"}, {signedR(m.avg_r)} (held-out {signedR(m.expected)})</>
          : `${m.calls} order block call${m.calls === 1 ? "" : "s"}, none finished yet`}
        {(r.n < card.min_samples || m.finished < card.min_samples) && (
          <span className="muted"> · warnings need {card.min_samples} of each</span>
        )}
      </p>
      {card.warnings.map((w) => <p key={w} className="ml-drift" role="status">{w}</p>)}
    </div>
  );
}
