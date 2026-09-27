import { useEffect, useRef, useState } from "react";

import type { Drawing, DrawingDash } from "../api";
import { isPosition, TEXT_SIZE } from "../drawings";
import type { Drawings } from "../drawingsData";
import { LockIcon, TrashIcon } from "../icons";

// Fixed hues that read on both themes; the first swatch (null) is the theme's drawing colour.
const COLORS: (string | null)[] = [null, "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#3b82f6", "#ec4899", "#9ca3af"];
const WIDTHS = [1, 2, 3, 4];
const DASHES: { dash: DrawingDash; label: string }[] = [
  { dash: "solid", label: "Solid" },
  { dash: "dashed", label: "Dashed" },
  { dash: "dotted", label: "Dotted" },
];
const SIZES = [10, 12, 14, 16, 20, 24, 32];

/**
 * The selected drawing's controls, floating over the top of the chart: colour, line width
 * and style (text: its text and size), lock and delete. A position has only lock and delete.
 */
export function DrawingStyleBar({ ctl }: { ctl: Drawings }) {
  const d = ctl.hidden ? null : ctl.items.find((x) => x.id === ctl.selected) ?? null;
  if (!d) return null;
  const set = (patch: Partial<Drawing["style"]>) => ctl.change({ ...d, style: { ...d.style, ...patch } }, true);
  const line = !isPosition(d) && d.kind !== "text";
  return (
    <div className="draw-style" role="toolbar" aria-label="Selected drawing">
      {!isPosition(d) && (
        <div className="draw-colors" role="radiogroup" aria-label="Colour">
          {COLORS.map((c) => (
            <button key={c ?? "theme"} type="button" role="radio" className={`color-swatch${c ? "" : " is-theme"}`}
              aria-checked={d.style.color === c} aria-label={c ?? "Theme colour"} title={c ?? "Theme colour"}
              style={c ? { background: c } : undefined} disabled={d.locked} onClick={() => set({ color: c })} />
          ))}
        </div>
      )}
      {line && (
        <>
          <div className="segmented" role="group" aria-label="Line width">
            {WIDTHS.map((w) => (
              <button key={w} type="button" className="seg" aria-pressed={d.style.width === w} title={`${w}px`} aria-label={`${w}px line`}
                disabled={d.locked} onClick={() => set({ width: w })}>
                <span className="width-mark" style={{ height: w }} />
              </button>
            ))}
          </div>
          <div className="segmented" role="group" aria-label="Line style">
            {DASHES.map(({ dash, label }) => (
              <button key={dash} type="button" className="seg" aria-pressed={(d.style.dash ?? "solid") === dash} title={label} aria-label={label}
                disabled={d.locked} onClick={() => set({ dash })}>
                <span className={`dash-mark dash-${dash}`} />
              </button>
            ))}
          </div>
        </>
      )}
      {d.kind === "text" && <TextFields d={d} ctl={ctl} />}
      <span className="draw-sep" aria-hidden="true" />
      <button type="button" className="draw-tool" aria-pressed={d.locked}
        title={d.locked ? "Unlock" : "Lock: no moving or deleting"} aria-label={d.locked ? "Unlock the drawing" : "Lock the drawing"}
        onClick={() => ctl.change({ ...d, locked: !d.locked }, true)}>
        <LockIcon size={16} />
      </button>
      <button type="button" className="draw-tool" disabled={d.locked} title="Delete (Delete key)" aria-label="Delete the drawing"
        onClick={() => ctl.remove(d.id)}>
        <TrashIcon size={16} />
      </button>
    </div>
  );
}

/** A text drawing's text and size. Typing shows at once; the edit saves (one undo step) on Enter or leaving the field. */
function TextFields({ d, ctl }: { d: Drawing; ctl: Drawings }) {
  const ref = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState(d.props?.text ?? "");
  useEffect(() => setDraft(d.props?.text ?? ""), [d.id, d.props?.text]);
  useEffect(() => {
    if (!ctl.textFocus) return;
    ref.current?.focus();
    ref.current?.select();
  }, [ctl.textFocus]);

  const pending = useRef(false); // typed but not saved yet
  const commit = () => {
    if (!pending.current) return;
    pending.current = false;
    const text = draft.trim();
    if (!text) ctl.remove(d.id); // an emptied text goes
    else ctl.change({ ...d, props: { ...d.props, text } }, true);
  };
  // Picking another drawing unmounts the field without a blur: save what was typed then.
  const commitRef = useRef(commit);
  commitRef.current = commit;
  useEffect(() => () => commitRef.current(), [d.id]);
  return (
    <>
      <input ref={ref} className="draw-text" type="text" value={draft} maxLength={500} aria-label="Text" disabled={d.locked}
        onChange={(e) => {
          setDraft(e.target.value);
          pending.current = true;
          if (e.target.value.trim()) ctl.change({ ...d, props: { ...d.props, text: e.target.value } }, false);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === "Escape") e.currentTarget.blur();
        }} />
      <select className="draw-size" value={d.style.size ?? TEXT_SIZE} aria-label="Text size" disabled={d.locked}
        onChange={(e) => ctl.change({ ...d, style: { ...d.style, size: Number(e.target.value) } }, true)}>
        {SIZES.map((s) => <option key={s} value={s}>{s}px</option>)}
      </select>
    </>
  );
}
