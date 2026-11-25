import { useEffect, useState } from "react";

import { addMarket, deleteMarket, fetchMarkets, saveMarkets, type Market } from "../api";
import { marketsChanged } from "../marketsList";
import { paneSymbol } from "../marketSymbol";

const same = (a: Market[], b: Market[]) => JSON.stringify(a) === JSON.stringify(b);

/**
 * The chart's symbol menu: which TradingView markets it lists, in what order and under what
 * label, and which ones keep storing candles while another is scanned. The same symbol from two
 * venues (FX:XAUUSD from FXCM, OANDA:XAUUSD) are separate markets with their own candles.
 */
export function MarketsSettings() {
  const [saved, setSaved] = useState<Market[] | null>(null);
  const [rows, setRows] = useState<Market[]>([]);
  const [symbol, setSymbol] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "ok"; text: string } | null>(null);

  const show = (markets: Market[]) => {
    setSaved(markets);
    setRows(markets);
    marketsChanged(markets);
  };

  useEffect(() => {
    fetchMarkets()
      .then((res) => show(res.markets))
      .catch((e) => setMessage({ kind: "error", text: `Can't load the markets: ${e instanceof Error ? e.message : e}` }));
  }, []);

  /** Do `work`; true when it went through. */
  const run = async (work: () => Promise<{ markets: Market[] }>, done: string): Promise<boolean> => {
    setBusy(true);
    setMessage(null);
    try {
      show((await work()).markets);
      setMessage({ kind: "ok", text: done });
      return true;
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const edit = (i: number, patch: Partial<Market>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const move = (i: number, by: number) => {
    const next = [...rows];
    [next[i], next[i + by]] = [next[i + by], next[i]];
    setRows(next);
  };

  const typed = paneSymbol(symbol, "");
  const dirty = saved !== null && !same(rows, saved);

  return (
    <section className="settings-form" aria-labelledby="markets-h">
      <div className="settings-intro">
        <h2 id="markets-h">Markets</h2>
        <p>
          The markets in the chart's symbol menu, from TradingView. A market's candles load when you pick it.
          Keep live stores its candles even while another market is scanned, so its history has no gaps.
        </p>
      </div>

      {message && <p className={message.kind === "error" ? "text-error" : "muted"} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}

      {saved && (
        <form onSubmit={(e) => { e.preventDefault(); run(() => saveMarkets(rows), "Saved the markets."); }}>
          <table className="data compact markets-table">
            <thead>
              <tr>
                <th scope="col">Label</th>
                <th scope="col">Symbol</th>
                <th scope="col">Name</th>
                <th scope="col">Venue</th>
                <th scope="col">Keep live</th>
                <th scope="col"><span className="sr-only">Order and delete</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m, i) => (
                <tr key={m.symbol}>
                  <td><input type="text" value={m.label} maxLength={16} required aria-label={`${m.symbol} label`}
                    onChange={(e) => edit(i, { label: e.target.value })} /></td>
                  <td className="num">{m.symbol}</td>
                  <td><input type="text" value={m.name ?? ""} maxLength={64} aria-label={`${m.symbol} name`}
                    onChange={(e) => edit(i, { name: e.target.value || null })} /></td>
                  <td><input type="text" value={m.venue ?? ""} maxLength={32} aria-label={`${m.symbol} venue`}
                    onChange={(e) => edit(i, { venue: e.target.value || null })} /></td>
                  <td><input type="checkbox" checked={m.keep_live} aria-label={`Keep ${m.symbol} live`}
                    onChange={(e) => edit(i, { keep_live: e.target.checked })} /></td>
                  <td className="end">
                    <button type="button" className="button quiet" disabled={busy || i === 0} onClick={() => move(i, -1)}
                      aria-label={`Move ${m.symbol} up`} title="Move up">↑</button>
                    <button type="button" className="button quiet" disabled={busy || i === rows.length - 1} onClick={() => move(i, 1)}
                      aria-label={`Move ${m.symbol} down`} title="Move down">↓</button>
                    <button type="button" className="button quiet" disabled={busy || dirty || rows.length === 1}
                      title={dirty ? "Save or undo your changes first" : "Take it off the menu. Its stored candles stay."}
                      onClick={() => run(() => deleteMarket(m.symbol), `Took ${m.symbol} off the menu.`)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="form-actions">
            <button type="submit" className="button primary" disabled={busy || !dirty}>{busy ? "Working…" : "Save"}</button>
            <button type="button" className="button secondary" disabled={busy || !dirty} onClick={() => setRows(saved)}>Undo</button>
          </div>
        </form>
      )}

      {saved && (
        <form className="field" onSubmit={(e) => {
          e.preventDefault();
          if (typeof typed === "string") run(() => addMarket({ symbol: typed }), `Added ${typed}.`).then((ok) => ok && setSymbol(""));
        }}>
          <label className="field-label" htmlFor="market-symbol">Add a market</label>
          <div className="input-with-button">
            <input id="market-symbol" type="text" placeholder="OANDA:XAUUSD" value={symbol} spellCheck={false}
              aria-invalid={symbol !== "" && typed === undefined} onChange={(e) => setSymbol(e.target.value)} />
            <button type="submit" className="button secondary" disabled={busy || dirty || typeof typed !== "string"}>Add</button>
          </div>
          <span className={symbol !== "" && typed === undefined ? "field-note text-error" : "field-note"}>
            {symbol !== "" && typed === undefined
              ? "Use TradingView's name with its exchange, e.g. FX:XAGUSD."
              : "TradingView's name with its exchange, as in its symbol search: FX: is FXCM, OANDA: is OANDA."}
          </span>
        </form>
      )}
    </section>
  );
}
