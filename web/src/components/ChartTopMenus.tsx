import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { CHART_TYPES, useChartType } from "../chartType";
import { DEFAULT_CHART_SETTINGS, useChartSettings, type ChartSettings } from "../chartSettings";
import { useIndicators } from "../indicatorPrefs";
import { cleanLength, cleanMult, indicatorLabel, INDICATOR_COLORS, INDICATOR_KINDS, LINE_WIDTHS, lineWidth, MAX_LENGTH, type Indicator, type IndicatorKind } from "../indicators";
import { TICKERS, tickerName } from "../marketSymbol";
import { TcIcon } from "../tcIcons";
import { useThemeChoice, type ThemeChoice } from "../theme";
import { CrossIcon } from "../icons";
import { ColorPicker } from "./ColorPicker";
import { usePopover } from "./usePopover";

/**
 * The symbol at the start of the chart's top bar: a menu of the markets the engine can scan
 * ([`TICKERS`]), with what feeds the chart below them. Picking one calls `onPick`; without it
 * (a pane that can't switch) the menu only shows the info.
 */
export function SymbolMenu({ symbol, rows, onPick, busy = false }: {
  symbol: string;
  rows: [string, string][];
  onPick?: (symbol: string) => void;
  busy?: boolean;
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const current = symbol.trim().toUpperCase();
  return (
    <div className="tb-menu tb-symbol" ref={ref}>
      <button type="button" className="tb-btn tb-symbol-btn" aria-expanded={open} aria-controls={id} aria-haspopup="menu"
        onClick={() => setOpen(!open)} disabled={busy} title={busy ? `Switching to ${symbol}…` : `${symbol}: change the market`}>
        <span className="tb-symbol-name">{tickerName(symbol)}</span>
        <TcIcon name="chevronDown" size={14} />
      </button>
      {open && (
        <div className="chart-pop symbol-pop" id={id} role="menu" aria-label="Market">
          {onPick && TICKERS.map((t) => (
            <button key={t.symbol} type="button" role="menuitemradio" aria-checked={t.symbol === current}
              className={`draw-flyout-item symbol-item${t.symbol === current ? " is-active" : ""}`}
              onClick={() => {
                setOpen(false);
                if (t.symbol !== current) onPick(t.symbol);
              }}>
              <span className="symbol-item-short">{t.short}</span>
              <span className="symbol-item-name">{t.name}</span>
              <span className="symbol-item-tv">{t.symbol}</span>
            </button>
          ))}
          {onPick && <hr />}
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

/** The quarterly pane as an indicator: on the chart or not, shown or hidden. */
export interface QuartersIndicator {
  added: boolean;
  hidden: boolean;
  setAdded: (v: boolean) => void;
  setHidden: (v: boolean) => void;
}

const QT = "qt"; // the quarters' id in the dialog state

/**
 * Indicators: add one from the list (its settings open at once); the ones on the chart, each
 * shown or hidden, with its settings and remove. The quarterly pane (QT) is one of them.
 */
export function IndicatorsMenu({ quarters }: { quarters?: QuartersIndicator }) {
  const [items, setItems] = useIndicators();
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const [editing, setEditing] = useState<string | null>(null);
  const id = useId();
  const add = (kind: IndicatorKind) => {
    const def = INDICATOR_KINDS.find((k) => k.kind === kind)!;
    const color = INDICATOR_COLORS[items.length % INDICATOR_COLORS.length];
    const ind = { id: `${kind}-${Date.now().toString(36)}`, kind, length: def.length, color };
    setItems([...items, ind]);
    setOpen(false);
    setEditing(ind.id);
  };
  const update = (i: Indicator) => setItems(items.map((x) => (x.id === i.id ? i : x)));
  const editedInd = items.find((x) => x.id === editing) ?? null;
  const count = items.length + (quarters?.added ? 1 : 0);
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Indicators: moving averages, Bollinger Bands, RSI, quarters">
        <TcIcon name="trendingUp" />
        <span>Indicators</span>
        {count > 0 && <span className="levels-count num">{count}</span>}
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
          {quarters && !quarters.added && (
            <button type="button" className="draw-flyout-item" title="Week, session and 90-minute quarters in a pane under the chart"
              onClick={() => {
                quarters.setAdded(true);
                quarters.setHidden(false);
                setOpen(false);
                setEditing(QT);
              }}>
              <span className="ind-short">QT</span>
              Quarters
              <TcIcon name="plus" size={14} />
            </button>
          )}
          {count > 0 && (
            <>
              <hr />
              <div className="pop-head">On the chart</div>
              {quarters?.added && (
                <IndicatorRow label="QT Quarters" color={null} hidden={quarters.hidden}
                  onHidden={quarters.setHidden} onSettings={() => { setOpen(false); setEditing(QT); }}
                  onRemove={() => quarters.setAdded(false)} />
              )}
              {items.map((i) => (
                <IndicatorRow key={i.id} label={indicatorLabel(i)} color={i.color} hidden={!!i.hidden}
                  onHidden={(h) => update({ ...i, hidden: h })} onSettings={() => { setOpen(false); setEditing(i.id); }}
                  onRemove={() => setItems(items.filter((x) => x.id !== i.id))} />
              ))}
            </>
          )}
          <p className="field-hint">Worked out on closed candles only; the same on every chart.</p>
        </div>
      )}
      {editedInd && <IndicatorDialog ind={editedInd} onChange={update} onClose={() => setEditing(null)} />}
      {editing === QT && quarters?.added && <QuartersDialog onClose={() => setEditing(null)} />}
    </div>
  );
}

function IndicatorRow({ label, color, hidden, onHidden, onSettings, onRemove }: {
  label: string;
  color: string | null;
  hidden: boolean;
  onHidden: (v: boolean) => void;
  onSettings: () => void;
  onRemove: () => void;
}) {
  return (
    <div className={`ind-row${hidden ? " is-hidden" : ""}`}>
      <span className="ind-dot" style={color ? { background: color } : undefined} aria-hidden="true" />
      <span className="ind-name">{label}</span>
      <button type="button" className="tb-btn tb-icon" aria-pressed={!hidden} title={hidden ? `Show ${label}` : `Hide ${label}`}
        aria-label={hidden ? `Show ${label}` : `Hide ${label}`} onClick={() => onHidden(!hidden)}>
        <TcIcon name={hidden ? "eyeOff" : "eye"} size={14} />
      </button>
      <button type="button" className="tb-btn tb-icon" title={`${label} settings`} aria-label={`${label} settings`} onClick={onSettings}>
        <TcIcon name="settings" size={14} />
      </button>
      <button type="button" className="tb-btn tb-icon" title={`Remove ${label}`} aria-label={`Remove ${label}`} onClick={onRemove}>
        <TcIcon name="trash" size={14} />
      </button>
    </div>
  );
}

/**
 * A settings dialog over the page (inside the full-screen view when that is open, so it shows
 * there too). Esc or a click outside closes it; changes apply as they are made.
 */
function SettingsDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation(); // capture: full screen stays open
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    panel.current?.focus();
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return createPortal(
    <div className="ind-backdrop" onPointerDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={panel} className="ind-dialog" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
        onKeyDown={(e) => e.stopPropagation() /* typing a length mustn't trigger the chart shortcuts */}>
        <div className="ind-dialog-head">
          <h2>{title}</h2>
          <button type="button" className="tb-btn tb-icon" aria-label="Close" title="Close (Esc)" onClick={onClose}>
            <CrossIcon size={16} />
          </button>
        </div>
        <div className="ind-dialog-body">{children}</div>
        <div className="ind-dialog-foot">
          <button type="button" className="button" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>,
    document.fullscreenElement ?? document.body,
  );
}

function IndicatorDialog({ ind, onChange, onClose }: { ind: Indicator; onChange: (i: Indicator) => void; onClose: () => void }) {
  const kind = INDICATOR_KINDS.find((k) => k.kind === ind.kind);
  return (
    <SettingsDialog title={`${kind?.name ?? ind.kind} (${indicatorLabel(ind)})`} onClose={onClose}>
      <label className="set-row">
        Length
        <input type="number" min={2} max={MAX_LENGTH} defaultValue={ind.length} key={`len-${ind.id}`}
          onChange={(e) => {
            const n = Number(e.target.value);
            if (Number.isFinite(n) && n >= 2) onChange({ ...ind, length: cleanLength(n, ind.length) });
          }}
          onBlur={(e) => { e.target.value = String(ind.length); }} />
      </label>
      {ind.kind === "bb" && (
        <label className="set-row">
          Standard deviations
          <input type="number" min={0.5} max={5} step={0.1} defaultValue={ind.mult ?? 2} key={`mult-${ind.id}`}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (Number.isFinite(n) && n >= 0.5) onChange({ ...ind, mult: cleanMult(n, ind.mult ?? 2) });
            }} />
        </label>
      )}
      <label className="set-row">
        Line width
        <select value={lineWidth(ind)} onChange={(e) => onChange({ ...ind, width: Number(e.target.value) })}>
          {LINE_WIDTHS.map((w) => <option key={w} value={w}>{w}px</option>)}
        </select>
      </label>
      <label className="set-row">
        Visible
        <input type="checkbox" checked={!ind.hidden} onChange={(e) => onChange({ ...ind, hidden: !e.target.checked })} />
      </label>
      <div className="pop-head">Colour</div>
      <ColorPicker value={ind.color} onChange={(c) => onChange({ ...ind, color: c ?? ind.color })} />
    </SettingsDialog>
  );
}

/** The quarterly pane's settings: how its blocks are coloured. */
function QuartersDialog({ onClose }: { onClose: () => void }) {
  const [s, set] = useChartSettings();
  const [q, setQ] = useState(0); // the quarter whose colour the picker edits
  return (
    <SettingsDialog title="Quarters (QT)" onClose={onClose}>
      <label className="set-row">
        Colour blocks by
        <select value={s.quarterColors} onChange={(e) => set({ ...s, quarterColors: e.target.value as ChartSettings["quarterColors"] })}>
          <option value="direction">Direction (green closed up)</option>
          <option value="quarter">Quarter</option>
        </select>
      </label>
      {s.quarterColors === "quarter" && (
        <>
          <div className="segmented ind-q-tabs" role="tablist" aria-label="Quarter">
            {s.quarters.map((c, i) => (
              <button key={i} type="button" role="tab" className="seg" aria-selected={q === i} aria-pressed={q === i} onClick={() => setQ(i)}>
                <span className="ind-dot" style={{ background: c }} aria-hidden="true" /> Q{i + 1}
              </button>
            ))}
          </div>
          <ColorPicker label={`Q${q + 1} colour`} value={s.quarters[q]} onChange={(c) => {
            if (!c) return;
            const next = [...s.quarters] as ChartSettings["quarters"];
            next[q] = c;
            set({ ...s, quarters: next });
          }} />
          <p className="field-hint">Mon to Thu, Tokyo to NY PM, and each 90 minutes' Q1 to Q4. The bottom edge still shows the direction.</p>
        </>
      )}
    </SettingsDialog>
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
