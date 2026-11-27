import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { TcIcon } from "../tcIcons";
import { ChartSettingsDialog } from "./ChartSettingsDialog";

/** The chart's right-click menu: reset the view, or open the settings. */
export function ChartContextMenu({ at, palette, hollow, onReset, onClose }: {
  at: { x: number; y: number };
  palette: { bull: string; bear: string };
  hollow: boolean;
  onReset: () => void;
  onClose: () => void;
}) {
  const [settings, setSettings] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(at);

  // Kept inside the window near the right and bottom edges.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({ x: Math.max(4, Math.min(at.x, innerWidth - r.width - 4)), y: Math.max(4, Math.min(at.y, innerHeight - r.height - 4)) });
  }, [at]);

  useEffect(() => {
    if (settings) return;
    const away = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation(); // not full screen too
        onClose();
      }
    };
    const blur = () => onClose();
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", esc, true);
    window.addEventListener("blur", blur);
    window.addEventListener("resize", blur);
    ref.current?.querySelector("button")?.focus();
    return () => {
      document.removeEventListener("pointerdown", away, true);
      window.removeEventListener("keydown", esc, true);
      window.removeEventListener("blur", blur);
      window.removeEventListener("resize", blur);
    };
  }, [settings, onClose]);

  if (settings) return <ChartSettingsDialog palette={palette} hollow={hollow} onClose={onClose} />;
  return createPortal(
    <div ref={ref} className="chart-pop menu-pop ctx-menu" role="menu" aria-label="Chart" style={{ left: pos.x, top: pos.y }}
      onContextMenu={(e) => e.preventDefault()}>
      <button type="button" role="menuitem" className="draw-flyout-item" onClick={() => { onReset(); onClose(); }}>
        <span>Reset chart view</span><kbd>Alt+R</kbd>
      </button>
      <hr />
      <button type="button" role="menuitem" className="draw-flyout-item" onClick={() => setSettings(true)}>
        <TcIcon name="settings" size={14} /><span>Settings…</span>
      </button>
    </div>,
    document.fullscreenElement ?? document.body,
  );
}
