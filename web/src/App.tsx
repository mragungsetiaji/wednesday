import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchQuarters, fetchScan, type QuartersResponse, type ScanResponse } from "./api";
import { buildEvents, buildZones, quarterRowsFor, useCandles, type LayerOptions } from "./chartData";
import { ChartFocus } from "./components/ChartFocus";
import { EventsPanel } from "./components/EventsPanel";
import { PriceChart } from "./components/PriceChart";
import { QuartersPanel } from "./components/QuartersPanel";
import { Rail } from "./components/Rail";
import { SettingsPage } from "./components/SettingsPage";
import { StructurePanel } from "./components/StructurePanel";
import { TimeframeTable } from "./components/TimeframeTable";
import { fmtAgo, fmtFeedTime, fmtPrice } from "./format";
import { ChartIcon, Direction, ExpandIcon, SlidersIcon } from "./icons";
import { usePref } from "./prefs";
import { buildRail } from "./rail";
import { useChartPalette } from "./theme";

const POLL_MS = 5000;
const STALE_MS = 150_000; // no successful scan for 2.5 minutes

function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

type View = "chart" | "settings";
const viewFromHash = (): View => (window.location.hash === "#settings" ? "settings" : "chart");

function useView(): [View, (v: View) => void] {
  const [view, setView] = useState<View>(viewFromHash);
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((v: View) => {
    window.location.hash = v === "settings" ? "settings" : "";
    setView(v);
  }, []);
  return [view, go];
}

const CLOCK_NAMES: Record<string, string> = { UTC: "UTC", "NY+7": "broker server time (New York +7)" };

export default function App() {
  const palette = useChartPalette();
  const now = useNow(1000);
  const [data, setData] = useState<ScanResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [tf, setTf] = usePref("xau.tf", "1H");
  const [showHigherTf, setShowHigherTf] = usePref("xau.otherTf", true);
  const [showMidOb, setShowMidOb] = usePref("xau.midOb", true);
  const [hidden, setHidden] = usePref<string[]>("xau.hiddenLayers", []);
  const [showQuarters, setShowQuarters] = usePref("xau.quarters", true);
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
        setData((prev) =>
          prev && prev.version === res.version && prev.error === res.error && biasKey(prev) === biasKey(res) ? prev : res);
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
    fetchScan().then(setData).catch((e) => setFetchError(e instanceof Error ? e.message : String(e)));
  }, []);
  const openSettings = useCallback(() => setView("settings"), [setView]);

  const version = data?.version ?? 0;
  const tradeBias = data?.trade_bias ?? null;
  const config = data?.config;
  const lookback = config?.lookback ?? 200;
  const timeframes = useMemo(() => config?.timeframes ?? [], [config]);
  const allDetectors = useMemo(() => config?.detectors ?? [], [config]);
  const detectors = useMemo(() => allDetectors.filter((d) => !hidden.includes(d.name)), [allDetectors, hidden]);
  const scan = data?.scan ?? null;

  useEffect(() => {
    if (timeframes.length && !timeframes.includes(tf)) setTf(timeframes[0]);
  }, [timeframes, tf, setTf]);

  const chart = useCandles(tf, version, lookback, setFetchError);

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

  const quarterRows = useMemo(() => (showQuarters ? quarterRowsFor(tf) : []), [showQuarters, tf]);

  const rail = useMemo(() => (scan ? buildRail(scan, detectors) : []), [scan, detectors]);

  const layers = useMemo<LayerOptions>(() => ({ detectors, showHigherTf, showMidOb }), [detectors, showHigherTf, showMidOb]);
  const zones = useMemo(() => (scan && chart ? buildZones(scan, chart, tf, rail, layers) : []), [scan, chart, tf, rail, layers]);
  const events = useMemo(() => (scan ? buildEvents(scan, tf, detectors) : []), [scan, tf, detectors]);
  const closeFocus = useCallback(() => setFocus(false), []);

  const lastOk = data?.scanned_at ? Date.parse(data.scanned_at) : null;
  const status = fetchError
    ? { cls: "error", text: "Offline" }
    : !data
      ? { cls: "idle", text: "Connecting" }
      : data.error
        ? { cls: "error", text: "Feed error" }
        : !lastOk
          ? { cls: "idle", text: "Loading" }
          : now - lastOk > STALE_MS
            ? { cls: "warn", text: "Stale" }
            : { cls: "ok", text: "Live" };

  const toggleLayer = (name: string) => setHidden(hidden.includes(name) ? hidden.filter((n) => n !== name) : [...hidden, name]);
  const hasOb = detectors.some((d) => d.name === "ob");

  return (
    <div className="app">
      <header className="topbar">
        <div className="ticker">
          <span className="brand">Wednesday</span>
          <h1>{data?.symbol ?? "XAUUSD"}</h1>
          <span className="ticker-price num">{scan ? fmtPrice(scan.price) : "—"}</span>
        </div>
        <p className="meta">
          <span className={`live ${status.cls}`}>
            <span className="dot" aria-hidden="true" />
            {status.text}
          </span>
          {data?.source && <span>{data.source}</span>}
          <span>scanned {fmtAgo(data?.scanned_at ?? null, now)}</span>
          {scan && <span>bar {fmtFeedTime(scan.time)}</span>}
        </p>
        {view === "chart" && (
          <div className="layers" role="group" aria-label="Detectors">
            {allDetectors.map((d) => (
              <button key={d.name} type="button" className="chip" aria-pressed={!hidden.includes(d.name)} onClick={() => toggleLayer(d.name)}>
                {d.title}
              </button>
            ))}
          </div>
        )}
        <nav className="nav" aria-label="Views">
          <a href="#" className="nav-item" aria-current={view === "chart" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("chart"); }}>
            <ChartIcon /> Chart
          </a>
          <a href="#settings" className="nav-item" aria-current={view === "settings" ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setView("settings"); }}>
            <SlidersIcon /> Settings
          </a>
        </nav>
      </header>

      {(fetchError || data?.error) && (
        <div className="banner" role="alert">
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
                <div className="toggles">
                  <label className="toggle">
                    <input type="checkbox" checked={showMidOb} onChange={(e) => setShowMidOb(e.target.checked)} />
                    Mid OBs
                  </label>
                  <label className="toggle">
                    <input type="checkbox" checked={showHigherTf} onChange={(e) => setShowHigherTf(e.target.checked)} />
                    Higher timeframes
                  </label>
                  <label className="toggle">
                    <input type="checkbox" checked={showQuarters} onChange={(e) => setShowQuarters(e.target.checked)} />
                    Quarters
                  </label>
                </div>
              </div>
              <PriceChart
                candles={chart?.candles ?? []}
                zones={zones}
                events={events}
                highlight={highlight}
                palette={palette}
                resetKey={tf}
                loading={!chart}
                quarters={quarters}
                quarterRows={quarterRows}
              />
              <div className="chart-foot">
                <ul className="legend" aria-label="Chart legend">
                  <li><span className="key key-setup" /> S / B: limit entry, dashed stop</li>
                  <li><span className="key key-bull" /> Bullish OB</li>
                  <li><span className="key key-bear" /> Bearish OB</li>
                  <li><span className="key key-liq" /> BSL / SSL</li>
                  <li><span className="key key-idm" /> IDM</li>
                  <li><span className="key key-bos" /> Last break</li>
                  {quarterRows.length > 0 && <li><span className="key key-quarter" /> Quarters: green closed up</li>}
                  <li className="muted">Times are {CLOCK_NAMES[data?.clock ?? ""] ?? data?.clock ?? "feed time"}</li>
                </ul>
                <button type="button" className="icon-button" onClick={() => setFocus(true)} disabled={!scan}
                  title="Full screen: one, two or four charts" aria-label="Full screen charts">
                  <ExpandIcon size={15} />
                </button>
              </div>
            </section>

            {scan ? (
              <Rail items={rail} price={scan.price} status={status} hasOb={hasOb} highlight={highlight} onHighlight={setHighlight} onOpen={setTf}
                bias={tradeBias} onBiasChanged={refreshScan} onOpenSettings={openSettings} />
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

          {scan && config && (
            <div className="details">
              <StructurePanel scan={scan} selected={tf} onSelect={setTf} />
              <EventsPanel scan={scan} detectors={detectors} recentBars={config.recent_bars} onSelect={setTf} />
              {quarters && <QuartersPanel quarters={quarters} />}
              <TimeframeTable rows={scan.timeframes} detectors={detectors} price={scan.price} selected={tf} onSelect={setTf} />
              <p className="settings">
                Lookback {config.lookback} candles, swing {config.swing_length}. OB {config.zone},{" "}
                {config.mitigation === "wick" ? "taken by a wick through the body" : "taken by a close beyond the body"}, max stop{" "}
                {config.max_sl}. Equal levels within {config.eq_tolerance}×ATR. IDM swing {config.idm_length}.
              </p>
            </div>
          )}
        </>
      )}

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
          toggles={[
            { label: "Mid OBs", checked: showMidOb, onChange: setShowMidOb },
            { label: "Higher timeframes", checked: showHigherTf, onChange: setShowHigherTf },
            { label: "Quarters", checked: showQuarters, onChange: setShowQuarters },
          ]}
          tf={tf}
          onTf={setTf}
          status={status}
          bias={tradeBias}
          onClose={closeFocus}
        />
      )}
    </div>
  );
}
