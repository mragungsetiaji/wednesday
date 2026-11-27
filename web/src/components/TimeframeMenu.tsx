import { useId } from "react";
import type { Bias } from "../api";
import { fmtPrice } from "../format";
import { Direction } from "../icons";
import { TcIcon } from "../tcIcons";
import { usePopover } from "./usePopover";

/**
 * A chart's timeframe as one dropdown (the current one on the button, the rest in the list), with
 * each timeframe's last break of structure beside it. The same on the main chart's toolbar
 * (`toolbar`) and on every pane's header, full screen and pop-outs included (`pane`).
 */
export function TimeframeMenu({ value, options, onChange, bias, label = "Chart timeframe", variant = "toolbar" }: {
  value: string;
  options: string[];
  onChange: (tf: string) => void;
  bias?: (tf: string) => Bias | undefined;
  label?: string;
  variant?: "toolbar" | "pane";
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const b = bias?.(value);
  const why = (name: string, x: Bias | undefined) => (x ? `${name}: ${x.direction} ${x.event} at ${fmtPrice(x.level)}` : name);
  return (
    <div className={`tb-menu tf-menu is-${variant}`} ref={ref}>
      <button type="button" className={variant === "toolbar" ? "tb-btn tb-tf" : "pane-tf-btn"} aria-expanded={open} aria-controls={id}
        aria-haspopup="menu" aria-label={`${label}: ${value}`} title={why(value, b)} onClick={() => setOpen(!open)}>
        <span className="tf-name">{value}</span>
        {b && (
          <span className={`dir ${b.direction}`}>
            <Direction dir={b.direction} size={variant === "toolbar" ? 12 : 11} title={`${b.direction} ${b.event}`} />
          </span>
        )}
        <span className="tb-caret"><TcIcon name="chevronDown" size={12} /></span>
      </button>
      {open && (
        <div className={`chart-pop menu-pop tf-pop${variant === "pane" ? " pane-layers-pop" : ""}`} id={id} role="menu" aria-label={label}>
          <div className="pop-head">Timeframe</div>
          {options.map((name) => {
            const x = bias?.(name);
            return (
              <button key={name} type="button" role="menuitemradio" aria-checked={name === value} title={why(name, x)}
                className={`draw-flyout-item${name === value ? " is-active" : ""}`}
                onClick={() => {
                  onChange(name);
                  setOpen(false);
                }}>
                <span className="tf-item-name">{name}</span>
                {x && (
                  <span className={`tf-item-bias dir ${x.direction}`}>
                    <Direction dir={x.direction} size={11} title={`${x.direction} ${x.event}`} />
                    {x.event}
                  </span>
                )}
              </button>
            );
          })}
          <p className="field-hint">Or type it on the chart and press Enter: 15, 1h, 4h.</p>
        </div>
      )}
    </div>
  );
}
