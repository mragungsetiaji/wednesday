/** A point in time (and price, when the pointer is over the candles) that linked charts follow. */
export interface CrosshairPoint {
  time: number; // unix seconds, feed clock
  price: number | null;
}

/**
 * Links the crosshairs of several charts: the chart under the pointer
 * publishes, every other chart moves its crosshair to the candle that
 * contains that time on its own timeframe.
 */
export class CrosshairBus {
  private readonly subs = new Map<number, (p: CrosshairPoint | null) => void>();

  subscribe(id: number, fn: (p: CrosshairPoint | null) => void): () => void {
    this.subs.set(id, fn);
    return () => {
      this.subs.delete(id);
    };
  }

  publish(from: number, p: CrosshairPoint | null): void {
    for (const [id, fn] of this.subs) if (id !== from) fn(p);
  }
}

/** What a chart needs from a crosshair link: the bus above, or a link group (windowLink.ts). */
export type CrosshairLink = Pick<CrosshairBus, "subscribe" | "publish">;
