import type { ReactNode } from "react";

export interface PanelTab {
  id: string;
  label: string;
  badge?: number; // shown after the label when above zero, e.g. new events
  content: ReactNode;
}

/** A tab strip for panels. Clicking the selected tab calls onSelect with it again (the dock uses that to close). */
export function PanelTabs({ tabs, selected, onSelect, label, idPrefix, compact }: {
  tabs: Pick<PanelTab, "id" | "label" | "badge">[];
  selected: string | null;
  onSelect: (id: string) => void;
  label: string;
  idPrefix: string;
  compact?: boolean;
}) {
  return (
    <div className={`tabs${compact ? " tabs-compact" : ""}`} role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} id={`${idPrefix}-tab-${t.id}`} type="button" role="tab" className="tab"
          aria-selected={t.id === selected} aria-controls={t.id === selected ? `${idPrefix}-panel` : undefined}
          onClick={() => onSelect(t.id)}>
          {t.label}
          {!!t.badge && <span className="tab-badge num">{t.badge}</span>}
        </button>
      ))}
    </div>
  );
}
