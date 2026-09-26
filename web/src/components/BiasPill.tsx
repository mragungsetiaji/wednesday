import type { TradeBias } from "../api";
import { BiasIcon } from "./BiasPanel";

/** The trade bias at a glance, for the top bars. */
export function BiasPill({ bias }: { bias: TradeBias }) {
  const label = bias.direction[0].toUpperCase() + bias.direction.slice(1);
  return (
    <span className={`bias-pill bias-${bias.direction}${bias.expired ? " is-expired" : ""}`}
      title={bias.expired ? "Bias expired: set it again" : bias.note || `${label} bias`}>
      <BiasIcon dir={bias.direction} size={12} />
      {bias.expired ? `${label} bias expired` : `${label} bias`}
    </span>
  );
}
