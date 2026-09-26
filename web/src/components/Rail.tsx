import { detail, fmtPrice, fmtSigned } from "../format";
import type { RailItem } from "../rail";
import { LevelTag } from "./LevelTag";

interface Props {
  items: RailItem[];
  price: number;
  status: { cls: string; text: string };
  hasOb: boolean;
  highlight: string | null;
  onHighlight: (id: string | null) => void;
  onOpen: (tf: string) => void;
}

function Row({ item, highlight, onHighlight, onOpen }: { item: RailItem } & Pick<Props, "highlight" | "onHighlight" | "onOpen">) {
  const lv = item.level;
  const s = item.setup;
  return (
    <li>
      <button
        type="button"
        className={`ladder-row${highlight === item.id ? " is-hot" : ""}${s ? ` setup ${s.side}` : ""}`}
        onMouseEnter={() => onHighlight(item.id)}
        onMouseLeave={() => onHighlight(null)}
        onFocus={() => onHighlight(item.id)}
        onBlur={() => onHighlight(null)}
        onClick={() => onOpen(item.tf)}
        aria-label={`${item.tag} at ${fmtPrice(item.price)}, ${item.tf}. Open ${item.tf} chart`}
      >
        {s ? <span className={`pill ${s.side}`}>{item.tag}</span> : <LevelTag level={lv} />}
        <span className="ladder-price num">{fmtPrice(item.price)}</span>
        <span className="ladder-dist num">{fmtSigned(item.distance)}</span>
        <span className="ladder-meta">
          {s ? (
            <>
              {s.side === "sell" ? "Sell" : "Buy"} limit · {item.tf} · {lv.meta.priority === "extreme" ? "extreme" : "mid"} · SL{" "}
              <span className="num">{fmtPrice(lv.meta.sl!)}</span> · risk <span className="num">{fmtPrice(lv.meta.risk!)}</span>
              {lv.meta.sl_capped && " (capped)"}
              {lv.touches > 0 && ` · tested ${lv.touches}×`}
            </>
          ) : (
            <>
              {item.tf} · {detail(lv)}
            </>
          )}
        </span>
      </button>
    </li>
  );
}

/** Price ladder: everything above price on top, the live price in the middle, everything below under it. */
export function Rail({ items, price, status, hasOb, highlight, onHighlight, onOpen }: Props) {
  const above = items.filter((i) => i.side === "above");
  const below = items.filter((i) => i.side === "below");
  const rowProps = { highlight, onHighlight, onOpen };
  return (
    <aside className="rail" aria-label="Levels around price">
      <div className="rail-head">
        <h2>Levels</h2>
        <p>Hover to find it on the chart. Click to open its timeframe.</p>
      </div>
      <ol className="ladder above" aria-label="Above price">
        {above.map((item) => <Row key={item.id} item={item} {...rowProps} />)}
        {above.length === 0 && (
          <li className="ladder-empty">
            {hasOb ? "No sell setup yet. One appears when a bearish break leaves an untaken green candle above price." : "Nothing above price."}
          </li>
        )}
      </ol>
      <div className="ladder-now" role="status">
        <span className="num">{fmtPrice(price)}</span>
        <span className={`live ${status.cls}`}>
          <span className="dot" aria-hidden="true" />
          {status.text}
        </span>
      </div>
      <ol className="ladder below" aria-label="Below price">
        {below.map((item) => <Row key={item.id} item={item} {...rowProps} />)}
        {below.length === 0 && (
          <li className="ladder-empty">
            {hasOb ? "No buy setup yet. One appears when a bullish break leaves an untaken red candle below price." : "Nothing below price."}
          </li>
        )}
      </ol>
    </aside>
  );
}
