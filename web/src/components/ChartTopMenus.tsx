import { useId } from "react";

import { CHART_TYPES, useChartType } from "../chartType";
import { DEFAULT_CHART_SETTINGS, useChartSettings, type ChartSettings } from "../chartSettings";
import { useIndicators } from "../indicatorPrefs";
import { cleanLength, indicatorLabel, INDICATOR_COLORS, INDICATOR_KINDS, MAX_LENGTH, type Indicator, type IndicatorKind } from "../indicators";
import { TcIcon } from "../tcIcons";
import { useThemeChoice, type ThemeChoice } from "../theme";
import { usePopover } from "./usePopover";

/** The symbol at the start of the chart's top bar, with what feeds it under the info button. */
export function SymbolInfo({ symbol, rows }: { symbol: string; rows: [string, string][] }) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  return (
    <div className="tb-menu tb-symbol" ref={ref}>
      <span className="tb-symbol-name">{symbol}</span>
      <button type="button" className="tb-btn tb-icon" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Symbol info" aria-label={`${symbol} info`}>
        <TcIcon name="info" />
      </button>
      {open && (
        <div className="chart-pop symbol-pop" id={id} role="group" aria-label={`${symbol} info`}>
          <div className="pop-head">{symbol}</div>
          <dl className="symbol-info">
            {rows.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  );
}

/** Candles, hollow candles or Heikin Ashi, for every chart. */
export function ChartTypeMenu() {
  const [type, setType] = useChartType();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const current = CHART_TYPES.find((t) => t.id === type) ?? CHART_TYPES[0];
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn tb-icon" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title={`Chart type: ${current.label}`} aria-label={`Chart type: ${current.label}`}>
        <TcIcon name={current.icon} />
      </button>
      {open && (
        <div className="chart-pop menu-pop" id={id} role="menu" aria-label="Chart type">
          {CHART_TYPES.map((t) => (
            <button key={t.id} type="button" role="menuitemradio" aria-checked={t.id === type}
              className={`draw-flyout-item${t.id === type ? " is-active" : ""}`}
              onClick={() => {
                setType(t.id);
                setOpen(false);
              }}>
              <TcIcon name={t.icon} size={16} />
              {t.label}
            </button>
          ))}
          <p className="field-hint">Heikin Ashi changes the bars only: levels, setups and drawings use the real candles.</p>
        </div>
      )}
    </div>
  );
}

/** Light or dark, over the system's theme. */
export function ThemeButton({ mode }: { mode: "light" | "dark" }) {
  const [, setChoice] = useThemeChoice();
  const next = mode === "light" ? "dark" : "light";
  return (
    <button type="button" className="tb-btn tb-icon" onClick={() => setChoice(next)}
      title={`Switch to the ${next} theme`} aria-label={`Switch to the ${next} theme`}>
      <TcIcon name={mode === "light" ? "moon" : "sun"} />
    </button>
  );
}

/** The rest: follow the system's theme, the keyboard shortcuts. */
export function MoreMenu({ onShortcuts }: { onShortcuts: () => void }) {
  const [choice, setChoice] = useThemeChoice();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const themes: { id: ThemeChoice; label: string }[] = [
    { id: "auto", label: "Theme: follow the system" },
    { id: "light", label: "Theme: light" },
    { id: "dark", label: "Theme: dark" },
  ];
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn tb-icon" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="More" aria-label="More">
        <TcIcon name="more" />
      </button>
      {open && (
        <div className="chart-pop menu-pop is-end" id={id} role="menu" aria-label="More">
          {themes.map((t) => (
            <button key={t.id} type="button" role="menuitemradio" aria-checked={choice === t.id}
              className={`draw-flyout-item${choice === t.id ? " is-active" : ""}`}
              onClick={() => {
                setChoice(t.id);
                setOpen(false);
              }}>
              {t.label}
            </button>
          ))}
          <hr />
          <button type="button" role="menuitem" className="draw-flyout-item" onClick={() => {
            setOpen(false);
            onShortcuts();
          }}>
            Keyboard shortcuts <kbd>?</kbd>
          </button>
        </div>
      )}
    </div>
  );
}

/** Indicators: add one from the list; the ones on the chart, each with its length, colour and remove. */
export function IndicatorsMenu() {
  const [items, setItems] = useIndicators();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const add = (kind: IndicatorKind) => {
    const def = INDICATOR_KINDS.find((k) => k.kind === kind)!;
    const color = INDICATOR_COLORS[items.length % INDICATOR_COLORS.length];
    setItems([...items, { id: `${kind}-${Date.now().toString(36)}`, kind, length: def.length, color }]);
  };
  const update = (i: Indicator) => setItems(items.map((x) => (x.id === i.id ? i : x)));
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Indicators: moving averages, Bollinger Bands, RSI">
        <TcIcon name="trendingUp" />
        <span>Indicators</span>
        {items.length > 0 && <span className="levels-count num">{items.length}</span>}
      </button>
      {open && (
        <div className="chart-pop menu-pop indicators-pop" id={id} role="group" aria-label="Indicators">
          <div className="pop-head">Add</div>
          {INDICATOR_KINDS.map((k) => (
            <button key={k.kind} type="button" className="draw-flyout-item" title={k.hint} onClick={() => add(k.kind)}>
              <span className="ind-short">{k.short}</span>
              {k.name}
              <TcIcon name="plus" size={14} />
            </button>
          ))}
          {items.length > 0 && (
            <>
              <hr />
              <div className="pop-head">On the chart</div>
              {items.map((i) => (
                <div key={i.id} className="ind-row">
                  <input type="color" value={i.color} aria-label={`${indicatorLabel(i)} colour`}
                    onChange={(e) => update({ ...i, color: e.target.value })} />
                  <span className="ind-name">{INDICATOR_KINDS.find((k) => k.kind === i.kind)?.short}</span>
                  <input type="number" min={2} max={MAX_LENGTH} defaultValue={i.length} aria-label={`${indicatorLabel(i)} length`}
                    onBlur={(e) => update({ ...i, length: cleanLength(Number(e.target.value), i.length) })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                    }} />
                  <button type="button" className="tb-btn tb-icon" title={`Remove ${indicatorLabel(i)}`} aria-label={`Remove ${indicatorLabel(i)}`}
                    onClick={() => setItems(items.filter((x) => x.id !== i.id))}>
                    <TcIcon name="trash" size={14} />
                  </button>
                </div>
              ))}
            </>
          )}
          <p className="field-hint">Worked out on closed candles only; the same on every chart.</p>
        </div>
      )}
    </div>
  );
}

/** Chart settings: candle colours, grid, last price line, crosshair, room right of the last candle. */
export function ChartSettingsMenu({ palette }: { palette: { bull: string; bear: string } }) {
  const [s, set] = useChartSettings();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn tb-icon" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Chart settings" aria-label="Chart settings">
        <TcIcon name="settings" />
      </button>
      {open && (
        <div className="chart-pop menu-pop settings-pop" id={id} role="group" aria-label="Chart settings">
          <div className="pop-head">Candles</div>
          <label className="set-row">
            Up
            <input type="color" value={s.bull ?? palette.bull} onChange={(e) => set({ ...s, bull: e.target.value })} />
          </label>
          <label className="set-row">
            Down
            <input type="color" value={s.bear ?? palette.bear} onChange={(e) => set({ ...s, bear: e.target.value })} />
          </label>
          <label className="set-row">
            Last price line
            <input type="checkbox" checked={s.priceLine} onChange={(e) => set({ ...s, priceLine: e.target.checked })} />
          </label>
          <hr />
          <div className="pop-head">Chart</div>
          <label className="set-row">
            Grid
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
          <button type="button" className="button quiet scale-reset" onClick={() => set(DEFAULT_CHART_SETTINGS)}>Reset to the defaults</button>
        </div>
      )}
    </div>
  );
}
