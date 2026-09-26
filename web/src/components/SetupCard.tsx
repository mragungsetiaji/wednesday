import type { Setup } from "../api";
import { fmtPrice } from "../format";

interface Props {
  side: "sell" | "buy";
  setups: Setup[];
  maxSl: number;
  onSelect: (tf: string) => void;
}

function Priority({ s }: { s: Setup }) {
  return s.meta.priority === "extreme" ? <span className="prio extreme">Extreme</span> : <span className="prio">Mid</span>;
}

/** Best order block limit setup on one side of price, plus the next candidates. */
export function SetupCard({ side, setups, maxSl, onSelect }: Props) {
  const title = side === "sell" ? "Sell limit ▼" : "Buy limit ▲";
  const [best, ...rest] = setups;
  if (!best) {
    return (
      <section className={`card setup ${side}`}>
        <h2>{title}</h2>
        <p className="empty">No untaken {side === "sell" ? "bearish" : "bullish"} order block {side === "sell" ? "above" : "below"} price</p>
      </section>
    );
  }
  const m = best.meta;
  return (
    <section className={`card setup ${side}`}>
      <h2>{title} <span className="muted small">extreme first, then nearest</span></h2>
      <button type="button" className="setup-best" onClick={() => onSelect(best.timeframe)} title={`Show ${best.timeframe} chart`}>
        <div className="setup-head">
          <span className="tf-badge">{best.timeframe}</span>
          <Priority s={best} />
          <span className="muted small">{best.touches === 0 ? "untested" : `entry tested ${best.touches}×`}</span>
        </div>
        <dl className="setup-grid">
          <div>
            <dt>Entry</dt>
            <dd className="num setup-entry">{fmtPrice(m.entry!)}</dd>
          </div>
          <div>
            <dt>Stop loss</dt>
            <dd className="num">{fmtPrice(m.sl!)}</dd>
          </div>
          <div>
            <dt>Risk</dt>
            <dd className="num">
              {fmtPrice(m.risk!)}
              {m.sl_capped && <span className="cap" title={`Body ${fmtPrice(m.body!)} > ${fmtPrice(maxSl)}: stop capped`}>cap</span>}
            </dd>
          </div>
          <div>
            <dt>To entry</dt>
            <dd className="num">{fmtPrice(best.distance)}</dd>
          </div>
        </dl>
      </button>
      {rest.length > 0 && (
        <ul className="setup-list">
          {rest.map((s) => (
            <li key={`${s.timeframe}-${s.time}`}>
              <button type="button" onClick={() => onSelect(s.timeframe)}>
                <span className="tf-badge">{s.timeframe}</span>
                <Priority s={s} />
                <span className="num">@ {fmtPrice(s.meta.entry!)}</span>
                <span className="num muted">SL {fmtPrice(s.meta.sl!)}</span>
                <span className="num muted setup-dist">{fmtPrice(s.distance)} away</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
