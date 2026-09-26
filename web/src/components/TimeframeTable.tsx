import type { DetectorInfo, Level, TimeframeScan } from "../api";
import { detail, distance, fmtLevelPrice, fmtSigned } from "../format";
import { LevelTag } from "./LevelTag";

interface Props {
  rows: TimeframeScan[];
  detectors: DetectorInfo[];
  price: number;
  selected: string;
  onSelect: (tf: string) => void;
}

function SideCell({ row, detectors, side, price }: { row: TimeframeScan; detectors: DetectorInfo[]; side: "above" | "below"; price: number }) {
  const levels = detectors.map((d) => row.detectors[d.name]?.[side]).filter((lv): lv is Level => !!lv);
  if (!levels.length) return <td className="muted">None</td>;
  levels.sort((a, b) => Math.abs(distance(a, price)) - Math.abs(distance(b, price)));
  return (
    <td>
      <ul className="cell-list">
        {levels.map((lv) => (
          <li key={lv.detector}>
            <LevelTag level={lv} />
            <span className="num">{fmtLevelPrice(lv)}</span>
            <span className="num muted">{fmtSigned(distance(lv, price))}</span>
            <span className="muted">{detail(lv)}</span>
          </li>
        ))}
      </ul>
    </td>
  );
}

/** Nearest level per detector on each side, for every timeframe (the table view of the chart). */
export function TimeframeTable({ rows, detectors, price, selected, onSelect }: Props) {
  return (
    <section className="detail detail-wide" aria-labelledby="tf-h">
      <div className="detail-head">
        <h2 id="tf-h">All timeframes</h2>
        <span className="detail-note">Nearest level per detector, above and below price</span>
      </div>
      <div className="table-scroll">
        <table className="data tf-table">
          <thead>
            <tr>
              <th scope="col">TF</th>
              <th scope="col" className="end">Active</th>
              <th scope="col">Above</th>
              <th scope="col">Below</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const inside = detectors.flatMap((d) => r.detectors[d.name]?.inside ?? []);
              const active = detectors.reduce((n, d) => n + (r.detectors[d.name]?.active_count ?? 0), 0);
              return (
                <tr key={r.timeframe} className={r.timeframe === selected ? "is-selected" : undefined} onClick={() => onSelect(r.timeframe)}>
                  <th scope="row">
                    <button type="button" className="link" onClick={() => onSelect(r.timeframe)}>{r.timeframe}</button>
                    {inside.length > 0 && (
                      <span className="badge accent inside" title={inside.map((lv) => `${lv.label} ${fmtLevelPrice(lv)}`).join(", ")}>
                        In zone
                      </span>
                    )}
                  </th>
                  <td className="end num">{active}</td>
                  <SideCell row={r} detectors={detectors} side="above" price={price} />
                  <SideCell row={r} detectors={detectors} side="below" price={price} />
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
