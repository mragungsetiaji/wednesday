import type { DetectorInfo, Level, Scan } from "../api";
import { eventVerb, fmtFeedTime, fmtLevelPrice } from "../format";
import { LevelTag } from "./LevelTag";

interface Props {
  scan: Scan;
  detectors: DetectorInfo[];
  recentBars: number;
  onSelect: (tf: string) => void;
}

/** Levels swept / mitigated within the last few candles of each timeframe, newest first. */
export function EventsPanel({ scan, detectors, recentBars, onSelect }: Props) {
  const events = scan.timeframes
    .flatMap((t) => detectors.flatMap((d) => (t.detectors[d.name]?.recent ?? []).map((lv) => ({ tf: t.timeframe, lv }))))
    .sort((a, b) => (b.lv.ended_time ?? "").localeCompare(a.lv.ended_time ?? ""));

  return (
    <section className="card events">
      <h2>Recent events <span className="muted small">last {recentBars} candles per TF</span></h2>
      {events.length === 0 ? (
        <p className="empty">No sweeps or mitigations</p>
      ) : (
        <ul className="event-list">
          {events.map(({ tf, lv }: { tf: string; lv: Level }) => (
            <li key={`${tf}-${lv.detector}-${lv.time}-${lv.kind}`}>
              <button type="button" onClick={() => onSelect(tf)}>
                <span className="tf-badge">{tf}</span>
                <LevelTag level={lv} />
                <span className="num">{fmtLevelPrice(lv)}</span>
                <strong className="event-verb">{eventVerb(lv)}</strong>
                <span className="muted small num">{lv.ended_time ? fmtFeedTime(lv.ended_time).slice(11) : ""}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
