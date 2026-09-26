import type { Scan } from "../api";
import { fmtPrice, fmtSpan } from "../format";

interface Props {
  scan: Scan;
  selected: string;
  onSelect: (tf: string) => void;
}

/** Direction of the latest break of structure per timeframe, high to low. */
export function StructurePanel({ scan, selected, onSelect }: Props) {
  const known = scan.timeframes.filter((t) => t.bias);
  const bulls = known.filter((t) => t.bias!.direction === "bullish").length;
  const bears = known.length - bulls;
  const summary =
    known.length === 0
      ? "no breaks yet"
      : bulls === known.length
        ? "all bullish"
        : bears === known.length
          ? "all bearish"
          : `${bulls} bullish · ${bears} bearish`;

  return (
    <section className="card structure">
      <h2>
        Market structure <span className="muted small">{summary}</span>
      </h2>
      <ul className="structure-list">
        {scan.timeframes.map((t) => {
          const b = t.bias;
          return (
            <li key={t.timeframe}>
              <button type="button" className={t.timeframe === selected ? "selected" : undefined}
                onClick={() => onSelect(t.timeframe)}>
                <span className="tf-badge">{t.timeframe}</span>
                {b ? (
                  <>
                    <span className={`bias ${b.direction}`}>
                      {b.direction === "bullish" ? "▲ Bullish" : "▼ Bearish"}
                    </span>
                    <span className={`event-pill ${b.event === "CHoCH" ? "choch" : ""}`}
                      title={b.event === "CHoCH" ? "Change of character: this break flipped the direction" : "Break of structure in the current direction"}>
                      {b.event}
                    </span>
                    <span className="num">{fmtPrice(b.level)}</span>
                    <span className="muted small structure-age">
                      {fmtSpan(b.break_close_time, scan.time)} ago · {b.bars_ago} bars
                      {b.streak > 1 && ` · ${b.streak}× in a row`}
                    </span>
                  </>
                ) : (
                  <span className="muted small">No break in the last {t.candles} candles</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
