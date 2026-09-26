import { useEffect, useState } from "react";

import { fetchPlugins, type PluginsResponse } from "./api";

/** Installed plugins, fetched once. `has(feature)` tells whether a plugin provides a feature id. */
export function usePlugins() {
  const [data, setData] = useState<PluginsResponse | null>(null);
  useEffect(() => {
    let alive = true;
    fetchPlugins()
      .then((res) => alive && setData(res))
      .catch(() => alive && setData({ api: 0, plugins: [], features: [] }));
    return () => {
      alive = false;
    };
  }, []);
  const has = (feature: string) => !!data?.features.includes(feature);
  return { data, has };
}
