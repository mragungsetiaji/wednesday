import type { DetectorInfo, Level, TimeframeScan } from "../api";
import { detail, distance, fmtLevelPrice, fmtSigned } from "../format";
import { LevelTag } from "./LevelTag";

interface Props {
  rows: TimeframeScan[];
  detectors: DetectorInfo[]; // enabled layers only
  price: number;
  selected: string;
  onSelect: (tf: string) => void;
}

function LevelLine({ lv, price }: { lv: Level; price: number }) {
  return (
    <div className="level-line">
      <LevelTag level={lv} />
      <span className="num">{fmtLevelPrice(lv)}</span>
      <span className="num muted">{fmtSigned(distance(lv, price))}</span>
      <span className="muted small">{detail(lv)}</span>
    </div>
  );
}

function SideCell({ row, detectors, side, price }: { row: TimeframeScan; detectors: DetectorInfo[]; side: "above" | "below"; price: number }) {
  const levels = detectors.map((d) => row.detectors[d.name]?.[side]).filter((lv): lv is Level => !!lv);
  if (!levels.length) return <td className="muted">—</td>;
  levels.sort((a, b) => Math.abs(distance(a, price)) - Math.abs(distance(b, price)));
  return <td>{levels.map((lv) => <LevelLine key={lv.detector} lv={lv} price={price} />)}</td>;
}

/** One row per timeframe, highest first: the scan order. */
export function TimeframeTable({ rows, detectors, price, selected, onSelect }: Props) {
  return (
    <section className="card table-card">
      <h2>Timeframes <span className="muted small">high → low</span></h2>
      <div className="table-scroll">
        <table className="tf-table">
          <thead>
            <tr>
              <th scope="col">TF</th>
              <th scope="col">Nearest above</th>
              <th scope="col">Nearest below</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const inside = detectors.flatMap((d) => r.detectors[d.name]?.inside ?? []);
              const active = detectors.reduce((n, d) => n + (r.detectors[d.name]?.active_count ?? 0), 0);
              return (
                <tr key={r.timeframe} className={r.timeframe === selected ? "selected" : undefined}
                  onClick={() => onSelect(r.timeframe)}>
                  <th scope="row">
                    <button type="button" className="tf-badge" aria-pressed={r.timeframe === selected}>
                      {r.timeframe}
                    </button>
                    <span className="muted small tf-count" title="Active levels on this timeframe">{active} active</span>
                    {inside.length > 0 && (
                      <span className="inside-tag" title={inside.map((lv) => `${lv.label} ${fmtLevelPrice(lv)}`).join(", ")}>
                        in zone
                      </span>
                    )}
                  </th>
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
