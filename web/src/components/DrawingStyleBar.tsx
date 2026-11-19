import { useEffect, useId, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

import {
  deleteDrawingTemplate,
  fetchDrawingTemplates,
  putDrawingTemplate,
  type Drawing,
  type DrawingDash,
  type DrawingFamily,
  type DrawingTemplate,
} from "../api";
import { canAlert, isPosition, LABEL_SIZE, newId, TEXT_SIZE } from "../drawings";
import type { Drawings } from "../drawingsData";
import { CrossIcon, GripIcon, LockIcon, MoreIcon, PencilIcon, TemplatesIcon, TextIcon, TrashIcon, UnlockIcon } from "../icons";
import { usePref } from "../prefs";
import { DrawingAlertButton, Floating } from "./DrawingAlertForm";
import { usePopover } from "./usePopover";

// Fixed hues that read on both themes; the first swatch (null) is the theme's drawing colour.
const COLORS: (string | null)[] = [null, "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#3b82f6", "#ec4899", "#9ca3af"];
const WIDTHS = [1, 2, 3, 4];
const DASHES: { dash: DrawingDash; label: string }[] = [
  { dash: "solid", label: "Solid" },
  { dash: "dashed", label: "Dashed" },
  { dash: "dotted", label: "Dotted" },
];
const SIZES = [10, 12, 14, 16, 20, 24, 32];

type Pos = { x: number; y: number } | null;

/**
 * The selected drawing's toolbar, floating over the chart: a grip to move it, then colour,
 * text (a text's words, or a line's label), line width and style, a price alert (lines and
 * rectangles), lock, delete and more. Each setting opens its own small menu. A position has
 * only the alert-free part: lock, delete and more.
 */
export function DrawingStyleBar({ ctl }: { ctl: Drawings }) {
  const d = ctl.hidden ? null : ctl.items.find((x) => x.id === ctl.selected) ?? null;
  const [pos, setPos] = usePref<Pos>("wed.drawBar", null);
  const [dragPos, setDragPos] = useState<Pos>(null);
  const barRef = useRef<HTMLDivElement>(null);
  if (!d) return null;

  const set = (patch: Partial<Drawing["style"]>) => ctl.change({ ...d, style: { ...d.style, ...patch } }, true);
  const position = isPosition(d);
  const line = !position && d.kind !== "text";
  const at = dragPos ?? pos;
  const style: CSSProperties | undefined = at ? { left: at.x, top: at.y, transform: "none" } : undefined;

  // Drag by the grip; the place is remembered. A double click puts it back at the top centre.
  const startDrag = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const bar = barRef.current;
    const host = bar?.parentElement;
    if (!bar || !host) return;
    e.preventDefault();
    const b = bar.getBoundingClientRect();
    const h = host.getBoundingClientRect();
    const dx = e.clientX - b.left;
    const dy = e.clientY - b.top;
    const place = (ev: PointerEvent): Pos => ({
      x: Math.round(Math.min(Math.max(0, ev.clientX - h.left - dx), h.width - b.width)),
      y: Math.round(Math.min(Math.max(0, ev.clientY - h.top - dy), h.height - b.height)),
    });
    let last: Pos = null;
    const move = (ev: PointerEvent) => {
      last = place(ev);
      setDragPos(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (last) setPos(last);
      setDragPos(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <div ref={barRef} className="draw-style" role="toolbar" aria-label="Selected drawing" style={style}>
      <button type="button" className="draw-grip" title="Drag to move this toolbar (double-click: back to the top)"
        aria-label="Move the toolbar" onPointerDown={startDrag} onDoubleClick={() => setPos(null)}>
        <GripIcon size={14} />
      </button>
      {familyOf(d) && (
        <Menu label="Templates" title="Drawing templates" button={<TemplatesIcon size={16} />} disabled={d.locked} width={240}>
          {(close) => <Templates d={d} family={familyOf(d)!} ctl={ctl} onDone={close} />}
        </Menu>
      )}
      {!position && (
        <Menu label="Colour" title="Line colour" button={
          <span className="draw-ink">
            <PencilIcon size={16} />
            <span className={`draw-ink-bar${d.style.color ? "" : " is-theme"}`} style={d.style.color ? { background: d.style.color } : undefined} />
          </span>
        } disabled={d.locked} width={208}>
          {() => (
            <div className="draw-colors" role="radiogroup" aria-label="Colour">
              {COLORS.map((c) => (
                <button key={c ?? "theme"} type="button" role="radio" className={`color-swatch${c ? "" : " is-theme"}`}
                  aria-checked={d.style.color === c} aria-label={c ?? "Theme colour"} title={c ?? "Theme colour"}
                  style={c ? { background: c } : undefined} onClick={() => set({ color: c })} />
              ))}
            </div>
          )}
        </Menu>
      )}
      {!position && (
        <Menu label="Text" title={d.kind === "text" ? "Text and size" : "Label on the line"} open={d.kind === "text"}
          focusKey={d.kind === "text" ? ctl.textFocus : 0} button={
            <span className="draw-ink">
              <TextIcon size={16} />
              <span className={`draw-ink-bar${d.props?.text ? "" : " is-empty"}`} />
            </span>
          } disabled={d.locked} width={260}>
          {(close) => <TextFields d={d} ctl={ctl} onDone={close} />}
        </Menu>
      )}
      {line && (
        <Menu label="Line width" title="Line width" button={
          <span className="draw-width"><span className="width-mark" style={{ height: d.style.width }} />{d.style.width}px</span>
        } disabled={d.locked} width={120}>
          {(close) => (
            <div className="draw-menu" role="group" aria-label="Line width">
              {WIDTHS.map((w) => (
                <button key={w} type="button" className="draw-menu-item" aria-pressed={d.style.width === w}
                  onClick={() => { set({ width: w }); close(); }}>
                  <span className="width-mark" style={{ height: w }} />{w}px
                </button>
              ))}
            </div>
          )}
        </Menu>
      )}
      {line && (
        <Menu label="Line style" title="Line style" button={<span className={`dash-mark dash-${d.style.dash ?? "solid"}`} />}
          disabled={d.locked} width={140}>
          {(close) => (
            <div className="draw-menu" role="group" aria-label="Line style">
              {DASHES.map(({ dash, label }) => (
                <button key={dash} type="button" className="draw-menu-item" aria-pressed={(d.style.dash ?? "solid") === dash}
                  onClick={() => { set({ dash }); close(); }}>
                  <span className={`dash-mark dash-${dash}`} />{label}
                </button>
              ))}
            </div>
          )}
        </Menu>
      )}
      {canAlert(d) && <DrawingAlertButton key={d.id} d={d} ctl={ctl} />}
      <button type="button" className="draw-tool" aria-pressed={d.locked}
        title={d.locked ? "Unlock" : "Lock: no moving or deleting"} aria-label={d.locked ? "Unlock the drawing" : "Lock the drawing"}
        onClick={() => ctl.change({ ...d, locked: !d.locked }, true)}>
        {d.locked ? <LockIcon size={16} /> : <UnlockIcon size={16} />}
      </button>
      <button type="button" className="draw-tool" disabled={d.locked} title="Delete (Delete key)" aria-label="Delete the drawing"
        onClick={() => ctl.remove(d.id)}>
        <TrashIcon size={16} />
      </button>
      <Menu label="More" title="More" button={<MoreIcon size={16} />} width={180}>
        {(close) => (
          <div className="draw-menu" role="group" aria-label="More">
            <button type="button" className="draw-menu-item" onClick={() => {
              close();
              ctl.create({ ...d, id: newId(), locked: false, points: d.points.map((p) => ({ ...p })) });
            }}>
              Clone
            </button>
            {line && d.props?.text && (
              <button type="button" className="draw-menu-item" disabled={d.locked} onClick={() => {
                close();
                ctl.change({ ...d, props: null }, true);
              }}>
                Remove the label
              </button>
            )}
            {line && (d.style.color !== null || d.style.width !== 1 || (d.style.dash ?? "solid") !== "solid") && (
              <button type="button" className="draw-menu-item" disabled={d.locked} onClick={() => {
                close();
                ctl.change({ ...d, style: { color: null, width: 1, dash: "solid", ...(d.style.size ? { size: d.style.size } : {}) } }, true);
              }}>
                Reset the style
              </button>
            )}
          </div>
        )}
      </Menu>
    </div>
  );
}

let handledFocus = 0; // the last ctl.textFocus that opened the text menu

/** A toolbar button with its own menu under it (rendered beside the bar, so the bar can't clip it). */
function Menu({ label, title, button, disabled, width, open: wantOpen, focusKey, children }: {
  label: string;
  title: string;
  button: React.ReactNode;
  disabled?: boolean;
  width: number;
  open?: boolean; // open it now (a new text drawing asks for its words)
  focusKey?: number;
  children: (close: () => void) => React.ReactNode;
}) {
  const { open, setOpen, ref, popRef } = usePopover<HTMLDivElement>();
  const id = useId();
  useEffect(() => {
    if (!wantOpen || !focusKey || focusKey <= handledFocus) return;
    handledFocus = focusKey; // once per request, not every time a text is selected
    setOpen(true);
  }, [wantOpen, focusKey, setOpen]);
  return (
    <div className="draw-menu-anchor" ref={ref}>
      <button type="button" className="draw-tool" aria-haspopup="true" aria-expanded={open} aria-controls={id}
        title={title} aria-label={label} disabled={disabled} onClick={() => setOpen(!open)}>
        {button}
      </button>
      {open && !disabled && (
        <Floating anchor={ref} popRef={popRef} width={width}>
          <div className="chart-pop draw-pop" id={id}>{children(() => setOpen(false))}</div>
        </Floating>
      )}
    </div>
  );
}

/**
 * A text drawing's words, or a line's label, and its size. Typing shows at once; the edit
 * saves (one undo step) on Enter or leaving the field. An emptied text goes; an emptied
 * label just comes off the line.
 */
function TextFields({ d, ctl, onDone }: { d: Drawing; ctl: Drawings; onDone: () => void }) {
  const ref = useRef<HTMLInputElement>(null);
  const isText = d.kind === "text";
  const [draft, setDraft] = useState(d.props?.text ?? "");
  useEffect(() => setDraft(d.props?.text ?? ""), [d.id, d.props?.text]);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, [ctl.textFocus]);

  const pending = useRef(false); // typed but not saved yet
  const commit = () => {
    if (!pending.current) return;
    pending.current = false;
    const text = draft.trim();
    if (text) ctl.change({ ...d, props: { ...d.props, text } }, true);
    else if (isText) ctl.remove(d.id);
    else ctl.change({ ...d, props: null }, true);
  };
  // Closing the menu or picking another drawing unmounts the field without a blur: save then.
  const commitRef = useRef(commit);
  commitRef.current = commit;
  useEffect(() => () => commitRef.current(), [d.id]);
  return (
    <div className="draw-text-fields">
      <input ref={ref} className="draw-text" type="text" value={draft} maxLength={500} disabled={d.locked}
        aria-label={isText ? "Text" : "Label"} placeholder={isText ? "Text" : "Label, e.g. H1 OB"}
        onChange={(e) => {
          setDraft(e.target.value);
          pending.current = true;
          const text = e.target.value;
          if (text.trim()) ctl.change({ ...d, props: { ...d.props, text } }, false);
          else if (!isText) ctl.change({ ...d, props: null }, false);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            commit();
            onDone();
          }
        }} />
      <select className="draw-size" value={d.style.size ?? (isText ? TEXT_SIZE : LABEL_SIZE)} aria-label="Text size" disabled={d.locked}
        onChange={(e) => ctl.change({ ...d, style: { ...d.style, size: Number(e.target.value) } }, true)}>
        {SIZES.map((s) => <option key={s} value={s}>{s}px</option>)}
      </select>
    </div>
  );
}

const FAMILIES: Partial<Record<Drawing["kind"], DrawingFamily>> = { trendline: "line", hline: "line", path: "line", rect: "rect", text: "text" };
const familyOf = (d: Drawing): DrawingFamily | null => FAMILIES[d.kind] ?? null;

/** The look a template gives a drawing: its style, and its label (a text keeps its words without one). */
function applyTemplate(d: Drawing, t: { style: Drawing["style"]; text: string | null }): Drawing {
  const props = d.kind === "text" ? { ...d.props, text: t.text ?? d.props?.text ?? "" } : t.text ? { text: t.text } : null;
  return { ...d, style: { ...t.style }, props };
}

const DEFAULT_TEMPLATE = { style: { color: null, width: 1, dash: "solid" as const }, text: null };

// Templates load once per page and stay in step with every bar (the main chart, full screen, pop-outs).
let cache: DrawingTemplate[] | null = null;

/**
 * Drawing templates: save the selected drawing's look under a name, apply one, or go back to the
 * default look. Each drawing family (lines, rectangles, texts) has its own list.
 */
function Templates({ d, family, ctl, onDone }: { d: Drawing; family: DrawingFamily; ctl: Drawings; onDone: () => void }) {
  const [items, setItems] = useState<DrawingTemplate[] | null>(cache);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const keep = (xs: DrawingTemplate[]) => {
    cache = xs;
    setItems(xs);
  };
  useEffect(() => {
    fetchDrawingTemplates().then((r) => keep(r.templates)).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const apply = (t: { style: Drawing["style"]; text: string | null }) => {
    ctl.change(applyTemplate(d, t), true);
    onDone();
  };
  const save = () => run(async () => {
    const text = d.kind === "text" ? null : d.props?.text ?? null; // a text's words are its content, not its look
    const saved = await putDrawingTemplate(family, name, { style: d.style, text });
    keep([...(items ?? []).filter((t) => !(t.family === family && t.name === saved.name)), saved]
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true })));
    setNaming(false);
    setName("");
  });
  const remove = (t: DrawingTemplate) => run(async () => {
    await deleteDrawingTemplate(t.family, t.name);
    keep((items ?? []).filter((x) => x !== t));
  });

  const mine = (items ?? []).filter((t) => t.family === family);
  return (
    <div className="draw-menu" role="group" aria-label="Drawing templates">
      {naming ? (
        <form className="draw-template-save" onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) save();
        }}>
          <input type="text" value={name} maxLength={60} placeholder="Template name, e.g. H1 OB" aria-label="Template name"
            autoFocus onChange={(e) => setName(e.target.value)} />
          <button type="submit" className="button" disabled={busy || !name.trim()}>Save</button>
        </form>
      ) : (
        <button type="button" className="draw-menu-item" onClick={() => {
          setName(d.kind === "text" ? "" : d.props?.text ?? "");
          setNaming(true);
        }}>
          Save drawing template as…
        </button>
      )}
      <button type="button" className="draw-menu-item" onClick={() => apply(DEFAULT_TEMPLATE)}>Apply the default template</button>
      {error && <p className="draw-template-error" role="alert">{error}</p>}
      {mine.length > 0 && <hr className="draw-menu-sep" />}
      <div className="draw-template-list">
        {mine.map((t) => (
          <div key={t.name} className="draw-template">
            <button type="button" className="draw-menu-item" onClick={() => apply(t)}
              title={`Apply ${t.name}${t.text ? `: label "${t.text}"` : ""}`}>
              <span className="draw-template-swatch" style={{ background: t.style.color ?? "var(--draw)" }} aria-hidden="true" />
              {t.name}
            </button>
            <button type="button" className="draw-template-delete" disabled={busy} title={`Delete ${t.name}`}
              aria-label={`Delete the template ${t.name}`} onClick={() => remove(t)}>
              <CrossIcon size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
