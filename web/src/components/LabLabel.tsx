import { useCallback, useEffect, useMemo, useState } from "react";

import {
  addLabel, addReviewed, deleteLabel, deleteReviewed, fetchLabWindow, reviewBlock,
  type LabStatus, type LabSuggestion, type LabWindow, type MlBlock,
} from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { CheckIcon, CrossIcon } from "../icons";
import { labelMark, pct, predictionMark, suggestionMark, TAG_TITLE, type LabMark } from "../labPrimitive";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { LabChart } from "./LabChart";

const TIMEFRAMES = ["4H", "1H", "30M", "15M", "5M"];
const TF_SECONDS: Record<string, number> = { "4H": 14400, "1H": 3600, "30M": 1800, "15M": 900, "5M": 300 };
const LIMIT = 300;

type Selection = { anchor: number; start: number; end: number };

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

  const tag = useCallback((id: string) => {
    if (!sel) return;
    const value = negative ? 0 : 1;
    setNegative(false);
    act(() => addLabel({ timeframe: tf, tag: id, start: sel.start, end: sel.end, value }));
  }, [sel, negative, tf, act]);

  const accept = useCallback((list: LabSuggestion[]) => {
    if (!list.length) return;
    act(() => Promise.all(list.map((s) => addLabel({ timeframe: tf, tag: s.tag, start: s.time_unix, end: s.time_unix, value: 1,
      top: s.top, bottom: s.bottom, origin: "detector" }))));
  }, [tf, act]);

  const review = useCallback((list: MlBlock[], verdict: "valid" | "invalid") => {
    if (!list.length) return;
    act(() => Promise.all(list.map((b) => reviewBlock(b, verdict))));
  }, [act]);

  const markReviewed = useCallback((r: { start: number; end: number }) => {
    act(() => addReviewed({ timeframe: tf, start: r.start, end: r.end, tags: tags.map((t) => t.id) }));
  }, [tf, tags, act]);

  // Keyboard: numbers tag, N flips to "not", A accepts, V / X review, arrows move, Delete removes, Esc clears.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      const n = Number(e.key);
      if (Number.isInteger(n) && n >= 1 && n <= tags.length) tag(tags[n - 1].id);
      else if (e.key === "n" || e.key === "N") setNegative((v) => !v);
      else if (e.key === "a" || e.key === "A") accept(suggestionsAt);
      else if (e.key === "v" || e.key === "V") review(predictionsAt, "valid");
      else if (e.key === "x" || e.key === "X") review(predictionsAt, "invalid");
      else if ((e.key === "r" || e.key === "R") && range) markReviewed(range);
      else if ((e.key === "Delete" || e.key === "Backspace") && labelsAt.length) act(() => Promise.all(labelsAt.map((l) => deleteLabel(l.id))));
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
  }, [tags, tag, accept, review, markReviewed, act, pick, sel, range, times, suggestionsAt, predictionsAt, labelsAt]);

  const first = candles[0]?.time;
  const last = candles[candles.length - 1]?.time;
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
  };

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
          </div>
        </div>
        <LabChart candles={candles} marks={marks} reviewed={reviewed} selection={range} highlight={hot} palette={palette}
          resetKey={`${tf}:${end ?? "latest"}`} loading={loading && !win} onPick={pick} />
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
                    onClick={() => act(() => deleteLabel(l.id))}>
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
          </div>
        )}

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
                  <button type="button" className="icon-button" aria-label="Remove reviewed range" onClick={() => act(() => deleteReviewed(r.id))}>
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
            <dt><Kbd>Del</Kbd></dt><dd>Remove labels on the selection</dd>
            <dt><Kbd>Esc</Kbd></dt><dd>Clear the selection</dd>
          </dl>
        </details>
      </aside>
    </div>
  );
}
