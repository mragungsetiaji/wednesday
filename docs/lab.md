# Lab: labels and models

The Lab is a workbench next to the screener (**Lab** in the top bar). You tag
candles by hand, train models on those tags, and keep model files you can pass
on. The screener stays the production view: it only shows the active model's
blocks, as the **ML** layer, where each one can be marked valid or invalid.

Needs the `ml` extra (`make install` includes it, or `uv sync --extra ml`) and a
database (the default SQLite one is fine). Labelling works without the extra;
training, model files and the Parquet download need it.

<img src="images/lab-label.png" alt="The Lab: labelling 5M candles, with the detector's suggestions and the model's blocks on the chart" width="100%">

## Label

1. Pick a timeframe and page through the history (**Older**, **Newer**, a date,
   **Latest**). The history is the stored M1 bars (the latest 400,000 by
   default, about a year; see [Data](#data)) plus the live buffer.
2. Click a candle to select it. Shift-click, or Shift + arrow keys, selects a
   range (an order block can be more than one candle).
3. Tag it with a button or the number keys: `1` OB bull, `2` OB bear, `3` BSL,
   `4` SSL, `5` IDM bull, `6` IDM bear. Press `N` first to mark it *not* that tag
   instead: the near misses are the most useful examples.
4. **Reviewed ranges**: when you've tagged everything in a stretch, mark it
   reviewed (`R` for the selection, or **Whole page reviewed**). Inside a reviewed
   range, a candle you didn't tag counts as *not* that tag. Outside, candles are
   left out of training rather than guessed as negatives.

The detector's levels show as dotted suggestions; `A` accepts the ones on the
selection. That's quicker than tagging from scratch, but a model trained only on
accepted suggestions just learns the detector back. Fix what it gets wrong.

A label is saved with its timeframe, tag, the open times of its first and last
candle and, for accepted suggestions, the zone's top and bottom. Labels live in
the `lab_labels` table, reviewed ranges in `lab_reviewed`.

**Labels file.** Under **Train**, **Export labels** downloads every label and
reviewed range of the symbol as one JSON file: a backup, or labels to move to
another machine. **Import labels** shows what the file would change (new,
updated, already here) before anything is written, then merges it: a label with
the same id is updated, the same label under another id is skipped. A file from
another symbol needs a tick to go into this one, and never touches the labels of
the symbol it came from.

## Train

<img src="images/lab-train.png" alt="Train: timeframes, tags with their label counts, model settings, and the scores of the last run" width="100%">

Two kinds of model go into one file:

- **Tag models**, one per tag: "is this candle one?" Trained on your labels only,
  pooled over the timeframes you pick.
- **Outcome model**: "traded with the limit plan (buy at the top of the body, stop
  at the bottom capped at the max stop), does this order block reach the target
  before the stop?" The market answers this one, so it also learns from every
  order block the detector found in the history (you can turn that off).

**Features.** A candle is judged once the confirming candles after it have closed
(3 by default). The model sees, in ATRs: the 10 candles before, the candle, the
confirming candles, where its high and low sit in that window, the hour and
weekday, and the last closed candles of the next two timeframes up. Nothing later.
Each sample records `available_at`, the moment a model could have known.

**Scores.** Everything is split by time, never at random. The oldest part trains,
the next part tunes the probability cut (best F1), the latest part (20% by
default) is held out for the scores. Then the saved model is refit on everything.
Precision is how many of the model's calls you had tagged; recall is how many of
your tags it found; both at the cut. AUC doesn't depend on the cut.

For the outcome model the scores add what trading would have made: the average R
of every held-out trade against the average R of the trades the model liked.

**What the model looks at.** A model's scores open a list per tag of the
feature groups it leans on (the candle, the candles before, the confirming
candles, the window high / low, volatility, time of day, higher timeframes, and
the zone for the outcome model), with its top features in plain words. It is
permutation importance: how much held-out AUC drops when a group, or one feature,
is shuffled across the held-out candles, measured on the model fit before them.
A model that leans mostly on time of day rather than the candles is worth a
second look. Files from before 0.1.7 don't carry it.

**Stop** ends a run at its next stage (between tags); nothing is saved.
**Recent runs** keeps the last 20 runs with how they ended, errors included, so
a failed run's reason survives a restart (a run cut off by one shows as
*interrupted*).

The classifier is scikit-learn's `HistGradientBoostingClassifier`: quick, fine
with a few hundred labels, and it handles missing context (4H has nothing above
it) on its own.

## Models

<img src="images/lab-models.png" alt="Models: the list with the active one, and a model's scores opened" width="100%">

Models are saved as zip files in `data/models` (`XAU_MODELS_DIR`):

- `manifest.json`: name, author, note, when and on what it was trained (symbol,
  data source and the clock of its bar times), the timeframes, the scores and
  feature importance per tag, the settings, and the SHA-256 of the pickle.
- `model.pkl`: the fitted estimators.

**Use** sets a model active; the screener's ML layer and the Lab's **Model**
toggle show its blocks. **Download** gives you the file to pass on.

**Import** reads only the manifest first and shows it: name, author, date,
timeframes, tags, bar clock, fingerprint. Nothing is loaded until you click **Load model**.
It also warns when the model was trained on another symbol, or on bar times of
another clock (its time-of-day features would be shifted), or when the file
doesn't say which clock.
Loading a pickle can run code, so Wednesday refuses a file whose pickle doesn't
match the manifest's fingerprint, and loads it with an unpickler that accepts
only numpy and scikit-learn classes. Still, load model files only from people
you trust.

## Review on the screener

<img src="images/ml-review.png" alt="The screener with the ML layer on and the model's blocks listed with valid and invalid buttons" width="100%">

Turn on **ML** above the chart. The active model's blocks on the chart's
timeframe show as dashed outlines with their probability, and a **Model on 5M**
panel lists them, newest first, with the chance of winning for order blocks.
Hover a row to find it on the chart; ✓ and ✕ mark it valid or invalid. The
default shows each tag from its trained cut; the menu shows everything above a
fixed probability instead.

A review does two things:

- It becomes a label (tag yes for valid, *not* for invalid) that the next
  training run learns from.
- It is stored with the market's result: for an order block, the limit plan is
  simulated from when the model called it (`win`, `loss`, `open`, `untouched`,
  and the result in R).

The same buttons are in the Lab (`V` / `X` on the selection).

## Data

**Lab > Data** shows how much M1 history is stored for the running source and
symbol, and adds more. Early on there is little: Yahoo Finance only gives days
of M1, and MT5 only what was fetched since Wednesday started.

- **Stored history**: one row per month, one square per day. Paler squares
  have fewer bars, outlined ones none, grey ones are weekends.
- **Backfill from MT5** (MT5 as the source): pulls older M1 bars from the
  terminal back to a date, five days per call between scans, with progress and
  a Stop button. It stops with a note when the terminal has nothing older:
  raise *Tools > Options > Charts > Max bars in chart* and scroll the M1 chart
  back so the terminal downloads more.
- **Import a file**: M1 bars from an MT5 export (`<DATE> <TIME> <OPEN> ...`,
  UTF-16 is fine), Dukascopy (`Gmt time,...`), HistData ASCII (`20240102
  180000;...` or `2024.01.02,18:00,...`) or any CSV with a time column and
  open, high, low, close. The preview shows the format, range and first rows;
  you confirm the file's clock (Dukascopy UTC, HistData UTC-5, an MT5 export
  the broker's server time) and the times are moved to the feed's clock.
  Importing is an upsert on time, so a file imported twice adds nothing.
  Use prices of the same market as the feed: spot gold for MT5, COMEX
  futures for Yahoo Finance.
- **History the Lab uses**: how many of the latest stored bars are loaded for
  labelling, training and the dataset, up to 2,000,000 (about five years,
  roughly 100 MB of memory).

Imported and backfilled bars also let the chart scroll further back (see
[Scrolling back](data-sources.md#scrolling-back)). Demo data is never stored,
so there is nothing to add to.

## Datasets

**Train > Dataset** downloads the labels as one row per minute (Parquet or CSV,
the last N days):

| Columns | What |
| --- | --- |
| `time`, `open` … `volume` | the M1 bar |
| `m5_time`, `m5_open`, `m5_high`, `m5_low`, `m5_close` | the 5M candle forming at that minute: its open, the high and low so far, the current close. Same for `m15`, `m30`, `h1`, `h4` |
| `m5_ob_bull`, `m5_bsl`, … | the label of that 5M candle: 1 tagged, 0 not, empty if never reviewed. One per timeframe and tag |

The OHLC columns only use data up to that minute; the label columns are the
answer, known afterwards.

**feedback.csv** holds every review: the candle's features (the state), the tag
the model called (the action), your verdict as +1 / −1 and the traded result in
R (two rewards). It's the start of a reinforcement learning set, for training a
policy outside Wednesday.

## Model files for other people

The Lab is the same for everyone: anyone can label and train their own model. A
model file carries everything needed to run it, so you can train one and hand it
to others, who import it and turn it on without labelling anything. The manifest
names the author and the fingerprint lets them check they got the file you made.
