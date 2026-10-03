import { useEffect, useState } from "react";

/** Methods the Windows desktop app exposes to the page (see DesktopApi in desktop.py). */
export interface DesktopApi {
  pick_terminal(): Promise<string | null>;
  save_png(data: string, name: string): Promise<string | null>; // base64 PNG; the path saved to, or null (cancelled)
  open_chart(tf: string, group: string | null): Promise<boolean>; // a chart in a new native window
  window_layout(): Promise<import("./api").WorkspaceWindow[]>; // the open windows, for a workspace
  arrange(windows: import("./api").WorkspaceWindow[]): Promise<boolean>; // open a workspace's windows
}

declare global {
  interface Window {
    pywebview?: { api?: Partial<DesktopApi> };
  }
}

/** The desktop app's API, or null in a browser. pywebview injects it shortly after load. */
export function useDesktopApi(): Partial<DesktopApi> | null {
  const [api, setApi] = useState<Partial<DesktopApi> | null>(() => window.pywebview?.api ?? null);
  useEffect(() => {
    if (api) return;
    const ready = () => setApi(window.pywebview?.api ?? null);
    window.addEventListener("pywebviewready", ready);
    return () => window.removeEventListener("pywebviewready", ready);
  }, [api]);
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
