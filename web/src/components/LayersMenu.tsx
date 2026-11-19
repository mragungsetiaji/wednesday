import { useId } from "react";

import type { DetectorInfo } from "../api";
import { TcIcon } from "../tcIcons";
import { usePopover } from "./usePopover";

export type Overlay = { key: string; label: string; title?: string; on: boolean; set: (v: boolean) => void };

/** The chart's layers in one menu under a toolbar button: the detectors, then the overlays. */
export function LayersMenu({ detectors, hidden, onToggle, overlays }: {
  detectors: DetectorInfo[];
  hidden: string[];
  onToggle: (name: string) => void;
  overlays: Overlay[];
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const off = detectors.filter((d) => hidden.includes(d.name)).length;
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Layers: detectors and overlays on the chart">
        <TcIcon name="layers" />
        <span>Layers</span>
        {off > 0 && <span className="levels-count num" title={`${off} detector${off > 1 ? "s" : ""} hidden`}>−{off}</span>}
        <span className="tb-caret"><TcIcon name="chevronDown" size={12} /></span>
      </button>
      {open && (
        <div className="chart-pop layers-pop" id={id} role="group" aria-label="Layers">
          <div className="pop-head">Detectors</div>
          {detectors.map((d) => (
            <label key={d.name} className="scale-opt" title={d.title}>
              <input type="checkbox" checked={!hidden.includes(d.name)} onChange={() => onToggle(d.name)} />
              {d.title.replace(/\s*\(.*\)$/, "")}
            </label>
          ))}
          <hr />
          <div className="pop-head">Overlays</div>
          {overlays.map((o) => (
            <label key={o.key} className="scale-opt" title={o.title}>
              <input type="checkbox" checked={o.on} onChange={(e) => o.set(e.target.checked)} />
              {o.label}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
