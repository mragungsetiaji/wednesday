import { useEffect, useRef } from "react";

import type { QuartersResponse, Scan } from "../api";
import type { LayerOptions } from "../chartData";
import { fmtPrice } from "../format";
import { CollapseIcon, LayoutIcon } from "../icons";
import { usePref } from "../prefs";
import type { RailItem } from "../rail";
import type { ChartPalette } from "../theme";
import { ChartPane } from "./ChartPane";

type Layout = 1 | 2 | 4;
const LAYOUTS: { panes: Layout; title: string }[] = [
  { panes: 1, title: "One chart" },
  { panes: 2, title: "Two charts, side by side" },
  { panes: 4, title: "Four charts, 2 × 2" },
];
const DEFAULT_TFS = ["1H", "15M", "4H", "5M"];

interface Toggle {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}

interface Props {
  symbol: string;
  scan: Scan;
  timeframes: string[];
  version: number;
  lookback: number;
  rail: RailItem[];
  layers: LayerOptions;
  palette: ChartPalette;
  quarters: QuartersResponse | null;
  showQuarters: boolean;
  toggles: Toggle[];
  tf: string; // the first chart follows the dashboard's timeframe
  onTf: (tf: string) => void;
  status: { cls: string; text: string };
  onClose: () => void;
}

/**
 * Full-screen charts, TradingView style: one, two side by side or four in a
 * grid, each with its own timeframe. Uses the browser's full screen when it is
 * allowed; leaving it (Esc) closes the view.
 */
export function ChartFocus({ symbol, scan, timeframes, version, lookback, rail, layers, palette, quarters, showQuarters, toggles, tf, onTf, status, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = usePref<Layout>("wed.focusLayout", 2);
  const [tfs, setTfs] = usePref<string[]>("wed.focusTfs", DEFAULT_TFS);

  useEffect(() => {
    const el = ref.current;
    let entered = false;
    const onChange = () => {
      if (document.fullscreenElement) entered = true;
      else if (entered) onClose(); // Esc (or the browser's own exit) leaves the view too
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(); // browsers usually take Esc to leave full screen first; this covers the rest
    };
    document.addEventListener("fullscreenchange", onChange);
    window.addEventListener("keydown", onKey);
    el?.requestFullscreen?.().catch(() => {
      /* not allowed (e.g. in a frame): the view still fills the window */
    });
    el?.focus();
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      window.removeEventListener("keydown", onKey);
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    };
  }, [onClose]);

  const paneTf = (i: number) => (i === 0 ? tf : tfs[i] ?? DEFAULT_TFS[i]);
  const setPaneTf = (i: number, value: string) => {
    if (i === 0) onTf(value);
    else setTfs(DEFAULT_TFS.map((d, j) => (j === i ? value : tfs[j] ?? d)));
  };
  const known = timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"];

  return (
    <div ref={ref} className="focus" role="dialog" aria-modal="true" aria-label="Full screen charts" tabIndex={-1}>
      <header className="focus-bar">
        <div className="ticker">
          <span className="brand">Wednesday</span>
          <span className="focus-symbol">{symbol}</span>
          <span className="ticker-price num">{fmtPrice(scan.price)}</span>
          <span className={`live ${status.cls}`}>
            <span className="dot" aria-hidden="true" />
            {status.text}
          </span>
        </div>
        <div className="focus-tools">
          <div className="segmented" role="group" aria-label="Layout">
            {LAYOUTS.map((l) => (
              <button key={l.panes} type="button" className="seg" aria-pressed={layout === l.panes} title={l.title} aria-label={l.title}
                onClick={() => setLayout(l.panes)}>
                <LayoutIcon panes={l.panes} size={15} />
              </button>
            ))}
          </div>
          <div className="toggles">
            {toggles.map((t) => (
              <label key={t.label} className="toggle">
                <input type="checkbox" checked={t.checked} onChange={(e) => t.onChange(e.target.checked)} />
                {t.label}
              </label>
            ))}
          </div>
          <button type="button" className="button quiet focus-exit" onClick={onClose} title="Exit full screen (Esc)">
            <CollapseIcon /> Exit
          </button>
        </div>
      </header>
      <div className={`focus-grid layout-${layout}`}>
        {Array.from({ length: layout }, (_, i) => (
          <ChartPane key={i} label={`Chart ${i + 1}`} tf={paneTf(i)} onTf={(v) => setPaneTf(i, v)} timeframes={known}
            scan={scan} version={version} lookback={lookback} rail={rail} layers={layers} palette={palette}
            quarters={quarters} showQuarters={showQuarters} />
        ))}
      </div>
    </div>
  );
}
