import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  Logical,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { Candle, Drawing, DrawingKind, DrawingPoint, Sizing } from "./api";
import {
  HANDLE_HIT,
  RECT_CORNERS,
  handlesOf,
  hits,
  isPosition,
  logicalOfTime,
  newId,
  positionBox,
  positionOf,
  setTextFont,
  TEXT_SIZE,
  textBox,
  textFontOf,
  timeOfLogical,
  toPoint,
  toXY,
  trackPosition,
  type DrawingCtl,
  type PositionTrack,
} from "./drawings";
import { fmtMoney, fmtPrice, fmtSigned } from "./format";
import { sizeFor } from "./sizing";
import { withAlpha, type ChartPalette } from "./theme";

type XY = { x: number; y: number };

const TRACK_LABEL: Record<PositionTrack["state"], string> = {
  waiting: "Waiting", missed: "Missed", open: "Open", target: "Target hit", stop: "Stop hit", ended: "Closed",
};

/** Draws the drawings (and the one being placed) above the candles, handles on the selected one. */
class DrawingsRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: DrawingsPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, times, shown, selected, hovered, palette, font, sizing, candles, live } = this.source;
    if (!chart || !series || !times.length || !shown.length) return;
    const yOf = (p: number) => series.priceToCoordinate(p);

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
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
      for (const d of shown) {
        const xy = d.points.map((pt) => toXY(chart, series, times, pt));
        if (xy.some((p) => p === null)) continue;
        const pts = (xy as XY[]).map((p) => ({ x: p.x * hr, y: p.y * vr }));
        const color = d.style.color ?? palette.draw;
        const lw = Math.max(1, Math.round(d.style.width * hr));
        ctx.strokeStyle = color;
        ctx.lineWidth = lw;
        ctx.lineCap = d.style.dash === "dotted" ? "round" : "butt";
        ctx.setLineDash(d.style.dash === "dashed" ? [6 * hr, 4 * hr] : d.style.dash === "dotted" ? [0.1, 3 * lw] : []);

        if (isPosition(d)) {
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
            const xAt = (t: number) => chart.timeScale().logicalToCoordinate(logicalOfTime(times, t) as Logical);
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
        } else {
          const x = Math.min(pts[0].x, pts[1].x);
          const y = Math.min(pts[0].y, pts[1].y);
          const w = Math.abs(pts[1].x - pts[0].x);
          const h = Math.abs(pts[1].y - pts[0].y);
          ctx.fillStyle = withAlpha(color, 0.14);
          ctx.fillRect(x, y, w, h);
          ctx.strokeRect(Math.round(x) + 0.5, Math.round(y) + 0.5, Math.round(w), Math.round(h));
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

  update(patch: Partial<Pick<DrawingsPrimitive, "times" | "shown" | "selected" | "hovered" | "candles" | "live" | "palette" | "font" | "sizing">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

const make = (kind: DrawingKind, points: DrawingPoint[]): Drawing => ({
  id: newId(),
  kind,
  points,
  style: { color: null, width: kind === "trendline" || kind === "hline" || kind === "path" ? 2 : 1, dash: "solid",
    ...(kind === "text" ? { size: TEXT_SIZE } : {}) },
  props: kind === "text" ? { text: "Text" } : null,
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
  private times: number[] = [];
  private draft: Drawing | null = null; // the drawing being placed
  private placing: { start: XY; moved: boolean } | null = null;
  private drag: { orig: Drawing; last: Drawing; handle: number | null; startL: number; startP: number; moved: boolean } | null = null;

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
    window.addEventListener("keydown", this.onKey);
  }

  destroy(): void {
    this.stopTracking();
    this.el.removeEventListener("pointerdown", this.onDown, true);
    this.el.removeEventListener("pointermove", this.onHover);
    this.el.removeEventListener("pointerleave", this.onLeave);
    this.el.removeEventListener("dblclick", this.onDoubleClick, true);
    window.removeEventListener("keydown", this.onKey);
    this.series.detachPrimitive(this.primitive);
  }

  update(patch: { ctl?: DrawingCtl | null; candles?: Candle[]; live?: Candle | null; palette?: ChartPalette; font?: string }): void {
    if (patch.ctl !== undefined) {
      this.ctl = patch.ctl;
      if (this.draft && (!this.ctl || this.ctl.tool !== this.draft.kind)) this.cancelPlacing(); // Esc, or another tool
    }
    if (patch.candles) {
      this.times = patch.candles.map((c) => c.time);
      this.primitive.update({ candles: patch.candles });
    }
    if (patch.live !== undefined) this.primitive.update({ live: patch.live });
    if (patch.palette) this.primitive.update({ palette: patch.palette });
    if (patch.font) {
      setTextFont(patch.font);
      this.primitive.update({ font: patch.font });
    }
    this.render();
  }

  private render(): void {
    const ctl = this.ctl;
    const shown = ctl && !ctl.hidden ? ctl.items.filter((d) => !d.hidden) : [];
    this.primitive.update({
      times: this.times,
      shown: this.draft ? [...shown, this.draft] : shown,
      selected: this.draft ? this.draft.id : ctl?.selected ?? null,
      sizing: ctl?.sizing ?? null,
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
    return toPoint(this.chart, this.series, this.times, p.x, p.y);
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
      if (hits(sel, this.xyOf(sel), p, this.yOf)) return { d: sel, handle: null };
    }
    for (let i = shown.length - 1; i >= 0; i--) {
      if (hits(shown[i], this.xyOf(shown[i]), p, this.yOf)) return { d: shown[i], handle: null };
    }
    return null;
  }

  private readonly onDown = (e: PointerEvent) => {
    const ctl = this.ctl;
    if (!ctl || e.button !== 0) return;
    const p = this.local(e);
    if (!this.inPane(p)) return;

    if (ctl.tool !== "cursor") {
      claim(e);
      const pt = this.pointAt(p);
      if (!pt) return;
      if (ctl.tool === "hline") {
        ctl.create(make("hline", [pt]));
        ctl.setTool("cursor");
      } else if (ctl.tool === "long" || ctl.tool === "short") {
        const d = this.newPosition(ctl.tool, pt, p.y);
        if (d) ctl.create(d);
        ctl.setTool("cursor");
      } else if (ctl.tool === "text") {
        const d = make("text", [pt]);
        ctl.create(d);
        ctl.setTool("cursor");
        ctl.editText(d.id); // straight into typing it
      } else if (ctl.tool === "path" && this.draft) {
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
    this.drag = { orig: hit.d, last: hit.d, handle, startL: l, startP: price, moved: false };
    this.track();
  };

  private readonly onMove = (e: PointerEvent) => {
    const p = this.local(e);
    if (this.drag) {
      const next = this.dragged(p);
      if (!next) return;
      this.drag.moved = true;
      this.drag.last = next;
      this.ctl?.change(next, false);
    } else if (this.draft && this.placing) {
      const pt = this.pointAt(p);
      if (!pt) return;
      this.placing.moved ||= Math.hypot(p.x - this.placing.start.x, p.y - this.placing.start.y) > 4;
      this.draft = { ...this.draft, points: [...this.draft.points.slice(0, -1), pt] }; // the point under the pointer
      this.render();
    }
  };

  private readonly onUp = (e: PointerEvent) => {
    if (this.drag) {
      const { moved, last } = this.drag;
      this.drag = null;
      this.stopTracking();
      if (moved) this.ctl?.change(last, true);
    } else if (this.draft && this.draft.kind !== "path" && this.placing?.moved) {
      const pt = this.pointAt(this.local(e));
      if (pt) this.finish(pt); // pressed and dragged: done on release
    }
    // A click without a drag leaves the second point to the next click.
  };

  private readonly onHover = (e: PointerEvent) => {
    const ctl = this.ctl;
    if (!ctl || this.drag) return;
    const p = this.local(e);
    let cursor = "";
    let hovered: string | null = null;
    if (ctl.tool !== "cursor" && this.inPane(p)) cursor = "crosshair";
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
   * 20 candles to the right. The handles set the real ones.
   */
  private newPosition(kind: "long" | "short", pt: DrawingPoint, y: number): Drawing | null {
    const away = this.series.coordinateToPrice(kind === "long" ? y + 40 : y - 40);
    if (away === null) return null;
    const risk = Math.abs(pt.p - away);
    if (!risk) return null;
    const sign = kind === "long" ? 1 : -1;
    const end = timeOfLogical(this.times, logicalOfTime(this.times, pt.t) + 20);
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
    const minEnd = timeOfLogical(this.times, logicalOfTime(this.times, start.t) + 1);
    return { ...orig, points: [start, { t: Math.max(pt.t, minEnd), p: end.p }] };
  }

  /** The drag's drawing at pointer p: a handle moves its point (or corner), the body moves it all. */
  private dragged(p: XY): Drawing | null {
    const drag = this.drag!;
    const { orig } = drag;
    if (drag.handle === null) {
      const l = this.chart.timeScale().coordinateToLogical(p.x);
      const price = this.series.coordinateToPrice(p.y);
      if (l === null || price === null) return null;
      const dl = Math.round(l - drag.startL); // whole candles, so points stay on candle opens
      const dp = price - drag.startP;
      const moved: Drawing = {
        ...orig,
        points: orig.points.map((pt) => ({ t: timeOfLogical(this.times, logicalOfTime(this.times, pt.t) + dl), p: pt.p + dp })),
      };
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
    this.ctl?.setTool("cursor");
  }

  /** Enter ends a path at its last clicked point. */
  private readonly onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter" && this.draft?.kind === "path") {
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

  private cancelPlacing(): void {
    this.draft = null;
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
