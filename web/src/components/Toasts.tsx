import { useEffect, useState } from "react";

const EVENT = "wed:toast";
const SHOW_MS = 4000;

/** A short message at the bottom of the screen (a snapshot copied, saved or attached). */
export function toast(text: string, link?: { href: string; label: string }) {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { text, link } }));
}

interface Item {
  id: number;
  text: string;
  link?: { href: string; label: string };
}

let next = 0;

/** Where toasts show; one on the page and one in full screen (the browser shows only that element). */
export function Toasts() {
  const [items, setItems] = useState<Item[]>([]);
  useEffect(() => {
    const on = (e: Event) => {
      const item = { id: ++next, ...(e as CustomEvent<Omit<Item, "id">>).detail };
      setItems((xs) => [...xs.slice(-2), item]);
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== item.id)), SHOW_MS);
    };
    window.addEventListener(EVENT, on);
    return () => window.removeEventListener(EVENT, on);
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className="toast">
          {t.text}
          {t.link && <a href={t.link.href}>{t.link.label}</a>}
        </div>
      ))}
    </div>
  );
}
