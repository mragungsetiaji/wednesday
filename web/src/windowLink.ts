/**
 * Link groups (TradingView style): charts with the same colour follow each other's crosshair,
 * and, when the group says so, scroll and zoom (the visible time range) and timeframe. They can
 * sit in this window or another one: messages go through a BroadcastChannel, since every window
 * of the dashboard has the same origin. Unlinked charts aren't touched.
 */
import type { CrosshairPoint } from "./crosshairSync";

export type LinkGroup = "A" | "B" | "C";
export const LINK_GROUPS: LinkGroup[] = ["A", "B", "C"];

/** What a group shares besides the crosshair. Kept per group in this browser, so every window agrees. */
export interface GroupOptions {
  range: boolean; // scroll and zoom
  tf: boolean; // timeframe
}
export const DEFAULT_OPTIONS: GroupOptions = { range: true, tf: false };
const OPTIONS_KEY = "wed.linkGroups";

export type LinkMessage =
  | { kind: "crosshair"; point: CrosshairPoint | null }
  | { kind: "range"; from: number; to: number } // unix seconds, feed clock
  | { kind: "tf"; tf: string };

type Envelope = { group: LinkGroup; from: string; msg: LinkMessage };
type Listener = (msg: LinkMessage) => void;

/** The transport between windows; a BroadcastChannel in the browser, a fake in tests. */
export interface Channel {
  postMessage(data: unknown): void;
  onmessage: ((e: { data: unknown }) => void) | null;
}

export function readOptions(group: LinkGroup): GroupOptions {
  try {
    const all = JSON.parse(localStorage.getItem(OPTIONS_KEY) ?? "{}") as Partial<Record<LinkGroup, Partial<GroupOptions>>>;
    return { ...DEFAULT_OPTIONS, ...(all[group] ?? {}) };
  } catch {
    return DEFAULT_OPTIONS;
  }
}

export function saveOptions(group: LinkGroup, options: GroupOptions): void {
  try {
    const all = JSON.parse(localStorage.getItem(OPTIONS_KEY) ?? "{}");
    localStorage.setItem(OPTIONS_KEY, JSON.stringify({ ...all, [group]: options }));
  } catch {
    /* storage unavailable: the group shares the defaults */
  }
}

let nextId = 0;

export class LinkBus {
  private readonly subs = new Map<string, { group: LinkGroup; fn: Listener }>();
  private readonly channel: Channel | null;
  private readonly options: (g: LinkGroup) => GroupOptions;

  constructor(channel: Channel | null, options: (g: LinkGroup) => GroupOptions = readOptions) {
    this.channel = channel;
    this.options = options;
    if (channel) {
      channel.onmessage = (e) => {
        const env = e.data as Envelope;
        if (env && typeof env === "object" && LINK_GROUPS.includes(env.group)) this.deliver(env);
      };
    }
  }

  /** A chart's id, unique across windows (so a window never answers itself). */
  static id(): string {
    return `${Math.random().toString(36).slice(2, 8)}-${nextId++}`;
  }

  subscribe(id: string, group: LinkGroup, fn: Listener): () => void {
    this.subs.set(id, { group, fn });
    return () => {
      this.subs.delete(id);
    };
  }

  publish(group: LinkGroup, from: string, msg: LinkMessage): void {
    if (!this.shares(group, msg)) return;
    const env: Envelope = { group, from, msg };
    this.deliver(env);
    try {
      this.channel?.postMessage(env);
    } catch {
      /* the other windows just don't follow */
    }
  }

  private shares(group: LinkGroup, msg: LinkMessage): boolean {
    if (msg.kind === "crosshair") return true;
    const o = this.options(group);
    return msg.kind === "range" ? o.range : o.tf;
  }

  private deliver(env: Envelope): void {
    if (!this.shares(env.group, env.msg)) return;
    for (const [id, s] of this.subs) if (id !== env.from && s.group === env.group) s.fn(env.msg);
  }
}

function openChannel(): Channel | null {
  try {
    return typeof BroadcastChannel === "undefined" ? null : (new BroadcastChannel("wed.link") as unknown as Channel);
  } catch {
    return null;
  }
}

let shared: LinkBus | null = null;
/** The window's bus, made on first use. */
export function linkBus(): LinkBus {
  shared ??= new LinkBus(openChannel());
  return shared;
}

/**
 * The CrosshairBus shape PriceChart already takes, for one chart in a link group: its crosshair
 * goes to the group and the group's crosshair comes back.
 */
export function crosshairLink(group: LinkGroup, id: string, bus: LinkBus = linkBus()) {
  return {
    subscribe: (_ignored: number, fn: (p: CrosshairPoint | null) => void) =>
      bus.subscribe(`${id}:x`, group, (m) => m.kind === "crosshair" && fn(m.point)),
    publish: (_ignored: number, p: CrosshairPoint | null) => bus.publish(group, `${id}:x`, { kind: "crosshair", point: p }),
  };
}

// ---- other windows' drawings ----------------------------------------------------------------

let drawingsChannel: BroadcastChannel | null | undefined;
function drawings(): BroadcastChannel | null {
  if (drawingsChannel === undefined) {
    try {
      drawingsChannel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("wed.drawings");
    } catch {
      drawingsChannel = null;
    }
  }
  return drawingsChannel;
}

/** Tell the other windows the saved drawings changed, so they reload them. */
export function announceDrawings(): void {
  try {
    drawings()?.postMessage("changed");
  } catch {
    /* they catch up at the next reload */
  }
}

/** Run `fn` when another window changed the drawings. */
export function onOtherDrawings(fn: () => void): () => void {
  const ch = drawings();
  if (!ch) return () => {};
  const listener = () => fn();
  ch.addEventListener("message", listener);
  return () => ch.removeEventListener("message", listener);
}
