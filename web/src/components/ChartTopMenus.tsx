import { useId } from "react";

import { CHART_TYPES, useChartType } from "../chartType";
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
