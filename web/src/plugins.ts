import { useEffect, useState } from "react";

import { fetchPlugins, type PluginsResponse } from "./api";

/** Installed plugins, fetched once. `has(feature)` tells whether a plugin provides a feature id. */
export function usePlugins() {
  const [data, setData] = useState<PluginsResponse | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let alive = true;
    fetchPlugins()
      .then((res) => alive && setData(res))
      .catch(() => alive && setData({ api: 0, plugins: [], features: [], catalog: [] }));
    return () => {
      alive = false;
    };
  }, [reload]);
  const has = (feature: string) => !!data?.features.includes(feature);
  const refresh = () => setReload((n) => n + 1);
  return { data, has, refresh };
}
