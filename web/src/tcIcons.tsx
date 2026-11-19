/**
 * Icons from TradeCanvas (https://github.com/bonguynvan/tradecanvas, MIT, Copyright (c) 2026
 * TradeCanvas Contributors; see THIRD_PARTY_NOTICES.md): a 24 px grid, 1.75 px round strokes,
 * drawing tools marking their anchors with filled dots. Only the ones the chart toolbars use.
 */

import { TOOL_ICONS } from "./tcToolIcons";

interface IconDef {
  d?: string; // stroked path
  dash?: string; // dashed stroke, for guides
  fills?: readonly string[]; // solid shapes
  soft?: string; // an area filled at 22%
  dots?: readonly (readonly number[])[]; // filled dots: [cx, cy, r = 1.75]
}

const ICONS = {
  crosshairCursor: { d: "M12 3.5v6M12 14.5v6M3.5 12h6M14.5 12h6" },
  plus: { d: "M12 5V19M5 12H19" },
  trendingUp: { d: "M3 17L9 11L13 14.5L21 6.5M15 6.5H21V12.5" },
  trendingDown: { d: "M3 7L9 13L13 9.5L21 17.5M15 17.5H21V11.5" },
  undo: { d: "M9 14L4 9L9 4M4 9H14.5a5.5 5.5 0 0 1 0 11H11" },
  redo: { d: "M15 14L20 9L15 4M20 9H9.5a5.5 5.5 0 0 0 0 11H13" },
  trash: { d: "M4 7H20M9.5 7V4.5h5V7M6.5 7l.8 12.2A1.5 1.5 0 0 0 8.8 20.6h6.4a1.5 1.5 0 0 0 1.5-1.4L17.5 7M10 11v6M14 11v6" },
  camera: { d: "M4.5 7.5h3L9 5h6l1.5 2.5h3A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5V9a1.5 1.5 0 0 1 1.5-1.5zM15.5 13.5a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0z" },
  settings: { d: "M4 7H14M18 7H20M4 17H8M12 17H20M18 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM12 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0z" },
  chevronDown: { d: "M6 9L12 15L18 9" },
  play: { d: "M7 4.5v15L19.5 12z" },
  stop: { d: "M7.5 6h9A1.5 1.5 0 0 1 18 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 16.5v-9A1.5 1.5 0 0 1 7.5 6z" },
  layers: { d: "M12 3.5L21 8.5L12 13.5L3 8.5zM3 12.5L12 17.5L21 12.5M3 16.5L12 21.5L21 16.5" },
  eye: { d: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z" },
  eyeOff: { d: "M4 4L20 20M9.9 5.8A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16 16 0 0 1-2.3 3.1M6.6 7.1C3.9 8.9 2.5 12 2.5 12S6 18.5 12 18.5a9.3 9.3 0 0 0 4.7-1.3M10 10.1a3 3 0 0 0 4 4" },
  maximize: { d: "M8 4H5a1 1 0 0 0-1 1v3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" },
  info: { d: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 11V16.5", dots: [[12, 7.75, 1.25]] },
  calendar: { d: "M5.5 5h13a2 2 0 0 1 2 2v11.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM3.5 10H20.5M8 3V7M16 3V7" },
  save: { d: "M5.5 3.5h10.5l4.5 4.5v10.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2zM8 3.5v5h7v-5M7.5 20.5v-6h9v6" },
  candlestick: {
    d: "M8 3.5V7M8 16V20.5M16 5V8.5M16 15.5V19",
    fills: ["M6.5 7h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z", "M14.5 8.5h3a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1z"],
  },
  magnet: { d: "M6 4V11a6 6 0 0 0 12 0V4M6 4h3.5v7a2.5 2.5 0 0 0 5 0V4H18M6 8h3.5M14.5 8H18" },
  eraser: { d: "M8.5 19.5L3.9 14.9a1.5 1.5 0 0 1 0-2.1l8.5-8.5a1.5 1.5 0 0 1 2.1 0l5.2 5.2a1.5 1.5 0 0 1 0 2.1L12 19.5zM8 9l7 7M12 19.5H20" },
  zoomIn: { d: "M16.5 10.5a6 6 0 1 1-12 0 6 6 0 0 1 12 0zM15 15L20.5 20.5M10.5 7.5V13.5M7.5 10.5H13.5" },
  repeat: { d: "M17 3.5L20.5 7L17 10.5M3.5 11.5V10a3 3 0 0 1 3-3h14M7 20.5L3.5 17L7 13.5M20.5 12.5V14a3 3 0 0 1-3 3h-14" },
  star: { d: "M12 3.5L14.6 9L20.5 9.6L16 13.6L17.3 19.5L12 16.5L6.7 19.5L8 13.6L3.5 9.6L9.4 9z" },
  lock: { d: "M7 10.5h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM8 10.5V7.5a4 4 0 0 1 8 0v3" },
  unlock: { d: "M7 10.5h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM8 10.5V7.5a4 4 0 0 1 7.7-1.5" },
  moon: { d: "M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" },
  sun: { d: "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0zM12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4" },
  more: { dots: [[6, 12, 1.6], [12, 12, 1.6], [18, 12, 1.6]] },
  hollowCandle: { d: "M8 3.5V7M8 16V20.5M16 5V8.5M16 15.5V19M6.5 7h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zM14.5 8.5h3a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1z" },
  heikinAshi: {
    d: "M5.5 9.5V12M5.5 18V20M12 5.5V8M12 14V16M18.5 3V4.5M18.5 10.5V12.5",
    fills: ["M4 12h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-5A.5.5 0 0 1 4 12z", "M10.5 8h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-5a.5.5 0 0 1 .5-.5z", "M17 4.5h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5V5a.5.5 0 0 1 .5-.5z"],
  },
  // Drawing tools.
  trendLine: { d: "M5 18L19 6", dots: [[5, 18], [19, 6]] },
  horizontalLine: { d: "M3 12H21", dots: [[12, 12]] },
  path: { d: "M4 18L9 10L14 14L20 6M16.2 6.6L20 6L19.6 9.8", dots: [[4, 18], [9, 10], [14, 14]] },
  rectangle: { d: "M5.5 6h13A1.5 1.5 0 0 1 20 7.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 16.5v-9A1.5 1.5 0 0 1 5.5 6z", dots: [[4, 6], [20, 18]] },
  text: { d: "M6 6V4.5H18V6M12 4.5V19.5M9.5 19.5H14.5" },
  riskReward: { d: "M5 12V4H19V12M3 12H21", dash: "M5 12V18H19V12", dots: [[5, 12]] },
} satisfies Record<string, IconDef>;

const ALL: Record<string, IconDef> = { ...TOOL_ICONS, ...ICONS };

export type TcIconName = keyof typeof ICONS | keyof typeof TOOL_ICONS;

/** One of the icons above, drawn in `currentColor`. */
export function TcIcon({ name, size = 16 }: { name: TcIconName; size?: number }) {
  const icon: IconDef = ALL[name];
  return (
    <svg className="icon tc-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {icon.soft && <path d={icon.soft} fill="currentColor" fillOpacity={0.22} stroke="none" />}
      {icon.fills?.map((f) => <path key={f} d={f} fill="currentColor" stroke="none" />)}
      {icon.d && <path d={icon.d} />}
      {icon.dash && <path d={icon.dash} strokeDasharray="2 2.5" />}
      {icon.dots?.map(([cx, cy, r = 1.75]) => <circle key={`${cx},${cy}`} cx={cx} cy={cy} r={r} fill="currentColor" stroke="none" />)}
    </svg>
  );
}
