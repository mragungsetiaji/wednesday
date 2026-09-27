import { useEffect, useState } from "react";

import { fetchLicence, LICENCE_EVENT } from "../api";
import { fmtAgo, fmtFeedTime } from "../format";
import { TagIcon } from "../icons";

export const SOURCES: Record<string, string> = { yfinance: "Yahoo Finance", mt5: "MT5", csv: "CSV", synthetic: "Demo" };

interface Props {
  version: string | undefined;
  status: { cls: string; text: string };
  source: string | undefined;
  scannedAt: string | null;
  barTime: string | null;
}

/** The plan the licence gives ("PRO"), or null on the free version. Rechecked every few minutes. */
function usePlan(): string | null {
  const [plan, setPlan] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchLicence()
        .then((l) => alive && setPlan(l.available && l.valid && l.plan ? l.plan : null))
        .catch(() => {});
    load();
    const timer = window.setInterval(load, 5 * 60_000);
    window.addEventListener(LICENCE_EVENT, load);
    return () => {
      alive = false;
      window.clearInterval(timer);
      window.removeEventListener(LICENCE_EVENT, load);
    };
  }, []);
  return plan;
}

/** Bottom bar, always on screen: the app version and plan, and the feed's state in the right corner. */
/** The time now, every `ms`: here, not in the app, so only the status bar re-renders each second. */
function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

export function StatusBar({ version, status, source, scannedAt, barTime }: Props) {
  const plan = usePlan();
  const now = useNow(1000);
  const detail = [`Scanned ${fmtAgo(scannedAt, now)}`, barTime && `last bar ${fmtFeedTime(barTime)}`].filter(Boolean).join(" · ");
  return (
    <footer className="statusbar">
      <span className="statusbar-item num" title="Wednesday version">
        <TagIcon size={12} />
        {version ? `v${version}` : "—"}
      </span>
      <a href="#settings/plan" className={`statusbar-item statusbar-plan badge${plan ? " accent" : ""}`}
        title={plan ? "Your plan" : "Free version; see Settings > Plan"}>
        {plan ? plan.toUpperCase() : "FREE"}
      </a>
      {/* Lightweight Charts' license asks for its notice and a link to TradingView where users see the charts. */}
      <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer" className="statusbar-item statusbar-credit"
        title="The charts are drawn with TradingView Lightweight Charts™ (Apache License 2.0); see THIRD_PARTY_NOTICES">
        TradingView Lightweight Charts™ · © 2025 TradingView, Inc.
      </a>
      <span className="statusbar-feed" title={detail}>
        {source && <span className="statusbar-item">{SOURCES[source] ?? source}</span>}
        <span className={`statusbar-item live ${status.cls}`} role="status">
          <span className="dot" aria-hidden="true" />
          {status.text}
        </span>
      </span>
    </footer>
  );
}
