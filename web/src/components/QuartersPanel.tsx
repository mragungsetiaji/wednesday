import type { QuarterRow, QuartersResponse } from "../api";
import { fmtPrice } from "../format";

const GROUPS: { row: QuarterRow; title: string }[] = [
  { row: "week", title: "Weekday" },
  { row: "session", title: "Session" },
  { row: "q90", title: "90m quarter" },
];

const fmtChange = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))}`;

/** How often each weekday, session and 90-minute quarter closed green over the stored history. */
export function QuartersPanel({ quarters }: { quarters: QuartersResponse }) {
  const live = new Set(GROUPS.flatMap(({ row }) => quarters.rows[row].filter((b) => b.live).map((b) => `${row}|${b.label}`)));
  const days = quarters.stats.week.reduce((n, s) => n + s.count, 0);

  return (
    <section className="detail detail-wide" aria-labelledby="quarters-h">
      <div className="detail-head">
        <h2 id="quarters-h">Quarters</h2>
        <span className="detail-note">
          Green closes over the last {days} finished trading {days === 1 ? "day" : "days"}, New York time. The running block is marked.
        </span>
      </div>
      <div className="quarter-groups">
        {GROUPS.map(({ row, title }) => (
          <table key={row} className="data quarter-table">
            <thead>
              <tr>
                <th scope="col">{title}</th>
                <th scope="col">Green</th>
                <th scope="col" className="end">Avg move</th>
              </tr>
            </thead>
            <tbody>
              {quarters.stats[row].map((s) => {
                const pct = s.count ? s.green / s.count : 0;
                return (
                  <tr key={s.label} className={live.has(`${row}|${s.label}`) ? "is-current" : undefined}>
                    <th scope="row">{s.label}</th>
                    <td>
                      <span className="ratio num">
                        <span className="ratio-bar" aria-hidden="true">
                          {s.count > 0 && <span style={{ width: `${pct * 100}%` }} />}
                        </span>
                        {s.count ? `${s.green}/${s.count}` : "–"}
                      </span>
                    </td>
                    <td className={`end num ${s.avg_change === null ? "muted" : s.avg_change > 0 ? "up" : s.avg_change < 0 ? "down" : ""}`}>
                      {s.avg_change === null ? "–" : fmtChange(s.avg_change)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ))}
      </div>
    </section>
  );
}
