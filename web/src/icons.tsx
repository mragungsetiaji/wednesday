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
