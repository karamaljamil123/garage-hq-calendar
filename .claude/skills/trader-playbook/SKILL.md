---
name: trader-playbook
description: >-
  The Playbook. Runs the user's two personal day-trading setups on free Yahoo
  bars and says exactly what each rule says right now. Setup one, session
  sweep + FVG (1-minute bars): marks the Asia and London session highs and
  lows, waits for one to be swept, then looks for a fair-value-gap reversal
  the other way, with the stop at the sweep's extreme and a 2R or
  next-liquidity target. Setup two, 10 AM range breakout (15-minute bars): the
  high and low of the first two 15-minute candles (9:30-10:00 New York), a
  CLOSE outside the range, stop at the opposite end, a fixed-points take
  profit. Shows today's levels, sweeps, range and any trigger, replays every
  past signal the free bars allow, and hands a buy on a US stock or ETF to
  /trader-exit. The same rules run in two TradingView scripts in
  trade/tradingview/. It never places an order. Use when the user says "check
  my sweep setup", "did Asia or London get swept", "session highs and lows",
  "mark out Asia and London", "fair value gap", "FVG", "liquidity sweep",
  "10 AM range", "first two candles", "opening range on the 15 minute",
  "check my playbook", "how has my strategy done", or asks about the
  TradingView scripts.
argument-hint: "[check|history] [SYMBOL] [--setup sweep|orb]"
---

# trader-playbook

The Playbook holds two setups the user was taught and wants to trade. It turns
each one into exact rules, checks them on completed bars, and reports what the
rules say: the levels, what has been swept, whether a trigger has fired, and
where the stop and the target would be. It reports. It does not predict and it
never buys or sells anything. Day trading loses most people money, and a rule
that is easy to follow is not the same as a rule that pays.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/playbook.py" check NQ=F --setup sweep            # levels, sweeps, any trigger
python3 "${CLAUDE_SKILL_DIR}/playbook.py" check ES=F --setup orb              # today's range, any trigger
python3 "${CLAUDE_SKILL_DIR}/playbook.py" check QQQ --setup orb --points 1 --write
python3 "${CLAUDE_SKILL_DIR}/playbook.py" history NQ=F --setup sweep          # every past signal, in R
python3 "${CLAUDE_SKILL_DIR}/playbook.py" history ES=F --setup orb --points 10
python3 "${CLAUDE_SKILL_DIR}/playbook.py" check NQ=F --setup sweep --now "2026-11-20 10:05"   # replay a moment
```

Symbols are Yahoo's: futures `NQ=F` `ES=F` `MNQ=F` `GC=F`, FX `EURUSD=X`,
crypto `BTC-USD`, stocks and ETFs `QQQ` `SPY` `TSLA`. Other flags: `--rr 2`,
`--model fvg|ifvg|either`, `--target rr|liquidity`, `--long-only`,
`--short-only`, `--tick SIZE`, `--days N`, `--offline FILE` (a saved Yahoo
chart JSON), `--json`, and `--set KEY=VALUE` for any rule in the tables below
(repeatable, for example `--set asia=1800-0000 --set london=0300-0800`).
Exit code 0 when a trade is triggered or open (check) or the rule fired at
least once (history), 1 otherwise.

## What it reads and writes

| file / feed | what |
| --- | --- |
| Yahoo chart API | 1-minute bars (sweep) or 15-minute bars (orb), with pre-market and overnight bars where Yahoo has them. Free, unofficial, can lag, and Yahoo sometimes revises a bar after the fact |
| `trader/playbook.json` | with `--write`: the rules used, today's levels or range, and the signal if one fired |

Yahoo keeps about 30 days of 1-minute bars and 60 days of 15-minute bars, so
`history` covers at most that. It is a small sample. Say so every time.

## Setup A: session sweep + FVG (`--setup sweep`, 1-minute bars)

1. **Levels.** The Asia session (20:00-00:00 New York) and the London session
   (02:00-05:00 New York) each leave a high and a low. The trading day starts
   when the Asia session starts. A level is live once its session is over.
2. **Sweep.** A bar trades at least one tick beyond a live level. Each level
   is used once. A swept low arms a buy setup, a swept high arms a sell setup.
3. **Trigger** (default `fvg` with a structure shift): after the sweep, a bar
   closes beyond the last swing (above the last swing high for a buy), then a
   fair value gap completes in the new direction. A bullish gap is three bars
   where the third bar's low is above the first bar's high. `--model ifvg`
   uses a close through the last opposing gap instead; `--model either` takes
   whichever comes first; `--set require_mss=false` takes the first gap.
4. **Timing.** The entry is the close of the trigger bar. It must come inside
   the entry window (09:30-11:00 New York), within 20 bars of the sweep's
   extreme and 60 bars of the sweep itself, or the setup is void.
5. **Stop and target.** Stop one tick beyond the sweep's extreme. Target 2R,
   or with `--target liquidity` the nearest unswept session high or low that
   pays at least 1R. Flat by 15:55 New York. Two trades a day at most.

| rule | default | rule | default |
| --- | --- | --- | --- |
| `asia` | 2000-0000 | `london` | 0200-0500 |
| `entry` | 0930-1100 | `flat` | 15:55 |
| `model` | fvg | `require_mss` | true |
| `pivot_len` | 2 | `require_reclaim` | false |
| `max_bars` | 20 | `max_setup_bars` | 60 |
| `min_sweep_ticks` | 1 | `min_gap_ticks` | 1 |
| `stop_buffer_ticks` | 1 | `min_risk_ticks` | 2 |
| `target` | rr | `rr` | 2.0 |
| `min_rr` | 1.0 | `max_trades` | 2 |

It needs a market that trades overnight. US stocks and ETFs have no Asia
session on Yahoo and only an hour of the London window (pre-market starts at
04:00), so use futures, FX or crypto symbols for this setup. The tool says so
when it finds no session bars.

## Setup B: 10 AM range breakout (`--setup orb`, 15-minute bars)

1. **Range.** The high and low of the bars that open from 09:30 to 10:00 New
   York: the first two 15-minute candles. If the 09:30 bar is missing the day
   is skipped.
2. **Entry.** The first later bar that CLOSES above the range is a buy; below
   it, a sell. A wick outside is nothing. One trade a day. The breakout bar
   must open between 10:00 and 15:30.
3. **Stop and target.** Stop at the opposite end of the range. Take profit a
   fixed number of points from the entry (`--points`, default 10). Flat by
   15:55.

| rule | default | rule | default |
| --- | --- | --- | --- |
| `range` | 0930-1000 | `entry` | 1000-1530 |
| `tp_points` | 10.0 | `flat` | 15:55 |
| `stop_buffer_ticks` | 0 | `max_trades` | 1 |
| `max_range` | 0 (off) | `min_rr` | 0 (off) |

**Say the arithmetic out loud.** The stop is as wide as the whole range, the
target is a few points. The tool prints the reward against the risk and the
win rate needed to break even (for example "pays at most 0.43 : 1, has to win
at least 70 % of the time"). A high win rate with this rule is expected and is
not an edge by itself: one stop gives back many targets. "Points" are price
units of that market, so 10 fits an index future and is far too big for most
stocks; ask the user what they trade and set `--points` to match.

## Steps

1. Ask which market and which setup, if the user has not said. Run `check`.
2. Report in this order: the levels or the range, what has been swept, then
   the status line exactly as printed (WAITING, ARMED, TRIGGERED, IN A TRADE,
   DONE, SKIPPED). For a trigger, say the entry, the stop, the target, the
   risk a unit and reward:risk. If the trigger is more than a bar old, say so.
3. To watch for a trigger, run `check` on a `/loop` (every minute for sweep,
   every 15 minutes for orb). It reads completed bars only; a forming bar
   never counts. Never place or stage an order from a loop.
4. When the user asks how the rule has done, run `history` and read out the
   counts, the average R, the break-even line and the caveat. Never turn a
   few weeks of trades into a claim.
5. **Hand-off.** For a BUY on a US stock or ETF, `check` prints a ready
   `trader-exit` command (entry, stop and target as prices, the time moved to
   the next 5-minute mark because the Time Machine works on 5-minute bars).
   Run it, then `/trader-timemachine`, `/trader-budget`, `/trader-risk`, and
   only then `/trader-broker`. The Time Machine's history section skips this
   setup (it only knows the opening-range breakout); its replays still run.
   Sells, futures, FX and crypto cannot go into the pack: it is long-only and
   US stocks only. For those the Playbook is a signal for the user's own
   platform, and you say that plainly.
6. `/trader-risk` blocks any plan whose reward:risk is under the user's
   minimum (1.0 by default). The 10 AM rule with a small target usually is.
   Do not lower the limit for them. Tell them why it was blocked.

## The TradingView scripts

`trade/tradingview/session-sweep-fvg.pine` (1-minute chart) and
`trade/tradingview/ten-am-range-breakout.pine` (15-minute chart) apply the
same rules in the same order, as Pine Script v6 strategies with levels,
entries, alerts and a Strategy Tester report. `trade/README.md` has the steps
to add them. If the user reports a compile error from TradingView, ask for the
exact message and line, fix the script, and keep `playbook.py` in step: the
two must stay rule-for-rule the same. Tests: `python3 -m unittest discover -s
trade/tests`.

The two can still differ on a given bar: TradingView's feed and Yahoo's are
different sources, tick sizes can differ, and when one bar touches both the
stop and the target this tool counts the stop while TradingView's tester
guesses the order from the bar's shape.

## Rules

- Completed bars only. Never call a sweep, a gap or a breakout early.
- Report what the rule says. Never say a setup is good, likely, or "high
  probability", and never repeat a claim that a strategy makes money.
- Every number comes from the run. Say which symbol, which bars and what time.
- Free Yahoo bars are unofficial and can lag. Say so when a minute matters.

## What it never does

It never places, sizes or stages an order, never moves a stop or a target
after the fact, never counts a bar that has not closed, never hands a sell or
a non-US-stock signal into the pack, and never presents a short history as
proof.

## Safety

- Education and tooling, not financial advice. A trigger is a rule firing, not a recommendation.
- Paper trading by default (`"paper": true` in the pack's config). Going live is the user's own decision.
- Nothing places an order except `/trader-broker`, and only when all three hold: `/trader-risk` passed
  that exact order today, the user typed an explicit yes for that one order in the chat, and the user
  clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
- No options, margin, short selling or leverage unless the pack's config allows it and the user confirms.
