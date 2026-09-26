import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchCandles, fetchScan, type CandlesResponse, type Level, type ScanResponse } from "./api";
import { EventsPanel } from "./components/EventsPanel";
import { NearestPanel } from "./components/NearestPanel";
import { PriceChart } from "./components/PriceChart";
import { SetupCard } from "./components/SetupCard";
import { StructurePanel } from "./components/StructurePanel";
import { TimeframeTable } from "./components/TimeframeTable";
import { fmtAgo, fmtFeedTime, fmtLevelPrice, fmtPrice, roleOf } from "./format";
import { useChartPalette } from "./theme";
import type { Zone } from "./zonesPrimitive";

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

function loadPref<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

function savePref(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: preference just isn't remembered */
  }
}

function toZone(lv: Level, label: string, faded: boolean): Zone {
  return {
    role: roleOf(lv), top: lv.top, bottom: lv.bottom, startTime: lv.time_unix,
    label, faded, strong: Boolean(lv.meta.equal),
    weak: lv.meta.priority === "middle",
    stop: lv.meta.sl_capped ? lv.meta.sl : undefined,
  };
}

/** Chart label: OBs show their limit entry, other levels their price. */
const chartLabel = (tf: string, lv: Level) =>
  lv.detector === "ob" && lv.meta.entry !== undefined
    ? `${tf} ${lv.label} @ ${fmtPrice(lv.meta.entry)}`
    : `${tf} ${lv.label} ${fmtLevelPrice(lv)}`;

export default function App() {
  const palette = useChartPalette();
  const now = useNow(1000);
  const [data, setData] = useState<ScanResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [tf, setTfState] = useState<string>(() => loadPref("xau.tf", "1H"));
  const [showHigherTf, setShowHigherTfState] = useState<boolean>(() => loadPref("xau.otherTf", true));
  const [showMidOb, setShowMidObState] = useState<boolean>(() => loadPref("xau.midOb", true));
  const [hidden, setHiddenState] = useState<string[]>(() => loadPref("xau.hiddenLayers", []));
  const [chart, setChart] = useState<CandlesResponse | null>(null);

  const setTf = useCallback((v: string) => {
    setTfState(v);
    savePref("xau.tf", v);
  }, []);
  const setShowHigherTf = (v: boolean) => {
    setShowHigherTfState(v);
    savePref("xau.otherTf", v);
  };
  const setShowMidOb = (v: boolean) => {
    setShowMidObState(v);
    savePref("xau.midOb", v);
  };
  const toggleLayer = (name: string) => {
    const next = hidden.includes(name) ? hidden.filter((n) => n !== name) : [...hidden, name];
    setHiddenState(next);
    savePref("xau.hiddenLayers", next);
  };

  // Poll the scan; the server rescans once per minute and bumps `version`.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetchScan();
        if (!alive) return;
        setData((prev) => (prev && prev.version === res.version && prev.error === res.error ? prev : res));
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

  const version = data?.version ?? 0;
  const config = data?.config;
  const lookback = config?.lookback ?? 200;
  const timeframes = useMemo(() => config?.timeframes ?? [], [config]);
  const allDetectors = useMemo(() => config?.detectors ?? [], [config]);
  const detectors = useMemo(() => allDetectors.filter((d) => !hidden.includes(d.name)), [allDetectors, hidden]);

  useEffect(() => {
    if (timeframes.length && !timeframes.includes(tf)) setTf(timeframes[0]);
  }, [timeframes, tf, setTf]);

  // Reload candles on a new scan or timeframe switch.
  useEffect(() => {
    if (!version) return;
    let alive = true;
    fetchCandles(tf, lookback)
      .then((res) => alive && setChart(res))
      .catch((e) => alive && setFetchError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [tf, version, lookback]);

  const scan = data?.scan ?? null;

  const zones = useMemo<Zone[]>(() => {
    if (!scan || !chart || chart.timeframe !== tf) return [];
    const shown = new Set(detectors.map((d) => d.name));
    const visible = (lv: Level) => showMidOb || lv.meta.priority !== "middle";
    const own = chart.levels
      .filter((lv) => shown.has(lv.detector) && visible(lv))
      .map((lv) => toZone(lv, chartLabel(tf, lv), false));
    if (!showHigherTf) return own;
    // Only higher timeframes: they are the ones that matter when trading a lower one.
    const idx = scan.timeframes.findIndex((t) => t.timeframe === tf);
    const higher = scan.timeframes.slice(0, Math.max(0, idx)).flatMap((t) =>
      detectors.flatMap((d) => (t.detectors[d.name]?.active ?? []).filter(visible).map((lv) => toZone(lv, `${t.timeframe} ${lv.label}`, true))),
    );
    return [...higher, ...own];
  }, [scan, chart, tf, showHigherTf, showMidOb, detectors]);

  const lastOk = data?.scanned_at ? Date.parse(data.scanned_at) : null;
  const status = fetchError
    ? { cls: "error", text: "API unreachable" }
    : !data
      ? { cls: "idle", text: "Connecting" }
      : data.error
        ? { cls: "error", text: "Feed error" }
        : !lastOk
          ? { cls: "idle", text: "Waiting for first scan" }
          : now - lastOk > STALE_MS
            ? { cls: "warn", text: "Stale" }
            : { cls: "ok", text: "Live" };

  const insideNotes = scan
    ? scan.timeframes.flatMap((t) =>
        detectors.flatMap((d) => (t.detectors[d.name]?.inside ?? []).map((lv) => `${t.timeframe} ${lv.label} ${fmtLevelPrice(lv)}`)),
      )
    : [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="symbol">{data?.symbol ?? "XAUUSD"}</span>
          <span className="muted small">SMC screener</span>
        </div>
        <div className="price num">{scan ? fmtPrice(scan.price) : "—"}</div>
        <div className="layers" role="group" aria-label="Detectors shown">
          {allDetectors.map((d) => (
            <button key={d.name} type="button" className={`chip layer-${d.name}`}
              aria-pressed={!hidden.includes(d.name)} onClick={() => toggleLayer(d.name)}>
              {d.title}
            </button>
          ))}
        </div>
        <div className="status">
          <span className={`pill ${status.cls}`}>
            <span className="dot" aria-hidden="true" />
            {status.text}
          </span>
          <span className="muted small">
            {data?.source && `${data.source} · `}scanned {fmtAgo(data?.scanned_at ?? null, now)}
            {scan && ` · bar ${fmtFeedTime(scan.time)}`}
          </span>
        </div>
      </header>

      {(fetchError || data?.error) && (
        <div className="banner error" role="alert">
          {fetchError ? `Cannot reach the screener API: ${fetchError}` : `Last scan failed: ${data?.error}`}
        </div>
      )}

      {insideNotes.length > 0 && (
        <div className="banner info" role="status">Price is inside {insideNotes.join(" · ")}</div>
      )}

      {scan && config ? (
        <>
          {detectors.some((d) => d.name === "ob") && (
            <div className="setups-row">
              <SetupCard side="sell" setups={scan.setups.sell} maxSl={config.max_sl} onSelect={setTf} />
              <SetupCard side="buy" setups={scan.setups.buy} maxSl={config.max_sl} onSelect={setTf} />
            </div>
          )}

          <div className="nearest-row">
            <NearestPanel side="above" scan={scan} detectors={detectors} onSelect={setTf} />
            <NearestPanel side="below" scan={scan} detectors={detectors} onSelect={setTf} />
          </div>

          <main className="main-grid">
            <div className="left-col">
              <section className="card chart-card">
                <div className="chart-toolbar">
                  <div className="tabs" role="tablist" aria-label="Chart timeframe">
                    {timeframes.map((name) => (
                      <button key={name} type="button" role="tab" aria-selected={name === tf}
                        className={name === tf ? "tab active" : "tab"} onClick={() => setTf(name)}>
                        {name}
                      </button>
                    ))}
                  </div>
                  <div className="toggles">
                    <label className="toggle">
                      <input type="checkbox" checked={showMidOb} onChange={(e) => setShowMidOb(e.target.checked)} />
                      Mid OBs
                    </label>
                    <label className="toggle">
                      <input type="checkbox" checked={showHigherTf} onChange={(e) => setShowHigherTf(e.target.checked)} />
                      Higher TF levels
                    </label>
                  </div>
                </div>
                <PriceChart candles={chart?.timeframe === tf ? chart.candles : []} zones={zones}
                  palette={palette} resetKey={tf} />
                <div className="chart-foot muted small">
                  <span><span className="tag role-bull" /> Bullish OB (buy limit at top)</span>
                  <span><span className="tag role-bear" /> Bearish OB (sell limit at bottom)</span>
                  <span>lighter box = mid OB · dashed line in box = capped SL</span>
                  <span><span className="tag role-liquidity" /> BSL / SSL (thick = EQH/EQL)</span>
                  <span><span className="tag role-idm" /> IDM</span>
                  {showHigherTf && <span>faded / dashed = higher TF</span>}
                  <span>times = broker server time</span>
                </div>
              </section>
              <div className="below-chart">
                <StructurePanel scan={scan} selected={tf} onSelect={setTf} />
                <EventsPanel scan={scan} detectors={detectors} recentBars={config.recent_bars} onSelect={setTf} />
              </div>
            </div>

            <TimeframeTable rows={scan.timeframes} detectors={detectors} price={scan.price} selected={tf} onSelect={setTf} />
          </main>

          <footer className="muted small foot">
            lookback {config.lookback} candles · swing {config.swing_length} · OB {config.zone}, taken by{" "}
            {config.mitigation === "wick" ? "a wick through the body" : "a close beyond the body"} · max SL {config.max_sl} · equal levels ≤ {config.eq_tolerance}×ATR · IDM swing {config.idm_length}
          </footer>
        </>
      ) : (
        <div className="card placeholder">
          {fetchError ? "Start the server: uv run xau-screener --serve" : "Loading M1 history and running the first scan…"}
        </div>
      )}
    </div>
  );
}
