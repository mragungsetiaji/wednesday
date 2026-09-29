import type { DrawTool } from "../drawings";
import type { Drawings } from "../drawingsData";
import {
  CursorIcon,
  EyeIcon,
  HLineIcon,
  PathIcon,
  PositionIcon,
  RectIcon,
  RedoIcon,
  TextIcon,
  TrendlineIcon,
  UndoIcon,
} from "../icons";

const ICON = 20; // px; 18 in the full screen row

const LongIcon = (p: { size?: number }) => <PositionIcon {...p} />;
const ShortIcon = (p: { size?: number }) => <PositionIcon {...p} short />;

const TOOLS: { tool: DrawTool; label: string; Icon: (p: { size?: number }) => React.ReactNode }[] = [
  { tool: "cursor", label: "Cursor", Icon: CursorIcon },
  { tool: "trendline", label: "Trendline: click two points (Alt+T)", Icon: TrendlineIcon },
  { tool: "hline", label: "Horizontal line: click a price (Alt+H)", Icon: HLineIcon },
  { tool: "rect", label: "Rectangle: click two corners (Alt+B)", Icon: RectIcon },
  { tool: "path", label: "Path: click each point, click the last one again (or Enter) to end", Icon: PathIcon },
  { tool: "text", label: "Text: click where it goes, then type", Icon: TextIcon },
  { tool: "long", label: "Long position: click the entry", Icon: LongIcon },
  { tool: "short", label: "Short position: click the entry", Icon: ShortIcon },
];

const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = mac ? "⌘" : "Ctrl+";

/**
 * Drawing tools, TradingView style: a column beside the chart (a row in full screen), then
 * undo, redo and hide all. The selected drawing's own controls float over the chart
 * (DrawingStyleBar).
 */
export function DrawingToolbar({ ctl, row }: { ctl: Drawings; row?: boolean }) {
  if (!ctl.available) return null;
  const size = row ? ICON - 2 : ICON;
  return (
    <div className={`draw-tools${row ? " is-row" : ""}`} role="toolbar" aria-label="Drawing tools"
      aria-orientation={row ? "horizontal" : "vertical"}>
      {TOOLS.map(({ tool, label, Icon }) => (
        <button key={tool} type="button" className="draw-tool" aria-pressed={ctl.tool === tool} title={label} aria-label={label}
          onClick={() => ctl.setTool(ctl.tool === tool && tool !== "cursor" ? "cursor" : tool)}>
          <Icon size={size} />
        </button>
      ))}
      <span className="draw-sep" aria-hidden="true" />
      <button type="button" className="draw-tool" disabled={!ctl.canUndo} title={`Undo (${MOD}Z)`} aria-label="Undo" onClick={ctl.undo}>
        <UndoIcon size={size} />
      </button>
      <button type="button" className="draw-tool" disabled={!ctl.canRedo} title={`Redo (${MOD}${mac ? "⇧Z" : "Y"})`} aria-label="Redo" onClick={ctl.redo}>
        <RedoIcon size={size} />
      </button>
      <button type="button" className="draw-tool" aria-pressed={ctl.hidden} disabled={!ctl.items.length && !ctl.hidden}
        title={ctl.hidden ? "Show drawings" : "Hide drawings"} aria-label={ctl.hidden ? "Show drawings" : "Hide drawings"}
        onClick={() => ctl.setHidden(!ctl.hidden)}>
        <EyeIcon size={size} off={ctl.hidden} />
      </button>
      {ctl.error && <span className="draw-error" role="alert" title={ctl.error}>!</span>}
    </div>
  );
}
