import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  deleteDrawing,
  deleteDrawingAlert,
  fetchDrawingAlerts,
  fetchDrawings,
  putDrawing,
  putDrawingAlert,
  type Drawing,
  type DrawingAlert,
  type DrawingAlertInput,
  type Sizing,
} from "./api";
import type { DrawingCtl, DrawTool } from "./drawings";
import { announceDrawings, onOtherDrawings } from "./windowLink";
import { usePref } from "./prefs";

const typing = (el: EventTarget | null) =>
  el instanceof HTMLElement && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));

const HISTORY = 100; // undo steps kept

/** One undoable step: a drawing before and after it (null: it didn't exist). */
interface Step {
  before: Drawing | null;
  after: Drawing | null;
}

export type Drawings = DrawingCtl & {
  available: boolean; // false without a database
  error: string | null;
  setHidden: (v: boolean) => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  textFocus: number; // bumps when a text drawing's field should take the cursor
  setAlert: (id: string, a: DrawingAlertInput) => Promise<void>; // set or re-arm
  removeAlert: (id: string) => Promise<void>;
};

const byDrawing = (xs: DrawingAlert[]) => Object.fromEntries(xs.map((a) => [a.drawing_id, a]));

/**
 * The chart drawings of the running source and symbol, shared by every chart on screen.
 * Changes show at once and save in the background; a failed save reloads the saved ones.
 *
 * Every create, delete and finished change is one undo step (a drag or a text edit counts
 * once, when it ends). Keys: Esc cancels the tool or the selection (before anything else
 * takes Esc), Delete removes the selected drawing, Ctrl/Cmd+Z undoes, Ctrl/Cmd+Shift+Z or
 * Ctrl+Y redoes.
 */
export function useDrawings(source: string | undefined, symbol: string | undefined, sizing: Sizing | null, version = 0): Drawings {
  const [items, setItems] = useState<Drawing[]>([]);
  const [alerts, setAlerts] = useState<Record<string, DrawingAlert>>({});
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tool, setTool] = useState<DrawTool>("cursor");
  const [selected, select] = useState<string | null>(null);
  const [hidden, setHidden] = usePref<boolean>("wed.drawingsHidden", false);
  const [textFocus, setTextFocus] = useState(0);

  const itemsRef = useRef(items);
  itemsRef.current = items;
  const history = useRef<{ undo: Step[]; redo: Step[] }>({ undo: [], redo: [] });
  const started = useRef(new Map<string, Drawing>()); // the drawing as it was when a drag or edit began
  const [, setHistoryVersion] = useState(0); // rerender when what undo/redo can do changes

  const reload = useCallback(() => {
    fetchDrawings()
      .then((r) => {
        setItems(r.drawings);
        setAlerts(byDrawing(r.alerts ?? []));
        setAvailable(true);
      })
      .catch((e) => {
        setItems([]);
        setAlerts({});
        setAvailable(!String(e).includes("database"));
      });
  }, []);

  useEffect(() => {
    if (!source || !symbol) return;
    select(null);
    history.current = { undo: [], redo: [] };
    started.current.clear();
    setHistoryVersion((v) => v + 1);
    reload();
  }, [source, symbol, reload]);

  // A drawing saved in another window (a popped-out chart, say) shows here too.
  useEffect(() => onOtherDrawings(reload), [reload]);

  // After each scan: an alert that fired greys out.
  useEffect(() => {
    if (!version || !source) return;
    fetchDrawingAlerts().then((r) => setAlerts(byDrawing(r.alerts))).catch(() => {});
  }, [version, source]);

  const setAlert = useCallback(async (id: string, a: DrawingAlertInput) => {
    const saved = await putDrawingAlert(id, a);
    setAlerts((xs) => ({ ...xs, [id]: saved }));
  }, []);
  const removeAlert = useCallback(async (id: string) => {
    await deleteDrawingAlert(id);
    setAlerts(({ [id]: _gone, ...rest }) => rest);
  }, []);

  const failed = useCallback((what: string) => (e: unknown) => {
    setError(`Couldn't ${what} the drawing: ${e instanceof Error ? e.message : e}`);
    reload();
  }, [reload]);

  /** Show `d` (or remove `id` when d is null) and save that to the server. */
  const apply = useCallback((id: string, d: Drawing | null) => {
    if (d) {
      setItems((xs) => (xs.some((x) => x.id === id) ? xs.map((x) => (x.id === id ? d : x)) : [...xs, d]));
      putDrawing(d).then(() => { setError(null); announceDrawings(); }).catch(failed("save"));
    } else {
      setItems((xs) => xs.filter((x) => x.id !== id));
      select((s) => (s === id ? null : s));
      deleteDrawing(id).then(() => { setError(null); announceDrawings(); }).catch(failed("delete"));
    }
  }, [failed]);

  const record = useCallback((step: Step) => {
    const h = history.current;
    h.undo = [...h.undo, step].slice(-HISTORY);
    h.redo = [];
    setHistoryVersion((v) => v + 1);
  }, []);

  const create = useCallback((d: Drawing) => {
    apply(d.id, d);
    select(d.id);
    setHidden(false); // a new drawing never disappears into "hide all"
    record({ before: null, after: d });
  }, [apply, record, setHidden]);

  const change = useCallback((d: Drawing, commit: boolean) => {
    const current = itemsRef.current.find((x) => x.id === d.id) ?? null;
    if (!commit) {
      if (current && !started.current.has(d.id)) started.current.set(d.id, current);
      setItems((xs) => xs.map((x) => (x.id === d.id ? d : x)));
      return;
    }
    const before = started.current.get(d.id) ?? current;
    started.current.delete(d.id);
    apply(d.id, d);
    if (JSON.stringify(before) !== JSON.stringify(d)) record({ before, after: d });
  }, [apply, record]);

  const remove = useCallback((id: string) => {
    const before = started.current.get(id) ?? itemsRef.current.find((x) => x.id === id) ?? null;
    started.current.delete(id);
    apply(id, null);
    if (before) record({ before, after: null });
  }, [apply, record]);

  const move = useCallback((from: "undo" | "redo") => {
    const h = history.current;
    const step = h[from].at(-1);
    if (!step) return;
    h[from] = h[from].slice(0, -1);
    h[from === "undo" ? "redo" : "undo"] = [...h[from === "undo" ? "redo" : "undo"], step];
    const target = from === "undo" ? step.before : step.after;
    const id = (step.before ?? step.after)!.id;
    apply(id, target);
    select(target ? id : null);
    setHistoryVersion((v) => v + 1);
  }, [apply]);
  const undo = useCallback(() => move("undo"), [move]);
  const redo = useCallback(() => move("redo"), [move]);

  const editText = useCallback((id: string) => {
    select(id);
    setTextFocus((n) => n + 1);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target)) return;
      const mod = e.ctrlKey || e.metaKey;
      if (e.key === "Escape" && (tool !== "cursor" || selected)) {
        e.stopPropagation(); // capture phase: full screen doesn't close on this Esc
        setTool("cursor");
        select(null);
      } else if ((e.key === "Delete" || e.key === "Backspace") && selected) {
        const d = items.find((x) => x.id === selected);
        if (d && !d.locked) {
          e.preventDefault();
          remove(d.id);
        }
      } else if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (mod && e.key.toLowerCase() === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [tool, selected, items, remove, undo, redo]);

  const canUndo = history.current.undo.length > 0;
  const canRedo = history.current.redo.length > 0;
  return useMemo(() => ({
    items, hidden, tool, setTool, selected, select, create, change, remove, available, error,
    setHidden, sizing, editText, undo, redo, canUndo, canRedo, textFocus, alerts, setAlert, removeAlert,
  }), [items, hidden, tool, selected, create, change, remove, available, error, setHidden, sizing, editText, undo, redo,
    canUndo, canRedo, textFocus, alerts, setAlert, removeAlert]);
}
