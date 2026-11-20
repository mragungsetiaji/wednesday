// A stand-in for tradingview/feed.mjs for the other-symbol panes (markets.py, server-rs
// engine/markets.rs): the same JSON-line protocol, fixed answers per symbol, no network.
// Python's tests/test_markets_parity.py and Rust's tests/engine_markets.rs both run it.
import { createInterface } from 'node:readline';

const NOW = 1763632800; // 2025-11-20 10:00 UTC: candles end here unless `to` says otherwise
const counts = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reply = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
const fail = (code, message) => Object.assign(new Error(message), { code });

function bars(timeframe, count, to, base) {
  const step = Number(timeframe) * 60;
  const last = Math.floor((to ?? NOW) / step) * step;
  const rows = [];
  for (let i = 0; i < count; i++) {
    const o = base + i * 0.25;
    rows.push([last - step * (count - 1 - i), o, o + 1.5, o - 1.25, o + 0.5, 10]);
  }
  return rows;
}

const INFO = {
  'FX:XAGUSD': { description: 'Silver', exchange: 'FXCM', pricescale: 1000 },
  'FX:P0': { pricescale: 1 }, 'FX:PBIG': { pricescale: 1e10 }, 'FX:PZERO': { pricescale: 0 },
  'FX:PODD': { pricescale: 3162 }, 'FX:PFLOAT': { pricescale: 100000.0 }, 'FX:NOINFO': null,
};

const ops = {
  async info({ symbol }) {
    if (symbol === 'FX:INFOFAIL') throw fail('INFO_DOWN', 'symbol info unavailable');
    if (symbol === 'FX:NOPE') throw fail('SYMBOL_ERROR', 'invalid symbol');
    return symbol in INFO ? INFO[symbol] : { description: 'Some market' };
  },
  async candles({ symbol, timeframe, count, to }) {
    counts[symbol] = (counts[symbol] ?? 0) + 1;
    switch (symbol) {
      case 'FX:NOPE': throw fail('SYMBOL_ERROR', 'invalid symbol');
      case 'FX:FAIL': throw fail('TIMEOUT', 'boom');
      case 'FX:NOMSG': throw {};
      case 'FX:EMPTY': return [];
      case 'FX:NULL': return null;
      case 'FX:SLOW': await sleep(3000); return [];
      case 'FX:DIE': console.error('boom: fake failure'); await sleep(50); process.exit(3);
      case 'FX:COUNT': return [[NOW, counts[symbol], 2, 0.5, 1.5, 0]];
      case 'FX:MESSY': // unsorted, a repeated time, whole numbers among the decimals: kept as given
        return [[NOW, 5, 6.5, 4, 5.5, 1], [NOW - 3600, 4.25, 5, 4, 4.75, 1], [NOW - 3600, 4, 5, 3, 4, 2],
                [NOW - 7200.5, 3.5, 4, 3, 3.75, 0]];
      default: return bars(timeframe, count, to, symbol === 'FX:XAGUSD' ? 30.125 : 100.5);
    }
  },
  async quote({ symbol }) {
    counts['q' + symbol] = (counts['q' + symbol] ?? 0) + 1;
    switch (symbol) {
      case 'FX:NOPE': throw fail('SYMBOL_ERROR', 'invalid symbol');
      case 'FX:FAIL': throw fail('TIMEOUT', 'boom');
      case 'FX:EMPTY': return null;
      case 'FX:NULLP': return { price: null, time: NOW };
      case 'FX:STRQ': return { price: '12.5', time: String(NOW + 5) };
      case 'FX:FLOATT': return { price: 7, time: NOW + 5.9 };
      case 'FX:NOTIME': return { price: 31.5, time: null };
      case 'FX:COUNT': return { price: counts['q' + symbol], time: NOW };
      default: return { price: 31.987, time: NOW + 5 };
    }
  },
};

let queue = Promise.resolve();
createInterface({ input: process.stdin }).on('line', (line) => {
  queue = queue.then(async () => {
    const req = JSON.parse(line);
    try { reply({ id: req.id, result: await ops[req.op](req) }); }
    catch (e) { reply({ id: req.id, error: e.code ? { code: e.code, message: e.message } : {} }); }
  });
}).on('close', async () => { await queue; process.exit(0); });
