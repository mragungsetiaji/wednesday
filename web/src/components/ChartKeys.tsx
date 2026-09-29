import { useEffect, useRef, useState, type FormEvent } from "react";

import { nyToFeed } from "../api";
import type { DrawTool } from "../drawings";
import { feedUnix, parseTimeframe, parseWhen, SHORTCUTS, typing, wallNow, wallString, type Clock } from "../shortcuts";
import { SHOW_SHORTCUTS, type ChartNav } from "./PriceChart";

interface Props {
  active: boolean; // only one set of chart keys listens (the dashboard or full screen)
  timeframes: string[];
  setTf: (tf: string) => void; // the focused chart's
  nav: () => ChartNav | null; // the focused chart
  panes?: number; // full screen: Alt+1..n focus a pane
  focusPane?: (i: number) => void;
  setTool?: ((tool: DrawTool) => void) | null;
  toggleFullScreen: () => void;
  clockOffset: number;
}

const TOOL_KEYS: Record<string, DrawTool> = { KeyH: "hline", KeyT: "trendline", KeyB: "rect" };
const TYPED_MS = 2500; // a typed timeframe clears after this long without a key

/**
 * Keyboard-first charting: type a timeframe and Enter, Alt+1..4 to focus a pane, Alt+R to
 * reset the view, Alt+G to go to a date, Alt+H / T / B for drawing tools, F for full screen and
 * ? for the list. Nothing fires while a field has focus.
 */
export function ChartKeys({ active, timeframes, setTf, nav, panes = 1, focusPane, setTool, toggleFullScreen, clockOffset }: Props) {
  const [typed, setTyped] = useState("");
  const [typedError, setTypedError] = useState<string | null>(null);
  const [help, setHelp] = useState(false);
  const [goTo, setGoTo] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef({ typed, timeframes, setTf, nav, panes, focusPane, setTool, toggleFullScreen });
  latest.current = { typed, timeframes, setTf, nav, panes, focusPane, setTool, toggleFullScreen };
  const dialogOpen = help || goTo;

  useEffect(() => {
    if (!active) return;
    const show = () => setHelp(true);
    window.addEventListener(SHOW_SHORTCUTS, show);
    return () => window.removeEventListener(SHOW_SHORTCUTS, show);
  }, [active]);

  useEffect(() => {
    if (!active || dialogOpen) return;
    const clearLater = () => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setTyped(""), TYPED_MS);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || typing(e.target) || e.ctrlKey || e.metaKey) return;
      const k = latest.current;
      if (e.altKey) {
        const digit = /^Digit([1-9])$/.exec(e.code);
        if (digit && k.focusPane && k.panes > 1) {
          const i = Number(digit[1]) - 1;
          if (i < k.panes) {
            e.preventDefault();
            k.focusPane(i);
          }
        } else if (e.code === "KeyR") {
          e.preventDefault();
          k.nav()?.reset();
        } else if (e.code === "KeyG") {
          e.preventDefault();
          setGoTo(true);
        } else if (TOOL_KEYS[e.code] && k.setTool) {
          e.preventDefault();
          k.setTool(TOOL_KEYS[e.code]);
        }
        return;
      }
      if (e.key === "?") {
        e.preventDefault();
        setHelp(true);
      } else if (/^[0-9]$/.test(e.key) || (k.typed && /^[hmd]$/i.test(e.key))) {
        e.preventDefault();
        setTyped(k.typed + e.key);
        setTypedError(null);
        clearLater();
      } else if (k.typed && e.key === "Backspace") {
        e.preventDefault();
        e.stopImmediatePropagation();
        setTyped(k.typed.slice(0, -1));
        clearLater();
      } else if (k.typed && e.key === "Escape") {
        e.stopImmediatePropagation(); // just the typed timeframe, not full screen
        setTyped("");
      } else if (k.typed && e.key === "Enter") {
        e.preventDefault();
        e.stopImmediatePropagation();
        const tf = parseTimeframe(k.typed, k.timeframes);
        if (tf) {
          k.setTf(tf);
          setTyped("");
        } else {
          setTypedError(`No ${k.typed} chart. Timeframes: ${k.timeframes.join(", ")}`);
          clearLater();
        }
      } else if ((e.key === "f" || e.key === "F") && !e.shiftKey && !k.typed) {
        e.preventDefault();
        k.toggleFullScreen();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(timer.current);
    };
  }, [active, dialogOpen]);

  if (!active) return null;
  return (
    <>
      {typed && (
        <div className={`tf-typed${typedError ? " is-error" : ""}`} role="status">
          <span className="num">{typed}</span>
          <span className="muted">{typedError ?? "Enter to switch timeframe · Esc to cancel"}</span>
        </div>
      )}
      {help && <ShortcutSheet onClose={() => setHelp(false)} />}
      {goTo && <GoToDialog nav={nav} clockOffset={clockOffset} onClose={() => setGoTo(false)} />}
    </>
  );
}

/** A modal <dialog>: Esc and the close button end it; focus returns where it was. */
function useModal(onClose: () => void) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    const back = document.activeElement as HTMLElement | null;
    el?.showModal();
    return () => {
      el?.close();
      back?.focus?.();
    };
  }, []);
  return {
    ref,
    onCancel: (e: { preventDefault: () => void }) => {
      e.preventDefault();
      onClose();
    },
    // This Esc only closes the dialog, not full screen too.
    onKeyDown: (e: { key: string; stopPropagation: () => void }) => e.key === "Escape" && e.stopPropagation(),
  };
}

function ShortcutSheet({ onClose }: { onClose: () => void }) {
  const modal = useModal(onClose);
  return (
    <dialog ref={modal.ref} className="keys-dialog" aria-labelledby="keys-title" onCancel={modal.onCancel}
      onKeyDown={modal.onKeyDown} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="keys-head">
        <h2 id="keys-title">Keyboard shortcuts</h2>
        <button type="button" className="button quiet" onClick={onClose} autoFocus>Close</button>
      </div>
      <p className="field-hint">They don't fire while you're typing in a field.</p>
      <div className="table-scroll">
      <table className="data compact keys-table">
        <thead>
          <tr><th scope="col">Keys</th><th scope="col">Action</th></tr>
        </thead>
        <tbody>
          {SHORTCUTS.map((s) => (
            <tr key={s.keys}>
              <td><kbd>{s.keys}</kbd></td>
              <td>{s.action}{s.where && <span className="muted"> ({s.where})</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </dialog>
  );
}

function GoToDialog({ nav, clockOffset, onClose }: { nav: () => ChartNav | null; clockOffset: number; onClose: () => void }) {
  const modal = useModal(onClose);
  const [text, setText] = useState("");
  const [clock, setClock] = useState<Clock>("ny");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const when = parseWhen(text, wallNow(clock, clockOffset), clock);
    if (!when) {
      setError("Try “last Wednesday 09:30”, “yesterday 8pm”, “2026-09-24 14:00” or “fri 03:00 feed”.");
      return;
    }
    setBusy(true);
    try {
      // New York times go through the server, which knows the feed clock's offset on that date.
      const t = when.clock === "ny" ? (await nyToFeed(wallString(when.wall))).feed_unix : feedUnix(when.wall);
      onClose();
      nav()?.goTo(t);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  return (
    <dialog ref={modal.ref} className="keys-dialog goto-dialog" aria-labelledby="goto-title" onCancel={modal.onCancel}
      onKeyDown={modal.onKeyDown} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <form onSubmit={submit}>
        <h2 id="goto-title">Go to</h2>
        <label className="field">
          <span className="field-label">Date and time</span>
          <input type="text" value={text} autoFocus spellCheck={false} placeholder="last Wednesday 09:30"
            aria-describedby="goto-hint" onChange={(e) => { setText(e.target.value); setError(null); }} />
        </label>
        <fieldset className="goto-clock">
          <legend className="field-label">Clock</legend>
          <label><input type="radio" name="goto-clock" checked={clock === "ny"} onChange={() => setClock("ny")} /> New York</label>
          <label><input type="radio" name="goto-clock" checked={clock === "feed"} onChange={() => setClock("feed")} /> Feed (chart)</label>
        </fieldset>
        <p id="goto-hint" className="field-hint">
          A weekday, “yesterday”, “24 Sep” or 2026-09-24, then a time. End with “NY” or “feed” to pick the clock.
        </p>
        {error && <p className="text-error field-note" role="alert">{error}</p>}
        <div className="form-actions">
          <button type="button" className="button quiet" onClick={onClose}>Cancel</button>
          <button type="submit" className="button primary" disabled={busy || !text.trim()}>Go</button>
        </div>
      </form>
    </dialog>
  );
}
