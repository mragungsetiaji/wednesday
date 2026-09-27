import type { Scan } from "../api";
import { fmtPrice, fmtSpan } from "../format";
import { Direction } from "../icons";

interface Props {
  scan: Scan;
  selected: string;
  onSelect: (tf: string) => void;
}

/** Latest break of structure per timeframe, high to low. The charted one is drawn as a dashed segment. */
export function StructurePanel({ scan, selected, onSelect }: Props) {
  const known = scan.timeframes.filter((t) => t.bias);
  const bulls = known.filter((t) => t.bias!.direction === "bullish").length;
  const bears = known.length - bulls;
  const summary = !known.length ? "No breaks yet" : !bears ? "All bullish" : !bulls ? "All bearish" : `${bulls} bullish, ${bears} bearish`;

  return (
    <section className="detail" aria-labelledby="structure-h">
      <div className="detail-head">
        <h2 id="structure-h">Structure</h2>
        <span className="detail-note">{summary}</span>
      </div>
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th scope="col">TF</th>
              <th scope="col">Direction</th>
              <th scope="col">Break</th>
              <th scope="col" className="end">Level</th>
              <th scope="col" className="end">Since</th>
            </tr>
          </thead>
          <tbody>
            {scan.timeframes.map((t) => {
              const b = t.bias;
              return (
                <tr key={t.timeframe} className={t.timeframe === selected ? "is-selected" : undefined} onClick={() => onSelect(t.timeframe)}>
                  <th scope="row">
                    <button type="button" className="link" onClick={() => onSelect(t.timeframe)}>{t.timeframe}</button>
                  </th>
                  {b ? (
                    <>
                      <td>
                        <span className={`dir ${b.direction}`}>
                          <Direction dir={b.direction} />
                          <span className="dir-label">{b.direction === "bullish" ? "Bullish" : "Bearish"}</span>
                        </span>
                      </td>
                      <td>
                        <span className={b.event === "CHoCH" ? "badge accent" : "badge"}
                          title={b.event === "CHoCH" ? "Change of character: this break flipped the direction" : "Break in the current direction"}>
                          {b.event}
                        </span>
                        {b.streak > 1 && <span className="muted"> ×{b.streak}</span>}
                      </td>
                      <td className="end num">{fmtPrice(b.level)}</td>
                      <td className="end num muted">{fmtSpan(b.break_close_time, scan.time)}</td>
                    </>
                  ) : (
                    <td colSpan={4} className="muted">No break in the last {t.candles} candles</td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
