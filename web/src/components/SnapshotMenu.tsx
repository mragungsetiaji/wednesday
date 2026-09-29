import { useEffect, useId, useRef, useState } from "react";

import { attachSnapshot, fetchJournal, fetchJournals, uploadSnapshot, type Journal, type JournalTrade } from "../api";
import { fmtPrice } from "../format";
import { CameraIcon } from "../icons";
import { copyImage, saveImage, toBlob } from "../snapshot";
import { toast } from "./Toasts";
import { usePopover } from "./usePopover";

/** What a snapshot button needs: the picture, made on demand, and its file name. */
export interface SnapshotSource {
  make: () => HTMLCanvasElement | null;
  name: () => string;
}

/** Copy the picture, or save it where the clipboard is refused (the desktop app's WebView2). */
export async function copySnapshot(src: SnapshotSource) {
  const canvas = src.make();
  if (!canvas) return;
  const blob = await toBlob(canvas);
  if (await copyImage(blob)) {
    toast("Chart copied to the clipboard");
    return;
  }
  const where = await saveImage(blob, src.name());
  if (where) toast(`The clipboard isn't available here: saved ${where} instead`);
}

export async function saveSnapshot(src: SnapshotSource) {
  const canvas = src.make();
  if (!canvas) return;
  const where = await saveImage(await toBlob(canvas), src.name());
  if (where) toast(`Saved ${where}`);
}

/** The camera button: copy, save, or attach to a journal trade. */
export function SnapshotMenu({ source, label = "Snapshot", className = "chart-nav-btn" }: {
  source: SnapshotSource;
  label?: string;
  className?: string;
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const [attach, setAttach] = useState<Blob | null>(null);
  const id = useId();
  const run = (fn: () => Promise<void>) => {
    setOpen(false);
    fn().catch((e) => toast(`Snapshot failed: ${e instanceof Error ? e.message : e}`));
  };
  return (
    <div className="snapshot-menu" ref={ref}>
      <button type="button" className={className} aria-expanded={open} aria-controls={id} title={`${label}: copy, save or attach to the journal (Alt+S copies)`}
        aria-label={label} onClick={() => setOpen(!open)}>
        <CameraIcon size={14} />
      </button>
      {open && (
        <div className="chart-pop snapshot-pop" id={id} role="menu" aria-label={label}>
          <button type="button" role="menuitem" className="scale-opt" onClick={() => run(() => copySnapshot(source))}>Copy image</button>
          <button type="button" role="menuitem" className="scale-opt" onClick={() => run(() => saveSnapshot(source))}>Save as PNG</button>
          <button type="button" role="menuitem" className="scale-opt" onClick={() => run(async () => {
            const canvas = source.make();
            if (canvas) setAttach(await toBlob(canvas));
          })}>Attach to a journal trade…</button>
        </div>
      )}
      {attach && <AttachDialog blob={attach} onClose={() => setAttach(null)} />}
    </div>
  );
}

const when = (unix: number) => new Date(unix * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short", timeZone: "UTC" });

/** Pick a journal and one of its trades; the picture goes on that trade's note. */
function AttachDialog({ blob, onClose }: { blob: Blob; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [journals, setJournals] = useState<Journal[] | null>(null);
  const [journalId, setJournalId] = useState("");
  const [trades, setTrades] = useState<JournalTrade[] | null>(null);
  const [tradeId, setTradeId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const preview = useRef<string>("");
  if (!preview.current) preview.current = URL.createObjectURL(blob);

  useEffect(() => {
    ref.current?.showModal();
    fetchJournals().then((r) => {
      setJournals(r.journals);
      if (r.journals.length) setJournalId(r.journals[0].id);
    }).catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => URL.revokeObjectURL(preview.current);
  }, []);

  useEffect(() => {
    if (!journalId) return;
    setTrades(null);
    fetchJournal(journalId).then((s) => {
      // Newest first, open ones on top: the trade being planned or just taken.
      const list = [...s.trades].sort((a, b) => Number(b.close_time === null) - Number(a.close_time === null) || b.open_time - a.open_time);
      setTrades(list.slice(0, 50));
      setTradeId(list[0]?.id ?? "");
    }).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [journalId]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const shot = await uploadSnapshot(blob);
      await attachSnapshot(journalId, tradeId, shot.id);
      const j = journals?.find((x) => x.id === journalId);
      toast(`Attached to the trade in ${j?.name ?? "the journal"}`, { href: "#journal", label: "Open the journal" });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <dialog ref={ref} className="keys-dialog snapshot-dialog" aria-labelledby="attach-h" onClose={onClose}>
      <form method="dialog" onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}>
        <h2 id="attach-h">Attach to a journal trade</h2>
        <img className="snapshot-preview" src={preview.current} alt="The chart snapshot" />
        {journals && journals.length === 0 && (
          <p className="field-note">No journal yet. Make one on the <a href="#journal" onClick={onClose}>Journal</a> page, or save the image instead.</p>
        )}
        {journals && journals.length > 1 && (
          <label className="field">
            <span className="field-label">Journal</span>
            <select value={journalId} onChange={(e) => setJournalId(e.target.value)}>
              {journals.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
            </select>
          </label>
        )}
        {trades && trades.length === 0 && <p className="field-note">This journal has no trades yet: sync or import them first.</p>}
        {trades && trades.length > 0 && (
          <label className="field">
            <span className="field-label">Trade</span>
            <select value={tradeId} onChange={(e) => setTradeId(e.target.value)}>
              {trades.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.close_time === null ? "Open · " : ""}{when(t.open_time)} · {t.symbol} {t.side} {t.volume.toFixed(2)} @ {fmtPrice(t.open_price)}
                  {t.note ? ` · ${t.note.slice(0, 40)}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        {error && <p className="text-error" role="alert">{error}</p>}
        <div className="keys-actions">
          <button type="button" className="button quiet" onClick={() => ref.current?.close()}>Cancel</button>
          <button type="submit" className="button" disabled={busy || !tradeId}>Attach</button>
        </div>
      </form>
    </dialog>
  );
}
