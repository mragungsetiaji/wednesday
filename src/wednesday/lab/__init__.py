"""Lab: label candles by hand, train models on them, review what the models find.

The flow:

1. **Label** (:mod:`.labels`): pick candles on a timeframe and tag them
   (order block, liquidity, inducement). A *reviewed range* says every candle
   in it was looked at, so untagged candles there count as "not this tag";
   candles outside reviewed ranges are left out of training instead of being
   guessed as negatives.
2. **Dataset** (:mod:`.dataset`): the labels as a per-minute table (M1 OHLC,
   the forming 5M..4H candle of every minute, one column per timeframe and tag)
   for your own research, and as per-candle samples for training.
3. **Train** (:mod:`.train`): one classifier per tag ("is this candle one?")
   plus an outcome model for order blocks ("does the limit reach 2R before the
   stop?"). Split by time, never at random.
4. **Models** (:mod:`.model`): saved as a zip with a readable manifest and a
   pickle. Export, import (after the manifest is shown), set one active.
5. **Review**: the active model's blocks on the screener; each can be marked
   valid or invalid. Reviews become labels for the next training run and, with
   the market outcome, a feedback set for reinforcement learning later.

Needs the ``ml`` extra (``uv sync --extra ml``): scikit-learn and pyarrow.
"""
