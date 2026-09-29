import { useEffect, useId, useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";
import { createPortal } from "react-dom";

import type { AlertCondition, Drawing, DrawingAlert, DrawingAlertInput } from "../api";
import { alertFormRequest, OPEN_DRAWING_ALERT } from "../drawings";
import type { Drawings } from "../drawingsData";
import { fmtPrice } from "../format";
import { BellIcon } from "../icons";
import { usePopover } from "./usePopover";

const CONDITIONS: Record<string, { id: AlertCondition; label: string }[]> = {
  line: [
    { id: "cross", label: "Crossing" },
    { id: "cross_up", label: "Crossing up" },
    { id: "cross_down", label: "Crossing down" },
  ],
  rect: [
    { id: "enter", label: "Entering" },
    { id: "exit", label: "Leaving" },
  ],
};

const EXPIRIES: { id: string; label: string; hours: number | null }[] = [
  { id: "never", label: "Never", hours: null },
  { id: "1h", label: "In 1 hour", hours: 1 },
  { id: "4h", label: "In 4 hours", hours: 4 },
  { id: "1d", label: "In 1 day", hours: 24 },
  { id: "1w", label: "In 1 week", hours: 24 * 7 },
];

const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

function status(a: DrawingAlert): string {
  if (a.armed) return a.expires_at ? `Alert on until ${when(a.expires_at)}` : "Alert on";
  if (a.fired_at && a.mode === "once") return `Fired ${when(a.fired_at)} at ${fmtPrice(a.fired_price ?? 0)}: off until re-armed`;
  return a.expires_at && Date.parse(a.expires_at) <= Date.now() ? "Alert expired" : "Alert off";
}

/** The selected drawing's alert: a bell button in the style bar and its form. */
export function DrawingAlertButton({ d, ctl }: { d: Drawing; ctl: Drawings }) {
  const { open, setOpen, ref, popRef } = usePopover<HTMLDivElement>();
  const id = useId();
  const alert = ctl.alerts[d.id];

  useEffect(() => {
    const onOpen = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== d.id) return;
      alertFormRequest.id = null;
      setOpen(true);
    };
    if (alertFormRequest.id === d.id) onOpen(new CustomEvent(OPEN_DRAWING_ALERT, { detail: d.id }));
    window.addEventListener(OPEN_DRAWING_ALERT, onOpen);
    return () => window.removeEventListener(OPEN_DRAWING_ALERT, onOpen);
  }, [d.id, setOpen]);

  return (
    <div className="draw-alert" ref={ref}>
      <button type="button" className={`draw-tool${alert?.armed ? " is-on" : ""}`} aria-pressed={!!alert?.armed} aria-expanded={open}
        aria-controls={id} title={alert ? `Alert: ${status(alert)}` : "Add a price alert"} aria-label="Price alert"
        onClick={() => setOpen(!open)}>
        <BellIcon size={16} off={!!alert && !alert.armed} />
      </button>
      {open && <Floating anchor={ref} popRef={popRef}>
        <AlertForm id={id} d={d} ctl={ctl} alert={alert} onDone={() => setOpen(false)} />
      </Floating>}
    </div>
  );
}

/**
 * The form, placed under the button but rendered in the chart's container: the style bar
 * scrolls sideways and is transformed, which would clip it.
 */
function Floating({ anchor, popRef, children }: { anchor: RefObject<HTMLElement | null>; popRef: RefObject<HTMLElement | null>;
  children: React.ReactNode }) {
  const [place, setPlace] = useState<{ host: HTMLElement; style: CSSProperties } | null>(null);
  useLayoutEffect(() => {
    const btn = anchor.current;
    const bar = btn?.closest(".draw-style");
    const host = bar?.parentElement;
    if (!btn || !host) return;
    const b = btn.getBoundingClientRect();
    const h = host.getBoundingClientRect();
    const width = Math.min(320, h.width - 16);
    const left = Math.min(Math.max(8, b.left - h.left), h.width - width - 8);
    setPlace({ host, style: { position: "absolute", top: b.bottom - h.top + 6, left, width, zIndex: 30 } });
  }, [anchor]);
  if (!place) return null;
  return createPortal(<div ref={(el) => { popRef.current = el; }} style={place.style}>{children}</div>, place.host);
}

function AlertForm({ id, d, ctl, alert, onDone }: { id: string; d: Drawing; ctl: Drawings; alert?: DrawingAlert; onDone: () => void }) {
  const options = CONDITIONS[d.kind === "rect" ? "rect" : "line"];
  const [condition, setCondition] = useState<AlertCondition>(alert?.condition ?? options[0].id);
  const [mode, setMode] = useState<DrawingAlertInput["mode"]>(alert?.mode ?? "once");
  const [note, setNote] = useState(alert?.note ?? "");
  const [expiry, setExpiry] = useState("never");
  const [intrabar, setIntrabar] = useState(alert?.intrabar ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const save = () => run(() => {
    const hours = EXPIRIES.find((x) => x.id === expiry)?.hours ?? null;
    // Keep an existing expiry unless another one is picked.
    const expires_at = hours !== null ? new Date(Date.now() + hours * 3600_000).toISOString()
      : expiry === "never" && alert?.expires_at && Date.parse(alert.expires_at) > Date.now() && alert.armed ? alert.expires_at : null;
    return ctl.setAlert(d.id, { condition, mode, note: note.trim() || null, expires_at, intrabar });
  });
  const kind = d.kind === "rect" ? "zone" : d.kind === "hline" ? "line" : "trendline";

  return (
    <form className="chart-pop draw-alert-pop" id={id} aria-label="Price alert" onSubmit={(e) => {
      e.preventDefault();
      save();
    }}>
      <p className="draw-alert-status">{alert ? status(alert) : `Alert when price reaches this ${kind}`}</p>
      <label className="field-row">
        <span>When price is</span>
        <select value={condition} onChange={(e) => setCondition(e.target.value as AlertCondition)}>
          {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </label>
      <label className="field-row">
        <span>Fires</span>
        <select value={mode} onChange={(e) => setMode(e.target.value as DrawingAlertInput["mode"])}>
          <option value="once">Once, then off</option>
          <option value="every">Every time (once per bar)</option>
        </select>
      </label>
      <label className="field-row">
        <span>Name</span>
        <input type="text" value={note} maxLength={200} placeholder="e.g. Asia high" onChange={(e) => setNote(e.target.value)} />
      </label>
      <label className="field-row">
        <span>Expires</span>
        <select value={expiry} onChange={(e) => setExpiry(e.target.value)}>
          {EXPIRIES.map((x) => <option key={x.id} value={x.id}>{x.id === "never" && alert?.expires_at && alert.armed ? "Keep as is" : x.label}</option>)}
        </select>
      </label>
      <label className="scale-opt">
        <input type="checkbox" checked={intrabar} onChange={(e) => setIntrabar(e.target.checked)} />
        Also on the live price (intrabar)
      </label>
      <p className="field-hint">
        Checked on closed M1 bars{intrabar ? ", and on each live tick (fires before the bar closes)" : ""}. Sent to Telegram when it is
        set up; listed under Settings &gt; Telegram alerts either way.
      </p>
      {error && <p className="text-error" role="alert">{error}</p>}
      <div className="draw-alert-actions">
        <button type="submit" className="button" disabled={busy}>{alert ? (alert.armed ? "Save" : "Re-arm") : "Set alert"}</button>
        {alert && (
          <button type="button" className="button quiet" disabled={busy} onClick={() => run(() => ctl.removeAlert(d.id))}>Remove</button>
        )}
      </div>
    </form>
  );
}
