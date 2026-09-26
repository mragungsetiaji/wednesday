import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchCandles, fetchScan, type CandlesResponse, type Level, type ScanResponse } from "./api";
import { EventsPanel } from "./components/EventsPanel";
import { PriceChart, type ChartEvent } from "./components/PriceChart";
import { Rail } from "./components/Rail";
import { StructurePanel } from "./components/StructurePanel";
import { TimeframeTable } from "./components/TimeframeTable";
import { fmtAgo, fmtFeedTime, fmtPrice } from "./format";
import { Direction } from "./icons";
import { buildRail, contextZone, levelKey, railZone } from "./rail";
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
    /* storage unavailable: the preference just isn't remembered */
  }
}

function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => loadPref(key, fallback));
  const set = useCallback((v: T) => {
    setValue(v);
    savePref(key, v);
  }, [key]);
  return [value, set];
}

/** Which side of the bar an event marker goes: sweeps of highs above, of lows below. */
function eventAbove(lv: Level): boolean {
  if (lv.detector === "liquidity") return lv.kind === "bsl";
  return lv.kind === "bearish";
}

function eventText(lv: Level): string {
  if (lv.detector === "ob") return `${lv.kind === "bullish" ? "Bull" : "Bear"} OB taken`;
  const what = lv.detector === "liquidity" ? lv.kind.toUpperCase() : "IDM";
  return `${what} ${lv.meta.grab ? "grab" : "break"}`;
}

export default function App() {
  const palette = useChartPalette();
  const now = useNow(1000);
  const [data, setData] = useState<ScanResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [tf, setTf] = usePref("xau.tf", "1H");
  const [showHigherTf, setShowHigherTf] = usePref("xau.otherTf", true);
  const [showMidOb, setShowMidOb] = usePref("xau.midOb", true);
  const [hidden, setHidden] = usePref<string[]>("xau.hiddenLayers", []);
  const [chart, setChart] = useState<CandlesResponse | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);

  // Poll the scan; the server rescans once a minute and bumps `version`.
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
  const scan = data?.scan ?? null;

  useEffect(() => {
    if (timeframes.length && !timeframes.includes(tf)) setTf(timeframes[0]);
  }, [timeframes, tf, setTf]);

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

  const rail = useMemo(() => (scan ? buildRail(scan, detectors) : []), [scan, detectors]);
  const tfScan = scan?.timeframes.find((t) => t.timeframe === tf) ?? null;

  const zones = useMemo<Zone[]>(() => {
    if (!scan || !chart || chart.timeframe !== tf) return [];
    const shown = new Set(detectors.map((d) => d.name));
    const pinned = new Set(rail.map((i) => levelKey(i.tf, i.level)));
    const keep = (t: string, lv: Level) =>
      shown.has(lv.detector) && (showMidOb || lv.meta.priority !== "middle") && !pinned.has(levelKey(t, lv));

    const own = chart.levels.filter((lv) => keep(tf, lv)).map((lv) => contextZone(tf, lv, false));
    const idx = scan.timeframes.findIndex((t) => t.timeframe === tf);
    const higher = showHigherTf
      ? scan.timeframes.slice(0, Math.max(0, idx)).flatMap((t) =>
          detectors.flatMap((d) =>
            (t.detectors[d.name]?.active ?? []).filter((lv) => keep(t.timeframe, lv)).map((lv) => contextZone(t.timeframe, lv, true)),
          ),
        )
      : [];
    const bias = tfScan?.bias;
    const structure: Zone[] = bias
      ? [{
          id: `bos-${tf}`, role: "structure", mark: "segment", top: bias.level, bottom: bias.level,
          startTime: bias.swing_time_unix, endTime: bias.break_time_unix, label: bias.event,
        }]
      : [];
    return [...higher, ...own, ...structure, ...rail.map(railZone)];
  }, [scan, chart, tf, tfScan, showHigherTf, showMidOb, detectors, rail]);

  const events = useMemo<ChartEvent[]>(() => {
    if (!tfScan) return [];
    return detectors.flatMap((d) =>
      (tfScan.detectors[d.name]?.recent ?? [])
        .filter((lv) => lv.ended_time_unix !== null)
        .map((lv) => ({ time: lv.ended_time_unix!, above: eventAbove(lv), text: eventText(lv) })),
    );
  }, [tfScan, detectors]);

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
        <div className="layers" role="group" aria-label="Detectors">
          {allDetectors.map((d) => (
            <button key={d.name} type="button" className="chip" aria-pressed={!hidden.includes(d.name)} onClick={() => toggleLayer(d.name)}>
              {d.title}
            </button>
          ))}
        </div>
      </header>

      {(fetchError || data?.error) && (
        <div className="banner" role="alert">
          {fetchError
            ? `Can't reach the screener API (${fetchError}). Start it with: uv run xau-screener --serve`
            : `The last scan failed: ${data?.error}. Showing the previous scan; retrying every minute.`}
        </div>
      )}

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
            </div>
          </div>
          <PriceChart
            candles={chart?.timeframe === tf ? chart.candles : []}
            zones={zones}
            events={events}
            highlight={highlight}
            palette={palette}
            resetKey={tf}
            loading={!chart || chart.timeframe !== tf}
          />
          <ul className="legend" aria-label="Chart legend">
            <li><span className="key key-setup" /> S / B: limit entry, dashed stop</li>
            <li><span className="key key-bull" /> Bullish OB</li>
            <li><span className="key key-bear" /> Bearish OB</li>
            <li><span className="key key-liq" /> BSL / SSL</li>
            <li><span className="key key-idm" /> IDM</li>
            <li><span className="key key-bos" /> Last break</li>
            <li className="muted">Times are broker server time</li>
          </ul>
        </section>

        {scan ? (
          <Rail items={rail} price={scan.price} status={status} hasOb={hasOb} highlight={highlight} onHighlight={setHighlight} onOpen={setTf} />
        ) : (
          <aside className="rail" aria-busy="true">
            <div className="rail-head">
              <h2>Levels</h2>
              <p>{fetchError ? "Waiting for the screener API." : "Loading M1 history and running the first scan."}</p>
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
          <TimeframeTable rows={scan.timeframes} detectors={detectors} price={scan.price} selected={tf} onSelect={setTf} />
          <p className="settings">
            Lookback {config.lookback} candles, swing {config.swing_length}. OB {config.zone},{" "}
            {config.mitigation === "wick" ? "taken by a wick through the body" : "taken by a close beyond the body"}, max stop{" "}
            {config.max_sl}. Equal levels within {config.eq_tolerance}×ATR. IDM swing {config.idm_length}.
          </p>
        </div>
      )}
    </div>
  );
}
