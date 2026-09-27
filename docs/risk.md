# Position size

**Settings > Risk** puts a lot size on every limit setup in the ladder and in
Telegram alerts, e.g. `0.33 lot · risk $99.00 · 2R $198.00`. It is arithmetic
from your inputs, not advice: check the lot and the money at risk in your
broker's platform before every order.

## How the size is worked out

```
risk budget = balance × percent        (or a fixed amount)
money per lot = stop distance × money per 1.00 move per lot
lots = budget ÷ money per lot, rounded DOWN to the lot step
```

- The stop distance is the setup's own `risk` (entry to stop, capped at
  `--max-sl`).
- Lots always round **down**, so the money at risk never goes over the budget.
  When even the minimum lot risks more, the row says so instead of a size:
  *Min lot risks $30.00, over the $20.00 budget*.
- **2R** is twice the money at risk, the usual first target.
- **RISK OFF** setups (against your [bias](bias.md)) use a share of the budget,
  0.5 by default. RISK ON and NO TRADE setups use all of it.

## Where the numbers come from

With **MT5** as the data source and *Read the balance and lot rules from MT5*
ticked, every scan reads from the terminal:

- the account balance and currency;
- money per 1.00 move per lot (`trade_tick_value ÷ trade_tick_size`, in the
  account currency, so non-USD accounts are right too);
- minimum lot, lot step and maximum lot.

Otherwise, and whenever MT5 isn't connected, the values in Settings are used:
balance, currency, contract size (XAUUSD is usually 100, i.e. $100 per 1.00
move per lot on a USD account), minimum lot and lot step. With a fixed amount
no balance is needed.
