import type { DetectorInfo, NearestLevel, Scan } from "../api";
import { detail, distance, fmtLevelPrice, fmtSigned } from "../format";
import { LevelTag } from "./LevelTag";

interface Props {
  side: "above" | "below";
  scan: Scan;
  detectors: DetectorInfo[]; // enabled layers only
  onSelect: (tf: string) => void;
}

/** Nearest level per detector on one side of price, closest first. */
export function NearestPanel({ side, scan, detectors, onSelect }: Props) {
  const rows = detectors
    .map((d) => scan.nearest[d.name]?.[side])
    .filter((lv): lv is NearestLevel => !!lv)
    .sort((a, b) => Math.abs(distance(a, scan.price)) - Math.abs(distance(b, scan.price)));

  return (
    <section className="card nearest">
      <h2>{side === "above" ? "Nearest above ▲" : "Nearest below ▼"}</h2>
      {rows.length === 0 ? (
        <p className="empty">Nothing active {side} price</p>
      ) : (
        <ul className="nearest-list">
          {rows.map((lv) => (
            <li key={`${lv.detector}-${lv.timeframe}`}>
              <button type="button" onClick={() => onSelect(lv.timeframe)} title={`Show ${lv.timeframe} chart`}>
                <span className="nearest-dist num">{fmtSigned(distance(lv, scan.price))}</span>
                <span className="tf-badge">{lv.timeframe}</span>
                <LevelTag level={lv} />
                <span className="num">{fmtLevelPrice(lv)}</span>
                <span className="muted small nearest-detail">{detail(lv)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
