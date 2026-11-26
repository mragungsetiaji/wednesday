import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { DEFAULT_CHART_SETTINGS, useChartSettings, type ChartSettings } from "../chartSettings";
import { candleLook, parseColor, withAlpha } from "../candleStyle";
import { CrossIcon } from "../icons";
import { ColorPicker } from "./ColorPicker";

type Tab = "symbol" | "canvas";
type ColorKey = "bull" | "bear" | "borderUp" | "borderDown" | "wickUp" | "wickDown";

/**
 * The chart settings, over the page (from a right-click on the chart or the toolbar's gear):
 * candles (body, borders, wick, each with an up and a down colour and opacity), the last price
 * line, grid, crosshair and the space after the last candle. Changes show as they are made;
 * Cancel (or Esc) puts back what was there when it opened.
 */
export function ChartSettingsDialog({ palette, hollow, onClose }: {
  palette: { bull: string; bear: string };
  hollow: boolean; // the Hollow chart type: rising bodies stay empty
  onClose: () => void;
}) {
  const [s, set] = useChartSettings();
  const before = useRef(s); // what Cancel restores
  const [tab, setTab] = useState<Tab>("symbol");
  const [pick, setPick] = useState<ColorKey | null>(null); // the colour whose picker is open
  const panel = useRef<HTMLDivElement>(null);
  const latest = useRef({ pick, set, onClose });
  latest.current = { pick, set, onClose };

  const cancel = () => {
    set(before.current);
    onClose();
  };
  const cancelRef = useRef(cancel);
  cancelRef.current = cancel;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation(); // capture: full screen stays open
      if (latest.current.pick) setPick(null);
      else cancelRef.current();
    };
    window.addEventListener("keydown", onKey, true);
    panel.current?.focus();
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const look = candleLook(s, palette, hollow);
  const shown: Record<ColorKey, string> = {
    bull: look.bodyUp, bear: look.bodyDown,
    borderUp: look.borderUp, borderDown: look.borderDown,
    wickUp: look.wickUp, wickDown: look.wickDown,
  };
  const swatch = (key: ColorKey, label: string, disabled: boolean) => (
    <Swatch key={key} label={label} color={shown[key]} disabled={disabled} open={pick === key}
      onToggle={() => setPick(pick === key ? null : key)} onChange={(c) => set({ ...s, [key]: c })} />
  );
  const part = (name: string, on: keyof Pick<ChartSettings, "body" | "borders" | "wick">, up: ColorKey, down: ColorKey, note?: string) => {
    const forced = on === "borders" && hollow; // Hollow needs the borders
    const checked = s[on] || forced;
    return (
      <div className="cs-row">
        <label className="cs-check" title={note}>
          <input type="checkbox" checked={checked} disabled={forced} onChange={(e) => set({ ...s, [on]: e.target.checked })} />
          {name}
        </label>
        <div className="cs-swatches">
          {swatch(up, `${name} up colour`, !checked)}
          {swatch(down, `${name} down colour`, !checked)}
        </div>
      </div>
    );
  };

  return createPortal(
    <div className="ind-backdrop" onPointerDown={(e) => {
      if (e.target === e.currentTarget) cancel();
      else if (pick && !(e.target as Element).closest(".cs-pop, .cs-swatch")) setPick(null);
    }}>
      <div ref={panel} className="ind-dialog cs-dialog" role="dialog" aria-modal="true" aria-label="Chart settings" tabIndex={-1}
        onKeyDown={(e) => e.stopPropagation() /* typing a number mustn't trigger the chart shortcuts */}>
        <div className="ind-dialog-head">
          <h2>Settings</h2>
          <button type="button" className="tb-btn tb-icon" aria-label="Cancel" title="Cancel (Esc)" onClick={cancel}>
            <CrossIcon size={16} />
          </button>
        </div>
        <div className="cs-main">
          <div className="cs-tabs" role="tablist" aria-orientation="vertical">
            <TabButton id="symbol" tab={tab} setTab={setTab}>Symbol</TabButton>
            <TabButton id="canvas" tab={tab} setTab={setTab}>Canvas</TabButton>
          </div>
          <div className="cs-body" role="tabpanel">
            {tab === "symbol" ? (
              <>
                <div className="cs-head">Candles</div>
                <label className="cs-check cs-wide">
                  <input type="checkbox" checked={s.prevClose} onChange={(e) => set({ ...s, prevClose: e.target.checked })} />
                  Colour candles by the previous close
                </label>
                {part("Body", "body", "bull", "bear")}
                {part("Borders", "borders", "borderUp", "borderDown", hollow ? "Hollow candles always show their borders" : undefined)}
                {part("Wick", "wick", "wickUp", "wickDown")}
                <div className="cs-head">Price line</div>
                <label className="cs-check cs-wide">
                  <input type="checkbox" checked={s.priceLine} onChange={(e) => set({ ...s, priceLine: e.target.checked })} />
                  Last price line
                </label>
              </>
            ) : (
              <>
                <div className="cs-head">Chart</div>
                <label className="set-row">
                  Grid lines
                  <select value={s.grid} onChange={(e) => set({ ...s, grid: e.target.value as ChartSettings["grid"] })}>
                    <option value="both">Both</option>
                    <option value="horz">Horizontal</option>
                    <option value="vert">Vertical</option>
                    <option value="none">None</option>
                  </select>
                </label>
                <label className="set-row">
                  Crosshair
                  <select value={s.crosshair} onChange={(e) => set({ ...s, crosshair: e.target.value as ChartSettings["crosshair"] })}>
                    <option value="normal">Free</option>
                    <option value="magnet">On the close</option>
                  </select>
                </label>
                <label className="set-row">
                  Space after the last candle
                  <input type="number" min={0} max={100} value={s.rightOffset}
                    onChange={(e) => set({ ...s, rightOffset: Math.min(100, Math.max(0, Math.round(Number(e.target.value) || 0))) })} />
                </label>
              </>
            )}
          </div>
        </div>
        <div className="ind-dialog-foot cs-foot">
          <button type="button" className="button quiet" title="Every chart setting back to the defaults (Cancel still undoes it)"
            onClick={() => set({ ...DEFAULT_CHART_SETTINGS, quarterColors: s.quarterColors, quarters: s.quarters })}>
            Reset to the defaults
          </button>
          <span className="tb-spacer" />
          <button type="button" className="button quiet" onClick={cancel}>Cancel</button>
          <button type="button" className="button" onClick={onClose}>Ok</button>
        </div>
      </div>
    </div>,
    document.fullscreenElement ?? document.body,
  );
}

function TabButton({ id, tab, setTab, children }: { id: Tab; tab: Tab; setTab: (t: Tab) => void; children: ReactNode }) {
  return (
    <button type="button" role="tab" className="cs-tab" aria-selected={tab === id} onClick={() => setTab(id)}>
      {children}
    </button>
  );
}

/** A colour button; open, the swatches and an opacity slider below it. */
function Swatch({ label, color, disabled, open, onToggle, onChange }: {
  label: string;
  color: string;
  disabled: boolean;
  open: boolean;
  onToggle: () => void;
  onChange: (color: string) => void;
}) {
  const { hex, alpha } = parseColor(color);
  const pct = Math.round(alpha * 100);
  return (
    <div className="cs-swatch-wrap">
      <button type="button" className="cs-swatch" aria-label={label} title={`${label}: ${hex}${pct < 100 ? `, ${pct}%` : ""}`}
        aria-expanded={open} disabled={disabled} onClick={onToggle}>
        <span style={{ "--c": color } as CSSProperties} />
      </button>
      {open && !disabled && (
        <div className="cs-pop" role="group" aria-label={label}>
          <ColorPicker label={label} value={hex} onChange={(c) => c && onChange(withAlpha(c, alpha))} />
          <div className="cs-opacity">
            <span>Opacity</span>
            <input type="range" min={0} max={100} value={pct} aria-label="Opacity"
              style={{ "--cs-color": hex } as CSSProperties}
              onChange={(e) => onChange(withAlpha(hex, Number(e.target.value) / 100))} />
            <input type="number" min={0} max={100} value={pct} aria-label="Opacity, percent"
              onChange={(e) => onChange(withAlpha(hex, Math.min(100, Math.max(0, Number(e.target.value) || 0)) / 100))} />
            <span>%</span>
          </div>
        </div>
      )}
    </div>
  );
}
