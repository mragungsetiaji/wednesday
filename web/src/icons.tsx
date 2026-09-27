/** Authored icon set: 16px grid, 1.5px stroke, currentColor. */
interface IconProps {
  size?: number;
  title?: string;
}

function Svg({ size = 14, title, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}
      className="icon">
      {title && <title>{title}</title>}
      {children}
    </svg>
  );
}

export const ArrowUp = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 13V3.5M4 7.5l4-4 4 4" />
  </Svg>
);

export const ArrowDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 3v9.5M4 8.5l4 4 4-4" />
  </Svg>
);

/** Direction arrow for a bullish/bearish reading. */
export const Direction = ({ dir, ...p }: IconProps & { dir: "bullish" | "bearish" }) =>
  dir === "bullish" ? <ArrowUp {...p} /> : <ArrowDown {...p} />;

/** Candles: the chart view. */
export const ChartIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.5 2.5v11M11.5 2.5v11" />
    <rect x="3" y="5" width="3" height="5" rx="0.5" />
    <rect x="10" y="4" width="3" height="4" rx="0.5" />
  </Svg>
);

/** Sliders: settings. */
export const SlidersIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 4.5h11M2.5 11.5h11" />
    <circle cx="6" cy="4.5" r="1.75" fill="var(--surface, #1a1a19)" />
    <circle cx="10.5" cy="11.5" r="1.75" fill="var(--surface, #1a1a19)" />
  </Svg>
);

/** Corners out: enter full screen. */
export const ExpandIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10" />
  </Svg>
);

/** Corners in: leave full screen. */
export const CollapseIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 2.5V6H2.5M13.5 6H10V2.5M10 13.5V10h3.5M2.5 10H6v3.5" />
  </Svg>
);

/** Chart layout: one, two side by side, or a 2×2 grid. */
export const LayoutIcon = ({ panes, ...p }: IconProps & { panes: 1 | 2 | 4 }) => (
  <Svg {...p}>
    <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
    {panes >= 2 && <path d="M8 2.5v11" />}
    {panes === 4 && <path d="M2 8h12" />}
  </Svg>
);

/** Flask: the Lab (labels, training, models). */
export const FlaskIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 2.5h4M6.5 2.5v4L3 12.5a.8.8 0 0 0 .7 1h8.6a.8.8 0 0 0 .7-1L9.5 6.5v-4" />
    <path d="M4.5 10h7" />
  </Svg>
);

/** Book: the journal. */
export const BookIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 3.5A1.5 1.5 0 0 1 4.5 2H13v10.5H4.5A1.5 1.5 0 0 0 3 14V3.5z" />
    <path d="M3 14a1.5 1.5 0 0 0 1.5 1H13v-2.5M6 5h4" />
  </Svg>
);

export const CheckIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 8.5l3.2 3L13 4.5" />
  </Svg>
);

export const CrossIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Svg>
);

/** Tag: the app version. */
export const TagIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 2.5h5l6 6-5 5-6-6z" />
    <circle cx="5.5" cy="5.5" r="0.75" />
  </Svg>
);

/** Chevron up: open a panel upward (rotated by CSS when open). */
export const ChevronUp = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 10l4-4 4 4" />
  </Svg>
);

export const LockIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3.5" y="7" width="9" height="6.5" rx="1" />
    <path d="M5.5 7V5a2.5 2.5 0 015 0v2" />
  </Svg>
);

export const TrashIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 4.5h11M6.5 4.5V3h3v1.5M4 4.5l.7 9h6.6l.7-9" />
  </Svg>
);

export const EyeIcon = ({ off, ...p }: IconProps & { off?: boolean }) => (
  <Svg {...p}>
    <path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" />
    <circle cx="8" cy="8" r="2" />
    {off && <path d="M2.5 13.5l11-11" />}
  </Svg>
);

/** Drawing tools. */
export const CursorIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 2.5l8.5 5.2-3.8.9 2.2 4.1-1.6.8-2.2-4.1L4.2 12z" />
  </Svg>
);

export const TrendlineIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4.3 11.7l7.4-7.4" />
    <circle cx="3.2" cy="12.8" r="1.4" />
    <circle cx="12.8" cy="3.2" r="1.4" />
  </Svg>
);

export const HLineIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M1.5 8h10.5" />
    <circle cx="13.3" cy="8" r="1.4" />
  </Svg>
);

export const RectIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2.5" y="4" width="11" height="8" rx="0.5" />
  </Svg>
);

export const PathIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.5 12.5l3.5-6 3.5 3.5 4-7" />
  </Svg>
);

export const TextIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 4V2.8h10V4M8 2.8v10.4M6 13.2h4" />
  </Svg>
);

/** Long / short position: a box split at the entry, the arrow toward the target. */
export const PositionIcon = ({ short, ...p }: IconProps & { short?: boolean }) => (
  <Svg {...p}>
    <rect x="2.5" y="2.5" width="11" height="11" rx="0.5" />
    <path d="M2.5 8h11" />
    <path d={short ? "M8 9.5v2.5M6.5 10.5L8 12l1.5-1.5" : "M8 6.5V4M6.5 5.5L8 4l1.5 1.5"} />
  </Svg>
);

export const UndoIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5.5 3L2.5 6l3 3" />
    <path d="M2.5 6h7a4 4 0 010 8H7" />
  </Svg>
);

export const RedoIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.5 3l3 3-3 3" />
    <path d="M13.5 6h-7a4 4 0 000 8H9" />
  </Svg>
);
