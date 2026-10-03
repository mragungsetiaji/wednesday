import { useEffect, useId, useMemo, useRef, useState } from "react";

import { LinkIcon } from "../icons";
import { crosshairLink, LINK_GROUPS, linkBus, LinkBus, readOptions, saveOptions, type GroupOptions, type LinkGroup } from "../windowLink";
import { usePopover } from "./usePopover";

/**
 * One chart's link group: its crosshair link for PriceChart's `sync`, its scroll-and-zoom link for
 * `rangeLink`, and the timeframe the group asks for. `onTf` runs when another chart in the group
 * changes timeframe (only when the group shares it); call `shareTf` after this chart changes its own.
 */
export function useChartLink(group: LinkGroup | null, onTf: (tf: string) => void) {
  const id = useMemo(() => LinkBus.id(), []);
  const onTfRef = useRef(onTf);
  onTfRef.current = onTf;
  useEffect(() => {
    if (!group) return;
    return linkBus().subscribe(`${id}:t`, group, (m) => m.kind === "tf" && onTfRef.current(m.tf));
  }, [group, id]);
  const sync = useMemo(() => (group ? { bus: crosshairLink(group, id), id: 0 } : undefined), [group, id]);
  const rangeLink = useMemo(() => (group ? { group, id } : null), [group, id]);
  const shareTf = (tf: string) => group && linkBus().publish(group, `${id}:t`, { kind: "tf", tf });
  return { sync, rangeLink, shareTf };
}

const TITLE: Record<LinkGroup, string> = { A: "Link group A", B: "Link group B", C: "Link group C" };

/** The link button: none, A, B or C, and what the group shares besides the crosshair. */
export function LinkMenu({ value, onChange }: { value: LinkGroup | null; onChange: (g: LinkGroup | null) => void }) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const popId = useId();
  const [options, setOptions] = useState<GroupOptions | null>(null);
  useEffect(() => {
    setOptions(value ? readOptions(value) : null);
  }, [value, open]);
  const set = (o: GroupOptions) => {
    if (!value) return;
    saveOptions(value, o);
    setOptions(o);
  };
  return (
    <div className="link-menu" ref={ref}>
      <button type="button" className={`icon-button link-btn${value ? ` link-${value}` : ""}`} aria-expanded={open} aria-controls={popId}
        title={value ? `${TITLE[value]}: linked with the charts in the same group, in any window` : "Link this chart with others"}
        aria-label={value ? `${TITLE[value]}, change` : "Link this chart"} onClick={() => setOpen(!open)}>
        {value ?? <LinkIcon size={14} />}
      </button>
      {open && (
        <div className="chart-pop link-pop" id={popId} role="group" aria-label="Link group">
          <label className="scale-opt">
            <input type="radio" name={`${popId}-g`} checked={value === null} onChange={() => onChange(null)} /> Not linked
          </label>
          {LINK_GROUPS.map((g) => (
            <label key={g} className="scale-opt">
              <input type="radio" name={`${popId}-g`} checked={value === g} onChange={() => onChange(g)} />
              <span className={`link-dot link-${g}`} aria-hidden="true">{g}</span> Group {g}
            </label>
          ))}
          {value && options && (
            <>
              <hr />
              <p className="field-hint">Group {value} shares the crosshair, and:</p>
              <label className="scale-opt">
                <input type="checkbox" checked={options.range} onChange={(e) => set({ ...options, range: e.target.checked })} /> Scroll and zoom
              </label>
              <label className="scale-opt">
                <input type="checkbox" checked={options.tf} onChange={(e) => set({ ...options, tf: e.target.checked })} /> Timeframe
              </label>
            </>
          )}
        </div>
      )}
    </div>
  );
}
