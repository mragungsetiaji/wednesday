import { useEffect, useId, useRef, useState } from "react";

export type ScaleMode = "auto" | "log" | "percent";
export interface ScaleState {
  mode: ScaleMode;
  locked: boolean; // keep the current price range through new ticks and scans
  inverted: boolean;
}
export const AUTO_SCALE: ScaleState = { mode: "auto", locked: false, inverted: false };

const MODES: { id: ScaleMode; label: string }[] = [
  { id: "auto", label: "Price" },
  { id: "log", label: "Log" },
  { id: "percent", label: "% from session open" },
];

const summary = (s: ScaleState) =>
  [s.mode === "log" ? "Log" : s.mode === "percent" ? "%" : "Auto", s.locked && "locked", s.inverted && "inverted"].filter(Boolean).join(" · ");

/** The price scale menu under a chart: scale mode, lock the range, invert. */
export function ScaleMenu({ value, onChange, percentOk }: { value: ScaleState; onChange: (s: ScaleState) => void; percentOk: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        ref.current?.querySelector("button")?.focus();
      }
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc, true);
    };
  }, [open]);

  const changed = value.mode !== "auto" || value.locked || value.inverted;
  return (
    <div className="scale-menu" ref={ref}>
      <button type="button" className={`chart-nav-btn${changed ? " is-on" : ""}`} aria-expanded={open} aria-controls={id}
        title="Price scale" onClick={() => setOpen(!open)}>
        Scale: {summary(value)}
      </button>
      {open && (
        <div className="scale-pop" id={id} role="group" aria-label="Price scale">
          <fieldset>
            <legend className="sr-only">Scale</legend>
            {MODES.map((m) => (
              <label key={m.id} className="scale-opt">
                <input type="radio" name={`${id}-mode`} checked={value.mode === m.id} disabled={m.id === "percent" && !percentOk}
                  onChange={() => onChange({ ...value, mode: m.id })} />
                {m.label}
              </label>
            ))}
          </fieldset>
          <hr />
          <label className="scale-opt" title="Keep the current price range: new ticks and scans don't rescale it">
            <input type="checkbox" checked={value.locked} onChange={(e) => onChange({ ...value, locked: e.target.checked })} />
            Lock range
          </label>
          <label className="scale-opt" title="Flip the chart upside down, to check a bias the other way">
            <input type="checkbox" checked={value.inverted} onChange={(e) => onChange({ ...value, inverted: e.target.checked })} />
            Invert
          </label>
          {changed && (
            <button type="button" className="button quiet scale-reset" onClick={() => onChange(AUTO_SCALE)}>Reset</button>
          )}
        </div>
      )}
    </div>
  );
}
