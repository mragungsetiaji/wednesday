import { useState } from "react";

/**
 * Methods the desktop app exposes to the page: the Tauri shell's commands (src-tauri/src/lib.rs).
 */
export interface DesktopApi {
  save_png(data: string, name: string): Promise<string | null>; // base64 PNG; the path saved to, or null (cancelled)
  open_chart(tf: string, group: string | null): Promise<boolean>; // a chart in a new native window
  window_layout(): Promise<import("./api").WorkspaceWindow[]>; // the open windows, for a workspace
  arrange(windows: import("./api").WorkspaceWindow[]): Promise<boolean>; // open a workspace's windows
}

declare global {
  interface Window {
    __TAURI__?: { core?: { invoke?: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T> } };
  }
}

/** The Tauri shell's commands as a DesktopApi, or null outside it. Tauri injects them before the page runs. */
export function desktopApi(): DesktopApi | null {
  const invoke = window.__TAURI__?.core?.invoke;
  if (!invoke) return null;
  return {
    save_png: (data, name) => invoke("save_png", { data, name }),
    open_chart: (tf, group) => invoke("open_chart", { tf, group }),
    window_layout: () => invoke("window_layout"),
    arrange: (windows) => invoke("arrange", { windows }),
  };
}

/** The desktop app's API, or null in a browser. */
export function useDesktopApi(): Partial<DesktopApi> | null {
  const [api] = useState<Partial<DesktopApi> | null>(desktopApi);
  return api;
}

/** The pop-out chart's address in this app: `#popout/5M`, with its link group when it has one. */
export const popoutHash = (tf: string, group: string | null) => `#popout/${encodeURIComponent(tf)}${group ? `/${group}` : ""}`;

/**
 * Open a chart in its own window: a native window in the desktop app, a browser popup otherwise.
 * Every window talks to the same server, so the scan, ticks and drawings are the same.
 */
export function openChartWindow(api: Partial<DesktopApi> | null, tf: string, group: string | null = null): void {
  if (api?.open_chart) {
    api.open_chart(tf, group).catch(() => {});
    return;
  }
  const url = `${window.location.pathname}${window.location.search}${popoutHash(tf, group)}`;
  window.open(url, `wed-chart-${Date.now()}`, "popup,width=1100,height=700");
}
