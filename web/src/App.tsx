import { startTransition, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { fetchQuarters, fetchScan, fetchSessions, type QuartersResponse, type ScanResponse, type SessionsResponse } from "./api";
import { useCalendar } from "./calendarData";
import { buildEvents, buildZones, quarterRowsFor, useCandles, type LayerOptions } from "./chartData";
import { ChartFocus } from "./components/ChartFocus";
import { ChartPopOut } from "./components/ChartPopOut";
import { PerfOverlay } from "./components/PerfOverlay";
import { LinkMenu, useChartLink } from "./components/LinkMenu";
import { openChartWindow, useDesktopApi } from "./desktop";
import type { LinkGroup } from "./windowLink";
import { DistributionPanel } from "./components/DistributionPanel";
import { Dock } from "./components/Dock";
import { DrawingStyleBar } from "./components/DrawingStyleBar";
import { DrawingToolbar } from "./components/DrawingToolbar";
import { EventsPanel } from "./components/EventsPanel";
import { JournalPage } from "./components/JournalPage";
import { LabPage } from "./components/LabPage";
import { MlPanel } from "./components/MlPanel";
import { NewsAlert } from "./components/NewsAlert";
import { ChartKeys } from "./components/ChartKeys";
import { LevelsMenu } from "./components/LevelsMenu";
import { Toasts } from "./components/Toasts";
import { ChartMarket, PriceChart, type ChartNav } from "./components/PriceChart";
import { QuartersPanel } from "./components/QuartersPanel";
import { Rail } from "./components/Rail";
import { SettingsPage } from "./components/SettingsPage";
import { SOURCES, StatusBar } from "./components/StatusBar";
import { LiveTickerPrice } from "./components/TickerPrice";
import { StructurePanel } from "./components/StructurePanel";
import { TimeframeTable } from "./components/TimeframeTable";
import { fmtPrice } from "./format";
import { BookIcon, ChartIcon, Direction, ExpandIcon, FlaskIcon, PopOutIcon, SlidersIcon } from "./icons";
import { predictionMark } from "./labPrimitive";
import { useMl } from "./mlData";
import { usePref } from "./prefs";
import { newsMarks } from "./newsPrimitive";
import { buildRail } from "./rail";
import { levelGroups, levelView, DEFAULT_LEVELS, NO_LEVELS, type LevelGroup } from "./refLevels";
import { useDrawings } from "./drawingsData";
import { useReplay } from "./replayData";
import { ReplayBar } from "./components/ReplayBar";
import { useLiveFeed } from "./liveData";
import { useChartPalette } from "./theme";

const POLL_MS = 5000;
const STALE_MS = 150_000; // no successful scan for 2.5 minutes

/** Whether `since` (ms) is more than `ms` ago. Flips once when it gets that old, so nothing re-renders every second. */
function useOlderThan(since: number | null, ms: number): boolean {
  const [old, setOld] = useState(false);
  useEffect(() => {
    const left = since === null ? Infinity : since + ms - Date.now();
    setOld(left <= 0);
    if (left <= 0 || left === Infinity) return;
    const id = setTimeout(() => setOld(true), left);
    return () => clearTimeout(id);
  }, [since, ms]);
  return old;
}

type View = "chart" | "journal" | "lab" | "settings" | "popout";
const viewFromHash = (): View => {
  const h = window.location.hash;
  if (h === "#settings" || h.startsWith("#settings/")) return "settings";
  if (h === "#journal") return "journal";
  if (h.startsWith("#popout")) return "popout";
  return h.startsWith("#lab") ? "lab" : "chart";
};

function useView(): [View, (v: View) => void] {
  const [view, setView] = useState<View>(viewFromHash);
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((v: View) => {
    window.location.hash = v === "chart" ? "" : v;
    setView(v);
  }, []);
  return [view, go];
}

const CLOCK_NAMES: Record<string, string> = { UTC: "UTC", "NY+7": "broker server time (New York +7)" };

export default function App() {
  const palette = useChartPalette();
  const [data, setData] = useState<ScanResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [tf, setTf] = usePref("xau.tf", "1H");
  const [showHigherTf, setShowHigherTf] = usePref("xau.otherTf", true);
  const [showMidOb, setShowMidOb] = usePref("xau.midOb", true);
  const [hidden, setHidden] = usePref<string[]>("xau.hiddenLayers", []);
  const [showQuarters, setShowQuarters] = usePref("xau.quarters", true);
  const [showSwings, setShowSwings] = usePref("wed.swings", true);
  const [showNews, setShowNews] = usePref("wed.newsLines", true);
  const [savedLevels, setLevelGroups] = usePref<LevelGroup[]>("wed.refLevels", DEFAULT_LEVELS);
  const levelGroupsOn = useMemo(() => levelGroups(savedLevels), [savedLevels]);
  const [sessions, setSessions] = useState<SessionsResponse | null>(null);
  const calendar = useCalendar();
  const upcomingNews = useMemo(() => calendar?.events ?? [], [calendar]);
  const allNews = useMemo(() => newsMarks(calendar?.week ?? []), [calendar]);
  const news = useMemo(() => (showNews ? allNews : []), [allNews, showNews]);
  const [showMl, setShowMl] = usePref("wed.ml", false);
  const [mlThreshold, setMlThreshold] = usePref("wed.mlCut", 0);
  const [mlHot, setMlHot] = useState<string | null>(null);
  const [focus, setFocus] = useState(false);
  const [quarters, setQuarters] = useState<QuartersResponse | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [view, setView] = useView();

  // Poll the scan; the server rescans once a minute and bumps `version`.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetchScan();
        if (!alive) return;
        const biasKey = (d: ScanResponse | null) => JSON.stringify(d?.trade_bias ?? null);
        // A new scan redraws the zones, the ladder and every chart: not urgent, so a drag or zoom
        // in progress stays smooth while it renders.
        startTransition(() => setData((prev) =>
          prev && prev.version === res.version && prev.error === res.error && biasKey(prev) === biasKey(res) ? prev : res));
        setFetchError(null);
      } catch (e) {
        if (alive) setFetchError(e instanceof Error ? e.message : String(e));
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // After the bias changes, reload at once: the setups' risk labels come from the server.
  const refreshScan = useCallback(() => {
    fetchScan().then((res) => startTransition(() => setData(res))).catch((e) => setFetchError(e instanceof Error ? e.message : String(e)));
  }, []);
  // The bias panel only links to Settings to set up the brief: open that section.
  const openSettings = useCallback(() => {
    window.location.hash = "settings/brief";
  }, []);
  const openLab = useCallback(() => {
    window.location.hash = "lab/models";
  }, []);

  const version = data?.version ?? 0;
  const tradeBias = data?.trade_bias ?? null;
  const config = data?.config;
  const lookback = config?.lookback ?? 200;
  const timeframes = useMemo(() => config?.timeframes ?? [], [config]);
  const allDetectors = useMemo(() => config?.detectors ?? [], [config]);
  const detectors = useMemo(() => allDetectors.filter((d) => !hidden.includes(d.name)), [allDetectors, hidden]);
  const replay = useReplay(tf, lookback);
  const replaying = replay.on && view === "chart";
  const liveScan = data?.scan ?? null;
  // In replay everything on the chart page shows the market at the replay clock, never later.
  const scan = replaying ? replay.data?.scan ?? null : liveScan;
  // The live price stays out of this component's state: a tick re-renders only what shows it.
  const liveFeed = useLiveFeed(data?.tick_seconds ?? 0, data?.version ?? 0, refreshScan);
  const live = replaying ? null : liveFeed;
  const drawings = useDrawings(data?.source, data?.symbol, liveScan?.sizing ?? null, data?.version ?? 0);

  useEffect(() => {
    if (timeframes.length && !timeframes.includes(tf)) setTf(timeframes[0]);
  }, [timeframes, tf, setTf]);

  const [liveChart, loadOlder] = useCandles(tf, version, lookback, setFetchError);
  const chart = replaying ? replay.data : liveChart;
  const [ml, refreshMl] = useMl(tf, version, showMl && view === "chart", mlThreshold, lookback);
  const mlMarks = useMemo(() => (ml?.blocks ?? []).map(predictionMark), [ml]);

  useEffect(() => {
    if (!version) {
      setQuarters(null);
      return;
    }
    let alive = true;
    fetchQuarters()
      .then((res) => alive && setQuarters(res))
      .catch(() => alive && setQuarters(null)); // the pane just stays empty; the scan banner covers feed errors
    return () => {
      alive = false;
    };
  }, [version]);

  // Killzones and reference levels: recomputed by the server once per scan.
  useEffect(() => {
    if (!version) {
      setSessions(null);
      return;
    }
    let alive = true;
    fetchSessions()
      .then((res) => alive && setSessions(res))
      .catch(() => alive && setSessions(null));
    return () => {
      alive = false;
    };
  }, [version]);
  // Sessions, quarters and news come from the live buffer: they'd show the future in replay.
  const levels = useMemo(() => (replaying ? NO_LEVELS : levelView(sessions, levelGroupsOn)), [sessions, levelGroupsOn, replaying]);

  const quarterRows = useMemo(() => (showQuarters && !replaying ? quarterRowsFor(tf) : []), [showQuarters, tf, replaying]);

  const rail = useMemo(() => (scan ? buildRail(scan, detectors) : []), [scan, detectors]);

  const layers = useMemo<LayerOptions>(() => ({ detectors, showHigherTf, showMidOb, showSwings }),
    [detectors, showHigherTf, showMidOb, showSwings]);
  const zones = useMemo(() => (scan && chart ? buildZones(scan, chart, tf, rail, layers) : []), [scan, chart, tf, rail, layers]);
  const events = useMemo(() => (scan ? buildEvents(scan, tf, detectors) : []), [scan, tf, detectors]);
  const closeFocus = useCallback(() => setFocus(false), []);

  const lastOk = data?.scanned_at ? Date.parse(data.scanned_at) : null;
  const stale = useOlderThan(lastOk, STALE_MS);
  const status = fetchError
    ? { cls: "error", text: "Offline" }
    : !data
      ? { cls: "idle", text: "Connecting" }
      : data.conn === "failed"
        ? { cls: "error", text: "Not connected" }
        : data.conn === "reconnecting"
          ? { cls: "warn", text: "Reconnecting" }
          : data.error
            ? { cls: "error", text: "Feed error" }
            : data.conn === "connecting"
              ? { cls: "idle", text: "Connecting" }
              : !lastOk
                ? { cls: "idle", text: "Loading" }
                : data.poll === false
                  ? { cls: "idle", text: "Paused" } // --no-poll: old on purpose, not stale
                  : stale
                    ? { cls: "warn", text: "Stale" }
                    : { cls: "ok", text: "Live" };

  // In replay the chart shows the past: never call it live.
  const chartStatus = replaying ? { cls: "idle", text: "Replay" } : status;
  const toggleLayer = (name: string) => setHidden(hidden.includes(name) ? hidden.filter((n) => n !== name) : [...hidden, name]);
  const hasOb = detectors.some((d) => d.name === "ob");
  const [dockTab, setDockTab] = usePref<string | null>("wed.dockTab", null);
  const eventCount = scan ? scan.timeframes.reduce((n, t) => n + detectors.reduce((m, d) => m + (t.detectors[d.name]?.recent?.length ?? 0), 0), 0) : 0;
  const showBanner = view !== "journal" && !!(fetchError || data?.error);

  // The chart workspace fills the window below the top bar (and banner); the details sit below the fold.
  const appRef = useRef<HTMLDivElement>(null);
  const topbarRef = useRef<HTMLElement>(null);
  const bannerRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const els = [topbarRef.current, bannerRef.current].filter((el): el is HTMLElement => !!el);
    const measure = () => {
      const h = els.reduce((sum, el) => sum + el.getBoundingClientRect().height, 0);
      appRef.current?.style.setProperty("--chrome-h", `${h}px`);
    };
    const resize = new ResizeObserver(measure);
    els.forEach((el) => resize.observe(el));
    measure();
    return () => resize.disconnect();
  }, [showBanner]);

  const mainNav = useRef<ChartNav | null>(null);
  const desktop = useDesktopApi();
  // The main chart's link group: crosshair, scroll and zoom, and timeframe with charts in other windows.
  const [mainGroup, setMainGroup] = usePref<LinkGroup | null>("wed.linkMain", null);
  const tfFromLink = useRef(false);
  const mainLink = useChartLink(view === "chart" && !focus ? mainGroup : null, (t) => {
    tfFromLink.current = true;
    setTf(t);
  });
  const tfShared = useRef(tf);
  useEffect(() => {
    if (tfShared.current === tf) return; // only changes, not the first render
    tfShared.current = tf;
    if (tfFromLink.current) tfFromLink.current = false;
    else mainLink.shareTf(tf);
  }, [tf]); // eslint-disable-line react-hooks/exhaustive-deps
  const market = useMemo(() => ({ pip: data?.pip ?? 0.1, clockOffset: data?.clock_offset ?? 0, symbol: data?.symbol ?? "",
    clockName: data?.clock ?? "UTC" }), [data?.pip, data?.clock_offset, data?.symbol, data?.clock]);

  if (view === "popout") {
    return (
      <ChartMarket.Provider value={market}>
        <div className="app app-popout">
          <ChartPopOut symbol={data?.symbol ?? ""} scan={scan} timeframes={timeframes} version={version} lookback={lookback}
            rail={rail} layers={layers} palette={palette} quarters={quarters} showQuarters={showQuarters} showNews={showNews}
            allDetectors={allDetectors} sessions={sessions} levelGroups={levelGroupsOn} news={news} live={live}
            drawings={drawings} bias={tradeBias} status={status} clockOffset={market.clockOffset} />
          <StatusBar version={data?.app_version} status={status} source={data?.source}
            scannedAt={data?.scanned_at ?? null} barTime={scan?.time ?? null} />
          <NewsAlert events={upcomingNews} />
          <Toasts />
          <PerfOverlay />
        </div>
      </ChartMarket.Provider>
    );
  }

  return (
    <ChartMarket.Provider value={market}>
    <div ref={appRef} className="app">
      <header ref={topbarRef} className="topbar">
        {view === "journal" ? (
          <div className="ticker">
            <span className="brand">Wednesday</span>
            <h1 className="ticker-title">Journal</h1>
          </div>
        ) : (
          <div className="ticker">
            <span className="brand">Wednesday</span>
            <div className="ticker-symbol">
              <h1>{data?.symbol ?? "XAUUSD"}</h1>
              {data?.source && <span className="ticker-source">{SOURCES[data.source] ?? data.source}</span>}
            </div>
            <LiveTickerPrice live={live} fallback={scan?.price ?? null} />
            <span className={`live ${chartStatus.cls}`}>
              <span className="dot" aria-hidden="true" />
              {chartStatus.text}
            </span>
          </div>
        )}
        <nav className="nav" aria-label="Views">
          <a href="#" className="nav-item" aria-current={view === "chart" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("chart"); }}>
            <ChartIcon /> Chart
          </a>
          <a href="#journal" className="nav-item" aria-current={view === "journal" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("journal"); }}>
            <BookIcon /> Journal
          </a>
          <a href="#lab" className="nav-item" aria-current={view === "lab" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("lab"); }}>
            <FlaskIcon /> Lab
          </a>
          <a href="#settings" className="nav-item" aria-current={view === "settings" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("settings"); }}>
            <SlidersIcon /> Settings
          </a>
        </nav>
      </header>

      {showBanner && (
        <div ref={bannerRef} className="banner" role="alert">
          {fetchError ? (
            `Can't reach the screener API (${fetchError}). Start it with: uv run wednesday --serve`
          ) : (
            <>
              {scan ? "The last scan failed" : "No data yet"}: {data?.error?.replace(/[.\s]+$/, "")}.{" "}
              {scan ? "Showing the previous scan; retrying every minute. " : "Retrying every minute. "}
              <a href="#settings" onClick={(e) => { e.preventDefault(); setView("settings"); }}>Change the data source</a>
            </>
          )}
        </div>
      )}

      {view === "settings" ? (
        <SettingsPage />
      ) : view === "lab" ? (
        <LabPage palette={palette} />
      ) : view === "journal" ? (
        <JournalPage palette={palette} />
      ) : (
        <>
          <main className="workspace">
            <section className="chart-area" aria-label="Chart">
              <div className="chart-toolbar">
                <div className="tabs" role="tablist" aria-label="Chart timeframe">
                  {(timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"]).map((name) => {
                    const b = scan?.timeframes.find((t) => t.timeframe === name)?.bias;
                    return (
                      <button key={name} type="button" role="tab" aria-selected={name === tf} className="tab" onClick={() => setTf(name)}
                        title={b ? `${name}: ${b.direction} ${b.event} at ${fmtPrice(b.level)}` : name}>
                        {name}
                        {b && (
                          <span className={`dir ${b.direction}`}>
                            <Direction dir={b.direction} size={12} title={`${b.direction} ${b.event}`} />
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
                <div className="toolbar-layers">
                <div className="toggles" role="group" aria-label="Detectors">
                  {allDetectors.map((d) => (
                    <label key={d.name} className="toggle" title={d.title}>
                      <input type="checkbox" checked={!hidden.includes(d.name)} onChange={() => toggleLayer(d.name)} />
                      {d.title.replace(/\s*\(.*\)$/, "")}
                    </label>
                  ))}
                </div>
                <div className="toggles" role="group" aria-label="Overlays">
                  <label className="toggle">
                    <input type="checkbox" checked={showMidOb} onChange={(e) => setShowMidOb(e.target.checked)} />
                    Mid OBs
                  </label>
                  <label className="toggle" title="Higher timeframes">
                    <input type="checkbox" checked={showHigherTf} onChange={(e) => setShowHigherTf(e.target.checked)} />
                    HTF
                  </label>
                  <label className="toggle">
                    <input type="checkbox" checked={showSwings} onChange={(e) => setShowSwings(e.target.checked)} />
                    Swings
                  </label>
                  <label className="toggle">
                    <input type="checkbox" checked={showQuarters} onChange={(e) => setShowQuarters(e.target.checked)} />
                    Quarters
                  </label>
                  <label className="toggle">
                    <input type="checkbox" checked={showNews} onChange={(e) => setShowNews(e.target.checked)} />
                    News
                  </label>
                  <label className="toggle" title="The active model's blocks, from the Lab">
                    <input type="checkbox" checked={showMl} onChange={(e) => {
                      setShowMl(e.target.checked);
                      if (e.target.checked) setDockTab("ml"); // show the model's blocks to review right away
                    }} />
                    ML
                  </label>
                  <LevelsMenu value={levelGroupsOn} onChange={setLevelGroups} />
                </div>
                </div>
              </div>
              {replaying && <ReplayBar replay={replay} tf={tf} clockName={CLOCK_NAMES[data?.clock ?? ""] ?? data?.clock ?? "feed"} />}
              <div className="chart-body">
              <DrawingToolbar ctl={drawings} />
              <DrawingStyleBar ctl={drawings} />
              <PriceChart
                candles={chart?.candles ?? []}
                zones={zones}
                events={events}
                highlight={highlight}
                palette={palette}
                resetKey={tf}
                loading={!chart}
                onNeedOlder={replaying ? undefined : loadOlder}
                quarters={quarters}
                quarterRows={quarterRows}
                swings={showSwings ? chart?.swings : undefined}
                news={replaying ? undefined : news}
                ml={showMl && !replaying ? mlMarks : undefined}
                mlHighlight={mlHot}
                levels={levels}
                live={live}
                drawings={drawings}
                nav={mainNav}
                sync={mainLink.sync}
                rangeLink={mainLink.rangeLink}
              />
              </div>
              <div className="chart-foot">
                <ul className="legend" aria-label="Chart legend">
                  <li><span className="key key-setup" /> S / B: limit entry, dashed stop</li>
                  <li><span className="key key-bull" /> Bullish OB</li>
                  <li><span className="key key-bear" /> Bearish OB</li>
                  <li><span className="key key-liq" /> BSL / SSL</li>
                  <li><span className="key key-idm" /> IDM</li>
                  <li><span className="key key-bos" /> Last break</li>
                  {showSwings && <li><span className="key key-swing" /> HH / HL / LH / LL swings</li>}
                  {showNews && <li><span className="key key-news" /> High-impact news</li>}
                  {quarterRows.length > 0 && <li><span className="key key-quarter" /> Quarters: green closed up</li>}
                  {showMl && <li><span className="key key-ml" /> Model block (probability)</li>}
                  {levels.killzones.length > 0 && <li><span className="key key-killzone" /> Killzones</li>}
                  {levels.lines.length > 0 && <li><span className="key key-reflevel" /> Session levels, faint once swept</li>}
                  <li className="muted">Times are {CLOCK_NAMES[data?.clock ?? ""] ?? data?.clock ?? "feed time"}</li>
                </ul>
                <div className="chart-foot-tools">
                <button type="button" className={`button quiet replay-toggle${replaying ? " is-on" : ""}`} disabled={!liveScan}
                  aria-pressed={replaying} onClick={() => (replaying ? replay.stop() : replay.start())}
                  title="Bar replay: play the stored history forward candle by candle, without seeing what came next">
                  Replay
                </button>
                <LinkMenu value={mainGroup} onChange={setMainGroup} />
                <button type="button" className="icon-button" onClick={() => openChartWindow(desktop, tf, mainGroup)} disabled={!scan || replaying}
                  title={`Open ${tf} in its own window, for another monitor`} aria-label="Open the chart in its own window">
                  <PopOutIcon size={15} />
                </button>
                <button type="button" className="icon-button" onClick={() => setFocus(true)} disabled={!scan || replaying}
                  title="Full screen: one, two or four charts (F)" aria-label="Full screen charts">
                  <ExpandIcon size={15} />
                </button>
                </div>
              </div>
              {scan && config && (
                <Dock open={dockTab} onOpen={setDockTab} tabs={[
                  {
                    id: "timeframes",
                    label: "All timeframes",
                    content: (
                      <>
                        <TimeframeTable rows={scan.timeframes} detectors={detectors} price={scan.price} selected={tf} onSelect={setTf} />
                        <p className="settings">
                          Lookback {config.lookback} candles, swing {config.swing_length}. OB {config.zone},{" "}
                          {config.mitigation === "wick" ? "taken by a wick through the body" : "taken by a close beyond the body"}, max stop{" "}
                          {config.max_sl}. Equal levels within {config.eq_tolerance}×ATR. IDM swing {config.idm_length}.
                        </p>
                      </>
                    ),
                  },
                  ...(quarters ? [{ id: "quarters", label: "Quarters", content: <QuartersPanel quarters={quarters} /> }] : []),
                  { id: "distribution", label: "Distribution", content: <DistributionPanel version={version} /> },
                  ...(showMl && ml ? [{
                    id: "ml",
                    label: "Model",
                    content: (
                      <MlPanel tf={tf} ml={ml} threshold={mlThreshold} onThreshold={setMlThreshold} onReviewed={refreshMl}
                        onHighlight={setMlHot} onOpenLab={openLab} />
                    ),
                  }] : []),
                ]} />
              )}
            </section>

            {scan ? (
              <Rail items={rail} sizing={scan.sizing} price={scan.price} live={live} status={chartStatus} hasOb={hasOb} highlight={highlight} onHighlight={setHighlight} onOpen={setTf}
                bias={tradeBias} onBiasChanged={refreshScan} onOpenSettings={openSettings}
                panels={config ? [
                  { id: "structure", label: "Structure", content: <StructurePanel scan={scan} selected={tf} onSelect={setTf} /> },
                  {
                    id: "events",
                    label: "Events",
                    badge: eventCount,
                    content: <EventsPanel scan={scan} detectors={detectors} recentBars={config.recent_bars} onSelect={setTf} />,
                  },
                ] : []} />
            ) : (
              <aside className="rail" aria-busy="true">
                <div className="rail-head">
                  <h2>Levels</h2>
                  <p>
                {fetchError
                  ? "Waiting for the screener API."
                  : data?.error
                    ? "The data feed isn't delivering bars yet. See the message above."
                    : "Loading M1 history and running the first scan."}
              </p>
                </div>
                <div className="skeleton-rows" aria-hidden="true">
                  {Array.from({ length: 6 }, (_, i) => <span key={i} />)}
                </div>
              </aside>
            )}
          </main>
        </>
      )}

      <StatusBar version={data?.app_version} status={status} source={data?.source}
        scannedAt={data?.scanned_at ?? null} barTime={scan?.time ?? null} />

      <ChartKeys active={view === "chart" && !focus} timeframes={timeframes} setTf={setTf} nav={() => mainNav.current}
        setTool={drawings.available ? drawings.setTool : null} toggleFullScreen={() => scan && !replaying && setFocus(true)}
        clockOffset={market.clockOffset} />

      {/* Full screen renders its own copy: the browser only shows the full screen element. */}
      {!(focus && scan && data) && <NewsAlert events={upcomingNews} />}
      {!focus && <Toasts />}
      <PerfOverlay />

      {focus && scan && data && (
        <ChartFocus
          symbol={data.symbol}
          scan={scan}
          timeframes={timeframes}
          version={version}
          lookback={lookback}
          rail={rail}
          layers={layers}
          palette={palette}
          quarters={quarters}
          showQuarters={showQuarters}
          showNews={showNews}
          allDetectors={allDetectors}
          sessions={sessions}
          levelGroups={levelGroupsOn}
          onLevelGroups={setLevelGroups}
          toggles={[
            { label: "Mid OBs", checked: showMidOb, onChange: setShowMidOb },
            { label: "HTF", title: "Higher timeframes", checked: showHigherTf, onChange: setShowHigherTf },
            { label: "Swings", checked: showSwings, onChange: setShowSwings },
            { label: "Quarters", checked: showQuarters, onChange: setShowQuarters },
            { label: "News", checked: showNews, onChange: setShowNews },
          ]}
          tf={tf}
          onTf={setTf}
          status={status}
          bias={tradeBias}
          news={allNews}
          live={live}
          drawings={drawings}
          upcomingNews={upcomingNews}
          onClose={closeFocus}
        />
      )}
    </div>
    </ChartMarket.Provider>
  );
}
