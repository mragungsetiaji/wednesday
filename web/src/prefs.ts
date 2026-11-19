import { useCallback, useEffect, useState } from "react";

function loadPref<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

function savePref(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the preference just isn't remembered */
  }
}

/** useState that remembers its value in this browser. */
export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => loadPref(key, fallback));
  const set = useCallback((v: T) => {
    setValue(v);
    savePref(key, v);
  }, [key]);
  return [value, set];
}

const sharedListeners = new Map<string, Set<(v: unknown) => void>>();

/**
 * A remembered preference every component using the same key sees change at once (the chart type
 * on every chart, say), and other windows of this browser too, through the storage event.
 */
export function useSharedPref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => loadPref(key, fallback));
  useEffect(() => {
    const own = (v: unknown) => setValue(v as T);
    const set = sharedListeners.get(key) ?? new Set();
    set.add(own);
    sharedListeners.set(key, set);
    const other = (e: StorageEvent) => {
      if (e.key === key) setValue(loadPref(key, fallback));
    };
    window.addEventListener("storage", other);
    return () => {
      set.delete(own);
      window.removeEventListener("storage", other);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const set = useCallback((v: T) => {
    savePref(key, v);
    for (const fn of sharedListeners.get(key) ?? []) fn(v);
  }, [key]);
  return [value, set];
}
