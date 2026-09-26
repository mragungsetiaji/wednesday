# Detectors and the limit strategy

Every minute Wednesday resamples closed M1 bars into **4H, 1H, 30M, 15M and 5M**
candles, runs the detectors on the last `--lookback` candles of each timeframe,
and reports the **nearest active level above and below** price per detector,
from the highest timeframe down. Only closed candles are used; the candle still
forming on each timeframe is dropped. Candle times follow the feed's clock, so
with MT5 the 4H candles line up with the broker chart.

All detectors share one market-structure pass and return the same kind of
object (a `Level`: a zone, or a line when top equals bottom). Choose them with
`--detectors ob,liquidity,idm` (default: all).

## Shared structure

- **Swings**: a swing high/low is the extreme of `--swing-length` bars on each
  side. It only exists once those right-side bars have closed (no look-ahead).
- **Break of structure (BOS)**: a candle *closes* above the latest unbroken
  swing high (bullish) or below the latest unbroken swing low (bearish).
- **Change of character (CHoCH)**: the first break against the previous run of
  breaks.

## Order blocks (`ob`)

Built for limit entries.

- The OB candle is the **opposite-colour candle**: red for a buy OB, green for a
  sell OB (the candle before the impulse, not the impulsive candle itself).
- **Extreme OB** (high priority): the last red candle at or before the lowest low
  of the leg that broke structure up (sell: the last green candle at or before
  the highest high).
- **Mid OB** (low priority, still reported): red candles later in the move that
  are followed by an impulsive green candle closing above their body (sell: mirror).
- **Taken**: an OB is removed once a later wick trades through its whole body
  (`--mitigation close`: a close beyond it instead). A wick that only reaches the
  entry keeps it valid and counts as *entry tested*.
- `--zone body` (default) makes the level cover the body, `--zone wick` the full
  candle range. Entry and stop always come from the body.

### Limit plan

| | Entry | Stop |
| --- | --- | --- |
| Buy (red OB candle) | top of the body | bottom of the body |
| Sell (green OB candle) | bottom of the body | top of the body |

The stop is capped at `--max-sl` (default **3.00**) from the entry; a capped
stop is marked `(capped)`.

### Limit setups

Across all timeframes, untaken bearish OBs whose entry is above price are
sell-limit candidates and bullish OBs below price are buy-limit candidates,
ranked **extreme first, then nearest entry**. The console prints the top 3 per
side, the API returns them under `setups`, and the dashboard shows them as
`S1`–`S3` / `B1`–`B3`.

## Liquidity (`liquidity`)

Every swing high not yet traded through is **BSL** (buy stops above), every such
swing low is **SSL**. Active swings within `--eq-tolerance` × ATR(14) of each
other merge into an **EQH / EQL** pool. A level ends when a wick trades through
it: a *grab* if that candle closes back inside, a *break* if it closes beyond.

## Inducement (`idm`)

After a BOS, the first internal swing (built with `--idm-length` bars each side)
against the move: the first pullback low after a bullish BOS, the first pullback
high after a bearish BOS. Only the latest IDM per direction is kept; it ends
when swept (grab / break as above).

## Events

Levels that ended within the last `--recent-bars` candles of a timeframe are
reported as events (console *NOTES* column, dashboard *Events* panel), e.g.
`SSL 2468.81 grabbed @ 14:05`.

## Console output

```
[2026-09-26 02:47] XAUUSD price 2473.79
TF   DET    #  NEAREST ABOVE                      NEAREST BELOW                      NOTES
4H   OB     5  BEAR OB 2548.98-2561.48 (+75.19) t0 -                                  -
     LIQ    7  BSL 2588.64 (+114.84)              SSL 2429.90 (-43.89)               -
     IDM    0  -                                  -                                  -
1H   OB     5  BEAR OB 2504.24-2514.89 (+30.44) t0 BULL OB 2434.85-2440.92 (-32.87) t0 -
     LIQ    9  BSL 2535.36 (+61.56)               SSL 2434.85 (-38.95)               -
     IDM    1  IDM ▼ 2485.15 (+11.35)             -                                  -
...
nearest OB: above 5M BEAR OB 2475.37-2478.77 (+1.58) t2 | below 5M BULL OB 2468.32-2471.79 (-2.01) t0
nearest LIQ: above 5M BSL 2478.77 (+4.97) | below 5M SSL 2468.81 (-4.99)
nearest IDM: above 1H IDM ▼ 2485.15 (+11.35) | below 15M IDM ▲ 2439.56 (-34.23)
```

`(+4.50)` is the distance from price to the level's nearest edge, `tN` is how
many candles have tapped an order block since it formed, and *NOTES* lists
levels price is inside plus recent sweeps.

## Adding a detector

1. Create `src/wednesday/detectors/<name>.py` with a `Detector` subclass:
   set `name` and `title`, implement `detect(ctx) -> list[Level]`. Use
   `ctx.structure(length)` for swings / BOS and `ctx.atr` instead of recomputing,
   and set `ended_time` on levels that were mitigated or swept.
2. Register the class in `detectors/__init__.py` (`REGISTRY`).
3. If it needs a new setting, add it to `DetectorParams` and a CLI flag.

The scanner, console table, API and dashboard pick it up automatically. On the
chart a new detector is drawn with the neutral dotted style until it gets its
own role in `web/src/format.ts` (`roleOf`).
