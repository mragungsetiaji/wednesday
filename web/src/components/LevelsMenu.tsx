import { useId } from "react";

import { ChevronUp } from "../icons";
import { LEVEL_GROUPS, type LevelGroup } from "../refLevels";
import { usePopover } from "./usePopover";

/** Checkboxes for the killzones and reference levels, in a menu under a toolbar button. */
export function LevelsMenu({ value, onChange }: { value: LevelGroup[]; onChange: (v: LevelGroup[]) => void }) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  return (
    <div className="levels-menu" ref={ref}>
      <button type="button" className={`levels-btn${value.length ? " is-on" : ""}`} aria-expanded={open} aria-controls={id}
        title="Killzones and reference levels: previous day, week and month high / low, Asia range, opens" onClick={() => setOpen(!open)}>
        Sessions{value.length > 0 && <span className="levels-count num">{value.length}</span>}
        <span className="levels-caret"><ChevronUp size={12} /></span>
      </button>
      {open && <LevelChecks id={id} className="chart-pop levels-pop" value={value} onChange={onChange} />}
    </div>
  );
}

/** The group checkboxes themselves (also in a pane's layer menu). */
export function LevelChecks({ id, className, value, onChange }: {
  id?: string;
  className?: string;
  value: LevelGroup[];
  onChange: (v: LevelGroup[]) => void;
}) {
  return (
    <div className={className} id={id} role="group" aria-label="Sessions and levels">
      {LEVEL_GROUPS.map((g) => (
        <label key={g.id} className="scale-opt" title={g.title}>
          <input type="checkbox" checked={value.includes(g.id)}
            onChange={(e) => onChange(e.target.checked ? LEVEL_GROUPS.map((x) => x.id).filter((x) => x === g.id || value.includes(x))
              : value.filter((x) => x !== g.id))} />
          {g.label}
        </label>
      ))}
      <p className="field-hint">New York time. Levels come from closed bars; a swept one fades.</p>
    </div>
  );
}
