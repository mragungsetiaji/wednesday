import { useEffect, useState } from "react";

import { fetchCalendar, type CalendarResponse } from "./api";

/** The news calendar, shared by the risk-time card and the charts; refreshed every minute. */
export function useCalendar(): CalendarResponse | null {
  const [data, setData] = useState<CalendarResponse | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => fetchCalendar().then((r) => alive && setData(r)).catch(() => {});
    load();
    const id = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);
  return data;
}
