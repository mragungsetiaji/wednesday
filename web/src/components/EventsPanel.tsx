import type { DetectorInfo, Scan } from "../api";
import { eventVerb, fmtFeedTime, fmtLevelPrice } from "../format";
import { LevelTag } from "./LevelTag";

interface Props {
  scan: Scan;
  detectors: DetectorInfo[];
  recentBars: number;
  onSelect: (tf: string) => void;
}

/** Levels swept or taken within the last few candles of each timeframe, newest first. */
export function EventsPanel({ scan, detectors, recentBars, onSelect }: Props) {
  const events = scan.timeframes
    .flatMap((t) => detectors.flatMap((d) => (t.detectors[d.name]?.recent ?? []).map((lv) => ({ tf: t.timeframe, lv }))))
    .sort((a, b) => (b.lv.ended_time ?? "").localeCompare(a.lv.ended_time ?? ""));

  return (
    <section className="detail" aria-labelledby="events-h">
      <div className="detail-head">
        <h2 id="events-h">Events</h2>
        <span className="detail-note">Last {recentBars} candles per timeframe, marked on the chart</span>
      </div>
      {events.length === 0 ? (
        <p className="empty">
          Nothing swept or taken recently. Sweeps of liquidity and inducement, and order blocks taken by a wick, show up here and as
          markers on the chart.
        </p>
      ) : (
        <table className="data">
          <tbody>
            {events.map(({ tf, lv }) => (
              <tr key={`${tf}-${lv.detector}-${lv.time}-${lv.kind}`} onClick={() => onSelect(tf)}>
                <th scope="row">
                  <button type="button" className="link" onClick={() => onSelect(tf)}>{tf}</button>
                </th>
                <td><LevelTag level={lv} /></td>
                <td className="num">{fmtLevelPrice(lv)}</td>
                <td>{eventVerb(lv)}</td>
                <td className="end num muted">{lv.ended_time ? fmtFeedTime(lv.ended_time).slice(11) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
