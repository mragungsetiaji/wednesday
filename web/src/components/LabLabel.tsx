import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  fetchLabQueue, fetchLabWindow, labBatch, reviewBlock,
  type LabBatch, type LabBatchResult, type LabQueue, type LabStatus, type LabSuggestion, type LabWindow, type MlBlock,
  type QueueScope,
} from "../api";
import { fmtPrice, fmtUnix, fmtWhy } from "../format";
import { CheckIcon, CrossIcon } from "../icons";
import { labelMark, pct, predictionMark, suggestionMark, TAG_TITLE, type LabMark } from "../labPrimitive";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { LabChart } from "./LabChart";

const TIMEFRAMES = ["4H", "1H", "30M", "15M", "5M"];
const TF_SECONDS: Record<string, number> = { "4H": 14400, "1H": 3600, "30M": 1800, "15M": 900, "5M": 300 };
const LIMIT = 300;

type Selection = { anchor: number; start: number; end: number };

const UNDO_DEPTH = 100;
const QUEUE_LEAD = 100; // candles shown after a queue candle when the page has to move to it

const SCOPES: { id: QueueScope; title: string }[] = [
  { id: "all", title: "All candles" },
  { id: "outside", title: "Outside reviewed ranges" },
  { id: "inside", title: "Inside reviewed ranges" },
];

/** One labelling step as the batch that takes it back and the batch that does it again. */
type Step = { undo: LabBatch; redo: LabBatch };

function stepOf(res: LabBatchResult): Step {
  return {
    // Put back what was deleted (same ids, same fields); remove what was written.
    undo: { add_labels: res.deleted_labels, add_reviewed: res.deleted_reviewed,
      delete_labels: res.labels.map((l) => l.id), delete_reviewed: res.reviewed.map((r) => r.id) },
    redo: { add_labels: res.labels, add_reviewed: res.reviewed,
      delete_labels: res.deleted_labels.map((l) => l.id), delete_reviewed: res.deleted_reviewed.map((r) => r.id) },
  };
}

/** Queue candles sit near the cut, so their chances need a decimal to tell apart. */
const pct1 = (p: number) => `${(p * 100).toFixed(1)}%`;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const within = (t: number, s: { start: number; end: number }) => t >= s.start && t <= s.end;
const overlaps = (a: { start: number; end: number }, b: { start: number; end: number }) => a.start <= b.end && b.start <= a.end;

function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/** Label candles: select on the chart, tag with a click or a number key, mark what you've reviewed. */
export function LabLabel({ status, palette, onChanged }: { status: LabStatus; palette: ChartPalette; onChanged: () => void }) {
  const tags = useMemo(() => status.tags ?? [], [status.tags]);
  const hasModel = !!status.active && status.available;
  const [tf, setTf] = usePref("wed.labTf", "5M");
  const [end, setEnd] = useState<number | null>(null);
  const [showSuggest, setShowSuggest] = usePref("wed.labSuggest", true);
  const [showModel, setShowModel] = usePref("wed.labModel", true);
  const [negative, setNegative] = useState(false);
  const [win, setWin] = useState<LabWindow | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState<Selection | null>(null);
  const [hot, setHot] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const refresh = useCallback(() => setReload((n) => n + 1), []);
  const [clearTag, setClearTag] = useState("");
  // Undo / redo: steps live in refs (the keyboard handler reads them), `depth` re-renders the buttons.
  const past = useRef<Step[]>([]);
  const future = useRef<Step[]>([]);
  const [depth, setDepth] = useState({ undo: 0, redo: 0 });
  const syncDepth = () => setDepth({ undo: past.current.length, redo: future.current.length });
  // Review queue: candles the active model is least sure about.
  const [queueOn, setQueueOn] = usePref("wed.labQueue", false);
  const [qTag, setQTag] = usePref("wed.labQueueTag", "");
  const [qScope, setQScope] = usePref<QueueScope>("wed.labQueueScope", "all");
  const [queue, setQueue] = useState<LabQueue | null>(null);
  const [qId, setQId] = useState<string | null>(null); // the queue candle being looked at
  const [qPos, setQPos] = useState(0); // its place in the list, for when it leaves after tagging
  const [focus, setFocus] = useState<number | null>(null);
  const qStart = useRef<{ key: string; total: number } | null>(null);
  const queueActive = queueOn && hasModel;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchLabWindow(tf, end, LIMIT, showModel && hasModel)
      .then((w) => {
        if (!alive) return;
        setWin(w);
        setError(null);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [tf, end, showModel, hasModel, reload]);

  useEffect(() => {
    if (!queueActive) {
      setQueue(null);
      return;
    }
    let alive = true;
    fetchLabQueue(tf, qTag, qScope)
      .then((q) => {
        if (!alive) return;
        const key = `${q.model_id}:${tf}:${qTag}:${qScope}`;
        if (qStart.current?.key !== key) qStart.current = { key, total: q.total };
        setQueue(q);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [queueActive, tf, qTag, qScope, reload, status.active]);

  const candles = useMemo(() => win?.candles ?? [], [win]);
  const times = useMemo(() => candles.map((c) => c.time), [candles]);
  const byTime = useMemo(() => new Map(candles.map((c) => [c.time, c])), [candles]);
  const labels = useMemo(() => win?.labels ?? [], [win]);
  const labelled = useMemo(() => new Set(labels.flatMap((l) => (l.start === l.end ? [`${l.tag}:${l.start}`] : []))), [labels]);
  const suggestions = useMemo(
    () => (showSuggest ? (win?.suggestions ?? []).filter((s) => !labelled.has(`${s.tag}:${s.time_unix}`)) : []),
    [win, showSuggest, labelled]);
  const predictions = useMemo(() => (showModel ? win?.predictions ?? [] : []), [win, showModel]);

  const marks = useMemo<LabMark[]>(() => [
    ...suggestions.map(suggestionMark),
    ...labels.flatMap((l) => labelMark(l, byTime) ?? []),
    ...predictions.map(predictionMark),
  ], [suggestions, labels, predictions, byTime]);

  const reviewed = useMemo(() => win?.reviewed ?? [], [win]);
  const range = sel ? { start: sel.start, end: sel.end } : null;
  const selCandles = range ? times.filter((t) => within(t, range)).length : 0;
  const labelsAt = range ? labels.filter((l) => overlaps(l, range)) : [];
  const suggestionsAt = range ? suggestions.filter((s) => within(s.time_unix, range)) : [];
  const predictionsAt = range ? predictions.filter((p) => within(p.time_unix, range)) : [];

  const pick = useCallback((time: number, extend: boolean) => {
    setSel((cur) => {
      if (extend && cur) return { anchor: cur.anchor, start: Math.min(cur.anchor, time), end: Math.max(cur.anchor, time) };
      return { anchor: time, start: time, end: time };
    });
  }, []);

  const act = useCallback(async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
      refresh();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refresh, onChanged]);

  /** Apply a change and remember how to undo it. */
  const change = useCallback((b: LabBatch) => act(async () => {
    const res = await labBatch(b);
    past.current = [...past.current.slice(1 - UNDO_DEPTH), stepOf(res)];
    future.current = [];
    syncDepth();
  }), [act]);

  const undo = useCallback(() => {
    const step = past.current.at(-1);
    if (!step) return;
    act(async () => {
      await labBatch(step.undo);
      past.current = past.current.slice(0, -1);
      future.current = [...future.current, step];
      syncDepth();
    });
  }, [act]);

  const redo = useCallback(() => {
    const step = future.current.at(-1);
    if (!step) return;
    act(async () => {
      await labBatch(step.redo);
      future.current = future.current.slice(0, -1);
      past.current = [...past.current, step];
      syncDepth();
    });
  }, [act]);

  const tag = useCallback((id: string) => {
    if (!sel) return;
    const value = negative ? 0 : 1;
    setNegative(false);
    change({ add_labels: [{ timeframe: tf, tag: id, start: sel.start, end: sel.end, value }] });
  }, [sel, negative, tf, change]);

  const accept = useCallback((list: LabSuggestion[]) => {
    if (!list.length) return;
    if (list.length > 1 && !window.confirm(`Accept ${plural(list.length, "suggestion")} on the selection?`)) return;
    change({ add_labels: list.map((s) => ({ timeframe: tf, tag: s.tag, start: s.time_unix, end: s.time_unix, value: 1 as const,
      top: s.top, bottom: s.bottom, origin: "detector" as const })) });
  }, [tf, change]);

  const remove = useCallback((ids: string[], ask: boolean) => {
    if (!ids.length) return;
    if (ask && !window.confirm(`Remove ${plural(ids.length, "label")} on the selection?`)) return;
    change({ delete_labels: ids });
  }, [change]);

  const review = useCallback((list: MlBlock[], verdict: "valid" | "invalid") => {
    if (!list.length) return;
    act(() => Promise.all(list.map((b) => reviewBlock(b, verdict))));
  }, [act]);

  const markReviewed = useCallback((r: { start: number; end: number }) => {
    change({ add_reviewed: [{ timeframe: tf, start: r.start, end: r.end, tags: tags.map((t) => t.id) }] });
  }, [tf, tags, change]);

  const first = candles[0]?.time;
  const last = candles[candles.length - 1]?.time;

  /** Select a queue candle; the page only moves when the candle isn't on it. */
  const goTo = useCallback((i: number) => {
    const items = queue?.items ?? [];
    if (!items.length) return;
    const item = items[Math.max(0, Math.min(items.length - 1, i))];
    const t = item.time_unix;
    setQId(item.id);
    setQPos(items.indexOf(item));
    setSel({ anchor: t, start: t, end: t });
    setFocus(t);
    if (!(first !== undefined && last !== undefined && t >= first && t <= last)) {
      const next = t + QUEUE_LEAD * TF_SECONDS[tf];
      setEnd(win?.history && next >= win.history.last_unix ? null : next);
    }
  }, [queue, first, last, tf, win]);

  const step = useCallback((dir: 1 | -1) => {
    const items = queue?.items ?? [];
    const at = qId ? items.findIndex((x) => x.id === qId) : -1;
    if (at >= 0) goTo(at + dir);
    else if (qId) goTo(dir > 0 ? qPos : qPos - 1); // it left the queue: the next one slid into its place
    else goTo(0);
  }, [queue, qId, qPos, goTo]);

  // Keyboard: numbers tag, N flips to "not", A accepts, V / X review, arrows move, Delete removes, Esc clears,
  // Ctrl+Z / Ctrl+Shift+Z (or Ctrl+Y; Cmd on a Mac) undo and redo.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest("input, textarea, select, [contenteditable]")) return;
      const key = e.key.toLowerCase();
      if ((e.metaKey || e.ctrlKey) && !e.altKey && (key === "z" || key === "y")) {
        e.preventDefault();
        if (key === "y" || e.shiftKey) redo();
        else undo();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const n = Number(e.key);
      if (Number.isInteger(n) && n >= 1 && n <= tags.length) tag(tags[n - 1].id);
      else if ((e.key === "j" || e.key === "J") && queueActive) step(1);
      else if ((e.key === "k" || e.key === "K") && queueActive) step(-1);
      else if ((e.key === "q" || e.key === "Q") && hasModel) setQueueOn(!queueOn);
      else if (e.key === "n" || e.key === "N") setNegative((v) => !v);
      else if (e.key === "a" || e.key === "A") accept(suggestionsAt);
      else if (e.key === "v" || e.key === "V") review(predictionsAt, "valid");
      else if (e.key === "x" || e.key === "X") review(predictionsAt, "invalid");
      else if ((e.key === "r" || e.key === "R") && range) markReviewed(range);
      else if ((e.key === "Delete" || e.key === "Backspace") && labelsAt.length) remove(labelsAt.map((l) => l.id), labelsAt.length > 1);
      else if (e.key === "Escape") setSel(null);
      else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && sel && times.length) {
        const dir = e.key === "ArrowLeft" ? -1 : 1;
        const edge = sel.anchor === sel.start ? sel.end : sel.start; // the moving end
        const from = e.shiftKey ? edge : dir < 0 ? sel.start : sel.end;
        const i = Math.min(times.length - 1, Math.max(0, times.indexOf(from) + dir));
        pick(times[i], e.shiftKey);
      } else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [tags, tag, accept, review, markReviewed, remove, undo, redo, pick, sel, range, times, suggestionsAt, predictionsAt, labelsAt,
    queueActive, step, hasModel, queueOn, setQueueOn]);


  const older = () => first && setEnd(first - 1);
  const newer = () => {
    if (!last) return;
    const next = last + LIMIT * TF_SECONDS[tf];
    setEnd(win?.history && next >= win.history.last_unix ? null : next);
  };
  const jump = (value: string) => {
    if (!value) return setEnd(null);
    setEnd(Math.floor(Date.parse(`${value}T23:59:00Z`) / 1000));
  };
  const setTimeframe = (name: string) => {
    setTf(name);
    setSel(null);
    setQId(null);
  };

  const current = queue?.items.find((x) => x.id === qId) ?? null;
  const detectorAt = current ? (win?.suggestions ?? []).find((x) => x.tag === current.tag && x.time_unix === current.time_unix) : undefined;
  const doneHere = queue && qStart.current ? Math.max(0, qStart.current.total - queue.total) : 0;

  return (
    <div className="lab-label">
      <section className="chart-area" aria-label="Labelling chart">
        <div className="chart-toolbar">
          <div className="tabs" role="tablist" aria-label="Timeframe">
            {TIMEFRAMES.map((name) => (
              <button key={name} type="button" role="tab" aria-selected={name === tf} className="tab" onClick={() => setTimeframe(name)}>
                {name}
              </button>
            ))}
          </div>
          <div className="lab-nav" role="group" aria-label="History">
            <button type="button" className="button quiet" onClick={older} disabled={!win?.has_more || loading}>Older</button>
            <button type="button" className="button quiet" onClick={newer} disabled={end === null || loading}>Newer</button>
            <input type="date" className="lab-date" aria-label="Go to date" onChange={(e) => jump(e.target.value)}
              value={end ? new Date(end * 1000).toISOString().slice(0, 10) : ""} />
            <button type="button" className="button quiet" onClick={() => setEnd(null)} disabled={end === null}>Latest</button>
          </div>
          <div className="toggles">
            <label className="toggle">
              <input type="checkbox" checked={showSuggest} onChange={(e) => setShowSuggest(e.target.checked)} />
              Detector
            </label>
            <label className="toggle" title={hasModel ? undefined : "Set a model active in Models"}>
              <input type="checkbox" checked={showModel && hasModel} disabled={!hasModel} onChange={(e) => setShowModel(e.target.checked)} />
              Model
            </label>
            <label className="toggle" title={hasModel ? "Candles the model is least sure about (Q)" : "Set a model active in Models"}>
              <input type="checkbox" checked={queueActive} disabled={!hasModel} onChange={(e) => setQueueOn(e.target.checked)} />
              Queue
            </label>
          </div>
        </div>
        <LabChart candles={candles} marks={marks} reviewed={reviewed} selection={range} highlight={hot} palette={palette}
          resetKey={`${tf}:${end ?? "latest"}`} focus={focus} loading={loading && !win} onPick={pick} />
        <div className="chart-foot">
          <ul className="legend" aria-label="Chart legend">
            <li><span className="key key-bull" /> Your label</li>
            <li><span className="key key-suggest" /> Detector suggestion</li>
            <li><span className="key key-ml" /> Model</li>
            <li><span className="key key-not" /> Marked not</li>
            <li><span className="key key-reviewed" /> Reviewed range</li>
            {first && last && <li className="muted">{fmtUnix(first)} → {fmtUnix(last)}</li>}
          </ul>
        </div>
      </section>

      <aside className="rail lab-rail" aria-label="Label the selection">
        {queueActive && (
          <div className="lab-block lab-queue">
            <div className="lab-block-head">
              <h2>Queue</h2>
              <span className="muted num" role="status">
                {queue ? `${queue.total} in queue · ${doneHere} done this session` : "Scoring the history…"}
              </span>
            </div>
            <div className="lab-queue-filters">
              <select value={qTag} aria-label="Queue tag" onChange={(e) => { setQTag(e.target.value); setQId(null); }}>
                <option value="">All tags</option>
                {tags.map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
              </select>
              <select value={qScope} aria-label="Queue candles" onChange={(e) => { setQScope(e.target.value as QueueScope); setQId(null); }}>
                {SCOPES.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
              </select>
            </div>
            {queue?.note && <p className="lab-hint">{queue.note}</p>}
            {current ? (
              <dl className="lab-queue-call">
                <dt>Candle</dt><dd className="num">{current.title} · {fmtUnix(current.time_unix)}</dd>
                <dt>Model</dt>
                <dd>
                  {pct1(current.prob)} against a cut of {pct1(current.cut)}:{" "}
                  <strong>{current.prob >= current.cut ? "tags it" : "doesn't tag it"}</strong>
                </dd>
                <dt>Detector</dt>
                <dd>{detectorAt ? <>found it{detectorAt.priority === "middle" ? " (mid)" : ""}</> : "didn't find it"}</dd>
              </dl>
            ) : (
              queue && queue.total > 0 && <p className="lab-hint">Press <Kbd>J</Kbd> for the candle the model is least sure about.</p>
            )}
            {queue && queue.total === 0 && !queue.note && <p className="lab-hint">Nothing left: every candle here is labelled.</p>}
            <div className="lab-actions">
              <button type="button" className="button quiet" disabled={!queue?.items.length} onClick={() => step(-1)}>
                Previous <Kbd>K</Kbd>
              </button>
              <button type="button" className="button secondary" disabled={!queue?.items.length} onClick={() => step(1)}>
                Next <Kbd>J</Kbd>
              </button>
            </div>
            <p className="lab-hint">Tag it with the number keys (<Kbd>N</Kbd> first for “not”); tagged candles leave the queue.</p>
          </div>
        )}

        <div className="lab-block">
          <h2>Selection</h2>
          {range ? (
            <p className="lab-sel num">
              {tf} · {selCandles} candle{selCandles === 1 ? "" : "s"} · {fmtUnix(range.start)}
              {range.end !== range.start && <> → {fmtUnix(range.end)}</>}
            </p>
          ) : (
            <p className="lab-hint">Click a candle to select it. Shift-click (or Shift + arrow) to select a range.</p>
          )}
        </div>

        <div className="lab-block">
          <div className="lab-block-head">
            <h2>Tag</h2>
            <div className="segmented" role="group" aria-label="Tag as">
              <button type="button" className="seg seg-text" aria-pressed={!negative} onClick={() => setNegative(false)}>Is</button>
              <button type="button" className="seg seg-text" aria-pressed={negative} onClick={() => setNegative(true)}>
                Is not <Kbd>N</Kbd>
              </button>
            </div>
          </div>
          <div className="lab-tags">
            {tags.map((t, i) => (
              <button key={t.id} type="button" className={`lab-tag tag-${t.id}${negative ? " is-not" : ""}`} disabled={!range} onClick={() => tag(t.id)}>
                <span className={`key key-tag-${t.id}`} aria-hidden="true" />
                {negative ? `Not ${t.title}` : t.title}
                <Kbd>{i + 1}</Kbd>
              </button>
            ))}
          </div>
        </div>

        {range && (labelsAt.length > 0 || suggestionsAt.length > 0 || predictionsAt.length > 0) && (
          <div className="lab-block">
            <h2>On the selection</h2>
            <ul className="lab-items">
              {labelsAt.map((l) => (
                <li key={l.id} onMouseEnter={() => setHot(l.id)} onMouseLeave={() => setHot(null)}>
                  <span className="lab-item-text">
                    {l.value ? "" : "Not "}{TAG_TITLE[l.tag]}
                    <span className="muted"> · {l.origin === "manual" ? "yours" : l.origin === "detector" ? "accepted" : "reviewed"}</span>
                  </span>
                  <button type="button" className="icon-button" aria-label={`Remove ${TAG_TITLE[l.tag]} label`}
                    onClick={() => remove([l.id], false)}>
                    <CrossIcon size={13} />
                  </button>
                </li>
              ))}
              {suggestionsAt.map((s) => (
                <li key={s.id} onMouseEnter={() => setHot(s.id)} onMouseLeave={() => setHot(null)}>
                  <span className="lab-item-text">
                    Detector: {s.label}{s.priority === "middle" ? " (mid)" : ""}
                    <span className="muted num"> · {s.top === s.bottom ? fmtPrice(s.top) : `${fmtPrice(s.bottom)} – ${fmtPrice(s.top)}`}</span>
                  </span>
                  <button type="button" className="button quiet" onClick={() => accept([s])}>Accept <Kbd>A</Kbd></button>
                </li>
              ))}
              {predictionsAt.map((b) => (
                <li key={b.id} onMouseEnter={() => setHot(b.id)} onMouseLeave={() => setHot(null)}>
                  <span className="lab-item-text">
                    Model: {b.title} {pct(b.prob)}
                    {b.outcome_prob !== null && <span className="muted"> · win {pct(b.outcome_prob)}</span>}
                    {b.verdict && <span className="muted"> · marked {b.verdict}</span>}
                    {!!b.why?.length && <span className="lab-why">Why: {fmtWhy(b.why)}</span>}
                  </span>
                  <span className="verdict">
                    <button type="button" className="verdict-btn" aria-pressed={b.verdict === "valid"} onClick={() => review([b], "valid")}
                      aria-label="Valid" title="Valid (V)"><CheckIcon size={13} /></button>
                    <button type="button" className="verdict-btn" aria-pressed={b.verdict === "invalid"} onClick={() => review([b], "invalid")}
                      aria-label="Invalid" title="Invalid (X)"><CrossIcon size={13} /></button>
                  </span>
                </li>
              ))}
            </ul>
            <div className="lab-actions">
              {suggestionsAt.length > 1 && (
                <button type="button" className="button secondary" onClick={() => accept(suggestionsAt)}>
                  Accept all {suggestionsAt.length}
                </button>
              )}
              {labelsAt.length > 0 && (
                <span className="lab-clear">
                  <select value={clearTag} aria-label="Labels to clear" onChange={(e) => setClearTag(e.target.value)}>
                    <option value="">All tags</option>
                    {tags.filter((t) => labelsAt.some((l) => l.tag === t.id)).map((t) => <option key={t.id} value={t.id}>{t.title}</option>)}
                  </select>
                  <button type="button" className="button quiet"
                    onClick={() => remove(labelsAt.filter((l) => !clearTag || l.tag === clearTag).map((l) => l.id), true)}>
                    Clear labels
                  </button>
                </span>
              )}
            </div>
          </div>
        )}

        <div className="lab-block">
          <div className="lab-actions" role="group" aria-label="History of changes">
            <button type="button" className="button quiet" disabled={!depth.undo} onClick={undo}>Undo <Kbd>Ctrl Z</Kbd></button>
            <button type="button" className="button quiet" disabled={!depth.redo} onClick={redo}>Redo</button>
          </div>
        </div>

        <div className="lab-block">
          <h2>Reviewed</h2>
          <p className="lab-hint">
            In a reviewed range, every candle you didn't tag counts as <em>not</em> that tag when training. Candles outside
            reviewed ranges are left out, so mark a range only once you've tagged everything in it.
          </p>
          <div className="lab-actions">
            <button type="button" className="button secondary" disabled={!range} onClick={() => range && markReviewed(range)}>
              Selection reviewed <Kbd>R</Kbd>
            </button>
            <button type="button" className="button quiet" disabled={!first || !last} onClick={() => first && last && markReviewed({ start: first, end: last })}>
              Whole page reviewed
            </button>
          </div>
          {reviewed.length > 0 && (
            <ul className="lab-items">
              {reviewed.map((r) => (
                <li key={r.id}>
                  <span className="lab-item-text num">{fmtUnix(r.start)} → {fmtUnix(r.end)}</span>
                  <button type="button" className="icon-button" aria-label="Remove reviewed range" onClick={() => change({ delete_reviewed: [r.id] })}>
                    <CrossIcon size={13} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <p className="text-error lab-error" role="alert">{error}</p>}

        <details className="lab-keys">
          <summary>Keyboard</summary>
          <dl>
            <dt><Kbd>1</Kbd>–<Kbd>{tags.length}</Kbd></dt><dd>Tag the selection</dd>
            <dt><Kbd>N</Kbd></dt><dd>Next tag is “not”</dd>
            <dt><Kbd>A</Kbd></dt><dd>Accept the detector's suggestion</dd>
            <dt><Kbd>V</Kbd> <Kbd>X</Kbd></dt><dd>Model block valid / invalid</dd>
            <dt><Kbd>←</Kbd> <Kbd>→</Kbd></dt><dd>Move; with Shift, grow the range</dd>
            <dt><Kbd>R</Kbd></dt><dd>Selection reviewed</dd>
            <dt><Kbd>Q</Kbd></dt><dd>Review queue on / off</dd>
            <dt><Kbd>J</Kbd> <Kbd>K</Kbd></dt><dd>Next / previous candle in the queue</dd>
            <dt><Kbd>Del</Kbd></dt><dd>Remove labels on the selection</dd>
            <dt><Kbd>Ctrl</Kbd> <Kbd>Z</Kbd></dt><dd>Undo; with Shift (or <Kbd>Ctrl</Kbd> <Kbd>Y</Kbd>), redo. Cmd on a Mac</dd>
            <dt><Kbd>Esc</Kbd></dt><dd>Clear the selection</dd>
          </dl>
        </details>
      </aside>
    </div>
  );
}
