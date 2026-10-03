import { addEntry, fetchJournals } from "./api";
import { toast } from "./components/Toasts";
import type { RailItem } from "./rail";

/**
 * "Journal this" on a ladder setup (#14): add it to the open journal as an entry, with the market as it
 * is now frozen into it by the server. The sample portfolio isn't written to.
 */
export async function journalThis(item: RailItem): Promise<void> {
  if (!item.setup) return;
  try {
    const { journals, available } = await fetchJournals();
    let current: string | null = null;
    try {
      current = JSON.parse(localStorage.getItem("wed.journal") ?? "null");
    } catch {
      /* no remembered journal */
    }
    const own = journals.filter((j) => !j.sample);
    const j = own.find((x) => x.id === current) ?? own[0];
    if (!available || !j) {
      toast("Make a journal first to keep setups in it.", { href: "#settings/journal", label: "Journal settings" });
      return;
    }
    await addEntry(j.id, {
      kind: "setup",
      text: "",
      setup: { tag: item.tag, timeframe: item.tf, side: item.setup.side, entry: item.price, sl: item.level.meta.sl ?? null },
    });
    window.dispatchEvent(new Event("wed:journal-entry"));
    toast(`${item.tag} ${item.tf} journalled in ${j.name}, with the market as it is now.`, { href: "#journal", label: "Open the journal" });
  } catch (e) {
    toast(`Couldn't journal it: ${e instanceof Error ? e.message : String(e)}`);
  }
}
