/**
 * A colour picker for drawings and indicators: a row of greys from white to black, ten hues in six
 * shades each, and any other colour through the system picker.
 */

const GREYS = ["#ffffff", "#d1d4dc", "#b2b5be", "#9598a1", "#787b86", "#5d606b", "#434651", "#2a2e39", "#131722", "#000000"];
const SHADES = [
  ["#fccbcd", "#ffe0b2", "#fff9c4", "#c8e6c9", "#ace5dc", "#b2ebf2", "#bbd9fb", "#d1c4e9", "#e1bee7", "#f8bbd0"],
  ["#faa1a4", "#ffcc80", "#fff59d", "#a5d6a7", "#70ccbd", "#80deea", "#90bff9", "#b39ddb", "#ce93d8", "#f48fb1"],
  ["#f7525f", "#ffb74d", "#fff176", "#81c784", "#42bda8", "#4dd0e1", "#5b9cf6", "#9575cd", "#ba68c8", "#f06292"],
  ["#f23645", "#ff9800", "#ffeb3b", "#4caf50", "#089981", "#00bcd4", "#2962ff", "#673ab7", "#9c27b0", "#e91e63"],
  ["#b22833", "#f57c00", "#fbc02d", "#388e3c", "#056656", "#0097a7", "#1e53e5", "#512da8", "#7b1fa2", "#c2185b"],
  ["#801922", "#e65100", "#f57f17", "#1b5e20", "#00332a", "#006064", "#0c3299", "#311b92", "#4a148c", "#880e4f"],
];

/** Every swatch the picker offers, greys first (tests check black and white are there). */
export const SWATCHES = [GREYS, ...SHADES];

const same = (a: string | null, b: string | null) => (a ?? "").toLowerCase() === (b ?? "").toLowerCase();

/**
 * `value` null is the theme's own colour, offered first when `theme` is given (a drawing's
 * default ink, which differs between light and dark).
 */
export function ColorPicker({ value, onChange, theme, label = "Colour" }: {
  value: string | null;
  onChange: (color: string | null) => void;
  theme?: string; // the label of the theme swatch; omitted, there is none
  label?: string;
}) {
  const known = SWATCHES.some((row) => row.some((c) => same(c, value)));
  return (
    <div className="color-picker" role="radiogroup" aria-label={label}>
      {theme && (
        <button type="button" role="radio" className="color-swatch is-theme color-theme" aria-checked={value === null}
          title={theme} onClick={() => onChange(null)}>
          {theme}
        </button>
      )}
      {SWATCHES.map((row, i) => (
        <div key={i} className="color-row">
          {row.map((c) => (
            <button key={c} type="button" role="radio" className="color-swatch" aria-checked={same(c, value)} aria-label={c}
              title={c} style={{ background: c }} onClick={() => onChange(c)} />
          ))}
        </div>
      ))}
      <label className="color-custom" title="Any other colour">
        <input type="color" value={value && /^#[0-9a-f]{6}$/i.test(value) ? value : "#2962ff"}
          onChange={(e) => onChange(e.target.value)} />
        <span>{value && !known ? value : "Custom…"}</span>
      </label>
    </div>
  );
}
