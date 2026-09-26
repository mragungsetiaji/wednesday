import { fmtAgo, fmtFeedTime } from "../format";
import { TagIcon } from "../icons";

const SOURCES: Record<string, string> = { yfinance: "Yahoo Finance", mt5: "MT5", csv: "CSV", synthetic: "Demo" };

interface Props {
  version: string | undefined;
  status: { cls: string; text: string };
  source: string | undefined;
  scannedAt: string | null;
  barTime: string | null;
  now: number;
}

/** Bottom bar, always on screen: the app version, and the feed's state in the right corner. */
export function StatusBar({ version, status, source, scannedAt, barTime, now }: Props) {
  const detail = [`Scanned ${fmtAgo(scannedAt, now)}`, barTime && `last bar ${fmtFeedTime(barTime)}`].filter(Boolean).join(" · ");
  return (
    <footer className="statusbar">
      <span className="statusbar-item num" title="Wednesday version">
        <TagIcon size={12} />
        {version ? `v${version}` : "—"}
      </span>
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
