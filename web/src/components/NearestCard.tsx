import type { NearestOB } from "../api";
import { distance, fmtPrice, fmtSigned, kindArrow, kindLabel } from "../format";

interface Props {
  title: string;
  ob: NearestOB | null;
  price: number;
  onSelect: (tf: string) => void;
}

export function NearestCard({ title, ob, price, onSelect }: Props) {
  if (!ob) {
    return (
      <section className="card nearest">
        <h2>{title}</h2>
        <p className="empty">No active order block</p>
      </section>
    );
  }
  return (
    <button type="button" className={`card nearest ${ob.kind}`} onClick={() => onSelect(ob.timeframe)}
      title={`Show ${ob.timeframe} chart`}>
      <h2>{title}</h2>
      <div className="nearest-dist">{fmtSigned(distance(ob, price))}</div>
      <div className="nearest-zone">
        <span className="tf-badge">{ob.timeframe}</span>
        <span className={`kind ${ob.kind}`}>{kindArrow(ob)} {kindLabel(ob)}</span>
        <span className="num">{fmtPrice(ob.bottom)} – {fmtPrice(ob.top)}</span>
      </div>
      <div className="muted small">
        {ob.touches === 0 ? "untested" : `tested ${ob.touches}×`} · formed {ob.time.replace("T", " ").slice(0, 16)}
      </div>
    </button>
  );
}
