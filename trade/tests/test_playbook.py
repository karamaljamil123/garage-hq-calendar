"""Offline tests for .claude/skills/trader-playbook/playbook.py.

Run from the repository root:   python3 -m unittest discover -s trade/tests -v

Every scenario is a small hand-built set of candles, so each expected number below can be
checked by reading the candles. No network is used.
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, ".claude", "skills", "trader-playbook", "playbook.py")
spec = importlib.util.spec_from_file_location("playbook", TOOL)
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)

TICK = 0.25


def ts(day, hm):
    """UTC epoch for a New York date ('2026-11-18') and clock time ('09:33')."""
    return pb.ny_to_ts(datetime.datetime.strptime(day + " " + hm, "%Y-%m-%d %H:%M"))


def minute_bars(start_day, start_hm, end_day, end_hm, special):
    """One-minute candles from start to end (New York), flat around 100 except the ones in
    `special`: {(day, 'HH:MM'): (open, high, low, close)}."""
    t, end, out = ts(start_day, start_hm), ts(end_day, end_hm), []
    lookup = {ts(d, hm): v for (d, hm), v in special.items()}
    while t <= end:
        o, h, l, c = lookup.get(t, (100.0, 100.25, 99.75, 100.0))
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1})
        t += 60
    return out


D0, D1 = "2026-11-17", "2026-11-18"      # Tuesday evening into Wednesday, Eastern Standard Time

# Asia (20:00-00:00) makes 101.00 / 99.00. London (02:00-05:00) makes 100.75 / 99.50.
SESSIONS = {
    (D0, "21:00"): (100.0, 101.00, 99.90, 100.0),
    (D0, "22:00"): (100.0, 100.10, 99.00, 100.0),
    (D1, "03:00"): (100.0, 100.75, 99.90, 100.0),
    (D1, "04:00"): (100.0, 100.10, 99.50, 100.0),
}

# New York open: a swing high at 09:28 (100.10), then a slide that takes the London low at 09:33,
# a reversal that closes above the swing high at 09:36 and leaves a bullish gap (99.60 to 99.85).
NY_LONG = {
    (D1, "09:26"): (99.90, 99.95, 99.75, 99.90),
    (D1, "09:27"): (99.90, 99.95, 99.75, 99.90),
    (D1, "09:28"): (99.90, 100.10, 99.80, 99.85),
    (D1, "09:29"): (99.85, 99.90, 99.70, 99.75),
    (D1, "09:30"): (99.75, 99.85, 99.65, 99.70),
    (D1, "09:31"): (99.70, 99.80, 99.60, 99.65),
    (D1, "09:32"): (99.65, 99.75, 99.55, 99.60),
    (D1, "09:33"): (99.60, 99.70, 99.25, 99.40),
    (D1, "09:34"): (99.40, 99.60, 99.30, 99.55),
    (D1, "09:35"): (99.55, 99.95, 99.50, 99.90),
    (D1, "09:36"): (99.90, 100.30, 99.85, 100.25),
    (D1, "09:37"): (100.25, 101.20, 100.20, 101.10),
    (D1, "09:38"): (101.10, 102.80, 101.05, 102.60),
}


def calm_after(day, first_hm, last_hm, price):
    """Overlapping candles around `price`, so no gap and no new sweep forms."""
    out, t, end = {}, ts(day, first_hm), ts(day, last_hm)
    while t <= end:
        d = pb.ny(t)
        out[(day, "%02d:%02d" % (d.hour, d.minute))] = (price, price + 0.25, price - 0.25, price)
        t += 60
    return out


def long_scenario(extra=None, last="12:00"):
    special = dict(SESSIONS)
    special.update(NY_LONG)
    special.update(calm_after(D1, "09:39", last, 102.50))
    if extra:
        special.update(extra)
    return minute_bars(D0, "19:50", D1, last, special)


def run_sweep(bars, **cfg):
    return pb.SweepEngine(bars, TICK, cfg).run()


class ClockTests(unittest.TestCase):
    def test_new_york_offset_follows_daylight_saving(self):
        # 2026: daylight time starts Sunday 8 March and ends Sunday 1 November.
        self.assertEqual(pb.ny_utc_offset(ts("2026-03-06", "12:00")), -5 * 3600)
        self.assertEqual(pb.ny_utc_offset(ts("2026-03-09", "12:00")), -4 * 3600)
        self.assertEqual(pb.ny_utc_offset(ts("2026-10-30", "12:00")), -4 * 3600)
        self.assertEqual(pb.ny_utc_offset(ts("2026-11-02", "12:00")), -5 * 3600)

    def test_round_trip_and_clock_text(self):
        t = ts("2026-07-15", "09:47")
        self.assertEqual(pb.ny(t), datetime.datetime(2026, 7, 15, 9, 47))
        self.assertEqual(pb.clock(t), "9:47 AM")
        self.assertEqual(pb.clock(ts("2026-07-15", "15:55")), "3:55 PM")

    def test_sessions(self):
        asia = pb.parse_sessions("2000-0000")
        self.assertTrue(pb.in_sessions(20 * 60, asia))
        self.assertTrue(pb.in_sessions(23 * 60 + 59, asia))
        self.assertFalse(pb.in_sessions(0, asia))
        overnight = pb.parse_sessions("1800-0300")
        self.assertTrue(pb.in_sessions(60, overnight))
        self.assertFalse(pb.in_sessions(3 * 60, overnight))
        two = pb.parse_sessions("0200-0500,0930-1100")
        self.assertTrue(pb.in_sessions(9 * 60 + 30, two))
        self.assertFalse(pb.in_sessions(11 * 60, two))
        with self.assertRaises(ValueError):
            pb.parse_sessions("930-1100")

    def test_ticks_round_half_up(self):
        self.assertEqual(pb.ticks(0.25, 0.25), 1)
        self.assertEqual(pb.ticks(0.01, 0.01), 1)
        self.assertEqual(pb.ticks(100.07 - 100.06, 0.01), 1)     # float noise must not lose a tick
        self.assertEqual(pb.ticks(0.0, 0.25), 0)
        self.assertEqual(pb.round_tick(101.70, 0.25), 101.75)


class SweepTests(unittest.TestCase):
    def test_levels_are_built_from_the_two_sessions(self):
        e = run_sweep(long_scenario())
        day = e.cycles[-1]
        self.assertEqual(day["date"], D1)
        self.assertEqual(day["levels"], {"AH": 101.0, "AL": 99.0, "LH": 100.75, "LL": 99.5})

    def test_buy_after_london_low_is_swept(self):
        e = run_sweep(long_scenario(), max_trades=1)
        self.assertEqual(len(e.trades), 1)
        t = e.trades[0]
        self.assertEqual(t["side"], "long")
        self.assertEqual(t["model"], "fvg")
        self.assertEqual(t["swept"], ["London low"])
        self.assertEqual(pb.clock(t["entry_t"]), "9:36 AM")          # the candle that opens 09:36
        self.assertEqual(t["entry"], 100.25)
        self.assertEqual(t["extreme"], 99.25)
        self.assertEqual(t["stop"], 99.0)                             # one tick under the sweep low
        self.assertEqual(t["risk"], 1.25)
        self.assertEqual(t["target"], 102.75)                         # 2R
        self.assertEqual(t["zone"], [99.6, 99.85])                    # the gap: 09:34 high to 09:36 low
        self.assertEqual(t["result"], "target")
        self.assertEqual(pb.clock(t["exit_t"]), "9:38 AM")
        self.assertEqual(t["r"], 2.0)

    def test_no_entry_before_the_entry_window(self):
        e = run_sweep(long_scenario(), entry="1000-1100", max_trades=1)
        self.assertEqual(e.trades, [])

    def test_structure_shift_is_needed_by_default(self):
        # Raise the swing high to 100.50: the 09:36 close (100.25) no longer breaks it.
        higher_swing = {(D1, "09:28"): (99.90, 100.50, 99.80, 99.85)}
        with_shift = run_sweep(long_scenario(higher_swing), max_trades=1)
        self.assertEqual(pb.clock(with_shift.trades[0]["entry_t"]), "9:37 AM")   # 09:37 closes at 101.10
        self.assertEqual(with_shift.trades[0]["entry"], 101.10)
        first_gap = run_sweep(long_scenario(higher_swing), max_trades=1, require_mss=False)
        self.assertEqual(pb.clock(first_gap.trades[0]["entry_t"]), "9:36 AM")
        self.assertEqual(first_gap.trades[0]["entry"], 100.25)

    def test_inverse_fvg_trigger(self):
        # A bearish gap forms into the low (09:31 low 99.70 down to 09:33 high 99.55).
        # The first close back above 99.70 is 09:35 (99.90).
        leg = {
            (D1, "09:31"): (99.75, 99.80, 99.70, 99.72),
            (D1, "09:32"): (99.70, 99.72, 99.40, 99.45),
            (D1, "09:33"): (99.45, 99.55, 99.25, 99.40),
        }
        e = run_sweep(long_scenario(leg), model="ifvg", max_trades=1)
        t = e.trades[0]
        self.assertEqual(t["model"], "ifvg")
        self.assertEqual(pb.clock(t["entry_t"]), "9:35 AM")
        self.assertEqual(t["entry"], 99.90)
        self.assertEqual(t["zone"], [99.55, 99.70])
        self.assertEqual(t["stop"], 99.0)
        self.assertEqual(t["target"], 101.75)                         # 99.90 + 2 x 0.90, to the tick

    def test_liquidity_target_takes_the_nearest_level_that_pays_enough(self):
        default_min = run_sweep(long_scenario(), target="liquidity", max_trades=1)
        self.assertEqual(default_min.trades[0]["target"], 102.75)    # nothing pays 1R: falls back to 2R
        lower_min = run_sweep(long_scenario(), target="liquidity", min_rr=0.5, max_trades=1)
        self.assertEqual(lower_min.trades[0]["target"], 101.0)       # Asia high pays 0.6R
        self.assertEqual(lower_min.trades[0]["target_name"], "Asia high")

    def test_reclaim_filter_and_too_tight_stop(self):
        e = run_sweep(long_scenario(), require_reclaim=True, max_trades=1)
        self.assertEqual(e.trades[0]["entry"], 100.25)               # 100.25 is back above 99.50
        tight = run_sweep(long_scenario(), min_risk_ticks=50, max_trades=1)
        self.assertEqual(tight.trades, [])
        self.assertIn("stop too close", [ev.get("why") for ev in tight.events])

    def test_setup_expires(self):
        e = run_sweep(long_scenario(), max_bars=2, max_trades=1)
        self.assertEqual(e.trades, [])                                # the trigger comes 3 bars after the low
        self.assertIn("expired", [ev["type"] for ev in e.events])

    def test_stop_first_and_flat_by(self):
        # After the entry, a candle that touches both the stop and the target counts as the stop.
        both = {(D1, "09:37"): (100.25, 103.00, 98.90, 100.0)}
        e = run_sweep(long_scenario(both), max_trades=1)
        self.assertEqual(e.trades[0]["result"], "stop")
        self.assertEqual(e.trades[0]["r"], -1.0)
        # No stop, no target: closed at the candle that ends at 15:55.
        quiet = {(D1, "09:37"): (100.25, 100.50, 100.00, 100.25), (D1, "09:38"): (100.25, 100.50, 100.00, 100.25)}
        quiet.update(calm_after(D1, "09:39", "16:10", 100.25))
        e = run_sweep(long_scenario(quiet, last="16:10"), max_trades=1)
        t = e.trades[0]
        self.assertEqual(t["result"], "flat")
        self.assertEqual(pb.clock(t["exit_t"] + 60), "3:55 PM")
        self.assertEqual(t["r"], 0.0)

    def test_sell_after_a_high_is_swept(self):
        # Mirror of the buy: prices are reflected around 100.
        special = {}
        for key, (o, h, l, c) in list(SESSIONS.items()) + list(NY_LONG.items()):
            special[key] = (200 - o, 200 - l, 200 - h, 200 - c)
        special.update(calm_after(D1, "09:39", "12:00", 97.50))
        e = run_sweep(minute_bars(D0, "19:50", D1, "12:00", special), max_trades=1)
        t = e.trades[0]
        self.assertEqual(t["side"], "short")
        self.assertEqual(t["swept"], ["London high"])
        self.assertEqual(t["entry"], 99.75)
        self.assertEqual(t["stop"], 101.0)
        self.assertEqual(t["target"], 97.25)
        self.assertEqual(t["result"], "target")
        self.assertEqual(t["r"], 2.0)

    def test_first_partial_day_is_warm_up_only(self):
        bars = [b for b in long_scenario() if b["t"] >= ts(D0, "22:30")]
        e = run_sweep(bars)
        self.assertEqual(e.cycles, [])
        self.assertEqual(e.trades, [])


def quarter_bars(day, rows):
    """15-minute candles: rows are ('HH:MM', open, high, low, close)."""
    return [{"t": ts(day, hm), "o": o, "h": h, "l": l, "c": c, "v": 1} for hm, o, h, l, c in rows]


ORB_DAY = [
    ("09:30", 100.0, 101.00, 99.00, 100.5),
    ("09:45", 100.5, 101.50, 99.50, 101.0),      # range: 99.00 to 101.50
    ("10:00", 101.0, 102.00, 100.8, 101.2),      # a wick above the range, but the close is inside
    ("10:15", 101.2, 101.90, 101.0, 101.75),     # closes above 101.50: buy at 101.75
    ("10:30", 101.75, 103.0, 101.6, 102.9),
    ("10:45", 102.9, 103.0, 102.5, 102.6),
]


class OrbTests(unittest.TestCase):
    def run_orb(self, rows, day=D1, **cfg):
        return pb.OrbEngine(quarter_bars(day, rows), TICK, cfg).run()

    def test_buy_on_a_close_above_the_range(self):
        e = self.run_orb(ORB_DAY, tp_points=1.0)
        self.assertEqual(e.days[-1]["high"], 101.5)
        self.assertEqual(e.days[-1]["low"], 99.0)
        self.assertEqual(len(e.trades), 1)
        t = e.trades[0]
        self.assertEqual(t["side"], "long")
        self.assertEqual(pb.clock(t["entry_t"]), "10:15 AM")
        self.assertEqual(t["entry"], 101.75)
        self.assertEqual(t["stop"], 99.0)                    # the opposite end of the range
        self.assertEqual(t["target"], 102.75)
        self.assertEqual(t["risk"], 2.75)
        self.assertEqual(t["rr"], 0.36)
        self.assertEqual(t["result"], "target")
        self.assertEqual(t["r"], round(1.0 / 2.75, 4))

    def test_a_wick_outside_is_not_an_entry(self):
        e = self.run_orb(ORB_DAY[:3], tp_points=1.0)
        self.assertEqual(e.trades, [])

    def test_sell_on_a_close_below_the_range(self):
        rows = ORB_DAY[:2] + [("10:00", 100.5, 100.6, 98.5, 98.75), ("10:15", 98.75, 98.8, 97.0, 97.2)]
        e = self.run_orb(rows, tp_points=1.0)
        t = e.trades[0]
        self.assertEqual(t["side"], "short")
        self.assertEqual(t["entry"], 98.75)
        self.assertEqual(t["stop"], 101.5)
        self.assertEqual(t["target"], 97.75)
        self.assertEqual(t["result"], "target")

    def test_one_trade_a_day(self):
        rows = ORB_DAY + [("11:00", 102.6, 104.0, 102.5, 103.9)]
        self.assertEqual(len(self.run_orb(rows, tp_points=1.0).trades), 1)
        self.assertEqual(len(self.run_orb(rows, tp_points=1.0, max_trades=2).trades), 2)

    def test_flat_by_the_close(self):
        rows = ORB_DAY[:4] + [("%02d:%02d" % (h, m), 101.75, 102.0, 101.5, 101.75)
                              for h in range(10, 16) for m in (0, 15, 30, 45) if (h, m) > (10, 15)]
        e = self.run_orb(rows)                               # the 10-point target is never reached
        t = e.trades[0]
        self.assertEqual(t["result"], "flat")
        self.assertEqual(pb.clock(t["exit_t"]), "3:45 PM")   # the candle that ends at 16:00
        self.assertEqual(t["r"], 0.0)

    def test_skips(self):
        late_start = self.run_orb(ORB_DAY[1:], tp_points=1.0)
        self.assertEqual(late_start.trades, [])
        self.assertEqual(late_start.days[-1]["note"], "range incomplete")
        too_wide = self.run_orb(ORB_DAY, tp_points=1.0, max_range=2.0)
        self.assertEqual(too_wide.days[-1]["note"], "range too wide")
        low_rr = self.run_orb(ORB_DAY, tp_points=1.0, min_rr=1.0)
        self.assertEqual(low_rr.trades, [])
        no_target = self.run_orb(ORB_DAY, tp_points=0)
        self.assertIsNone(no_target.trades[0]["target"])
        self.assertEqual(no_target.trades[0]["result"], "open")

    def test_summary(self):
        s = pb.summarize(self.run_orb(ORB_DAY, tp_points=1.0).trades)
        self.assertEqual((s["closed"], s["target"], s["stop"], s["flat"]), (1, 1, 0, 0))
        self.assertEqual(s["avg_planned_rr"], 0.36)
        self.assertEqual(s["breakeven_win_pct"], 73.5)


class TradeTests(unittest.TestCase):
    def test_gaps_fill_at_the_open(self):
        long_trade = {"side": "long", "entry": 100.0, "stop": 99.0, "target": 102.0, "risk": 1.0, "entry_i": 0}
        pb.manage_trade(long_trade, 1, {"t": 0, "o": 98.5, "h": 98.8, "l": 98.0, "c": 98.2}, False)
        self.assertEqual((long_trade["result"], long_trade["exit"], long_trade["r"]), ("stop", 98.5, -1.5))
        short_trade = {"side": "short", "entry": 100.0, "stop": 101.0, "target": 98.0, "risk": 1.0, "entry_i": 0}
        pb.manage_trade(short_trade, 1, {"t": 0, "o": 97.5, "h": 97.8, "l": 97.0, "c": 97.2}, False)
        self.assertEqual((short_trade["result"], short_trade["exit"], short_trade["r"]), ("target", 97.5, 2.5))


class CommandTests(unittest.TestCase):
    def chart_file(self, bars, kind="FUTURE", tz="America/New_York"):
        doc = {"chart": {"error": None, "result": [{
            "meta": {"instrumentType": kind, "exchangeTimezoneName": tz, "priceHint": 2},
            "timestamp": [b["t"] for b in bars],
            "indicators": {"quote": [{k: [b[k[0]] for b in bars] for k in ("open", "high", "low", "close", "volume")}]}}]}}
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(doc, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def call(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pb.main(list(argv))
        return code, buf.getvalue()

    def test_check_reports_a_trigger_and_never_hands_futures_to_the_pack(self):
        path = self.chart_file(long_scenario(last="09:37"))
        code, text = self.call("check", "NQ=F", "--setup", "sweep", "--offline", path, "--now", D1 + " 09:37")
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED  BUY at 100.25, 9:37 AM after London low was swept", text)
        self.assertIn("Stop 99.00. Target 102.75 (2R).", text)
        self.assertIn("there is no hand-off", text)
        self.assertIn("Nothing was placed.", text)

    def test_check_hands_a_stock_buy_to_trader_exit_on_the_five_minute_grid(self):
        path = self.chart_file(long_scenario(last="09:37"), kind="ETF")
        code, text = self.call("check", "QQQ", "--setup", "sweep", "--tick", "0.25", "--offline", path, "--now", D1 + " 09:37")
        self.assertEqual(code, 0)
        self.assertIn('plan --symbol QQQ --setup level --entry 100.25 --date 2026-11-18 --time "9:40 AM" --interval 5m --stop price:99.0 --target price:102.75 --write', text)
        self.assertIn("moved from 9:37 AM to 9:40 AM", text)

    def test_check_waits_and_the_forming_bar_is_ignored(self):
        path = self.chart_file(long_scenario(last="09:37"))
        code, text = self.call("check", "NQ=F", "--setup", "sweep", "--offline", path, "--now", D1 + " 09:36")
        self.assertEqual(code, 1)                             # the 09:36 candle has not closed yet
        self.assertIn("ARMED (buy side)  London low swept", text)

    def test_history_and_json(self):
        path = self.chart_file(long_scenario())
        code, text = self.call("history", "NQ=F", "--setup", "sweep", "--offline", path, "--now", D1 + " 12:00", "--set", "max_trades=1", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["summary"]["target"], 1)
        self.assertEqual(doc["summary"]["total_r"], 2.0)

    def test_orb_check_states_the_break_even_win_rate(self):
        path = self.chart_file(quarter_bars(D1, ORB_DAY[:3]))
        code, text = self.call("check", "ES=F", "--setup", "orb", "--offline", path, "--now", D1 + " 10:15")
        self.assertEqual(code, 1)
        self.assertIn("Range 09:30-10:00 on 2026-11-18: high 101.50, low 99.00 (2.50 points wide).", text)
        self.assertIn("pays at most 4.00 : 1", text)
        self.assertIn("WAITING", text)

    def test_bad_input_is_one_plain_line(self):
        code, text = self.call("check", "NQ=F", "--setup", "sweep", "--offline", "/nonexistent.json", "--set", "nope=1")
        self.assertEqual(code, 1)
        self.assertIn("unknown rule 'nope'", text)


if __name__ == "__main__":
    unittest.main()
