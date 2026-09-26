import { useCallback, useEffect, useState } from "react";

import { fetchPredictions, type MlBlock } from "./api";

export interface MlState {
  model: { id: string; name: string } | null;
  blocks: MlBlock[];
}

/** The active model's blocks on one timeframe, refreshed with every scan. */
export function useMl(tf: string, version: number, enabled: boolean, threshold: number, limit: number) {
  const [state, setState] = useState<MlState | null>(null);
  const [reload, setReload] = useState(0);
  const refresh = useCallback(() => setReload((n) => n + 1), []);

  useEffect(() => {
    if (!enabled || !version) {
      setState(null);
      return;
    }
    let alive = true;
    fetchPredictions(tf, limit, threshold)
      .then((res) => alive && setState(res))
      .catch(() => alive && setState({ model: null, blocks: [] }));
    return () => {
      alive = false;
    };
  }, [tf, version, enabled, threshold, limit, reload]);

  return [state, refresh] as const;
}
