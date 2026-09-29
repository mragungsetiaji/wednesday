/**
 * Chart snapshots: the chart's own screenshot (candles, zones, quarters, news lines and the
 * drawings are all series primitives, so lightweight-charts paints them into it) with a footer
 * naming the symbol, the timeframes, the time in New York and on the feed clock, and
 * TradingView's notice, which every exported chart image carries.
 */

export const ATTRIBUTION = "TradingView Lightweight Charts™ · © 2025 TradingView, Inc. · tradingview.com";

export interface SnapshotInfo {
  symbol: string;
  timeframes: string[];
  at: number; // ms, real time
  clockOffset: number; // seconds the feed clock is ahead of UTC
  clockName: string; // e.g. "UTC", "NY+7"
}

const two = (n: number) => String(n).padStart(2, "0");

/** "2026-09-24 09:31" of a Date's UTC fields. */
const utcStamp = (d: Date) =>
  `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;

/** "2026-09-24 09:31" in New York. */
export function nyStamp(ms: number): string {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(ms)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** The footer's two lines: what and when (left), and the notice (right). */
export function footerText(info: SnapshotInfo): { left: string; right: string } {
  const feed = utcStamp(new Date(info.at + info.clockOffset * 1000));
  const tfs = info.timeframes.filter(Boolean).join(" · ");
  const clock = info.clockName === "NY+7" || info.clockName === "UTC" ? info.clockName : `feed ${info.clockName}`;
  return {
    left: `${info.symbol}${tfs ? `  ${tfs}` : ""}  ·  ${nyStamp(info.at)} New York  ·  ${feed} ${clock}`,
    right: ATTRIBUTION,
  };
}

/** A file name: "wednesday-XAUUSD-15M-2026-09-24-0931.png". */
export function fileName(info: SnapshotInfo): string {
  const stamp = nyStamp(info.at).replace(/[: ]/g, (c) => (c === " " ? "-" : ""));
  const tfs = info.timeframes.length === 1 ? `-${info.timeframes[0]}` : info.timeframes.length ? "-layout" : "";
  return `wednesday-${info.symbol.replace(/[^A-Za-z0-9]+/g, "")}${tfs}-${stamp}.png`;
}

export interface Part {
  canvas: HTMLCanvasElement;
  x: number; // CSS px in the picture
  y: number;
  w: number;
  h: number;
  label?: string; // a pane's timeframe, in a layout
}

export interface Colors {
  surface: string;
  text: string;
  muted: string;
  grid: string;
  font: string;
}

/**
 * One picture from chart screenshots placed at CSS px rectangles (one for a chart, one per pane
 * for a layout), at the screenshots' pixel ratio, with the footer under them.
 */
export function compose(parts: Part[], width: number, height: number, info: SnapshotInfo, c: Colors): HTMLCanvasElement {
  const ratio = parts.length ? parts[0].canvas.width / Math.max(1, parts[0].w) : window.devicePixelRatio || 1;
  const footer = 30;
  const out = document.createElement("canvas");
  out.width = Math.round(width * ratio);
  out.height = Math.round((height + footer) * ratio);
  const ctx = out.getContext("2d")!;
  ctx.scale(ratio, ratio);
  ctx.fillStyle = c.surface;
  ctx.fillRect(0, 0, width, height + footer);
  for (const p of parts) {
    ctx.drawImage(p.canvas, p.x, p.y, p.w, p.h);
    if (p.label) {
      ctx.font = `600 11px ${c.font}`;
      const w = ctx.measureText(p.label).width + 12;
      ctx.fillStyle = c.surface;
      ctx.globalAlpha = 0.9;
      ctx.fillRect(p.x + 6, p.y + 6, w, 18);
      ctx.globalAlpha = 1;
      ctx.fillStyle = c.text;
      ctx.textBaseline = "middle";
      ctx.fillText(p.label, p.x + 12, p.y + 15);
    }
  }
  // Footer: a hairline, what and when on the left, the notice on the right (or under, when narrow).
  ctx.fillStyle = c.grid;
  ctx.fillRect(0, height, width, 1);
  const { left, right } = footerText(info);
  ctx.textBaseline = "middle";
  ctx.font = `600 11px ${c.font}`;
  ctx.fillStyle = c.text;
  ctx.fillText(left, 10, height + footer / 2);
  ctx.font = `11px ${c.font}`;
  ctx.fillStyle = c.muted;
  const rw = ctx.measureText(right).width;
  const lw = ctx.measureText(left).width;
  if (lw + rw + 40 <= width) {
    ctx.textAlign = "right";
    ctx.fillText(right, width - 10, height + footer / 2);
  } else {
    // Too narrow for one line: grow the picture and put the notice under.
    const taller = document.createElement("canvas");
    taller.width = out.width;
    taller.height = Math.round((height + footer + 18) * ratio);
    const t = taller.getContext("2d")!;
    t.drawImage(out, 0, 0);
    t.scale(ratio, ratio);
    t.fillStyle = c.surface;
    t.fillRect(0, height + footer, width, 18);
    t.font = `11px ${c.font}`;
    t.fillStyle = c.muted;
    t.textBaseline = "middle";
    t.fillText(right, 10, height + footer + 5);
    return taller;
  }
  return out;
}

export const toBlob = (canvas: HTMLCanvasElement): Promise<Blob> =>
  new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("The image couldn't be made"))), "image/png"));

/** Put the image on the clipboard; false where the browser (or the desktop app's WebView2) won't. */
export async function copyImage(blob: Blob): Promise<boolean> {
  try {
    if (!navigator.clipboard || typeof ClipboardItem === "undefined") return false;
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    return true;
  } catch {
    return false;
  }
}

const base64 = async (blob: Blob) => {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
};

/** Save as a PNG file: the desktop app's save dialog, else a browser download. Resolves to where, or null. */
export async function saveImage(blob: Blob, name: string): Promise<string | null> {
  const api = window.pywebview?.api;
  if (api?.save_png) return api.save_png(await base64(blob), name);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return name;
}
