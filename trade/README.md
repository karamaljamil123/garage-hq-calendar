# Trade

Two day-trading setups, written as exact rules, in two places that follow the
same rules in the same order:

| file | what it is |
| --- | --- |
| `tradingview/session-sweep-fvg.pine` | TradingView strategy for the **1-minute** chart: Asia / London session sweep, then a fair-value-gap reversal |
| `tradingview/ten-am-range-breakout.pine` | TradingView strategy for the **15-minute** chart: the 10 AM range breakout |
| `../.claude/skills/trader-playbook/` | A Claude skill (`/trader-playbook`) that checks the same two setups on free Yahoo bars and sits next to the Day Trader skill pack |
| `tests/test_playbook.py` | 29 offline tests of the rules (`python3 -m unittest discover -s trade/tests`) |

Nothing here places an order. It is a tool for testing rules, not financial
advice. Most people who day trade lose money.

## Put a script on TradingView

1. Open a chart of the market you trade. Set the timeframe the script was
   built for (1 minute for the sweep, 15 minutes for the 10 AM range).
2. Select the **Pine Editor** tab at the bottom of the chart.
3. Open the editor's dropdown menu (the arrow at its top-left, beside the
   script name) and choose **Create new > Strategy**.
4. Delete everything in the editor. Open the `.pine` file from this folder,
   copy all of it, and paste it in.
5. Save (Ctrl+S, or click the script name) and choose **Add to chart**.

You should see the levels on the chart, a small table in the top-right
corner, and the strategy report in a tab in the bottom panel. Both scripts
were checked with a Pine syntax parser and against TradingView's v6 manual,
but they have not been compiled by TradingView itself. If the editor shows an
error, copy the message and the line number to Claude and it gets fixed.

**Alerts.** Create an alert on the chart, pick the strategy as the condition,
and choose the option for `alert()` function calls. Each entry alert carries
the side, entry, stop and target. Alerts fire at the close of the candle.

## Script 1: Session Sweep + FVG (1-minute chart)

Use a market that trades overnight: index futures, FX, gold, crypto. A US
stock has no Asia session, and the table will say so.

- Orange lines are the Asia high and low (20:00-00:00 New York). Blue lines
  are the London high and low (02:00-05:00). A line ends when it is swept.
- After a sweep, the script waits for a close beyond the last swing and then a
  fair value gap in the new direction. It enters at that candle's close, only
  between 09:30 and 11:00 New York.
- Stop: one tick beyond the sweep's extreme. Target: 2R. Flat by 15:55.

Settings worth knowing: **Entry trigger** (structure shift + FVG, first FVG,
inverse FVG, or either), **Target** (fixed reward:risk or next liquidity
level), and the three session windows. Other common session choices are
18:00-00:00 for Asia and 03:00-08:00 for London.

## Script 2: 10 AM Range Breakout (15-minute chart)

- The shaded candles (9:30 and 9:45 New York) set the range.
- The first later candle that **closes** outside the range is the entry. A
  wick outside is ignored. One trade a day.
- Stop: the opposite end of the range. Take profit: 10 points by default.
  Set **Take profit (points)** to suit your market.

The table shows the reward against the risk and the win rate needed to break
even. Read it before trusting a high win rate: with a stop as wide as the
range and a target of a few points, most days win a little and a few days lose
a lot.

## Reading the Strategy Tester

- **History is short on small timeframes.** TradingView loads about 5,000
  bars on the free plan, 10,000 on Essential and Plus, 20,000 on Premium and
  40,000 on Ultimate. On a 1-minute chart of a market that trades nearly
  around the clock, 5,000 bars is under four trading days. Deep Backtesting
  (Premium and above) reaches further back.
- **Order size** is 1 contract / share / unit by default. Change it under the
  script's settings ("Order size"), or switch to risking a percentage of
  equity. One full-size index future is large next to a small account, so the
  report reads more sensibly on a micro contract (MNQ, MES).
- **Margin checks are switched off** in both scripts (`margin_long = 0`), so
  every signal is simulated whatever the account size. The tester therefore
  does not model your broker's margin.
- **Fills** happen at the close of the signal candle. When one candle touches
  both the stop and the target, TradingView guesses the order from the
  candle's shape.
- No commission or slippage is set. Add yours under Properties.
- Delayed data: futures quotes on TradingView can be delayed unless your
  account has the exchange's real-time data. Alerts are only as timely as the
  data.

## With the Day Trader skill pack

The pack's own pipeline is `scanner -> news -> entry -> exit -> timemachine ->
budget -> risk -> broker`. It trades **US stocks and ETFs, buys only**, and
its own entry rule is an opening-range breakout on 5-minute bars.

- `/trader-playbook` checks these two setups on any Yahoo symbol and can
  replay their history (about 30 days of 1-minute bars, 60 days of 15-minute
  bars).
- A **buy on a US stock or ETF** can go on into the pack: the Playbook prints
  the `/trader-exit` command with the entry, stop and target filled in. Then
  Time Machine, Budget, Risk, and only then Broker.
- A **sell**, or anything on **futures, FX or crypto**, cannot go into the
  pack. There the Playbook and the TradingView scripts are signals for your
  own platform.
- The pack's Risk Manager blocks trades whose reward:risk is under 1.0 by
  default. The 10 AM rule with a 10-point target usually is.

## What a first look at free data showed

Run on 23 November 2026 with the default rules, no costs, on Yahoo's bars:

| rule | market | period | signals | target | stop | flat | total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Session sweep + FVG | NQ futures, 1-min | 24 trading days | 7 | 2 | 3 | 2 | +1.90R |
| 10 AM range, 10 points | ES futures, 15-min | 60 days | 47 | 36 | 7 | 4 | +3.07R |
| 10 AM range, 10 points | NQ futures, 15-min | 60 days | 44 | 42 | 2 | 0 | +0.05R |

On NQ the 10-point target sat against an average stop of 257 points, so 42
wins out of 44 came out at roughly zero. These are far too few trades to prove
anything either way. They are not a prediction.
