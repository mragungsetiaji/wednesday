import { useEffect, useRef, useState } from "react";

import type { DetectorInfo, QuartersResponse, Scan, SessionsResponse, TradeBias } from "../api";
import type { LayerOptions } from "../chartData";
import type { Drawings } from "../drawingsData";
import { popoutHash } from "../desktop";
import type { LiveFeed } from "../liveData";
import type { NewsMark } from "../newsPrimitive";
import type { RailItem } from "../rail";
import type { LevelGroup } from "../refLevels";
import type { ChartPalette } from "../theme";
import { LINK_GROUPS, type LinkGroup } from "../windowLink";
import { BiasPill } from "./BiasPill";
import { ChartKeys } from "./ChartKeys";
import { ChartPane } from "./ChartPane";
import type { ChartNav } from "./PriceChart";
import { DrawingStyleBar } from "./DrawingStyleBar";
import { DrawingToolbar } from "./DrawingToolbar";
import { LinkMenu, useChartLink } from "./LinkMenu";
import { LiveTickerPrice } from "./TickerPrice";

/** `#popout/5M/A` → the timeframe and link group a popped-out window opens with. */
export function parsePopout(hash: string): { tf: string | null; group: LinkGroup | null } {
  const [, tf, group] = hash.replace(/^#/, "").split("/");
  return {
    tf: tf ? decodeURIComponent(tf) : null,
    group: LINK_GROUPS.includes(group as LinkGroup) ? (group as LinkGroup) : null,
  };
}

interface Props {
  symbol: string;
  scan: Scan | null;
  timeframes: string[];
  version: number;
  lookback: number;
  rail: RailItem[];
  layers: LayerOptions;
  palette: ChartPalette;
  quarters: QuartersResponse | null;
  showQuarters: boolean;
  showNews: boolean;
  allDetectors: DetectorInfo[];
  sessions: SessionsResponse | null;
  levelGroups: LevelGroup[];
  news: NewsMark[];
  live: LiveFeed | null;
  drawings: Drawings;
  bias: TradeBias | null;
  status: { cls: string; text: string };
  clockOffset: number;
}

/**
 * A chart in a window of its own (another monitor, say): timeframe tabs, the live price, the bias,
 * drawings and a link group. No dock and no rail. The layers follow the main window's toggles.
 */
export function ChartPopOut({ symbol, scan, timeframes, version, lookback, rail, layers, palette, quarters, showQuarters, showNews,
  allDetectors, sessions, levelGroups, news, live, drawings, bias, status, clockOffset }: Props) {
  const start = parsePopout(window.location.hash);
  const known = timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"];
  const [tf, setTfState] = useState(start.tf ?? known[0]);
  const [group, setGroupState] = useState<LinkGroup | null>(start.group);
  const nav = useRef<ChartNav | null>(null);
  const remember = (t: string, g: LinkGroup | null) => window.history.replaceState(null, "", popoutHash(t, g));
  const link = useChartLink(group, (t) => {
    setTfState(t);
    remember(t, group);
  });
  const setTf = (t: string) => {
    setTfState(t);
    remember(t, group);
    link.shareTf(t);
  };
  const setGroup = (g: LinkGroup | null) => {
    setGroupState(g);
    remember(tf, g);
  };

  useEffect(() => {
    document.title = `${symbol || "XAUUSD"} ${tf} · Wednesday`;
  }, [symbol, tf]);

  return (
    <div className="popout">
      <header className="popout-bar">
        <div className="ticker">
          <span className="brand">Wednesday</span>
          <span className="focus-symbol">{symbol}</span>
          <LiveTickerPrice live={live} fallback={scan?.price ?? null} />
          {bias && <BiasPill bias={bias} />}
          <span className={`live ${status.cls}`}>
            <span className="dot" aria-hidden="true" />
            {status.text}
          </span>
        </div>
        <div className="popout-tools">
          <DrawingToolbar ctl={drawings} row />
          <LinkMenu value={group} onChange={setGroup} />
        </div>
      </header>
      <div className="popout-chart">
        {scan ? (
          <ChartPane label="Chart" tf={tf} onTf={setTf} timeframes={known} scan={scan} version={version} lookback={lookback}
            rail={rail} layers={layers} palette={palette} quarters={quarters} showQuarters={showQuarters} showNews={showNews}
            allDetectors={allDetectors} sessions={sessions} levelGroups={levelGroups} news={news} live={live} drawings={drawings}
            sync={link.sync} rangeLink={link.rangeLink} nav={(h) => { nav.current = h; }} />
        ) : (
          <p className="empty">{status.text === "Offline" ? "Can't reach the screener API." : "Loading the scan…"}</p>
        )}
      </div>
      <DrawingStyleBar ctl={drawings} />
      <ChartKeys active timeframes={known} setTf={setTf} nav={() => nav.current}
        setTool={drawings.available ? drawings.setTool : null} toggleFullScreen={() => {}} clockOffset={clockOffset} />
    </div>
  );
}
