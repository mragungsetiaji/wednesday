import { useEffect, useRef, useState } from "react";

import { fmtPrice } from "../format";
import { ArrowDown, ArrowUp } from "../icons";
import { useLiveTick, type LiveFeed } from "../liveData";

const HOLD_MS = 150; // the flash holds this long, then fades out by CSS transition

/**
 * The live price. Each tick flashes green (up) or red (down) and fades back; the arrow keeps
 * the last direction, so the change still reads without catching the flash.
 */
export function TickerPrice({ price }: { price: number | null }) {
  const prev = useRef<number | null>(null);
  const [dir, setDir] = useState<"up" | "down" | null>(null);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (price === null) return;
    const last = prev.current;
    prev.current = price;
    if (last === null || last === price) return;
    setDir(price > last ? "up" : "down");
    setFlash(true);
    const id = setTimeout(() => setFlash(false), HOLD_MS);
    return () => clearTimeout(id);
  }, [price]);

  if (price === null) return <span className="ticker-price num">—</span>;
  return (
    <span className={`ticker-price num${dir ? ` ${dir}` : ""}${flash ? " is-flash" : ""}`}>
      {fmtPrice(price)}
      <span className="tick-dir">
        {dir === "up" ? <ArrowUp size={13} title="Up since the last tick" /> : dir === "down" ? <ArrowDown size={13} title="Down since the last tick" /> : null}
      </span>
    </span>
  );
}

/** TickerPrice of the live feed, or `fallback` (the last scan's price) before a tick; only it re-renders on a tick. */
export function LiveTickerPrice({ live, fallback }: { live: LiveFeed | null; fallback: number | null }) {
  return <TickerPrice price={useLiveTick(live)?.price ?? fallback} />;
}
