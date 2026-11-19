import { useEffect, useRef, useState } from "react";

import type { DrawTool } from "../drawings";
import type { Drawings } from "../drawingsData";
import { usePref } from "../prefs";
import { TcIcon, type TcIconName } from "../tcIcons";
import { toast } from "./Toasts";
import { TOOL_GROUPS, type MenuTool } from "../shapes";

const ICON = 20; // px; 18 in the full screen row

type Tool = MenuTool & { tool: DrawTool };

/** The tool groups, as TradeCanvas's sidebar has them (shapes.ts): a group's button draws with its last tool. */
const GROUPS = TOOL_GROUPS as { id: string; name: string; tools: Tool[] }[];
const ALL: Tool[] = GROUPS.flatMap((g) => g.tools);
/** Where a group's menu may reach: the window's height, less a margin. */
const MENU_MARGIN = 8;

const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = mac ? "⌘" : "Ctrl+";
/** How long a group's menu stays after the pointer leaves it, so the way to it can cross a gap. */
const CLOSE_MS = 150;

/**
 * Drawing tools, TradeCanvas style: a column beside the chart (a row in full screen and pop-out
 * windows). Grouped tools keep the last one picked on their button and show the rest in a menu
 * on hover (or on the corner mark, by click or keyboard). `history` adds undo and redo, for
 * windows without the chart's top bar.
 */
export function DrawingToolbar({ ctl, row, history = row }: { ctl: Drawings; row?: boolean; history?: boolean }) {
  const [last, setLast] = usePref<Record<string, DrawTool>>("wed.drawGroups", {});
  const [favs, setFavs] = usePref<DrawTool[]>("wed.drawFavorites", []);
  const [favBar, setFavBar] = usePref<boolean>("wed.drawFavoritesBar", false);
  const [menu, setMenu] = useState<string | null>(null);
  const [at, setAt] = useState<{ left: number; top: number; maxHeight?: number }>({ left: 0, top: 0 });
  const timer = useRef<number>(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!menu) return;
    const away = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setMenu(null);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setMenu(null);
      }
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc, true);
    };
  }, [menu]);

  if (!ctl.available) return null;
  const size = row ? ICON - 2 : ICON;
  const pick = (tool: DrawTool) => ctl.setTool(ctl.tool === tool && tool !== "cursor" ? "cursor" : tool);
  // The menu is fixed to the page, so the column's scrolling doesn't clip it: beside the group, or under it in a row.
  const show = (id: string, group: HTMLElement) => {
    const r = group.getBoundingClientRect();
    // A long menu (Fibonacci has 19 tools) moves up to fit the window, then scrolls.
    const top = row ? r.bottom + 4 : Math.max(MENU_MARGIN, Math.min(r.top, window.innerHeight - MENU_MARGIN - 560));
    setAt({ left: row ? r.left : r.right + 4, top, maxHeight: window.innerHeight - top - MENU_MARGIN });
    setMenu(id);
  };
  const hover = (id: string | null, group?: HTMLElement) => {
    window.clearTimeout(timer.current);
    if (id && group) show(id, group);
    else timer.current = window.setTimeout(() => setMenu(null), CLOSE_MS);
  };

  const favorite = (t: Tool) => {
    const on = favs.includes(t.tool);
    setFavs(on ? favs.filter((x) => x !== t.tool) : [...favs, t.tool]);
    if (!on && !favBar) setFavBar(true);
    toast(on ? `${t.label} left the favorites` : `${t.label} added to the favorites`);
  };
  const allLocked = ctl.items.length > 0 && ctl.items.every((d) => d.locked);
  const removeAll = () => {
    const n = ctl.removeAll();
    const kept = ctl.items.length - n;
    toast(n ? `Removed ${n} drawing${n > 1 ? "s" : ""}${kept ? `, kept ${kept} locked` : ""}. ${MOD}Z brings them back.` : "Every drawing is locked: unlock them first.");
  };
  const toggle = (on: boolean, label: string, icon: TcIconName, set: () => void, hint: string) => (
    <button type="button" className="draw-tool" aria-pressed={on} title={hint} aria-label={label} onClick={set}>
      <TcIcon name={icon} size={size} />
    </button>
  );
  const title = (t: Tool) => (t.keys ? `${t.label} (${t.keys})` : t.label);
  const button = (t: Tool) => (
    <button key={t.tool} type="button" className="draw-tool" aria-pressed={ctl.tool === t.tool} title={title(t)} aria-label={t.label}
      onClick={() => pick(t.tool)} onContextMenu={(e) => {
        e.preventDefault();
        favorite(t);
      }}>
      <TcIcon name={t.icon} size={size} />
    </button>
  );

  return (
    <div ref={ref} className={`draw-tools${row ? " is-row" : ""}`} role="toolbar" aria-label="Drawing tools"
      aria-orientation={row ? "horizontal" : "vertical"}>
      <button type="button" className="draw-tool" aria-pressed={ctl.tool === "cursor"} title="Cursor (Esc)" aria-label="Cursor"
        onClick={() => ctl.setTool("cursor")}>
        <TcIcon name="crosshairCursor" size={size} />
      </button>
      <span className="draw-sep" aria-hidden="true" />
      {GROUPS.map((g) => {
        const shown = g.tools.find((t) => t.tool === last[g.id] && !t.disabled) ?? g.tools[0];
        const on = g.tools.find((t) => t.tool === ctl.tool);
        const current = on ?? shown;
        if (g.tools.length === 1) return button(current);
        const open = menu === g.id;
        return (
          <div key={g.id} className="draw-group" onMouseEnter={row ? undefined : (e) => hover(g.id, e.currentTarget)} onMouseLeave={row ? undefined : () => hover(null)}>
            <button type="button" className="draw-tool" aria-pressed={!!on} title={title(current)} aria-label={current.label}
              onClick={() => pick(current.tool)} onContextMenu={(e) => {
                e.preventDefault();
                favorite(current);
              }}>
              <TcIcon name={current.icon} size={size} />
            </button>
            <button type="button" className="draw-group-more" aria-label={`${g.name}: more tools`} aria-expanded={open}
              aria-haspopup="menu" onClick={(e) => (open ? setMenu(null) : show(g.id, e.currentTarget.parentElement!))} />
            {open && (
              <div className={`draw-flyout${row ? " is-below" : ""}`} role="menu" aria-label={g.name} style={at}>
                {g.tools.map((t) => [
                  t.heading && <div key={`h-${t.heading}`} className="draw-flyout-head">{t.heading}</div>,
                  <button key={t.tool} type="button" role="menuitemradio" aria-checked={ctl.tool === t.tool} disabled={!!t.disabled}
                    title={t.disabled}
                    className={`draw-flyout-item${t.tool === current.tool ? " is-active" : ""}`}
                    onClick={() => {
                      setLast({ ...last, [g.id]: t.tool });
                      setMenu(null);
                      ctl.setTool(t.tool);
                    }}>
                    <TcIcon name={t.icon} size={16} />
                    {t.label}
                    {t.keys && <kbd>{t.keys}</kbd>}
                    <span role="button" tabIndex={-1} className={`draw-fav${favs.includes(t.tool) ? " is-on" : ""}`}
                      title={favs.includes(t.tool) ? "Remove from favorites" : "Add to favorites"}
                      onClick={(e) => {
                        e.stopPropagation();
                        favorite(t);
                      }}>
                      <TcIcon name="star" size={14} />
                    </span>
                  </button>,
                ])}
              </div>
            )}
          </div>
        );
      })}
      <span className="draw-sep" aria-hidden="true" />
      {toggle(ctl.magnet, "Magnet", "magnet", () => ctl.setMagnet(!ctl.magnet),
        ctl.magnet ? "Magnet on: points snap to the candle's open, high, low or close" : "Magnet: snap points to the candle's open, high, low or close")}
      <button type="button" className="draw-tool" aria-pressed={ctl.tool === "eraser"} title="Eraser: click a drawing to remove it"
        aria-label="Eraser" onClick={() => pick("eraser")}>
        <TcIcon name="eraser" size={size} />
      </button>
      <button type="button" className="draw-tool" aria-pressed={ctl.tool === "zoom"} title="Zoom: drag over the candles to show"
        aria-label="Zoom in on an area" onClick={() => pick("zoom")}>
        <TcIcon name="zoomIn" size={size} />
      </button>
      {toggle(ctl.stay, "Stay in drawing mode", "repeat", () => ctl.setStay(!ctl.stay),
        ctl.stay ? "Stay in drawing mode: on, the tool stays after each drawing" : "Stay in drawing mode: keep the tool after drawing")}
      {toggle(favBar, "Favorites bar", "star", () => setFavBar(!favBar),
        favBar ? "Hide the favorites bar" : "Show the favorites bar (right-click a tool, or its star, to add it)")}
      <span className="draw-sep" aria-hidden="true" />
      {history && (
        <>
          <button type="button" className="draw-tool" disabled={!ctl.canUndo} title={`Undo (${MOD}Z)`} aria-label="Undo" onClick={ctl.undo}>
            <TcIcon name="undo" size={size} />
          </button>
          <button type="button" className="draw-tool" disabled={!ctl.canRedo} title={`Redo (${MOD}${mac ? "⇧Z" : "Y"})`} aria-label="Redo" onClick={ctl.redo}>
            <TcIcon name="redo" size={size} />
          </button>
        </>
      )}
      <button type="button" className="draw-tool" aria-pressed={allLocked} disabled={!ctl.items.length}
        title={allLocked ? "Unlock all drawings" : "Lock all drawings"} aria-label={allLocked ? "Unlock all drawings" : "Lock all drawings"}
        onClick={() => ctl.lockAll(!allLocked)}>
        <TcIcon name={allLocked ? "lock" : "unlock"} size={size} />
      </button>
      <button type="button" className="draw-tool" aria-pressed={ctl.hidden} disabled={!ctl.items.length && !ctl.hidden}
        title={ctl.hidden ? "Show drawings" : "Hide drawings"} aria-label={ctl.hidden ? "Show drawings" : "Hide drawings"}
        onClick={() => ctl.setHidden(!ctl.hidden)}>
        <TcIcon name={ctl.hidden ? "eyeOff" : "eye"} size={size} />
      </button>
      <button type="button" className="draw-tool is-danger" disabled={!ctl.items.length}
        title={`Remove all drawings (locked ones stay; ${MOD}Z undoes)`} aria-label="Remove all drawings" onClick={removeAll}>
        <TcIcon name="trash" size={size} />
      </button>
      {favBar && (
        <div className="draw-favs" role="toolbar" aria-label="Favorite drawing tools">
          {favs.length === 0 ? (
            <span className="draw-favs-hint">Right-click a tool, or its star, to add it here</span>
          ) : ALL.filter((t) => favs.includes(t.tool)).map((t) => (
            <button key={t.tool} type="button" className="draw-tool" aria-pressed={ctl.tool === t.tool} title={title(t)} aria-label={t.label}
              onClick={() => pick(t.tool)} onContextMenu={(e) => {
                e.preventDefault();
                favorite(t);
              }}>
              <TcIcon name={t.icon} size={18} />
            </button>
          ))}
        </div>
      )}
      {ctl.error && <span className="draw-error" role="alert" title={ctl.error}>!</span>}
    </div>
  );
}
