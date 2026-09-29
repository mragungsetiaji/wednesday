"""Journal: trading accounts imported from MetaTrader 5, with numbers that can't be faked.

1. **Import** (:mod:`.imports`): closed deals straight from the MT5 terminal
   Wednesday is connected to, or the terminal's history report (HTML). Deals are
   turned into trades (one per closing deal) and cash (deposits, withdrawals).
2. **Stats** (:mod:`.stats`): gain (time-weighted, so deposits don't count as
   profit), absolute gain, balance, equity, deposits, withdrawals and the growth
   curve. The drawdown is rebuilt from the price: every minute a trade was open,
   its floating loss at that minute's worst price. A statement only shows closed
   results, so a trade that sat deep in loss before coming back still counts.
   Deal prices are checked against the bars too, which catches a report that
   doesn't match the market.
3. **Sample** (:mod:`.sample`): a made-up portfolio with its own M1 bars, shown
   until the user deletes it.

One journal is free (the sample doesn't count); more need the ``journal.multi`` feature.
"""
