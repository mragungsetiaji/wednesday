import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchCandles, fetchScan, type CandlesResponse, type ScanResponse } from "./api";
import { NearestCard } from "./components/NearestCard";
import { PriceChart } from "./components/PriceChart";
import { TimeframeTable } from "./components/TimeframeTable";
import { fmtAgo, fmtFeedTime, fmtPrice, kindLabel } from "./format";
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

export default function App() {
  const palette = useChartPalette();
  const now = useNow(1000);
  const [data, setData] = useState<ScanResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [tf, setTfState] = useState<string>(() => loadPref("xau.tf", "1H"));
  const [showOtherTf, setShowOtherTfState] = useState<boolean>(() => loadPref("xau.otherTf", true));
  const [chart, setChart] = useState<CandlesResponse | null>(null);

  const setTf = useCallback((v: string) => {
    setTfState(v);
    savePref("xau.tf", v);
  }, []);
  const setShowOtherTf = (v: boolean) => {
    setShowOtherTfState(v);
    savePref("xau.otherTf", v);
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
  const lookback = data?.config.lookback ?? 200;
  const timeframes = data?.config.timeframes ?? [];
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
    const own: Zone[] = chart.order_blocks.map((ob) => ({
      kind: ob.kind, top: ob.top, bottom: ob.bottom, startTime: ob.time_unix,
      label: `${tf} ${kindLabel(ob)} ${fmtPrice(ob.bottom)}–${fmtPrice(ob.top)}`, faded: false,
    }));
    if (!showOtherTf) return own;
    // Only higher timeframes: they are the ones that matter when trading a lower one.
    const idx = scan.timeframes.findIndex((t) => t.timeframe === tf);
    const higher = scan.timeframes.slice(0, Math.max(0, idx)).flatMap((t) =>
      t.active.map((ob) => ({
        kind: ob.kind, top: ob.top, bottom: ob.bottom, startTime: ob.time_unix,
        label: `${t.timeframe} ${kindLabel(ob)}`, faded: true,
      })),
    );
    return [...higher, ...own];
  }, [scan, chart, tf, showOtherTf]);

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

  const insideTfs = scan?.timeframes.filter((t) => t.inside.length > 0) ?? [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="symbol">{data?.symbol ?? "XAUUSD"}</span>
          <span className="muted small">Order block screener</span>
        </div>
        <div className="price num">{scan ? fmtPrice(scan.price) : "—"}</div>
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

      {insideTfs.length > 0 && scan && (
        <div className="banner info" role="status">
          Price is inside {insideTfs.map((t) =>
            `${t.timeframe} ${t.inside.map((ob) => `${kindLabel(ob)} ${fmtPrice(ob.bottom)}–${fmtPrice(ob.top)}`).join(", ")}`,
          ).join(" · ")}
        </div>
      )}

      {scan ? (
        <>
          <div className="nearest-row">
            <NearestCard title="Nearest above" ob={scan.nearest_above} price={scan.price} onSelect={setTf} />
            <NearestCard title="Nearest below" ob={scan.nearest_below} price={scan.price} onSelect={setTf} />
          </div>

          <main className="main-grid">
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
                <label className="toggle">
                  <input type="checkbox" checked={showOtherTf} onChange={(e) => setShowOtherTf(e.target.checked)} />
                  Higher TF zones
                </label>
              </div>
              <PriceChart candles={chart?.timeframe === tf ? chart.candles : []} zones={zones}
                palette={palette} resetKey={tf} />
              <div className="chart-foot muted small">
                <span><i className="swatch bullish" /> ▲ Bullish OB (demand)</span>
                <span><i className="swatch bearish" /> ▼ Bearish OB (supply)</span>
                {showOtherTf && <span><i className="swatch faded" /> dashed = higher timeframe</span>}
                <span>times = broker server time</span>
              </div>
            </section>

            <TimeframeTable rows={scan.timeframes} price={scan.price} selected={tf} onSelect={setTf} />
          </main>

          <footer className="muted small foot">
            lookback {data!.config.lookback} candles · swing {data!.config.swing_length} · zone{" "}
            {data!.config.zone} · mitigation by {data!.config.mitigation}
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
