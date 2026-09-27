import { useRef, useState, type CSSProperties } from "react";

import { ChevronUp } from "../icons";
import { usePref } from "../prefs";
import { PanelTabs, type PanelTab } from "./PanelTabs";

const MIN_BODY = 120; // px of panel
const MIN_CHART = 240; // px the chart keeps above the dock

/**
 * Tabbed panels under the chart, TradingView style: collapsed to its tab strip until a tab is
 * picked, then the chart gives up the height. Clicking the open tab again collapses it. The top
 * edge drags to resize (arrow keys too); double-click puts it back to the default height.
 */
export function Dock({ tabs, open, onOpen }: {
  tabs: PanelTab[];
  open: string | null;
  onOpen: (id: string | null) => void;
}) {
  const [saved, save] = usePref<number | null>("wed.dockHeight", null); // null: the CSS default
  const [height, setHeight] = useState(saved); // follows the drag; saved when it ends
  const [dragging, setDragging] = useState(false);
  const ref = useRef<HTMLElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const start = useRef({ y: 0, h: 0 });
  const last = useRef(height);
  last.current = height;

  const active = tabs.find((t) => t.id === open) ?? null;
  if (!tabs.length) return null;

  const bodyHeight = () => bodyRef.current?.getBoundingClientRect().height ?? 0;
  // The dock grows into the chart, down to MIN_CHART of chart left.
  const clamp = (h: number) => {
    const chart = ref.current?.parentElement?.querySelector(".chart-wrap")?.getBoundingClientRect().height ?? 0;
    const max = Math.max(MIN_BODY, bodyHeight() + chart - MIN_CHART);
    return Math.round(Math.min(max, Math.max(MIN_BODY, h)));
  };
  const step = (d: number) => {
    const h = clamp(bodyHeight() + d);
    setHeight(h);
    save(h);
  };

  return (
    <section ref={ref} className={`dock${active ? " is-open" : ""}`} aria-label="Panels">
      {active && (
        <div
          className={`splitter splitter-y dock-resize${dragging ? " is-dragging" : ""}`}
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize panels"
          aria-valuenow={Math.round(bodyHeight()) || undefined}
          tabIndex={0}
          title="Drag to resize, double-click to reset"
          onPointerDown={(e) => {
            e.preventDefault();
            e.currentTarget.setPointerCapture(e.pointerId);
            start.current = { y: e.clientY, h: bodyHeight() };
            setDragging(true);
          }}
          onPointerMove={(e) => dragging && setHeight(clamp(start.current.h + start.current.y - e.clientY))}
          onPointerUp={(e) => {
            if (!dragging) return;
            e.currentTarget.releasePointerCapture(e.pointerId);
            setDragging(false);
            save(last.current);
          }}
          onDoubleClick={() => {
            setHeight(null);
            save(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowUp" || e.key === "ArrowDown") {
              e.preventDefault();
              step((e.key === "ArrowUp" ? 1 : -1) * (e.shiftKey ? 80 : 16));
            }
          }}
        />
      )}
      <div className="dock-bar">
        <PanelTabs tabs={tabs} selected={active?.id ?? null} onSelect={(id) => onOpen(id === active?.id ? null : id)}
          label="Panels" idPrefix="dock" compact />
        <button type="button" className="icon-button dock-toggle" aria-expanded={!!active}
          aria-label={active ? "Collapse panels" : "Expand panels"} title={active ? "Collapse" : "Expand"}
          onClick={() => onOpen(active ? null : tabs[0].id)}>
          <ChevronUp size={15} />
        </button>
      </div>
      {active && (
        <div ref={bodyRef} className="dock-body panel-body" id="dock-panel" role="tabpanel" aria-labelledby={`dock-tab-${active.id}`}
          style={height === null ? undefined : ({ height } as CSSProperties)}>
          {active.content}
        </div>
      )}
    </section>
  );
}
