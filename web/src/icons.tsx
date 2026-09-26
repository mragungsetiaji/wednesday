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
