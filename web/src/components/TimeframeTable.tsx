import type { OrderBlock, TimeframeScan } from "../api";
import { distance, fmtPrice, fmtSigned, kindArrow, kindLabel } from "../format";

interface Props {
  rows: TimeframeScan[];
  price: number;
  selected: string;
  onSelect: (tf: string) => void;
}

function ZoneCell({ ob, price }: { ob: OrderBlock | null; price: number }) {
  if (!ob) return <td className="muted">—</td>;
  return (
    <td>
      <div className="zone-cell">
        <span className={`kind ${ob.kind}`}>{kindArrow(ob)} {kindLabel(ob)}</span>
        <span className="num">{fmtPrice(ob.bottom)} – {fmtPrice(ob.top)}</span>
      </div>
      <div className="zone-meta">
        <span className="num">{fmtSigned(distance(ob, price))}</span>
        <span className="muted">{ob.touches === 0 ? "untested" : `tested ${ob.touches}×`}</span>
      </div>
    </td>
  );
}

/** One row per timeframe, highest first: the scan order. */
export function TimeframeTable({ rows, price, selected, onSelect }: Props) {
  return (
    <section className="card table-card">
      <h2>Timeframes <span className="muted small">high → low</span></h2>
      <div className="table-scroll">
        <table className="tf-table">
          <thead>
            <tr>
              <th scope="col">TF</th>
              <th scope="col" className="right">Active</th>
              <th scope="col">Nearest above</th>
              <th scope="col">Nearest below</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.timeframe} className={r.timeframe === selected ? "selected" : undefined}
                onClick={() => onSelect(r.timeframe)}>
                <th scope="row">
                  <button type="button" className="tf-badge" aria-pressed={r.timeframe === selected}>
                    {r.timeframe}
                  </button>
                  {r.inside.length > 0 && (
                    <span className="inside-tag" title="Price is inside an order block on this timeframe">
                      in zone
                    </span>
                  )}
                </th>
                <td className="right num">{r.active_count}</td>
                <ZoneCell ob={r.above} price={price} />
                <ZoneCell ob={r.below} price={price} />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
