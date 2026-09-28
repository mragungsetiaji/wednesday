import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  cancelTraining, confirmImport, confirmLabels, deleteModel, fetchLab, labelsExportUrl, modelFileUrl, setActiveModel,
  stageImport, stageLabels, startTraining,
  type Importance, type LabStatus, type MergeCounts, type ModelManifest, type StagedLabels, type StagedModel,
  type TrainingRun, type TrainParams,
} from "../api";
import { fmtUnix } from "../format";
import { usePref } from "../prefs";
import type { ChartPalette } from "../theme";
import { LabData } from "./LabData";
import { LabLabel } from "./LabLabel";

type Sub = "label" | "train" | "models" | "data";
const SUBS: { id: Sub; title: string }[] = [
  { id: "label", title: "Label" },
  { id: "train", title: "Train" },
  { id: "models", title: "Models" },
  { id: "data", title: "Data" },
];
const subFromHash = (): Sub => {
  const s = window.location.hash.split("/")[1];
  return s === "train" || s === "models" || s === "data" ? s : "label";
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
      {sub === "data" && <LabData onChanged={load} />}
    </main>
  );
}

// ---- Train -------------------------------------------------------------------------------

const DEFAULTS: TrainParams = {
  timeframes: ["5M", "15M"], tags: [], lookback: 10, confirm: 3, rr: 2, horizon_hours: 72,
  outcome_from_detector: true, test_fraction: 0.2, name: "", author: "", note: "",
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
            whether order blocks traded with the limit plan reach the target first. Everything is split by time: the
            probability cut is tuned on the part before the latest {Math.round(form.test_fraction * 100)}%, the scores
            come from that latest part, then the saved model is refit on everything.
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
            <span className="field-label">Held out for scores</span>
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
                  <td className="end num">{pctOf(m.precision)}</td>
                  <td className="end num">{pctOf(m.recall)}</td>
                  <td className="end num">{m.auc?.toFixed(2) ?? "—"}</td>
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
        Held-out part only. Precision: of the candles the model called, how many you had tagged. Recall: of your tags, how
        many it found. Both at the cut, the probability tuned before the held-out part.
      </p>
      {o && (
        <p className="field-note">
          {o.trained ? (
            <>
              <strong>Outcome</strong> ({o.samples} trades: {o.from_labels ?? 0} from labels, {o.from_detector ?? 0} from the detector):
              held-out AUC {o.auc?.toFixed(2) ?? "—"}, {pctOf(o.base_rate)} of held-out trades won; taking all of them averaged{" "}
              {fmtR(o.avg_r_all)}, taking the {o.picked ?? 0} the model liked averaged {fmtR(o.avg_r_picked)}.
            </>
          ) : (
            <><strong>Outcome</strong>: {o.skipped}</>
          )}
        </p>
      )}
      <Explained manifest={manifest} />
    </>
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
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

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
    run(async () => setStaged(await stageImport(file)));
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
            <div className="form-actions">
              <button type="button" className="button primary" disabled={busy}
                onClick={() => run(async () => {
                  const s = await confirmImport(staged.token);
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
                      <button type="button" className="link" aria-expanded={open === m.id} onClick={() => setOpen(open === m.id ? null : m.id)}>
                        {m.name}
                      </button>
                      {m.author && <span className="muted"> · {m.author}</span>}
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
                      <td colSpan={5}>
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
      </section>
    </div>
  );
}
