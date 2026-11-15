// Wednesday's bridge to TradingView market data, through @mathieuc/tradingview
// (an unofficial, community library: https://github.com/Mathieu2301/TradingView-API).
//
// The Python feed (feeds.TradingViewFeed) starts this script and talks to it in
// JSON lines: one request per line on stdin, one reply per line on stdout.
//
//   {"id": 1, "op": "info", "symbol": "FX:XAUUSD"}
//   {"id": 2, "op": "candles", "symbol": "FX:XAUUSD", "timeframe": "1", "count": 500, "to": 1794606240}
//   {"id": 3, "op": "quote", "symbol": "FX:XAUUSD"}
//
// Replies are {"id", "result"} or {"id", "error": {"code", "message"}}. One
// websocket stays open between requests; it is reopened if TradingView drops it.
// Logs go to stderr. The script exits when stdin closes.
import { createInterface } from 'node:readline';
import { TradingViewClient, getCandles, getSymbolInfo, watchQuotes } from '@mathieuc/tradingview';

let client = null;

function connection() {
  if (!client || client.isClosed) client = new TradingViewClient();
  return client;
}

const quotes = new Map(); // symbol -> { latest, dead }

async function quoteWatcher(symbol) {
  const known = quotes.get(symbol);
  if (known && !known.dead) return known;
  const entry = { latest: null, dead: false };
  const watcher = await watchQuotes({ symbols: [symbol], fields: ['lp', 'lp_time'], client: connection(), timeoutMs: 15000 }, {
    onData(_symbol, quote) { entry.latest = quote; },
    onError(error) { console.error(`quote ${symbol}: ${error.code ?? ''} ${error.message}`); },
  });
  entry.latest = watcher.latest?.[symbol] ?? entry.latest;
  watcher.closed.then(() => { entry.dead = true; });
  quotes.set(symbol, entry);
  return entry;
}

const ops = {
  async info({ symbol }) {
    const i = await getSymbolInfo(symbol, { client: connection(), timeoutMs: 20000 });
    return { full_name: i.full_name, description: i.description, exchange: i.exchange,
             timezone: i.timezone, session: i.session, pricescale: i.pricescale };
  },
  async candles({ symbol, timeframe = '1', count = 100, to, timeoutMs = 60000 }) {
    const query = { symbol, timeframe: String(timeframe), count, client: connection(), timeoutMs };
    if (to != null) query.to = to; // newest bar time, unix seconds: scrolling back
    const bars = await getCandles(query);
    return bars.map((b) => [b.time, b.open, b.high, b.low, b.close, b.volume ?? 0]);
  },
  async quote({ symbol }) {
    const q = (await quoteWatcher(symbol)).latest;
    return q && q.lp != null ? { price: q.lp, time: q.lp_time ?? null } : null;
  },
};

function reply(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

async function handle(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    reply({ id: null, error: { code: 'BAD_REQUEST', message: 'Not JSON' } });
    return;
  }
  const op = ops[req.op];
  if (!op) {
    reply({ id: req.id, error: { code: 'BAD_REQUEST', message: `Unknown op ${req.op}` } });
    return;
  }
  try {
    reply({ id: req.id, result: await op(req) });
  } catch (error) {
    reply({ id: req.id, error: { code: error?.code ?? 'ERROR', message: String(error?.message ?? error) } });
  }
}

process.on('unhandledRejection', (error) => console.error('unhandled:', error?.message ?? error));

// One request at a time, in order: the Python side waits for each reply anyway.
let queue = Promise.resolve();
const rl = createInterface({ input: process.stdin });
rl.on('line', (line) => { if (line.trim()) queue = queue.then(() => handle(line)); });
rl.on('close', async () => {
  await queue;
  await client?.close();
  process.exit(0);
});
