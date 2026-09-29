import { useEffect, useState } from "react";

/** Methods the Windows desktop app exposes to the page (see DesktopApi in desktop.py). */
export interface DesktopApi {
  pick_terminal(): Promise<string | null>;
  save_png(data: string, name: string): Promise<string | null>; // base64 PNG; the path saved to, or null (cancelled)
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
