/** The dashboard's layout preferences a workspace carries (#27): every `wed.` / `xau.` key but these. */
const NOT_LAYOUT = new Set(["wed.journal", "wed.journalView", "wed.trainParams", "wed.drawingsHidden"]);

export const isLayoutKey = (k: string) => (k.startsWith("wed.") || k.startsWith("xau.")) && !NOT_LAYOUT.has(k);

/** This browser's layout preferences, parsed. */
export function currentLayout(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !isLayoutKey(k)) continue;
      try {
        out[k] = JSON.parse(localStorage.getItem(k) ?? "null");
      } catch {
        /* not ours to read */
      }
    }
  } catch {
    /* storage unavailable: an empty layout */
  }
  return out;
}

/** Put a workspace's layout preferences back; the page reloads after to pick them up. */
export function applyLayout(layout: Record<string, unknown>): void {
  try {
    for (const [k, v] of Object.entries(layout)) if (isLayoutKey(k)) localStorage.setItem(k, JSON.stringify(v));
  } catch {
    /* storage unavailable: the windows still open */
  }
}
