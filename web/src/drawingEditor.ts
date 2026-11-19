import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { Candle, Drawing, DrawingAlert, DrawingKind, DrawingPoint, Sizing } from "./api";
import {
  canAlert,
  dragBus,
  HANDLE_HIT,
  RECT_CORNERS,
  handlesOf,
  hits,
  isPosition,
  LABEL_SIZE,
  newId,
  positionBox,
  positionOf,
  requestAlertForm,
  setTextFont,
  TEXT_SIZE,
  textBox,
  textFontOf,
  toPoint,
  toXY,
  trackPosition,
  type DrawingCtl,
  type PositionTrack,
  xOfLogical,
} from "./drawings";
import { logicalOfTime, stepOf, timeShift } from "./timeMap";
import { fmtMoney, fmtPrice, fmtSigned } from "./format";
import { sizeFor } from "./sizing";
import { withAlpha, type ChartPalette } from "./theme";
import { anchorXY, hitsGeom, labelBox, shapeOf, type Geom, type GeomCtx } from "./shapes";

type XY = { x: number; y: number };

/** How near (px) a candle's open, high, low or close pulls a point to it, with the magnet on. */
const MAGNET_PX = 24;

const TRACK_LABEL: Record<PositionTrack["state"], string> = {
  waiting: "Waiting", missed: "Missed", open: "Open", target: "Target hit", stop: "Stop hit", ended: "Closed",
};

/**
 * A small bell at (cx, cy) in bitmap pixels: a drawing with a price alert. Armed it is filled in
 * the drawing's colour; once fired (off until re-armed) it is an outline, struck through.
 */
function bell(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number, color: string, armed: boolean) {
  const s = 5 * r; // half its width
  ctx.save();
  ctx.lineWidth = Math.max(1, Math.round(1.3 * r));
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(cx - s, cy + s * 0.6);
  ctx.quadraticCurveTo(cx - s * 0.7, cy + s * 0.3, cx - s * 0.7, cy - s * 0.2);
  ctx.arc(cx, cy - s * 0.2, s * 0.7, Math.PI, 0);
  ctx.quadraticCurveTo(cx + s * 0.7, cy + s * 0.3, cx + s, cy + s * 0.6);
  ctx.closePath();
  if (armed) ctx.fill();
  else ctx.stroke();
  ctx.beginPath();
  ctx.arc(cx, cy + s * 0.85, s * 0.25, 0, Math.PI * 2);
  ctx.fill();
  if (!armed) {
    ctx.beginPath();
    ctx.moveTo(cx - s, cy - s);
    ctx.lineTo(cx + s, cy + s);
    ctx.stroke();
  }
  ctx.restore();
}

/** Draws the drawings (and the one being placed) above the candles, handles on the selected one. */
class DrawingsRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: DrawingsPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, times, shown, selected, hovered, palette, font, sizing, candles, live, alerts } = this.source;
    if (!chart || !series || !times.length || !shown.length) return;
    const yOf = (p: number) => series.priceToCoordinate(p);

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const g: GeomCtx = {
        w: W / hr,
        h: bitmapSize.height / vr,
        yOf,
        xOfT: (t) => xOfLogical(chart, logicalOfTime(times, t)),
        candles,
        step: stepOf(times),
        bull: palette.bull,
        bear: palette.bear,
      };
      const fontPx = Math.round(10 * vr);
      const pillWidth = (text: string) => {
        ctx.font = `600 ${fontPx}px ${font}`;
        return ctx.measureText(text).width + 10 * hr;
      };
      // A text pill centred on cx, or starting at cx with `left`.
      const pill = (text: string, cx: number, cy: number, bg: string, fg: string, left = false) => {
        ctx.font = `600 ${fontPx}px ${font}`;
        ctx.textBaseline = "middle";
        const w = pillWidth(text);
        const h = fontPx + 6 * vr;
        const x = Math.min(Math.max(0, left ? cx : cx - w / 2), W - w);
        ctx.fillStyle = bg;
        ctx.beginPath();
        ctx.roundRect(x, cy - h / 2, w, h, 3 * hr);
        ctx.fill();
        ctx.fillStyle = fg;
        ctx.fillText(text, x + 5 * hr, cy);
        return h;
      };
      // A line's label in its colour: left or right aligned at x, its bottom just above y.
      const label = (d: Drawing, x: number, y: number, align: "left" | "right") => {
        const text = d.props?.text;
        if (!text) return;
        ctx.setLineDash([]);
        ctx.font = `500 ${Math.round((d.style.size ?? LABEL_SIZE) * vr)}px ${font}`;
        ctx.textBaseline = "bottom";
        ctx.textAlign = align;
        ctx.fillStyle = d.style.color ?? palette.draw;
        ctx.fillText(text, x, y - 4 * vr);
        ctx.textAlign = "left";
      };
      // A tool from shapes.ts: its lines, then its areas, then its labels, all in its colour.
      const drawGeom = (gm: Geom, color: string, lw: number, dash: number[]) => {
        for (const f of gm.fills) {
          if (f.pts.length < 3) continue;
          ctx.fillStyle = withAlpha(f.color ?? color, f.alpha);
          ctx.beginPath();
          f.pts.forEach((q, i) => (i ? ctx.lineTo(q.x * hr, q.y * vr) : ctx.moveTo(q.x * hr, q.y * vr)));
          ctx.closePath();
          ctx.fill();
        }
        ctx.lineJoin = "round";
        for (const l of gm.lines) {
          if (l.pts.length < 2) continue;
          ctx.globalAlpha = l.alpha ?? 1;
          ctx.strokeStyle = l.color ?? color;
          ctx.lineWidth = l.width ? Math.max(lw, Math.round(l.width * hr)) : lw;
          ctx.lineCap = l.width ? "round" : ctx.lineCap;
          ctx.setLineDash(l.dash ? [5 * hr, 4 * hr] : dash);
          ctx.beginPath();
          l.pts.forEach((q, i) => (i ? ctx.lineTo(q.x * hr, q.y * vr) : ctx.moveTo(q.x * hr, q.y * vr)));
          if (l.closed) ctx.closePath();
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
        ctx.setLineDash([]);
        for (const lb of gm.labels) {
          const size = Math.round((lb.size ?? 12) * vr);
          ctx.font = `${lb.pill || lb.box ? 600 : 500} ${size}px ${font}`;
          const b = labelBox(lb);
          const c = lb.color ?? color;
          if (lb.pill || lb.box) {
            ctx.beginPath();
            ctx.roundRect(b.x * hr, b.y * vr, b.w * hr, b.h * vr, 3 * hr);
            ctx.fillStyle = lb.pill ? c : withAlpha(c, 0.14);
            ctx.fill();
            if (lb.box) {
              ctx.lineWidth = Math.max(1, Math.round(hr));
              ctx.strokeStyle = c;
              ctx.stroke();
            }
          }
          ctx.fillStyle = lb.pill ? palette.surface : lb.box ? palette.textStrong : c;
          ctx.textBaseline = "middle";
          ctx.textAlign = "left";
          const pad = lb.pill || lb.box ? 6 : 0;
          ctx.fillText(lb.text, (b.x + pad) * hr, (b.y + b.h / 2) * vr);
        }
      };

      for (const d of shown) {
        const def = shapeOf(d.kind);
        const xy = def?.anchored ? [anchorXY(d, g)] : d.points.map((pt) => toXY(chart, series, times, pt));
        if (xy.some((p) => p === null)) continue;
        const pts = (xy as XY[]).map((p) => ({ x: p.x * hr, y: p.y * vr }));
        const color = d.style.color ?? palette.draw;
        const lw = Math.max(1, Math.round(d.style.width * hr));
        ctx.strokeStyle = color;
        ctx.lineWidth = lw;
        ctx.lineCap = d.style.dash === "dotted" ? "round" : "butt";
        ctx.setLineDash(d.style.dash === "dashed" ? [6 * hr, 4 * hr] : d.style.dash === "dotted" ? [0.1, 3 * lw] : []);

        if (def) {
          drawGeom(def.geom(xy as XY[], d, g), color, lw, ctx.getLineDash());
        } else if (isPosition(d)) {
          ctx.setLineDash([]);
          const box = positionBox(d, xy as XY[], yOf);
          if (!box) continue;
          const [x0, x1] = [box.x0 * hr, box.x1 * hr];
          const [ye, ys, yt] = [box.ye * vr, box.ys * vr, box.yt * vr];
          const track = trackPosition(d, candles, live);
          const faded = track?.state === "missed"; // its entry never came: the plan is spent
          const band = (y0: number, y1: number, c: string) => {
            ctx.fillStyle = withAlpha(c, faded ? 0.06 : 0.14);
            ctx.fillRect(x0, Math.min(y0, y1), x1 - x0, Math.abs(y1 - y0));
          };
          const edge = (y: number, c: string) => {
            ctx.strokeStyle = c;
            ctx.lineWidth = Math.max(1, Math.round(hr));
            ctx.beginPath();
            ctx.moveTo(x0, Math.round(y) + 0.5);
            ctx.lineTo(x1, Math.round(y) + 0.5);
            ctx.stroke();
          };
          band(ye, yt, palette.bull);
          band(ye, ys, palette.bear);
          edge(yt, withAlpha(palette.bull, faded ? 0.4 : 0.8));
          edge(ys, withAlpha(palette.bear, faded ? 0.4 : 0.8));
          edge(ye, withAlpha(palette.textStrong, faded ? 0.4 : 0.85));

          const { entry, stop, target: tp } = positionOf(d);
          const risk = Math.abs(entry - stop);
          const rr = risk ? Math.abs(tp - entry) / risk : 0;
          const size = sizing ? sizeFor(sizing, risk) : null;
          const money = (v: number) => (size && sizing ? `  ${v < 0 ? "−" : "+"}${fmtMoney(Math.abs(v), sizing.currency)}` : "");
          if (track && track.fillT !== null) {
            // Filled: shade entry to where price stands (or where it closed), green in profit, red in loss.
            const xAt = (t: number) => xOfLogical(chart, logicalOfTime(times, t));
            const xf = xAt(track.fillT);
            const xt = xAt(track.t);
            const yp = yOf(track.price);
            if (xf !== null && xt !== null && yp !== null) {
              const clampX = (x: number) => Math.min(Math.max(x * hr, x0), x1);
              const [xa, xb] = [clampX(xf), clampX(xt)];
              const y = yp * vr;
              const gain = (track.price - entry) * (d.kind === "long" ? 1 : -1); // in price, + in profit
              const c = gain >= 0 ? palette.bull : palette.bear;
              ctx.fillStyle = withAlpha(c, 0.32);
              ctx.fillRect(xa, Math.min(ye, y), Math.max(xb - xa, hr), Math.abs(y - ye));
              ctx.strokeStyle = c;
              ctx.lineWidth = Math.max(1, Math.round(hr));
              ctx.setLineDash([2 * hr, 3 * hr]);
              ctx.beginPath();
              ctx.moveTo(Math.round(xb) + 0.5, ye);
              ctx.lineTo(Math.round(xb) + 0.5, y);
              ctx.stroke();
              ctx.setLineDash([]);
              ctx.beginPath();
              ctx.arc(xb, y, 3.5 * hr, 0, Math.PI * 2);
              ctx.fillStyle = palette.surface;
              ctx.fill();
              ctx.lineWidth = Math.max(1, Math.round(1.5 * hr));
              ctx.stroke();
              const pct = (gain / entry) * 100;
              const text = `${TRACK_LABEL[track.state]} ${fmtSigned(gain)} (${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)${
                size && risk ? money((gain / risk) * size.risk) : ""}`;
              const w = pillWidth(text);
              const right = xb + 8 * hr + w <= W;
              pill(text, right ? xb + 8 * hr : xb - 8 * hr - w, y, c, palette.surface, true);
            }
          }

          // Target above / stop below a long (the other way for a short), just outside the box,
          // while the position is hovered or selected: TradingView keeps them out of the way.
          const full = d.id === selected || d.id === hovered;
          if (full) {
            const cx = (x0 + x1) / 2;
            const out = (y: number) => y + (y < ye ? -1 : 1) * (fontPx / 2 + 6 * vr);
            const pct = ((tp - entry) / entry) * 100;
            pill(`Target ${fmtPrice(tp)}  ${fmtSigned(tp - entry)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)${size ? money(size.risk * rr) : ""}`,
              cx, out(yt), palette.bull, palette.surface);
            pill(`Stop ${fmtPrice(stop)}  ${fmtSigned(stop - entry)}${size ? money(-size.risk) : ""}`,
              cx, out(ys), palette.bear, palette.surface);
            const lots = size ? ` · ${size.lots} lot` : sizing ? " · min lot over budget" : "";
            const wait = track?.state === "waiting" ? " · waiting for entry" : track?.state === "missed" ? " · entry missed" : "";
            // Centred between the entry and end handles when it fits there, else past the end handle.
            const text = `${d.kind === "long" ? "Long" : "Short"} ${fmtPrice(entry)} · ${rr.toFixed(2)}R${lots}${wait}`;
            const fits = pillWidth(text) <= x1 - x0 - 24 * hr;
            pill(text, fits ? cx : x1 + 10 * hr, ye, palette.textStrong, palette.surface, !fits);
          }
        } else if (d.kind === "trendline" || d.kind === "path") {
          ctx.lineJoin = "round";
          ctx.beginPath();
          ctx.moveTo(pts[0].x, pts[0].y);
          for (const q of pts.slice(1)) ctx.lineTo(q.x, q.y);
          ctx.stroke();
          // Over the right end, like a level's name.
          const right = pts.reduce((a, q) => (q.x > a.x ? q : a));
          label(d, right.x, right.y, "right");
          const al = d.kind === "trendline" ? alerts[d.id] : undefined;
          if (al) {
            ctx.setLineDash([]);
            const end = pts[0].x > pts[1].x ? pts[0] : pts[1];
            bell(ctx, end.x + 12 * hr, end.y - 10 * vr, hr, al.armed ? color : palette.muted, al.armed);
          }
        } else if (d.kind === "text") {
          const size = d.style.size ?? TEXT_SIZE;
          ctx.font = textFontOf(Math.round(size * vr));
          ctx.textBaseline = "middle";
          ctx.fillStyle = color;
          ctx.fillText(d.props?.text ?? "", pts[0].x, pts[0].y);
          if (d.id === selected) {
            const b = textBox(d, (xy as XY[])[0]);
            ctx.setLineDash([3 * hr, 3 * hr]);
            ctx.lineWidth = Math.max(1, Math.round(hr));
            ctx.strokeRect(Math.round((b.x - 4) * hr) + 0.5, Math.round((b.y - 2) * vr) + 0.5, Math.round((b.w + 8) * hr), Math.round((b.h + 4) * vr));
          }
        } else if (d.kind === "hline") {
          const y = Math.round(pts[0].y) + 0.5;
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(W, y);
          ctx.stroke();
          label(d, 8 * hr, y, "left");
          ctx.setLineDash([]);
          // Its price in a pill at the right edge, like the chart's own price labels.
          const fontPx = Math.round(10 * vr);
          ctx.font = `600 ${fontPx}px ${font}`;
          ctx.textBaseline = "middle";
          const text = fmtPrice(d.points[0].p);
          const w = ctx.measureText(text).width + 10 * hr;
          const h = fontPx + 6 * vr;
          ctx.fillStyle = color;
          ctx.fillRect(W - w - 4 * hr, y - h / 2, w, h);
          ctx.fillStyle = palette.surface;
          ctx.fillText(text, W - w + 1 * hr, y);
          if (alerts[d.id]) bell(ctx, W - w - 14 * hr, y, hr, alerts[d.id].armed ? color : palette.muted, alerts[d.id].armed);
        } else {
          const x = Math.min(pts[0].x, pts[1].x);
          const y = Math.min(pts[0].y, pts[1].y);
          const w = Math.abs(pts[1].x - pts[0].x);
          const h = Math.abs(pts[1].y - pts[0].y);
          ctx.fillStyle = withAlpha(color, 0.14);
          ctx.fillRect(x, y, w, h);
          ctx.strokeRect(Math.round(x) + 0.5, Math.round(y) + 0.5, Math.round(w), Math.round(h));
          label(d, x, y, "left"); // over its top left corner
          const al = alerts[d.id];
          if (al) {
            ctx.setLineDash([]);
            bell(ctx, x + w - 10 * hr, y + 10 * vr, hr, al.armed ? color : palette.muted, al.armed);
          }
        }

        if (d.id === selected) {
          ctx.setLineDash([]);
          const r = 4.5 * hr;
          for (const hp of handlesOf(d, xy, W / hr, yOf)) {
            ctx.beginPath();
            ctx.arc(hp.x * hr, hp.y * vr, r, 0, Math.PI * 2);
            ctx.fillStyle = palette.surface;
            ctx.fill();
            ctx.lineWidth = Math.max(1, Math.round(1.5 * hr));
            ctx.strokeStyle = color;
            ctx.stroke();
          }
        }
      }
    });
  }
}

class DrawingsView implements IPrimitivePaneView {
  constructor(private readonly source: DrawingsPrimitive) {}
  zOrder(): "top" {
    return "top";
  }
  renderer(): IPrimitivePaneRenderer {
    return new DrawingsRenderer(this.source);
  }
}

class DrawingsPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  times: number[] = [];
  shown: Drawing[] = [];
  selected: string | null = null;
  hovered: string | null = null; // shows a position's target and stop like the selected one
  candles: Candle[] = []; // to track positions: filled, and where price stands
  live: Candle | null = null; // the forming candle
  sizing: Sizing | null = null;
  alerts: Record<string, DrawingAlert> = {};
  font = "system-ui, sans-serif";
  private requestUpdate?: () => void;
  private readonly views = [new DrawingsView(this)];

  constructor(public palette: ChartPalette) {}

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this.chart = param.chart as IChartApi;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  update(patch: Partial<Pick<DrawingsPrimitive, "times" | "shown" | "selected" | "hovered" | "candles" | "live" | "palette" | "font" | "sizing" | "alerts">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

const make = (kind: DrawingKind, points: DrawingPoint[]): Drawing => ({
  id: newId(),
  kind,
  points,
  style: { color: null, width: shapeOf(kind)?.width ?? (kind === "trendline" || kind === "hline" || kind === "path" ? 2 : 1), dash: "solid",
    ...(kind === "text" ? { size: TEXT_SIZE } : {}) },
  props: kind === "text" ? { text: "Text" } : shapeOf(kind)?.text ? { text: shapeOf(kind)!.text } : null,
  timeframes: null,
  locked: false,
  hidden: false,
});

/**
 * Makes and edits drawings on one chart. With a drawing tool picked, clicks place points:
 * a horizontal line takes one, a trendline or rectangle two (click, click; or press and drag).
 * With the cursor, pressing a drawing selects it and drags it (or the handle under the
 * pointer); anywhere else the chart pans as usual.
 *
 * It listens in the capture phase and claims a press by cancelling it, which also keeps the
 * chart from starting a pan under a drag.
 */
export class DrawingEditor {
  private readonly primitive: DrawingsPrimitive;
  private ctl: DrawingCtl | null = null;
  private times: number[] = []; // candle opens, the forming candle's included: what the chart shows
  private closed: number[] = []; // closed candle opens only
  private liveTime: number | null = null;
  private step = 60; // seconds per candle
  private draft: Drawing | null = null; // the drawing being placed
  private dragging: Drawing | null = null; // one being dragged on any chart in this window (dragBus)
  private readonly unsubscribeDrag: () => void;
  private placing: { start: XY; moved: boolean } | null = null;
  private bars: Candle[] = []; // closed candles, for the magnet
  private liveBar: Candle | null = null;
  private zoom: { start: XY; box: HTMLDivElement } | null = null; // the zoom tool's drag
  private drag: { orig: Drawing; last: Drawing; handle: number | null; startL: number; startP: number; startXY: XY; moved: boolean } | null = null;
  private freehand = false; // the draft is a brush stroke: every move adds a point

  constructor(
    private readonly chart: IChartApi,
    private readonly series: ISeriesApi<SeriesType>,
    private readonly el: HTMLElement,
    palette: ChartPalette,
  ) {
    this.primitive = new DrawingsPrimitive(palette);
    series.attachPrimitive(this.primitive);
    el.addEventListener("pointerdown", this.onDown, true);
    el.addEventListener("pointermove", this.onHover);
    el.addEventListener("pointerleave", this.onLeave);
    el.addEventListener("dblclick", this.onDoubleClick, true);
    el.addEventListener("contextmenu", this.onContextMenu, true);
    window.addEventListener("keydown", this.onKey);
    this.unsubscribeDrag = dragBus.subscribe((d) => {
      this.dragging = d;
      this.render();
    });
  }

  destroy(): void {
    this.unsubscribeDrag();
    this.stopTracking();
    this.el.removeEventListener("pointerdown", this.onDown, true);
    this.el.removeEventListener("pointermove", this.onHover);
    this.el.removeEventListener("pointerleave", this.onLeave);
    this.el.removeEventListener("dblclick", this.onDoubleClick, true);
    this.el.removeEventListener("contextmenu", this.onContextMenu, true);
    window.removeEventListener("keydown", this.onKey);
    this.series.detachPrimitive(this.primitive);
  }

  update(patch: { ctl?: DrawingCtl | null; candles?: Candle[]; live?: Candle | null; palette?: ChartPalette; font?: string }): void {
    if (patch.ctl !== undefined) {
      this.ctl = patch.ctl;
      const moving = this.dragging;
      if (moving && !this.drag && patch.ctl?.items.includes(moving)) dragBus.clear(moving); // the drop is saved
      if (this.draft && (!this.ctl || this.ctl.tool !== this.draft.kind)) this.cancelPlacing(); // Esc, or another tool
    }
    if (patch.candles) {
      this.closed = patch.candles.map((c) => c.time);
      this.bars = patch.candles;
      this.step = stepOf(this.closed);
      this.primitive.update({ candles: patch.candles });
    }
    if (patch.live !== undefined) {
      this.liveBar = patch.live;
      this.primitive.update({ live: patch.live });
    }
    const liveTime = patch.live === undefined ? this.liveTime : patch.live?.time ?? null;
    if (patch.candles || liveTime !== this.liveTime) {
      // The forming candle is on the chart too: a point on it maps to it, not to a guess past the last close.
      this.liveTime = liveTime;
      const last = this.closed[this.closed.length - 1];
      this.times = liveTime !== null && last !== undefined && liveTime > last ? [...this.closed, liveTime] : this.closed;
    }
    if (patch.palette) this.primitive.update({ palette: patch.palette });
    if (patch.font) {
      setTextFont(patch.font);
      this.primitive.update({ font: patch.font });
    }
    this.render();
  }

  private render(): void {
    const ctl = this.ctl;
    const moving = this.dragging;
    const items = ctl && moving ? ctl.items.map((d) => (d.id === moving.id ? moving : d)) : ctl?.items ?? [];
    const shown = ctl && !ctl.hidden ? items.filter((d) => !d.hidden) : [];
    this.primitive.update({
      times: this.times,
      shown: this.draft ? [...shown, this.draft] : shown,
      selected: this.draft ? this.draft.id : ctl?.selected ?? null,
      sizing: ctl?.sizing ?? null,
      alerts: ctl?.alerts ?? {},
    });
  }

  private local(e: PointerEvent): XY {
    const r = this.el.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  private inPane(p: XY): boolean {
    const h = this.chart.panes()[0]?.getHeight() ?? 0;
    return p.x >= 0 && p.x <= this.chart.timeScale().width() && p.y >= 0 && p.y <= h;
  }

  private pointAt(p: XY): DrawingPoint | null {
    const pt = toPoint(this.chart, this.series, this.times, p.x, p.y);
    if (!pt || !this.ctl?.magnet) return pt;
    // The magnet: the open, high, low or close of the candle under the pointer, when one is near.
    const l = this.chart.timeScale().coordinateToLogical(p.x);
    if (l === null) return pt;
    const i = Math.round(l);
    const bar = i >= 0 && i < this.bars.length ? this.bars[i] : i === this.bars.length ? this.liveBar : null;
    if (!bar) return pt;
    let best: number | null = null;
    let gap = MAGNET_PX;
    for (const v of [bar.open, bar.high, bar.low, bar.close]) {
      const y = this.series.priceToCoordinate(v);
      if (y !== null && Math.abs(y - p.y) <= gap) {
        gap = Math.abs(y - p.y);
        best = v;
      }
    }
    return best === null ? pt : { ...pt, p: best };
  }

  private xyOf(d: Drawing) {
    return d.points.map((pt) => toXY(this.chart, this.series, this.times, pt));
  }

  private readonly yOf = (price: number) => this.series.priceToCoordinate(price);

  /** The drawing (and handle of the selected one) under the pointer, topmost first. */
  private hitTest(p: XY): { d: Drawing; handle: number | null } | null {
    const ctl = this.ctl;
    if (!ctl || ctl.hidden) return null;
    const shown = ctl.items.filter((d) => !d.hidden);
    const sel = shown.find((d) => d.id === ctl.selected);
    if (sel) {
      const handle = handlesOf(sel, this.xyOf(sel), this.chart.timeScale().width(), this.yOf)
        .findIndex((h) => Math.hypot(h.x - p.x, h.y - p.y) <= HANDLE_HIT);
      if (handle >= 0) return { d: sel, handle };
      if (this.on(sel, p)) return { d: sel, handle: null };
    }
    for (let i = shown.length - 1; i >= 0; i--) {
      if (this.on(shown[i], p)) return { d: shown[i], handle: null };
    }
    return null;
  }

  /** What the tools of shapes.ts need to work out their geometry on this chart. */
  private geomCtx(): GeomCtx {
    return {
      w: this.chart.timeScale().width(),
      h: this.chart.panes()[0]?.getHeight() ?? this.el.clientHeight,
      yOf: this.yOf,
      xOfT: (t) => xOfLogical(this.chart, logicalOfTime(this.times, t)),
      candles: this.bars,
      step: this.step,
      bull: this.primitive.palette.bull,
      bear: this.primitive.palette.bear,
    };
  }

  /** Whether the pointer is on drawing d. */
  private on(d: Drawing, p: XY): boolean {
    const def = shapeOf(d.kind);
    if (!def) return hits(d, this.xyOf(d), p, this.yOf);
    const g = this.geomCtx();
    const xy = def.anchored ? [anchorXY(d, g)] : this.xyOf(d);
    if (xy.some((q) => q === null)) return false;
    return hitsGeom(def.geom(xy as XY[], d, g), p);
  }

  private readonly onDown = (e: PointerEvent) => {
    const ctl = this.ctl;
    if (!ctl || e.button !== 0) return;
    const p = this.local(e);
    if (!this.inPane(p)) return;

    if (ctl.tool === "eraser") {
      claim(e);
      const hit = this.hitTest(p);
      if (hit && !hit.d.locked) ctl.remove(hit.d.id);
      return;
    }
    if (ctl.tool === "zoom") {
      claim(e);
      const box = document.createElement("div");
      box.className = "draw-zoom-box";
      if (getComputedStyle(this.el).position === "static") this.el.style.position = "relative";
      this.el.appendChild(box);
      this.zoom = { start: p, box };
      this.zoomBox(p);
      this.track();
      return;
    }
    if (ctl.tool !== "cursor") {
      claim(e);
      const pt = this.pointAt(p);
      if (!pt) return;
      const def = shapeOf(ctl.tool);
      if (def && !this.draft) {
        if (def.anchored) {
          const g = this.geomCtx();
          ctl.create({ ...make(ctl.tool, [pt]), props: { text: def.text, ax: p.x / g.w, ay: p.y / g.h } });
          this.done();
          return;
        }
        if (def.points === 1) {
          ctl.create(make(ctl.tool, [pt]));
          this.done();
          return;
        }
        if (def.points === "free") {
          this.draft = make(ctl.tool, [pt]);
          this.placing = { start: p, moved: false };
          this.freehand = true;
          this.track();
          this.render();
          return;
        }
      }
      if (def && this.draft && typeof def.points === "number") {
        // Each click fixes the point under the pointer; the last one finishes it.
        const fixed = [...this.draft.points.slice(0, -1), pt];
        if (fixed.length >= def.points) this.finishWith(fixed);
        else {
          this.draft = { ...this.draft, points: [...fixed, pt] };
          this.render();
        }
        return;
      }
      if (ctl.tool === "hline") {
        ctl.create(make("hline", [pt]));
        this.done();
      } else if (ctl.tool === "long" || ctl.tool === "short") {
        const d = this.newPosition(ctl.tool, pt, p.y);
        if (d) ctl.create(d);
        this.done();
      } else if (ctl.tool === "text") {
        const d = make("text", [pt]);
        ctl.create(d);
        ctl.setTool("cursor");
        ctl.editText(d.id); // straight into typing it
      } else if ((ctl.tool === "path" || def?.points === "multi") && this.draft) {
        // Each click fixes a point; clicking the last one again (a double click) ends the path.
        const fixed = this.draft.points.slice(0, -1);
        const last = toXY(this.chart, this.series, this.times, fixed[fixed.length - 1]);
        if (last && Math.hypot(last.x - p.x, last.y - p.y) <= 5) this.finishWith(fixed);
        else {
          this.draft = { ...this.draft, points: [...fixed, pt, pt] };
          this.render();
        }
      } else if (this.draft) {
        this.finish(pt); // the second click
      } else {
        this.draft = make(ctl.tool, [pt, pt]);
        this.placing = { start: p, moved: false };
        this.track();
        this.render();
      }
      return;
    }

    const hit = this.hitTest(p);
    if (!hit) {
      if (ctl.selected) ctl.select(null);
      return; // the chart pans
    }
    claim(e);
    ctl.select(hit.d.id);
    if (hit.d.locked) return;
    const l = this.chart.timeScale().coordinateToLogical(p.x);
    const price = this.series.coordinateToPrice(p.y);
    if (l === null || price === null) return;
    // A text's handle is only a grip: it moves the whole text.
    const handle = hit.d.kind === "text" ? null : hit.handle;
    this.drag = { orig: hit.d, last: hit.d, handle, startL: l, startP: price, startXY: p, moved: false };
    this.track();
  };

  private readonly onMove = (e: PointerEvent) => {
    const p = this.local(e);
    if (this.zoom) {
      this.zoomBox(p);
    } else if (this.drag) {
      const next = this.dragged(p);
      if (!next) return;
      this.drag.moved = true;
      this.drag.last = next;
      dragBus.publish(next); // every chart redraws it; React hears only the drop
    } else if (this.draft && this.freehand) {
      const pt = this.pointAt(p);
      const last = this.draft.points[this.draft.points.length - 1];
      const lastXY = last ? toXY(this.chart, this.series, this.times, last) : null;
      if (!pt || (lastXY && Math.hypot(lastXY.x - p.x, lastXY.y - p.y) < 3) || this.draft.points.length >= 2000) return;
      this.draft = { ...this.draft, points: [...this.draft.points, pt] };
      this.render();
    } else if (this.draft && this.placing) {
      const pt = this.pointAt(p);
      if (!pt) return;
      this.placing.moved ||= Math.hypot(p.x - this.placing.start.x, p.y - this.placing.start.y) > 4;
      this.draft = { ...this.draft, points: [...this.draft.points.slice(0, -1), pt] }; // the point under the pointer
      this.render();
    }
  };

  private readonly onUp = (e: PointerEvent) => {
    if (this.zoom) {
      const { start, box } = this.zoom;
      this.zoom = null;
      box.remove();
      this.stopTracking();
      const end = this.local(e);
      const ts = this.chart.timeScale();
      const a = ts.coordinateToLogical(Math.min(start.x, end.x));
      const b = ts.coordinateToLogical(Math.max(start.x, end.x));
      if (a !== null && b !== null && Math.abs(end.x - start.x) > 6) ts.setVisibleLogicalRange({ from: a, to: b });
      this.done();
    } else if (this.drag) {
      const { moved, last } = this.drag;
      this.drag = null;
      this.stopTracking();
      if (moved) {
        this.ctl?.change(last, true);
        // Keep drawing it until the saved items hold it (React may commit a frame later), or 1 s at most.
        window.setTimeout(() => dragBus.clear(last), 1000);
      }
    } else if (this.draft && this.freehand) {
      this.freehand = false;
      const pts = this.draft.points;
      if (pts.length >= 2) this.finishWith(pts);
      else this.cancelPlacing();
    } else if (this.draft && this.draft.points.length === 2 && this.draft.kind !== "path" && shapeOf(this.draft.kind)?.points !== "multi"
      && this.placing?.moved) {
      const pt = this.pointAt(this.local(e));
      const n = shapeOf(this.draft.kind)?.points;
      if (pt && typeof n === "number" && n > 2) {
        // Pressed and dragged the first leg: the next clicks place the rest.
        this.draft = { ...this.draft, points: [this.draft.points[0], pt, pt] };
        this.render();
      } else if (pt) this.finish(pt); // pressed and dragged: done on release
    }
    // A click without a drag leaves the second point to the next click.
  };

  private readonly onHover = (e: PointerEvent) => {
    const ctl = this.ctl;
    if (!ctl || this.drag) return;
    const p = this.local(e);
    let cursor = "";
    let hovered: string | null = null;
    if (ctl.tool === "eraser" && this.inPane(p)) {
      const hit = this.hitTest(p);
      cursor = hit && !hit.d.locked ? "pointer" : "crosshair";
      hovered = hit?.d.id ?? null;
    } else if (ctl.tool === "zoom" && this.inPane(p)) cursor = "zoom-in";
    else if (ctl.tool !== "cursor" && this.inPane(p)) cursor = "crosshair";
    else if (this.inPane(p)) {
      const hit = this.hitTest(p);
      if (hit) cursor = hit.d.locked ? "pointer" : hit.handle !== null ? "grab" : "move";
      hovered = hit?.d.id ?? null;
    }
    this.setCursor(cursor);
    this.setHovered(hovered);
  };

  private readonly onLeave = () => {
    if (!this.drag) this.setCursor("");
    this.setHovered(null);
  };

  private setHovered(id: string | null): void {
    if (this.primitive.hovered !== id) this.primitive.update({ hovered: id });
  }

  private setCursor(cursor: string): void {
    if (cursor) this.el.style.setProperty("--draw-cursor", cursor);
    else this.el.style.removeProperty("--draw-cursor");
    this.el.toggleAttribute("data-draw-cursor", !!cursor);
  }

  /**
   * A position at the click: entry there, the stop 40px away and the target at 2R, running
   * 20 candles (of this timeframe's length) to the right. The handles set the real ones.
   */
  private newPosition(kind: "long" | "short", pt: DrawingPoint, y: number): Drawing | null {
    const away = this.series.coordinateToPrice(kind === "long" ? y + 40 : y - 40);
    if (away === null) return null;
    const risk = Math.abs(pt.p - away);
    if (!risk) return null;
    const sign = kind === "long" ? 1 : -1;
    const end = pt.t + 20 * this.step;
    return {
      ...make(kind, [pt, { t: end, p: pt.p }]),
      props: { stop: pt.p - sign * risk, target: pt.p + sign * 2 * risk },
    };
  }

  /** A position dragged by handle h (entry, stop, target, end) to pt; each price stays on its side. */
  private draggedPosition(orig: Drawing, h: number, pt: DrawingPoint): Drawing {
    const { entry, stop, target } = positionOf(orig);
    const sign = orig.kind === "long" ? 1 : -1;
    const eps = Math.abs(entry) * 1e-5 || 1e-5;
    const [start, end] = orig.points;
    if (h === 0) {
      const lo = Math.min(stop, target) + eps;
      const hi = Math.max(stop, target) - eps;
      const p = Math.min(hi, Math.max(lo, pt.p));
      return { ...orig, points: [{ t: pt.t, p }, { t: end.t, p }] };
    }
    if (h === 1) {
      const s = sign > 0 ? Math.min(pt.p, entry - eps) : Math.max(pt.p, entry + eps);
      return { ...orig, props: { ...orig.props, stop: s } };
    }
    if (h === 2) {
      const t = sign > 0 ? Math.max(pt.p, entry + eps) : Math.min(pt.p, entry - eps);
      return { ...orig, props: { ...orig.props, target: t } };
    }
    const minEnd = start.t + this.step;
    return { ...orig, points: [start, { t: Math.max(pt.t, minEnd), p: end.p }] };
  }

  /** The drag's drawing at pointer p: a handle moves its point (or corner), the body moves it all. */
  private dragged(p: XY): Drawing | null {
    const drag = this.drag!;
    const { orig } = drag;
    if (drag.handle === null && shapeOf(orig.kind)?.anchored) {
      // Anchored to the pane: it moves by the pointer's share of the pane's size.
      const g = this.geomCtx();
      const clamp = (v: number) => Math.min(0.98, Math.max(0, v));
      return { ...orig, props: { ...orig.props, ax: clamp((orig.props?.ax ?? 0.1) + (p.x - drag.startXY.x) / g.w),
        ay: clamp((orig.props?.ay ?? 0.1) + (p.y - drag.startXY.y) / g.h) } };
    }
    if (drag.handle === null) {
      const l = this.chart.timeScale().coordinateToLogical(p.x);
      const price = this.series.coordinateToPrice(p.y);
      if (l === null || price === null) return null;
      // Whole candles of this chart, as a time shift: every point moves by the same time, so the
      // drawing keeps its duration (and its place on the other timeframes) across a gap.
      const dt = timeShift(this.times, drag.startL, l, this.step);
      const dp = price - drag.startP;
      const moved: Drawing = { ...orig, points: orig.points.map((pt) => ({ t: pt.t + dt, p: pt.p + dp })) };
      if (isPosition(orig)) {
        const { stop, target } = positionOf(orig);
        moved.props = { ...orig.props, stop: stop + dp, target: target + dp };
      }
      return moved;
    }
    const pt = this.pointAt(p);
    if (!pt) return null;
    if (isPosition(orig)) return this.draggedPosition(orig, drag.handle, pt);
    if (orig.kind === "rect") {
      const [ti, pi] = RECT_CORNERS[drag.handle];
      const points = orig.points.map((q) => ({ ...q }));
      points[ti].t = pt.t;
      points[pi].p = pt.p;
      return { ...orig, points };
    }
    const points = [...orig.points];
    points[drag.handle] = pt;
    return { ...orig, points };
  }

  private finish(pt: DrawingPoint): void {
    this.finishWith([this.draft!.points[0], pt]);
  }

  private finishWith(points: DrawingPoint[]): void {
    const d = { ...this.draft!, points };
    this.cancelPlacing();
    if (points.length >= 2) this.ctl?.create(d);
    this.done();
  }

  /** A drawing (or zoom) is done: back to the cursor, unless "stay in drawing mode" is on. */
  private done(): void {
    if (!this.ctl?.stay) this.ctl?.setTool("cursor");
  }

  /** The zoom tool's box, from where the drag began to p, the pane's full height. */
  private zoomBox(p: XY): void {
    const z = this.zoom;
    if (!z) return;
    const h = this.chart.panes()[0]?.getHeight() ?? this.el.clientHeight;
    const x = Math.max(0, Math.min(z.start.x, p.x));
    const w = Math.min(this.chart.timeScale().width(), Math.max(z.start.x, p.x)) - x;
    Object.assign(z.box.style, { left: `${x}px`, top: "0px", width: `${Math.max(0, w)}px`, height: `${h}px` });
  }

  /** Enter ends a path at its last clicked point. */
  private readonly onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter" && this.draft && (this.draft.kind === "path" || shapeOf(this.draft.kind)?.points === "multi")) {
      e.preventDefault();
      this.finishWith(this.draft.points.slice(0, -1));
    }
  };

  /** Double-clicking a text opens it for editing. */
  private readonly onDoubleClick = (e: MouseEvent) => {
    const ctl = this.ctl;
    if (!ctl || ctl.tool !== "cursor") return;
    const r = this.el.getBoundingClientRect();
    const hit = this.hitTest({ x: e.clientX - r.left, y: e.clientY - r.top });
    if (hit?.d.kind === "text" && !hit.d.locked) {
      e.stopPropagation();
      ctl.editText(hit.d.id);
    }
  };

  /** Right-clicking a line, trendline or rectangle selects it and opens its price alert. */
  private readonly onContextMenu = (e: MouseEvent) => {
    const ctl = this.ctl;
    if (!ctl || ctl.tool !== "cursor" || ctl.hidden) return;
    const r = this.el.getBoundingClientRect();
    const hit = this.hitTest({ x: e.clientX - r.left, y: e.clientY - r.top });
    if (!hit || !canAlert(hit.d)) return;
    e.preventDefault();
    e.stopPropagation();
    ctl.select(hit.d.id);
    requestAlertForm(hit.d.id);
  };

  private cancelPlacing(): void {
    this.draft = null;
    this.freehand = false;
    this.placing = null;
    this.stopTracking();
    this.render();
  }

  private track(): void {
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onUp);
  }

  private stopTracking(): void {
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onUp);
  }
}

/** Take the press from the chart: no pan, and no mouse events after it. */
function claim(e: PointerEvent): void {
  e.preventDefault();
  e.stopPropagation();
}
