import { useEffect } from "react";

import type { SessionsResponse } from "../api";
import { useLiveTick, type LiveFeed } from "../liveData";
import { APP_TITLE, tabTitle } from "../tabTitle";

/** Keeps the browser tab's title on the live price, tick by tick; renders nothing. */
export function TabTitle({ symbol, live, fallback, sessions }: {
  symbol: string; live: LiveFeed | null; fallback: number | null; sessions: SessionsResponse | null;
}) {
  const price = useLiveTick(live)?.price ?? fallback;
  const dayOpen = sessions?.lines.find((l) => l.kind === "day_open" && l.current)?.price ?? null;
  const title = tabTitle(symbol, price, dayOpen);
  useEffect(() => {
    document.title = title;
  }, [title]);
  useEffect(() => () => {
    document.title = APP_TITLE;
  }, []);
  return null;
}
