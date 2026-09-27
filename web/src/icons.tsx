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

/**
 * Drawing tools, TradingView's: filled on a 28px grid with 1px lines, unlike the stroked set
 * above. Draw them about a third larger than their stroked neighbours so they read the same size.
 */
function ToolSvg({ size = 18, title, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" fill="currentColor" aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined} className="icon">
      {title && <title>{title}</title>}
      {children}
    </svg>
  );
}

export const CursorIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M11.682 16.09l3.504 6.068 1.732-1-3.497-6.057 3.595-2.1L8 7.74v10.512l3.682-2.163zm-.362 1.372L7 20V6l12 7-4.216 2.462 3.5 6.062-3.464 2-3.5-6.062z" />
  </ToolSvg>
);

export const TrendlineIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M7.354 21.354l14-14-.707-.707-14 14z" />
    <path d="M22.5 7c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5zM5.5 24c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5z" />
  </ToolSvg>
);

export const HLineIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M8.5 15h16.5v-1h-16.5z" />
    <path d="M6.5 16c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5z" />
  </ToolSvg>
);

export const RectIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M7.5 6h13v-1h-13zM7.5 23h13v-1h-13zM5 7.5v13h1v-13zM22 7.5v13h1v-13z" />
    <path d="M5.5 7.0c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5zM22.5 7.0c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5zM22.5 24.0c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5zM5.5 24.0c.828 0 1.5-.672 1.5-1.5s-.672-1.5-1.5-1.5-1.5.672-1.5 1.5.672 1.5 1.5 1.5zm0 1c-1.381 0-2.5-1.119-2.5-2.5s1.119-2.5 2.5-2.5 2.5 1.119 2.5 2.5-1.119 2.5-2.5 2.5z" />
  </ToolSvg>
);

export const PathIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M11 10.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zm4 7a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0zm11-8.8V13h1V7h-6v1h4.3l-7.42 7.41a2.49 2.49 0 0 0-2.76 0l-3.53-3.53a2.5 2.5 0 1 0-4.17 0L1 18.29l.7.71 6.42-6.41a2.49 2.49 0 0 0 2.76 0l3.53 3.53a2.5 2.5 0 1 0 4.17 0z" />
  </ToolSvg>
);

export const TextIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path d="M8 6.5c0-.28.22-.5.5-.5H14v16h-2v1h5v-1h-2V6h5.5c.28 0 .5.22.5.5V9h1V6.5c0-.83-.67-1.5-1.5-1.5h-12C7.67 5 7 5.67 7 6.5V9h1V6.5Z" />
  </ToolSvg>
);

/** Long / short position: target, entry and stop lines with a dotted path from the entry toward the target. */
export const LongPositionIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path fillRule="evenodd" clipRule="evenodd" d="M4.5 5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM2 6.5A2.5 2.5 0 0 1 6.95 6H24v1H6.95A2.5 2.5 0 0 1 2 6.5zM4.5 15a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM2 16.5a2.5 2.5 0 0 1 4.95-.5h13.1a2.5 2.5 0 1 1 0 1H6.95A2.5 2.5 0 0 1 2 16.5zM22.5 15a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zm-18 6a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zM2 22.5a2.5 2.5 0 0 1 4.95-.5H24v1H6.95A2.5 2.5 0 0 1 2 22.5z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M22.4 8.94l-1.39.63-.41-.91 1.39-.63.41.91zm-4 1.8l-1.39.63-.41-.91 1.39-.63.41.91zm-4 1.8l-1.4.63-.4-.91 1.39-.63.41.91zm-4 1.8l-1.4.63-.4-.91 1.39-.63.41.91z" />
  </ToolSvg>
);

export const ShortPositionIcon = (p: IconProps) => (
  <ToolSvg {...p}>
    <path fillRule="evenodd" clipRule="evenodd" d="M4.5 24a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM2 22.5a2.5 2.5 0 0 0 4.95.5H24v-1H6.95a2.5 2.5 0 0 0-4.95.5zM4.5 14a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM2 12.5a2.5 2.5 0 0 0 4.95.5h13.1a2.5 2.5 0 1 0 0-1H6.95a2.5 2.5 0 0 0-4.95.5zM22.5 14a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm-18-6a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM2 6.5a2.5 2.5 0 0 0 4.95.5H24V6H6.95A2.5 2.5 0 0 0 2 6.5z" />
    <path fillRule="evenodd" clipRule="evenodd" d="M22.4 20.06l-1.39-.63-.41.91 1.39.63.41-.91zm-4-1.8l-1.39-.63-.41.91 1.39.63.41-.91zm-4-1.8l-1.4-.63-.4.91 1.39.63.41-.91zm-4-1.8L9 14.03l-.4.91 1.39.63.41-.91z" />
  </ToolSvg>
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
