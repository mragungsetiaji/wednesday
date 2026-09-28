import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  cancelTraining, confirmImport, confirmLabels, deleteModel, fetchLab, fetchScorecard, labelsExportUrl, modelFileUrl,
  scoreOnSameWindow, setActiveModel, stageImport, stageLabels, startTraining,
  type Importance, type LabStatus, type MergeCounts, type ModelManifest, type Scorecard, type StagedLabels,
  type Signature, type StagedModel, type TagMetrics, type TrainingRun, type TrainParams, type WindowScores,
} from "../api";
import { fmtUnix } from "../format";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { LabBacktest } from "./LabBacktest";
import { LabData } from "./LabData";
import { LabLabel } from "./LabLabel";
import { ScorecardLine } from "./MlPanel";

type Sub = "label" | "train" | "models" | "backtest" | "data";
const SUBS: { id: Sub; title: string }[] = [
  { id: "label", title: "Label" },
  { id: "train", title: "Train" },
  { id: "models", title: "Models" },
  { id: "backtest", title: "Backtest" },
  { id: "data", title: "Data" },
];
const subFromHash = (): Sub => {
  const s = window.location.hash.split("/")[1];
  return s === "train" || s === "models" || s === "backtest" || s === "data" ? s : "label";
};

const pctOf = (v: number | undefined | null) => (v === undefined || v === null ? "—" : `${Math.round(v * 100)}%`);
const fmtR = (v: number | undefined | null) => (v === undefined || v === null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}R`);
const fmtDate = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

/**
 * The Lab: a workbench apart from the screener. Label candles, train a model on the labels,
 * keep model files (yours or imported). The screener only shows the active model's output.
 */
export function LabPage({ palette }: { palette: ChartPalette }) {
  const [sub, setSub] = useState<Sub>(subFromHash);
  const [status, setStatus] = useState<LabStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setSub(subFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const load = useCallback(() => {
    fetchLab()
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const running = !!status?.training?.running;
  useEffect(() => {
    load();
    const id = setInterval(load, running ? 1500 : 20000);
    return () => clearInterval(id);
  }, [load, running]);

  const go = (s: Sub) => {
    window.location.hash = s === "label" ? "lab" : `lab/${s}`;
    setSub(s);
  };

  if (!status) {
    return <main className="lab-page"><p className="empty">{error ? `Can't load the Lab: ${error}` : "Loading the Lab…"}</p></main>;
  }
  if (!status.editable) {
    return (
      <main className="lab-page">
        <div className="settings-intro">
          <h2>Lab</h2>
          <p>{status.reason ?? "The Lab needs the server running with --serve and a database."}</p>
        </div>
      </main>
    );
  }

  const h = status.history;
  return (
    <main className={`lab-page${sub === "label" ? " is-wide" : ""}`}>
      <div className="lab-head">
        <div className="tabs" role="tablist" aria-label="Lab">
          {SUBS.map((s) => (
            <button key={s.id} type="button" role="tab" className="tab" aria-selected={sub === s.id} onClick={() => go(s.id)}>
              {s.title}
              {s.id === "train" && running && <span className="lab-dot" aria-label="training" />}
            </button>
          ))}
        </div>
        <p className="meta">
          <span>{status.symbol}</span>
          {h && <span className="num">{h.bars.toLocaleString()} M1 bars · {fmtUnix(h.first_unix).slice(0, 10)} → {fmtUnix(h.last_unix).slice(0, 10)}</span>}
          {status.active && <span>Active model: {status.models?.find((m) => m.id === status.active)?.name ?? status.active}</span>}
        </p>
      </div>
      {!status.available && (
        <div className="banner" role="alert">
          {status.reason} Labelling works without it; training, models and the dataset download need it.
        </div>
      )}
      {sub === "label" && <LabLabel status={status} palette={palette} onChanged={load} />}
      {sub === "train" && <LabTrain status={status} onStatus={setStatus} />}
      {sub === "models" && <LabModels status={status} onStatus={setStatus} />}
      {sub === "backtest" && <LabBacktest status={status} palette={palette} />}
      {sub === "data" && <LabData onChanged={load} />}
    </main>
  );
}

// ---- Train -------------------------------------------------------------------------------

const DEFAULTS: TrainParams = {
  timeframes: ["5M", "15M"], tags: [], lookback: 10, confirm: 3, rr: 2, horizon_hours: 72,
  outcome_from_detector: true, test_fraction: 0.2, folds: 4, name: "", author: "", note: "",
};

function LabTrain({ status, onStatus }: { status: LabStatus; onStatus: (s: LabStatus) => void }) {
  const tags = useMemo(() => status.tags ?? [], [status.tags]);
  const [saved, setSaved] = usePref<TrainParams>("wed.trainParams", DEFAULTS);
  const [form, setForm] = useState<TrainParams>(() => ({ ...DEFAULTS, ...saved, tags: saved.tags.length ? saved.tags : tags.map((t) => t.id) }));
  const [error, setError] = useState<string | null>(null);
  const [format, setFormat] = useState("parquet");
  const [days, setDays] = useState(30);
  const training = status.training;
  const set = (patch: Partial<TrainParams>) => setForm({ ...form, ...patch });
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  const count = (tag: string) => form.timeframes.reduce((acc, tf) => {
    const c = status.counts?.[tf]?.tags[tag];
    return { yes: acc.yes + (c?.yes ?? 0), no: acc.no + (c?.no ?? 0) };
  }, { yes: 0, no: 0 });
  const reviewedRanges = form.timeframes.reduce((n, tf) => n + (status.counts?.[tf]?.reviewed ?? 0), 0);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      setSaved({ ...form, name: "", note: "" });
      onStatus(await startTraining(form));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const stop = async () => {
    try {
      onStatus(await cancelTraining());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="lab-columns">
      <form className="settings-form" onSubmit={submit} aria-labelledby="train-h">
        <div className="settings-intro">
          <h2 id="train-h">Train a model</h2>
          <p>
            One classifier per tag learns from your labels on the timeframes you pick, and an outcome model learns
            whether order blocks traded with the limit plan reach the target first. Everything is split by time:{" "}
            {form.folds > 1
              ? <>the history is cut into {form.folds + 1} slices and each of {form.folds} folds is scored on one slice after
                  training on everything before it, so you see how stable the model is, not one lucky week.</>
              : <>the scores come from the latest {Math.round(form.test_fraction * 100)}%, trained on what came before.</>}{" "}
            The probability cut is tuned on the latest {Math.round(form.test_fraction * 100)}% of each training part, then
            the saved model is refit on everything.
          </p>
        </div>

        <fieldset className="fields" disabled={training?.running}>
          <legend>Data</legend>
          <div className="field">
            <span className="field-label">Timeframes</span>
            <div className="chip-row" role="group" aria-label="Timeframes">
              {["5M", "15M", "30M", "1H", "4H"].map((tf) => (
                <button key={tf} type="button" className="chip" aria-pressed={form.timeframes.includes(tf)}
                  onClick={() => set({ timeframes: toggle(form.timeframes, tf) })}>{tf}</button>
              ))}
            </div>
            <span className="field-hint">{reviewedRanges} reviewed range{reviewedRanges === 1 ? "" : "s"} on these timeframes.</span>
          </div>
          <div className="field">
            <span className="field-label">Tags</span>
            <table className="data compact lab-count">
              <thead>
                <tr><th scope="col">Train</th><th scope="col">Tag</th><th scope="col" className="end">Yes</th><th scope="col" className="end">Not</th></tr>
              </thead>
              <tbody>
                {tags.map((t) => {
                  const c = count(t.id);
                  return (
                    <tr key={t.id}>
                      <td><input type="checkbox" aria-label={`Train ${t.title}`} checked={form.tags.includes(t.id)}
                        onChange={() => set({ tags: toggle(form.tags, t.id) })} /></td>
                      <td>{t.title}</td>
                      <td className="end num">{c.yes}</td>
                      <td className="end num">{c.no}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <span className="field-hint">“Not” counts only explicit ones; untagged candles in reviewed ranges add more.</span>
          </div>
        </fieldset>

        <fieldset className="fields fields-grid" disabled={training?.running}>
          <legend>Model</legend>
          <label className="field">
            <span className="field-label">Candles before</span>
            <input type="number" min={2} max={50} value={form.lookback} onChange={(e) => set({ lookback: Number(e.target.value) })} />
          </label>
          <label className="field">
            <span className="field-label">Confirming candles</span>
            <input type="number" min={1} max={20} value={form.confirm} onChange={(e) => set({ confirm: Number(e.target.value) })} />
          </label>
          <label className="field">
            <span className="field-label">Target (R)</span>
            <input type="number" min={0.5} max={10} step={0.5} value={form.rr} onChange={(e) => set({ rr: Number(e.target.value) })} />
          </label>
          <label className="field">
            <span className="field-label">Trade horizon (hours)</span>
            <input type="number" min={1} max={720} value={form.horizon_hours} onChange={(e) => set({ horizon_hours: Number(e.target.value) })} />
          </label>
          <label className="field">
            <span className="field-label">Scored on</span>
            <select value={form.folds} onChange={(e) => set({ folds: Number(e.target.value) })}>
              <option value={1}>One split (latest part)</option>
              {[2, 3, 4, 5, 6].map((v) => <option key={v} value={v}>{v} walk-forward folds</option>)}
            </select>
          </label>
          <label className="field">
            <span className="field-label">{form.folds > 1 ? "Tuning share" : "Held out for scores"}</span>
            <select value={form.test_fraction} onChange={(e) => set({ test_fraction: Number(e.target.value) })}>
              {[0.1, 0.2, 0.3].map((v) => <option key={v} value={v}>Latest {v * 100}%</option>)}
            </select>
          </label>
          <label className="field field-check">
            <input type="checkbox" checked={form.outcome_from_detector} onChange={(e) => set({ outcome_from_detector: e.target.checked })} />
            <span>Outcome model also learns from the detector's order blocks</span>
          </label>
          <p className="field-hint field-span">
            A candle is judged once the confirming candles after it have closed: the model sees the candles before it,
            the candle and those, plus the last closed candles of the next two timeframes up. Nothing later.
          </p>
        </fieldset>

        <fieldset className="fields fields-grid" disabled={training?.running}>
          <legend>File</legend>
          <label className="field">
            <span className="field-label">Name</span>
            <input type="text" value={form.name} placeholder="e.g. OB 5M v1" onChange={(e) => set({ name: e.target.value })} />
          </label>
          <label className="field">
            <span className="field-label">Author</span>
            <input type="text" value={form.author} placeholder="Shown when someone imports it" onChange={(e) => set({ author: e.target.value })} />
          </label>
          <label className="field field-span">
            <span className="field-label">Note</span>
            <input type="text" value={form.note} onChange={(e) => set({ note: e.target.value })} />
          </label>
        </fieldset>

        <div className="form-actions">
          <button type="submit" className="button primary"
            disabled={training?.running || !status.available || !form.timeframes.length || !form.tags.length}>
            {training?.running ? "Training…" : "Train model"}
          </button>
          {training?.running && (
            <button type="button" className="button secondary" disabled={training.stage === "Stopping"} onClick={stop}>
              Stop
            </button>
          )}
          <span className="form-status" role="status">
            {training?.running ? training.stage : error ? <span className="text-error">{error}</span> : ""}
          </span>
        </div>
        {training?.error && !training.running && <p className="text-error field-note">Training failed: {training.error}</p>}
        {training?.cancelled && !training.running && <p className="field-note">Training stopped; nothing was saved.</p>}
      </form>

      <div className="lab-side">
        {training?.last && (
          <section className="settings-form" aria-labelledby="last-h">
            <div className="settings-intro">
              <h2 id="last-h">Last run: {training.last.name}</h2>
              <p>Saved in Models. Set it active there to see it on the chart.</p>
            </div>
            <ModelScores manifest={training.last} />
          </section>
        )}
        <section className="settings-form" aria-labelledby="data-h">
          <div className="settings-intro">
            <h2 id="data-h">Dataset</h2>
            <p>
              One row per minute: M1 OHLCV, then for 5M to 4H the candle forming at that minute and one column per tag
              (1 tagged, 0 not, empty if never reviewed). For your own notebooks.
            </p>
          </div>
          <div className="lab-download">
            <select value={format} aria-label="Format" onChange={(e) => setFormat(e.target.value)}>
              <option value="parquet">Parquet</option>
              <option value="csv">CSV</option>
            </select>
            <label className="lab-days">
              Last
              <input type="number" min={1} max={400} value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Days" />
              days
            </label>
            <a className="button secondary" href={`/api/lab/dataset?format=${format}&days=${days}`} download>Download</a>
          </div>
          <p className="field-hint">
            Reviews of model blocks, with the candle's features, your verdict (+1 / −1) and the traded result in R:{" "}
            <a className="link" href="/api/lab/feedback" download>feedback.csv</a>. The start of a reinforcement learning set.
          </p>
        </section>
        <LabelsFile symbol={status.symbol ?? ""} onStatus={onStatus} />
        {!!status.runs?.length && <RecentRuns runs={status.runs} />}
      </div>
    </div>
  );
}

const RUN_STATUS: Record<TrainingRun["status"], string> = {
  running: "Training", done: "Saved", error: "Failed", cancelled: "Stopped", interrupted: "Interrupted",
};

function RecentRuns({ runs }: { runs: TrainingRun[] }) {
  return (
    <section className="settings-form" aria-labelledby="runs-h">
      <div className="settings-intro">
        <h2 id="runs-h">Recent runs</h2>
      </div>
      <div className="table-scroll">
        <table className="data compact">
          <thead>
            <tr><th scope="col">Started</th><th scope="col">Timeframes</th><th scope="col">Result</th></tr>
          </thead>
          <tbody>
            {runs.slice(0, 8).map((r) => (
              <tr key={r.id}>
                <td className="num">{fmtDate(r.started_at)}</td>
                <td>{r.params.timeframes?.join(", ") ?? "—"}</td>
                <td className={r.status === "error" || r.status === "interrupted" ? "text-error" : undefined}>
                  {RUN_STATUS[r.status]}{r.error && `: ${r.error}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const fmtCounts = (c: MergeCounts) => `${c.new} new, ${c.updated} updated, ${c.skipped} already here`;

/** Labels and reviewed ranges as a JSON file: a backup, or the labels of another machine or person. */
function LabelsFile({ symbol, onStatus }: { symbol: string; onStatus: (s: LabStatus) => void }) {
  const [staged, setStaged] = useState<StagedLabels | null>(null);
  const [map, setMap] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const choose = async (file: File | undefined) => {
    if (fileRef.current) fileRef.current.value = "";
    if (!file) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      setStaged(await stageLabels(file));
      setMap(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const merge = async () => {
    if (!staged) return;
    setBusy(true);
    setError(null);
    try {
      const s = await confirmLabels(staged.token, map);
      setStaged(null);
      setNote(`Labels: ${fmtCounts(s.imported.labels)}. Reviewed ranges: ${fmtCounts(s.imported.reviewed)}.`);
      onStatus(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-form" aria-labelledby="labels-file-h">
      <div className="settings-intro">
        <h2 id="labels-file-h">Labels file</h2>
        <p>
          Every label and reviewed range of {symbol || "this symbol"} as one JSON file: a backup, or labels to move to
          another machine. Importing merges: the same label updates, one already here is skipped.
        </p>
      </div>
      <div className="form-actions">
        <a className="button secondary" href={labelsExportUrl} download>Export labels</a>
        <input ref={fileRef} type="file" accept=".json,application/json" hidden onChange={(e) => choose(e.target.files?.[0])} />
        <button type="button" className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}>
          Import labels
        </button>
      </div>
      {staged && (
        <div className="import-card" role="dialog" aria-labelledby="labels-import-h">
          <h3 id="labels-import-h">Labels of {staged.symbol || "an unnamed symbol"}</h3>
          <dl className="import-facts">
            <dt>Labels</dt><dd>{fmtCounts(staged.labels)}</dd>
            <dt>Reviewed ranges</dt><dd>{fmtCounts(staged.reviewed)}</dd>
          </dl>
          {!staged.matches && (
            <label className="field field-check">
              <input type="checkbox" checked={map} onChange={(e) => setMap(e.target.checked)} />
              <span>The file is for {staged.symbol || "another symbol"}; add its labels to {symbol} anyway</span>
            </label>
          )}
          <div className="form-actions">
            <button type="button" className="button primary" disabled={busy || (!staged.matches && !map)} onClick={merge}>
              Merge labels
            </button>
            <button type="button" className="button quiet" disabled={busy} onClick={() => setStaged(null)}>Cancel</button>
          </div>
        </div>
      )}
      {(note || error) && (
        <p className={`field-note${error ? " text-error" : ""}`} role="status">{error ?? note}</p>
      )}
    </section>
  );
}

function ModelScores({ manifest }: { manifest: ModelManifest }) {
  const rows = Object.entries(manifest.tags);
  const o = manifest.outcome;
  const folds = Math.max(0, ...rows.map(([, m]) => m.folds?.length ?? 0), o?.folds?.length ?? 0);
  return (
    <>
      <table className="data compact">
        <thead>
          <tr>
            <th scope="col">Tag</th>
            <th scope="col" className="end">Yes / all</th>
            <th scope="col" className="end">Precision</th>
            <th scope="col" className="end">Recall</th>
            <th scope="col" className="end">AUC</th>
            <th scope="col" className="end">Cut</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([id, m]) => (
            <tr key={id}>
              <th scope="row">{m.title ?? id}</th>
              {m.trained ? (
                <>
                  <td className="end num">{m.positives} / {m.samples}</td>
                  <td className="end num">{pctOf(m.precision)}<Spread v={m.spread?.precision} pct /></td>
                  <td className="end num">{pctOf(m.recall)}<Spread v={m.spread?.recall} pct /></td>
                  <td className="end num">{m.auc?.toFixed(2) ?? "—"}<Spread v={m.spread?.auc} /></td>
                  <td className="end num">{pctOf(m.threshold)}</td>
                </>
              ) : (
                <td colSpan={5} className="muted">{m.skipped}</td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="field-hint">
        {folds
          ? <>Mean ± spread over {folds} walk-forward folds, each scored on data after what it trained on, with a gap of{" "}
              {manifest.params.gap_minutes ?? 0} minutes between. The cut is the latest fold's.</>
          : <>Held-out part only.</>}{" "}
        Precision: of the candles the model called, how many you had tagged. Recall: of your tags, how many it found.
        Both at the cut, the probability tuned before the held-out part.
      </p>
      {o && (
        <p className="field-note">
          {o.trained ? (
            <>
              <strong>Outcome</strong> ({o.samples} trades: {o.from_labels ?? 0} from labels, {o.from_detector ?? 0} from the detector):
              held-out AUC {o.auc?.toFixed(2) ?? "—"}, {pctOf(o.base_rate)} of held-out trades won; taking all of them averaged{" "}
              {fmtR(o.avg_r_all)}, taking the {o.picked ?? 0} the model liked averaged {fmtR(o.avg_r_picked)}
              {o.folds && <> (per fold: {o.folds.map((f) => fmtR(f.avg_r_picked)).join(", ")})</>}.
            </>
          ) : (
            <><strong>Outcome</strong>: {o.skipped}</>
          )}
        </p>
      )}
      {folds > 0 && <PerFold manifest={manifest} />}
      <Explained manifest={manifest} />
    </>
  );
}

function Spread({ v, pct }: { v?: number; pct?: boolean }) {
  if (v === undefined) return null;
  return <span className="muted lab-spread"> ± {pct ? Math.round(v * 100) : v.toFixed(2)}</span>;
}

/** Each fold's own scores, so a model that only shone in one window shows it. */
function PerFold({ manifest }: { manifest: ModelManifest }) {
  const parts = [
    ...Object.entries(manifest.tags).filter(([, m]) => m.folds?.length).map(([id, m]) => [m.title ?? id, m] as const),
    ...(manifest.outcome?.folds?.length ? [["Outcome", manifest.outcome] as const] : []),
  ];
  return (
    <details className="lab-explain">
      <summary>Scores per fold</summary>
      <div className="lab-explain-grid lab-folds">
        {parts.map(([title, m]) => {
          const outcome = title === "Outcome";
          return (
            <section key={title} aria-label={title}>
              <h4>{title}</h4>
              <table className="data compact">
                <thead>
                  <tr>
                    <th scope="col">Tested from</th>
                    <th scope="col" className="end">Yes / all</th>
                    <th scope="col" className="end">{outcome ? "Avg R" : "Prec."}</th>
                    <th scope="col" className="end">{outcome ? "Liked" : "Recall"}</th>
                    <th scope="col" className="end">AUC</th>
                  </tr>
                </thead>
                <tbody>
                  {m.folds!.map((f) => (
                    <tr key={f.test_from ?? ""}>
                      <td className="num">{f.test_from?.slice(0, 10)}</td>
                      <td className="end num">{f.test_positives} / {f.test_samples}</td>
                      <td className="end num">{outcome ? fmtR(f.avg_r_all) : pctOf(f.precision)}</td>
                      <td className="end num">{outcome ? fmtR(f.avg_r_picked) : pctOf(f.recall)}</td>
                      <td className="end num">{f.auc?.toFixed(2) ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          );
        })}
      </div>
    </details>
  );
}

/** What each model leans on: held-out AUC lost when a family or feature is shuffled. */
function Explained({ manifest }: { manifest: ModelManifest }) {
  const parts: [string, Importance][] = [
    ...Object.entries(manifest.tags).flatMap(([id, m]) => (m.importance ? [[m.title ?? id, m.importance] as [string, Importance]] : [])),
    ...(manifest.outcome?.importance ? [["Outcome", manifest.outcome.importance] as [string, Importance]] : []),
  ];
  if (!parts.length) return null; // files from before 0.1.7 don't carry it
  return (
    <details className="lab-explain">
      <summary>What the model looks at</summary>
      <p className="field-hint">
        How much held-out AUC drops when a group of features, or one feature, is shuffled across the held-out candles.
        Bigger means the model leans on it more; near zero means it barely uses it.
      </p>
      <div className="lab-explain-grid">
        {parts.map(([title, imp]) => {
          const max = Math.max(0.001, ...imp.families.map((f) => f.drop));
          return (
            <section key={title} aria-label={title}>
              <h4>{title}</h4>
              <ul className="lab-bars">
                {imp.families.slice(0, 5).map((f) => (
                  <li key={f.id}>
                    <span>{f.title}</span>
                    <span className="lab-bar" style={{ inlineSize: `${Math.max(0, f.drop / max) * 100}%` }} />
                    <span className="num">{fmtDrop(f.drop)}</span>
                  </li>
                ))}
              </ul>
              <p className="field-hint">
                Top features: {imp.features.slice(0, 3).map((f) => `${f.label} (${fmtDrop(f.drop)})`).join(", ")}
              </p>
            </section>
          );
        })}
      </div>
    </details>
  );
}

const fmtDrop = (v: number) => `${v >= 0 ? "−" : "+"}${Math.abs(v).toFixed(3)}`;

// ---- Models --------------------------------------------------------------------------------

function LabModels({ status, onStatus }: { status: LabStatus; onStatus: (s: LabStatus) => void }) {
  const models = status.models ?? [];
  const [staged, setStaged] = useState<StagedModel | null>(null);
  const [trustFile, setTrustFile] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [card, setCard] = useState<Scorecard | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const activeModel = models.find((m) => m.id === status.active);
  const compared = picked.map((id) => models.find((m) => m.id === id)).filter((m): m is ModelManifest => !!m);
  const togglePick = (id: string) =>
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : cur.length < 3 ? [...cur, id] : cur));

  useEffect(() => {
    if (!status.active || !status.available) return setCard(null);
    let alive = true;
    fetchScorecard().then((r) => alive && setCard(r.scorecard)).catch(() => alive && setCard(null));
    return () => {
      alive = false;
    };
  }, [status.active, status.available]);

  const run = async (fn: () => Promise<LabStatus | void>) => {
    setBusy(true);
    setError(null);
    try {
      const s = await fn();
      if (s) onStatus(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const choose = (file: File | undefined) => {
    if (!file) return;
    run(async () => {
      setStaged(await stageImport(file));
      setTrustFile(false);
    });
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="lab-models">
      <section className="settings-form" aria-labelledby="models-h">
        <div className="settings-intro">
          <h2 id="models-h">Models</h2>
          <p>
            The active model draws its blocks on the screener (turn on <em>ML</em> above the chart), where each one can be
            marked valid or invalid. Model files are zips you can pass on; importing shows what's inside before anything loads.
          </p>
        </div>
        <div className="form-actions">
          <input ref={fileRef} type="file" accept=".zip,application/zip" hidden onChange={(e) => choose(e.target.files?.[0])} />
          <button type="button" className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}>Import a model file</button>
          <span className="form-status">{error && <span className="text-error">{error}</span>}</span>
        </div>

        {staged && (
          <div className="import-card" role="dialog" aria-labelledby="import-h">
            <h3 id="import-h">{staged.manifest.name}</h3>
            <dl className="import-facts">
              <dt>Author</dt><dd>{staged.manifest.author || "not given"}</dd>
              <dt>Made</dt><dd>{fmtDate(staged.manifest.created_at)} on {staged.manifest.symbol}</dd>
              <dt>Timeframes</dt><dd>{staged.manifest.timeframes.join(", ")}</dd>
              <dt>Bar clock</dt><dd>{staged.manifest.feed?.clock ?? "not given"}</dd>
              <dt>Tags</dt><dd>{Object.values(staged.manifest.tags).filter((t) => t.trained).map((t) => t.title).join(", ") || "none"}</dd>
              <dt>Signature</dt><dd><SignatureText sig={staged.signature} /></dd>
              <dt>Fingerprint</dt><dd className="num">{staged.manifest.sha256.slice(0, 16)}…</dd>
              {staged.manifest.note && <><dt>Note</dt><dd>{staged.manifest.note}</dd></>}
            </dl>
            <p className="import-warn">
              A model file holds a Python pickle. Wednesday loads only numpy and scikit-learn objects from it and checks the
              fingerprint, but load files only from people you trust.
              {staged.exists && " A model with this id is already here and will be replaced."}
            </p>
            {staged.warnings.length > 0 && (
              <ul className="import-warn" aria-label="Feed differences">
                {staged.warnings.map((w) => <li key={w}>{w}</li>)}
              </ul>
            )}
            {(() => {
              const state = staged.signature.state;
              if (state === "trusted") return null;
              if (state === "invalid") return <p className="text-error field-note">Not loading it: {staged.signature.reason}</p>;
              if (staged.only_signed) {
                return <p className="text-error field-note">Only models signed by a trusted key load (Settings &gt; Lab).</p>;
              }
              return (
                <label className="field field-check">
                  <input type="checkbox" checked={trustFile} onChange={(e) => setTrustFile(e.target.checked)} />
                  <span>
                    {state === "unknown" ? `Signed by a key you haven't trusted (${staged.signature.key_id}).` : "Nobody signed this file."}{" "}
                    I trust this file
                  </span>
                </label>
              );
            })()}
            <div className="form-actions">
              <button type="button" className="button primary"
                disabled={busy || staged.signature.state === "invalid"
                  || (staged.signature.state !== "trusted" && (staged.only_signed || !trustFile))}
                onClick={() => run(async () => {
                  const s = await confirmImport(staged.token, trustFile);
                  setStaged(null);
                  return s;
                })}>
                Load model
              </button>
              <button type="button" className="button quiet" disabled={busy} onClick={() => setStaged(null)}>Cancel</button>
            </div>
          </div>
        )}

        {models.length === 0 ? (
          <p className="empty">No models yet. Train one in Train, or import a file.</p>
        ) : (
          <div className="table-scroll">
          <table className="data lab-models-table">
            <thead>
              <tr>
                <th scope="col"><span className="sr-only">Compare</span></th>
                <th scope="col">Model</th>
                <th scope="col">Made</th>
                <th scope="col">Timeframes</th>
                <th scope="col">On the chart</th>
                <th scope="col" className="end">File</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => {
                const active = status.active === m.id;
                return [
                  <tr key={m.id} className={active ? "is-selected" : undefined}>
                    <td>
                      <input type="checkbox" aria-label={`Compare ${m.name}`} checked={picked.includes(m.id)}
                        disabled={!picked.includes(m.id) && picked.length >= 3} onChange={() => togglePick(m.id)} />
                    </td>
                    <td>
                      <button type="button" className="link" aria-expanded={open === m.id} onClick={() => setOpen(open === m.id ? null : m.id)}>
                        {m.name}
                      </button>
                      {m.author && <span className="muted"> · {m.author}</span>}
                      {m.signature && <SignatureBadge sig={m.signature} />}
                    </td>
                    <td className="num">{fmtDate(m.created_at)}</td>
                    <td>{m.timeframes.join(", ")}</td>
                    <td>
                      <button type="button" className={`button ${active ? "quiet" : "secondary"}`} disabled={busy || !status.available}
                        onClick={() => run(() => setActiveModel(active ? null : m.id))}>
                        {active ? "Turn off" : "Use"}
                      </button>
                    </td>
                    <td className="end lab-file">
                      <a className="link" href={modelFileUrl(m.id)} download>Download</a>
                      <button type="button" className="link link-danger" disabled={busy}
                        onClick={() => window.confirm(`Delete ${m.name}? The file is removed.`) && run(() => deleteModel(m.id))}>
                        Delete
                      </button>
                    </td>
                  </tr>,
                  open === m.id && (
                    <tr key={`${m.id}-scores`} className="lab-scores-row">
                      <td colSpan={6}>
                        {m.note && <p className="field-note">{m.note}</p>}
                        <p className="field-note num">
                          Trained on {m.data.m1_bars.toLocaleString()} M1 bars ({m.data.first.slice(0, 10)} → {m.data.last.slice(0, 10)}),{" "}
                          {m.data.labels} labels. Judged after {m.params.confirm} candles; target {m.params.rr}R within {m.params.horizon_hours}h.
                        </p>
                        <ModelScores manifest={m} />
                      </td>
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
          </div>
        )}
        {models.length > 1 && compared.length < 2 && (
          <p className="field-hint">Tick two or three models to compare them side by side.</p>
        )}
        {activeModel && card && <ActiveScorecard name={activeModel.name} card={card} />}
      </section>
      {compared.length >= 2 && <Compare models={compared} />}
    </div>
  );
}

const SIGNATURE_TEXT: Record<Signature["state"], string> = {
  trusted: "Signed", unknown: "Signed by an unknown key", unsigned: "Not signed", invalid: "Signature broken",
};

function SignatureText({ sig }: { sig: Signature }) {
  if (sig.state === "trusted") return <strong className="sig-trusted">Signed by {sig.signer}</strong>;
  if (sig.state === "unknown") return <span className="sig-unknown">Signed by an unknown key <span className="num">{sig.key_id}</span></span>;
  if (sig.state === "invalid") return <span className="text-error">{sig.reason}</span>;
  return <span className="sig-unknown">Not signed</span>;
}

function SignatureBadge({ sig }: { sig: Signature }) {
  const title = sig.state === "trusted" ? `Signed by ${sig.signer}` : sig.state === "unknown" ? `Key ${sig.key_id}` : sig.reason ?? undefined;
  return <span className={`badge sig-badge sig-${sig.state}`} title={title}>{SIGNATURE_TEXT[sig.state]}</span>;
}

function ActiveScorecard({ name, card }: { name: string; card: Scorecard }) {
  return (
    <div className="lab-scorecard">
      <h3>{name} on live data</h3>
      <ScorecardLine card={card} />
      {card.inputs === null ? (
        <p className="field-hint">This file has no training input ranges, so only reviews and trades are checked.</p>
      ) : card.inputs.length > 0 && (
        <p className="field-hint">
          Recent candles against training (median):{" "}
          {card.inputs.map((i) => `${i.timeframe} ${i.title} ${i.ratio.toFixed(2)}×`).join(", ")}.
        </p>
      )}
    </div>
  );
}

// ---- Compare ---------------------------------------------------------------------------------

type Row = { label: string; values: (number | null | undefined)[]; fmt: (v: number) => string; best?: "high" | "low" };

const num2 = (v: number) => v.toFixed(2);
const bestOf = (row: Row) => {
  if (!row.best) return null;
  const vals = row.values.filter((v): v is number => typeof v === "number");
  if (vals.length < 2 || new Set(vals).size === 1) return null;
  return row.best === "high" ? Math.max(...vals) : Math.min(...vals);
};

function scoreRows(title: string, metrics: (TagMetrics | null | undefined)[], outcome: boolean): Row[] {
  const get = (k: keyof TagMetrics) => metrics.map((m) => (m ? (m[k] as number | null | undefined) : null));
  const rows: Row[] = [
    { label: `${title}: labels used`, values: get("samples"), fmt: String },
    { label: `${title}: AUC`, values: get("auc"), fmt: num2, best: "high" },
  ];
  if (outcome) {
    rows.push(
      { label: `${title}: avg R, all trades`, values: get("avg_r_all"), fmt: (v) => fmtR(v) },
      { label: `${title}: avg R, trades it liked`, values: get("avg_r_picked"), fmt: (v) => fmtR(v), best: "high" },
    );
  } else {
    rows.push(
      { label: `${title}: precision`, values: get("precision"), fmt: (v) => pctOf(v), best: "high" },
      { label: `${title}: recall`, values: get("recall"), fmt: (v) => pctOf(v), best: "high" },
      { label: `${title}: cut`, values: get("threshold"), fmt: (v) => pctOf(v) },
    );
  }
  return rows;
}

function CompareTable({ models, rows, caption }: { models: ModelManifest[]; rows: Row[]; caption: string }) {
  return (
    <div className="table-scroll">
      <table className="data compact lab-compare">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col"><span className="sr-only">Score</span></th>
            {models.map((m) => <th key={m.id} scope="col" className="end">{m.name}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const best = bestOf(row);
            return (
              <tr key={row.label}>
                <th scope="row">{row.label}</th>
                {row.values.map((v, i) => (
                  <td key={models[i].id} className={`end num${best !== null && v === best ? " is-best" : ""}`}>
                    {typeof v === "number" ? row.fmt(v) : "—"}
                    {best !== null && v === best && <span className="sr-only"> (best)</span>}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Two or three models side by side, from their manifests; optionally re-scored on one common window. */
function Compare({ models }: { models: ModelManifest[] }) {
  const [window_, setWindow] = useState<WindowScores | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const key = models.map((m) => m.id).join(",");
  useEffect(() => {
    setWindow(null);
    setError(null);
  }, [key]);

  const tags = [...new Set(models.flatMap((m) => Object.keys(m.tags).filter((t) => m.tags[t].trained)))];
  const titleOf = (t: string) => models.find((m) => m.tags[t])?.tags[t].title ?? t;
  const heldOut: Row[] = [
    ...tags.flatMap((t) => scoreRows(titleOf(t), models.map((m) => (m.tags[t]?.trained ? m.tags[t] : null)), false)),
    ...(models.some((m) => m.outcome?.trained)
      ? scoreRows("Outcome", models.map((m) => (m.outcome?.trained ? m.outcome : null)), true) : []),
  ];
  const settings: Row[] = [
    { label: "Candles before", values: models.map((m) => m.params.lookback), fmt: String },
    { label: "Confirming candles", values: models.map((m) => m.params.confirm), fmt: String },
    { label: "Target (R)", values: models.map((m) => m.params.rr), fmt: String },
    { label: "Horizon (hours)", values: models.map((m) => m.params.horizon_hours), fmt: String },
  ];
  const windows = new Set(models.flatMap((m) => Object.values(m.tags).map((t) => t.test_from?.slice(0, 16)).filter(Boolean)));

  const score = async () => {
    setBusy(true);
    setError(null);
    try {
      setWindow(await scoreOnSameWindow(models.map((m) => m.id)));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const sameWindow: Row[] | null = window_ && [
    ...tags.flatMap((t) => scoreRows(titleOf(t), models.map((m) => window_.models[m.id]?.tags[t]), false)),
    ...(models.some((m) => window_.models[m.id]?.outcome)
      ? scoreRows("Outcome", models.map((m) => window_.models[m.id]?.outcome), true) : []),
  ];

  return (
    <section className="settings-form" aria-labelledby="compare-h">
      <div className="settings-intro">
        <h2 id="compare-h">Compare</h2>
        <p>
          Held-out scores from each model's training, best value per row in bold. Data:{" "}
          {models.map((m) => `${m.name} ${m.data.first.slice(0, 10)} → ${m.data.last.slice(0, 10)} (${m.timeframes.join(", ")})`).join("; ")}.
        </p>
      </div>
      {windows.size > 1 && (
        <p className="banner" role="note">
          Their held-out windows differ, so these scores aren't strictly comparable: a calm or wild week can flatter one.
          Score them on the same window to be sure.
        </p>
      )}
      <CompareTable models={models} rows={[...heldOut, ...settings]} caption="Held-out scores and settings" />
      <div className="form-actions">
        <button type="button" className="button secondary" disabled={busy} onClick={score}>
          {busy ? "Scoring…" : "Score on the same window"}
        </button>
        <span className="form-status">{error && <span className="text-error">{error}</span>}</span>
      </div>
      <p className="field-hint">
        Scores every model on the labelled candles known after the newest one's training data ends, on the timeframes
        they share, each at its own cut. None of them trained on those candles.
      </p>
      {window_ && sameWindow && (
        <>
          <h3 className="lab-compare-h">
            Same window: {window_.from.slice(0, 16).replace("T", " ")} → {window_.to.slice(0, 16).replace("T", " ")}{" "}
            ({window_.timeframes.join(", ")})
          </h3>
          <CompareTable models={models} rows={sameWindow} caption="Scores on the same window" />
        </>
      )}
    </section>
  );
}
