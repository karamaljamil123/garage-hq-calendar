#!/usr/bin/env python3
"""trader-playbook: two personal day-trading setups, checked on free Yahoo bars.

  sweep  Session sweep + FVG (1-minute bars). Mark the Asia and London session highs and
         lows, wait for one to be swept, then take a fair-value-gap reversal the other way.
         Stop one tick beyond the sweep's extreme, target 2R or the next liquidity level.
  orb    10 AM range breakout (15-minute bars). The range is the first two 15-minute
         candles (9:30-10:00 New York). A CLOSE above it is a buy, a close below it a sell.
         Stop at the opposite end of the range, take profit a fixed number of points.

Usage:
  python3 playbook.py check NQ=F --setup sweep          # today's levels, sweeps and any trigger
  python3 playbook.py check ES=F --setup orb            # today's range and any trigger
  python3 playbook.py history NQ=F --setup sweep        # every past signal Yahoo's bars allow
  python3 playbook.py history ES=F --setup orb --points 10
  python3 playbook.py check QQQ --setup orb --points 1 --write   # also save trader/playbook.json

Common flags: --now "2026-11-20 10:15" (New York time, replays a past moment),
--offline FILE (a saved Yahoo chart JSON, .json or .json.gz), --json, --tick SIZE,
--set KEY=VALUE (any rule below, repeatable), --points, --rr, --model, --target.

The same rules run in the two TradingView scripts in trade/tradingview/. This tool never
places an order. Free Yahoo bars are unofficial and can lag; they are not a live feed.
Education and tooling, not financial advice.
"""
import argparse
import calendar
import datetime
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; trader-playbook/1.0)"
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
PAUSE = 0.4
HERE = os.path.dirname(os.path.abspath(__file__))

# Tick sizes Yahoo does not report. Anything else uses Yahoo's price precision, or --tick.
TICKS = {"NQ=F": 0.25, "MNQ=F": 0.25, "ES=F": 0.25, "MES=F": 0.25, "YM=F": 1.0, "MYM=F": 1.0,
         "RTY=F": 0.1, "M2K=F": 0.1, "GC=F": 0.1, "MGC=F": 0.1, "SI=F": 0.005, "CL=F": 0.01}

# ---------------------------------------------------------------- New York clock (no tz database)


def _nth_sunday(year, month, n):
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


_DST = {}


def ny_utc_offset(ts):
    """Seconds to add to a UTC epoch to get New York wall time (-4 h in daylight time, else -5 h)."""
    year = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).year
    if year not in _DST:
        start = _nth_sunday(year, 3, 2)   # second Sunday of March, 2:00 EST = 07:00 UTC
        end = _nth_sunday(year, 11, 1)    # first Sunday of November, 2:00 EDT = 06:00 UTC
        _DST[year] = (calendar.timegm((year, 3, start.day, 7, 0, 0)),
                      calendar.timegm((year, 11, end.day, 6, 0, 0)))
    start_ts, end_ts = _DST[year]
    return -4 * 3600 if start_ts <= ts < end_ts else -5 * 3600


def ny(ts):
    """Naive New York wall-clock datetime for a UTC epoch."""
    return datetime.datetime.fromtimestamp(ts + ny_utc_offset(ts), datetime.timezone.utc).replace(tzinfo=None)


def ny_to_ts(dt):
    """UTC epoch for a naive New York wall-clock datetime."""
    wall = calendar.timegm(dt.timetuple())
    for off in (-4 * 3600, -5 * 3600):
        if ny_utc_offset(wall - off) == off:
            return wall - off
    return wall + 5 * 3600


def clock(ts):
    """'9:47 AM' for a UTC epoch, New York time."""
    d = ny(ts)
    return "%d:%02d %s" % (d.hour % 12 or 12, d.minute, "AM" if d.hour < 12 else "PM")


# ---------------------------------------------------------------- sessions

_SESSION = re.compile(r"^(\d{2})(\d{2})-(\d{2})(\d{2})$")


def parse_sessions(spec):
    """'0930-1130' or '0200-0500,0930-1130' -> [(start_min, end_min), ...] in New York minutes of day."""
    out = []
    for part in str(spec).split(","):
        part = part.strip().replace(":", "")
        if not part:
            continue
        m = _SESSION.match(part)
        if not m:
            raise ValueError("bad session %r: use HHMM-HHMM, for example 0930-1130" % part)
        s = int(m.group(1)) * 60 + int(m.group(2))
        e = int(m.group(3)) * 60 + int(m.group(4))
        if s > 1440 or e > 1440 or int(m.group(2)) > 59 or int(m.group(4)) > 59:
            raise ValueError("bad session %r: hours 00-24, minutes 00-59" % part)
        out.append((s, e))
    if not out:
        raise ValueError("empty session")
    return out


def in_sessions(tod, sessions):
    """True when a bar that OPENS at minute-of-day `tod` is inside any [start, end) window.
    start > end runs through midnight; start == end means all day."""
    for s, e in sessions:
        if s == e:
            return True
        if s < e:
            if s <= tod < e:
                return True
        elif tod >= s or tod < e:
            return True
    return False


def parse_hhmm(text):
    m = re.match(r"^(\d{1,2}):?(\d{2})$", str(text).strip())
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59:
        raise ValueError("bad time %r: use HH:MM" % text)
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm(minutes):
    minutes %= 1440
    return "%02d:%02d" % (minutes // 60, minutes % 60)


# ---------------------------------------------------------------- shared engine pieces


def ticks(diff, tick):
    """A price distance in whole ticks, rounded half up (the TradingView scripts do the same)."""
    return int(math.floor(diff / tick + 0.5 + 1e-9))


def round_tick(price, tick):
    return round(math.floor(price / tick + 0.5 + 1e-9) * tick, 10)


def bar_minutes(bars):
    """The bar size in minutes: the most common spacing between consecutive bars."""
    counts = {}
    for a, b in zip(bars, bars[1:]):
        d = b["t"] - a["t"]
        if d > 0:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return 1
    return max(1, int(round(max(counts, key=counts.get) / 60.0)))


def manage_trade(trade, i, bar, flat_now):
    """Advance an open trade over one completed bar. Returns True when it closed.
    Stop first when one bar touches both. A bar that opens beyond the stop fills at the
    open; a bar that opens beyond the target fills at the open too."""
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    side, stop, target = trade["side"], trade["stop"], trade["target"]
    exit_price = result = None
    if side == "long":
        if l <= stop:
            exit_price, result = min(o, stop), "stop"
        elif target is not None and h >= target:
            exit_price, result = max(o, target), "target"
    else:
        if h >= stop:
            exit_price, result = max(o, stop), "stop"
        elif target is not None and l <= target:
            exit_price, result = min(o, target), "target"
    if result is None and flat_now:
        exit_price, result = c, "flat"
    if result is None:
        return False
    sign = 1.0 if side == "long" else -1.0
    trade.update(exit_i=i, exit_t=bar["t"], exit=exit_price, result=result,
                 r=round(sign * (exit_price - trade["entry"]) / trade["risk"], 4))
    return True


# ---------------------------------------------------------------- setup A: session sweep + FVG

SWEEP_DEFAULTS = {
    "asia": "2000-0000",          # Asia session, New York time; the trading day starts here
    "london": "0200-0500",        # London session, New York time
    "entry": "0930-1100",         # entries only inside this window (several windows: comma separated)
    "flat": "15:55",              # flat by this New York time; "" = never force flat
    "model": "fvg",               # fvg | ifvg | either
    "require_mss": True,          # fvg trigger: a close beyond the last swing must come first
    "pivot_len": 2,               # bars each side that define a swing high / low
    "require_reclaim": False,     # the entry bar must close back inside the swept level
    "min_sweep_ticks": 1,         # how far beyond the level counts as a sweep
    "min_gap_ticks": 1,           # smallest fair value gap that counts
    "max_bars": 20,               # the entry must come within this many bars of the sweep extreme
    "max_setup_bars": 60,         # the setup is void this many bars after the level was taken
    "ifvg_lookback": 30,          # ifvg trigger: the opposing gap formed within this many bars before the extreme
    "stop_buffer_ticks": 1,       # stop this many ticks beyond the sweep extreme
    "min_risk_ticks": 2,          # skip when the stop is closer than this
    "max_risk": 0.0,              # skip when the stop is further than this (price units); 0 = off
    "target": "rr",               # rr | liquidity
    "rr": 2.0,                    # reward : risk of the fixed target
    "min_rr": 1.0,                # a liquidity target must pay at least this, else the fixed target
    "prev_day_targets": True,     # previous day's high / low count as liquidity targets
    "max_trades": 2,              # entries per trading day
    "long": True,
    "short": True,
}

LEVEL_NAMES = {"AH": "Asia high", "AL": "Asia low", "LH": "London high", "LL": "London low",
               "PDH": "previous day high", "PDL": "previous day low"}


def is_pivot_high(H, i, n):
    """True when bar i-n is a swing high: each of the n bars after it has a lower high and
    none of the n bars before it has a higher high."""
    p = i - n
    if p - n < 0:
        return False
    v = H[p]
    for k in range(p + 1, i + 1):
        if H[k] >= v:
            return False
    for k in range(p - n, p):
        if H[k] > v:
            return False
    return True


def is_pivot_low(L, i, n):
    p = i - n
    if p - n < 0:
        return False
    v = L[p]
    for k in range(p + 1, i + 1):
        if L[k] <= v:
            return False
    for k in range(p - n, p):
        if L[k] < v:
            return False
    return True


class SweepEngine:
    """Runs the session sweep + FVG rules over completed bars, one bar at a time, in the same
    order as trade/tradingview/session-sweep-fvg.pine."""

    def __init__(self, bars, tick, cfg=None):
        self.cfg = dict(SWEEP_DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        c = self.cfg
        if c["model"] not in ("fvg", "ifvg", "either"):
            raise ValueError("model must be fvg, ifvg or either")
        if c["target"] not in ("rr", "liquidity"):
            raise ValueError("target must be rr or liquidity")
        if int(c["pivot_len"]) < 1:
            raise ValueError("pivot_len must be 1 or more")
        self.bars = bars
        self.tick = float(tick)
        self.bar_min = bar_minutes(bars)
        self.asia = parse_sessions(c["asia"])
        self.london = parse_sessions(c["london"])
        self.entry = parse_sessions(c["entry"])
        self.day_start = self.asia[0][0]
        self.flat_cyc = None if not c["flat"] else (parse_hhmm(c["flat"]) - self.day_start) % 1440
        self.H = [b["h"] for b in bars]
        self.L = [b["l"] for b in bars]
        self.cycle_key = None
        self.cycle_valid = False
        self.cycles = []
        self.events = []
        self.trades = []
        self.trade = None
        self.cyc_hi = self.cyc_lo = None
        self.pdh = self.pdl = None
        self.last_ph = self.last_ph_bar = None
        self.last_pl = self.last_pl_bar = None
        self.bull_gaps = []
        self.bear_gaps = []
        self._reset_cycle(None)

    def _reset_cycle(self, key):
        self.lv = {"AH": None, "AL": None, "LH": None, "LL": None}
        self.seen = {"asia": False, "london": False}
        self.done = {"asia": False, "london": False}
        self.swept = {"AH": False, "AL": False, "LH": False, "LL": False, "PDH": False, "PDL": False}
        self.trades_today = 0
        self.bull = None            # armed buy setup (a low was swept)
        self.bear = None            # armed sell setup (a high was swept)
        self.cyc = {"date": key, "levels": self.lv, "done": self.done, "swept_at": {}, "trades": []}

    def level(self, name):
        if name == "PDH":
            return self.pdh
        if name == "PDL":
            return self.pdl
        return self.lv[name]

    def active(self, name):
        if self.swept[name] or self.level(name) is None:
            return False
        if name in ("AH", "AL"):
            return self.done["asia"]
        if name in ("LH", "LL"):
            return self.done["london"]
        return True

    def run(self):
        for i in range(len(self.bars)):
            self.step(i)
        return self

    def step(self, i):
        c = self.cfg
        bar = self.bars[i]
        h, l, cl = bar["h"], bar["l"], bar["c"]
        tk = self.tick
        dt = ny(bar["t"])
        tod = dt.hour * 60 + dt.minute
        cyc_min = (tod - self.day_start) % 1440
        key = (dt + datetime.timedelta(minutes=1440 - self.day_start)).date().isoformat()

        # 1. trading day rollover (the day starts with the Asia session)
        if key != self.cycle_key:
            first = self.cycle_key is None
            self.cycle_key = key
            self.pdh, self.pdl = self.cyc_hi, self.cyc_lo
            self.cyc_hi, self.cyc_lo = h, l
            self._reset_cycle(key)
            self.cycle_valid = not first      # the first, partial day of the data is warm-up only
            if self.cycle_valid:
                self.cycles.append(self.cyc)
        else:
            self.cyc_hi = max(self.cyc_hi, h)
            self.cyc_lo = min(self.cyc_lo, l)

        # 2. swing points (known pivot_len bars later)
        n = int(c["pivot_len"])
        new_ph = is_pivot_high(self.H, i, n)
        new_pl = is_pivot_low(self.L, i, n)
        if new_ph:
            self.last_ph, self.last_ph_bar = self.H[i - n], i - n
        if new_pl:
            self.last_pl, self.last_pl_bar = self.L[i - n], i - n

        flat_now = self.flat_cyc is not None and cyc_min + self.bar_min >= self.flat_cyc

        # an open trade, entered on an earlier bar
        if self.trade is not None and i > self.trade["entry_i"]:
            if manage_trade(self.trade, i, bar, flat_now):
                self.events.append({"i": i, "t": bar["t"], "type": "exit", "trade": self.trade})
                self.trade = None

        # 3. session highs and lows
        for sess, in_now, hi_name, lo_name in (("asia", in_sessions(tod, self.asia), "AH", "AL"),
                                               ("london", in_sessions(tod, self.london), "LH", "LL")):
            if in_now and not self.done[sess]:
                self.seen[sess] = True
                self.lv[hi_name] = h if self.lv[hi_name] is None else max(self.lv[hi_name], h)
                self.lv[lo_name] = l if self.lv[lo_name] is None else min(self.lv[lo_name], l)
            elif self.seen[sess] and not self.done[sess]:
                self.done[sess] = True

        # 4. fair value gaps completed by this bar
        bull_gap = i >= 2 and ticks(l - self.H[i - 2], tk) >= c["min_gap_ticks"]
        bear_gap = i >= 2 and ticks(self.L[i - 2] - h, tk) >= c["min_gap_ticks"]

        if self.cycle_valid:
            # 5a. which levels did this bar take?
            low_names = [nm for nm in ("AL", "LL") if self.active(nm)
                         and ticks(self.level(nm) - l, tk) >= c["min_sweep_ticks"]]
            high_names = [nm for nm in ("AH", "LH") if self.active(nm)
                          and ticks(h - self.level(nm), tk) >= c["min_sweep_ticks"]]
            if self.active("PDL") and ticks(self.pdl - l, tk) >= c["min_sweep_ticks"]:
                self.swept["PDL"] = True
            if self.active("PDH") and ticks(h - self.pdh, tk) >= c["min_sweep_ticks"]:
                self.swept["PDH"] = True

            # 5b. arm a setup, or add to the one already armed
            if low_names:
                lvl = min(self.level(nm) for nm in low_names)
                for nm in low_names:
                    self.swept[nm] = True
                    self.cyc["swept_at"][nm] = bar["t"]
                    self.events.append({"i": i, "t": bar["t"], "type": "sweep", "level": nm,
                                        "price": self.level(nm), "side": "low"})
                if self.bull is None:
                    self.bull = {"ext": l, "ext_bar": i, "lvl": lvl, "names": list(low_names),
                                 "ref": self.last_ph, "ref_bar": self.last_ph_bar, "mss": False,
                                 "armed_i": i}
                else:
                    self.bull["names"] += low_names
                    self.bull["lvl"] = min(self.bull["lvl"], lvl)
                    self.bull["armed_i"] = i
            if high_names:
                lvl = max(self.level(nm) for nm in high_names)
                for nm in high_names:
                    self.swept[nm] = True
                    self.cyc["swept_at"][nm] = bar["t"]
                    self.events.append({"i": i, "t": bar["t"], "type": "sweep", "level": nm,
                                        "price": self.level(nm), "side": "high"})
                if self.bear is None:
                    self.bear = {"ext": h, "ext_bar": i, "lvl": lvl, "names": list(high_names),
                                 "ref": self.last_pl, "ref_bar": self.last_pl_bar, "mss": False,
                                 "armed_i": i}
                else:
                    self.bear["names"] += high_names
                    self.bear["lvl"] = max(self.bear["lvl"], lvl)
                    self.bear["armed_i"] = i

            # 5c. keep armed setups current: new extreme, swing reference, structure shift, expiry
            s = self.bull
            if s is not None:
                if l < s["ext"]:
                    s.update(ext=l, ext_bar=i, mss=False, ref=self.last_ph, ref_bar=self.last_ph_bar)
                if new_ph and self.last_ph_bar < s["ext_bar"] and (s["ref_bar"] is None or self.last_ph_bar > s["ref_bar"]):
                    s.update(ref=self.last_ph, ref_bar=self.last_ph_bar)
                if s["ref"] is not None and cl > s["ref"]:
                    s["mss"] = True
                if i - s["ext_bar"] > c["max_bars"] or i - s["armed_i"] > c["max_setup_bars"]:
                    self.events.append({"i": i, "t": bar["t"], "type": "expired", "side": "long"})
                    self.bull = None
            s = self.bear
            if s is not None:
                if h > s["ext"]:
                    s.update(ext=h, ext_bar=i, mss=False, ref=self.last_pl, ref_bar=self.last_pl_bar)
                if new_pl and self.last_pl_bar < s["ext_bar"] and (s["ref_bar"] is None or self.last_pl_bar > s["ref_bar"]):
                    s.update(ref=self.last_pl, ref_bar=self.last_pl_bar)
                if s["ref"] is not None and cl < s["ref"]:
                    s["mss"] = True
                if i - s["ext_bar"] > c["max_bars"] or i - s["armed_i"] > c["max_setup_bars"]:
                    self.events.append({"i": i, "t": bar["t"], "type": "expired", "side": "short"})
                    self.bear = None

            # 5d. entries, at the close of the bar that completes the trigger
            can_enter = (self.trade is None and in_sessions(tod, self.entry) and not flat_now
                         and self.trades_today < c["max_trades"])
            s = self.bull
            if s is not None and can_enter and c["long"]:
                gap = self._opposing_gap(self.bear_gaps, s["ext_bar"])
                sig_fvg = c["model"] in ("fvg", "either") and bull_gap and (s["mss"] or not c["require_mss"])
                sig_ifvg = c["model"] in ("ifvg", "either") and gap is not None and cl > gap["top"]
                if (sig_fvg or sig_ifvg) and (cl > s["lvl"] or not c["require_reclaim"]):
                    zone = (self.H[i - 2], l) if sig_fvg else (gap["bot"], gap["top"])
                    self._open("long", i, bar, s, "fvg" if sig_fvg else "ifvg", zone)
                    can_enter = False
            s = self.bear
            if s is not None and can_enter and c["short"]:
                gap = self._opposing_gap(self.bull_gaps, s["ext_bar"])
                sig_fvg = c["model"] in ("fvg", "either") and bear_gap and (s["mss"] or not c["require_mss"])
                sig_ifvg = c["model"] in ("ifvg", "either") and gap is not None and cl < gap["bot"]
                if (sig_fvg or sig_ifvg) and (cl < s["lvl"] or not c["require_reclaim"]):
                    zone = (h, self.L[i - 2]) if sig_fvg else (gap["bot"], gap["top"])
                    self._open("short", i, bar, s, "fvg" if sig_fvg else "ifvg", zone)

        # 6. gap book-keeping: a gap is used up once price closes through it
        keep = c["ifvg_lookback"] + c["max_bars"]
        self.bear_gaps = [g for g in self.bear_gaps if cl <= g["top"] and i - g["bar"] <= keep]
        self.bull_gaps = [g for g in self.bull_gaps if cl >= g["bot"] and i - g["bar"] <= keep]
        if bull_gap:
            self.bull_gaps.append({"top": l, "bot": self.H[i - 2], "bar": i})
        if bear_gap:
            self.bear_gaps.append({"top": self.L[i - 2], "bot": h, "bar": i})
        self.bull_gaps = self.bull_gaps[-12:]
        self.bear_gaps = self.bear_gaps[-12:]

    def _opposing_gap(self, gaps, ext_bar):
        """The most recent stored gap that formed at or before the sweep extreme, within the lookback."""
        for g in reversed(gaps):
            if g["bar"] <= ext_bar and ext_bar - g["bar"] <= self.cfg["ifvg_lookback"]:
                return g
        return None

    def _open(self, side, i, bar, setup, model, zone):
        c = self.cfg
        tk = self.tick
        entry = bar["c"]
        sign = 1.0 if side == "long" else -1.0
        stop = round_tick(setup["ext"] - sign * c["stop_buffer_ticks"] * tk, tk)
        risk = sign * (entry - stop)
        if side == "long":
            self.bull = None
        else:
            self.bear = None
        why = None
        if ticks(risk, tk) < max(1, c["min_risk_ticks"]):
            why = "stop too close"
        elif c["max_risk"] > 0 and risk > c["max_risk"]:
            why = "stop too far"
        if why:
            self.events.append({"i": i, "t": bar["t"], "type": "skipped", "side": side, "why": why})
            return
        target = round_tick(entry + sign * c["rr"] * risk, tk)
        target_name = "%gR" % c["rr"]
        if c["target"] == "liquidity":
            best = None
            for nm in (("AH", "LH", "PDH") if side == "long" else ("AL", "LL", "PDL")):
                if nm.startswith("PD") and not c["prev_day_targets"]:
                    continue
                if not self.active(nm):
                    continue
                dist = sign * (self.level(nm) - entry)
                if dist <= 0 or dist / risk < c["min_rr"]:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, nm)
            if best is not None:
                target, target_name = round_tick(self.level(best[1]), tk), LEVEL_NAMES[best[1]]
        trade = {"setup": "sweep", "side": side, "model": model, "entry_i": i, "entry_t": bar["t"],
                 "entry": entry, "stop": stop, "target": target, "risk": round(risk, 10),
                 "rr": round(sign * (target - entry) / risk, 2), "target_name": target_name,
                 "swept": [LEVEL_NAMES[nm] for nm in setup["names"]], "swept_level": setup["lvl"],
                 "extreme": setup["ext"], "extreme_i": setup["ext_bar"], "zone": list(zone),
                 "date": self.cycle_key, "result": "open", "r": None}
        self.trade = trade
        self.trades.append(trade)
        self.cyc["trades"].append(trade)
        self.trades_today += 1
        self.events.append({"i": i, "t": bar["t"], "type": "entry", "trade": trade})


# ---------------------------------------------------------------- setup B: 10 AM range breakout

ORB_DEFAULTS = {
    "range": "0930-1000",         # the range: bars that open inside this window (New York time)
    "entry": "1000-1530",         # a breakout bar must open inside this window
    "flat": "15:55",              # flat by this New York time; "" = never force flat
    "tp_points": 10.0,            # take profit in price points from the entry; 0 = no take profit
    "stop_buffer_ticks": 0,       # stop this many ticks beyond the opposite end of the range
    "max_range": 0.0,             # skip the day when the range is wider than this; 0 = off
    "min_rr": 0.0,                # skip when take profit / stop distance is under this; 0 = off
    "max_trades": 1,              # entries per day
    "long": True,
    "short": True,
}


class OrbEngine:
    """Runs the 10 AM range breakout rules over completed bars, in the same order as
    trade/tradingview/ten-am-range-breakout.pine."""

    def __init__(self, bars, tick, cfg=None):
        self.cfg = dict(ORB_DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        c = self.cfg
        self.bars = bars
        self.tick = float(tick)
        self.bar_min = bar_minutes(bars)
        self.rng = parse_sessions(c["range"])
        self.entry = parse_sessions(c["entry"])
        self.flat_min = None if not c["flat"] else parse_hhmm(c["flat"])
        self.day_key = None
        self.days = []
        self.events = []
        self.trades = []
        self.trade = None
        self._reset_day(None)

    def _reset_day(self, key):
        self.hi = self.lo = None
        self.range_bars = 0
        self.range_clean = False
        self.range_done = False
        self.trades_today = 0
        self.day = {"date": key, "high": None, "low": None, "trades": [], "note": ""}

    def run(self):
        for i in range(len(self.bars)):
            self.step(i)
        return self

    def step(self, i):
        c = self.cfg
        bar = self.bars[i]
        h, l, cl = bar["h"], bar["l"], bar["c"]
        tk = self.tick
        dt = ny(bar["t"])
        tod = dt.hour * 60 + dt.minute
        key = dt.date().isoformat()

        if key != self.day_key:
            self.day_key = key
            self._reset_day(key)
            self.days.append(self.day)

        flat_now = self.flat_min is not None and tod + self.bar_min >= self.flat_min

        if self.trade is not None and i > self.trade["entry_i"]:
            if manage_trade(self.trade, i, bar, flat_now):
                self.events.append({"i": i, "t": bar["t"], "type": "exit", "trade": self.trade})
                self.trade = None

        in_range = in_sessions(tod, self.rng)
        if in_range and not self.range_done:
            if self.range_bars == 0:
                self.range_clean = tod == self.rng[0][0]      # the first range bar must be there
            self.range_bars += 1
            self.hi = h if self.hi is None else max(self.hi, h)
            self.lo = l if self.lo is None else min(self.lo, l)
        elif self.range_bars > 0 and not self.range_done:
            self.range_done = True
            self.day.update(high=self.hi, low=self.lo)
            if not self.range_clean:
                self.day["note"] = "range incomplete"
            elif c["max_range"] > 0 and self.hi - self.lo > c["max_range"]:
                self.day["note"] = "range too wide"

        if (self.range_done and not self.day["note"] and self.trade is None and not in_range
                and in_sessions(tod, self.entry) and not flat_now and self.trades_today < c["max_trades"]):
            side = None
            if cl > self.hi and c["long"]:
                side = "long"
            elif cl < self.lo and c["short"]:
                side = "short"
            if side:
                sign = 1.0 if side == "long" else -1.0
                entry = cl
                edge = self.lo if side == "long" else self.hi
                stop = round_tick(edge - sign * c["stop_buffer_ticks"] * tk, tk)
                risk = sign * (entry - stop)
                tp = float(c["tp_points"])
                target = round_tick(entry + sign * tp, tk) if tp > 0 else None
                rr = tp / risk if risk > 0 and tp > 0 else None
                self.trades_today += 1                       # the day's break is used, taken or not
                if risk <= 0 or (c["min_rr"] > 0 and (rr is None or rr < c["min_rr"])):
                    self.events.append({"i": i, "t": bar["t"], "type": "skipped", "side": side,
                                        "why": "reward:risk under the minimum"})
                else:
                    trade = {"setup": "orb", "side": side, "entry_i": i, "entry_t": bar["t"], "entry": entry,
                             "stop": stop, "target": target, "risk": round(risk, 10),
                             "rr": None if rr is None else round(rr, 2),
                             "range_high": self.hi, "range_low": self.lo, "date": key,
                             "target_name": "%g points" % tp if tp > 0 else "none",
                             "result": "open", "r": None}
                    self.trade = trade
                    self.trades.append(trade)
                    self.day["trades"].append(trade)
                    self.events.append({"i": i, "t": bar["t"], "type": "entry", "trade": trade})


# ---------------------------------------------------------------- statistics


def summarize(trades):
    closed = [t for t in trades if t["result"] != "open"]
    n = len(closed)
    out = {"signals": len(trades), "closed": n, "open": len(trades) - n, "target": 0, "stop": 0, "flat": 0,
           "avg_r": None, "total_r": 0.0, "avg_planned_rr": None, "breakeven_win_pct": None,
           "target_pct": None, "avg_risk": None}
    if not n:
        return out
    for t in closed:
        out[t["result"]] += 1
    out["total_r"] = round(sum(t["r"] for t in closed), 2)
    out["avg_r"] = round(out["total_r"] / n, 3)
    out["target_pct"] = round(100.0 * out["target"] / n, 1)
    out["avg_risk"] = round(sum(t["risk"] for t in closed) / n, 4)
    planned = [t["rr"] for t in closed if t["rr"]]
    if planned:
        rr = sum(planned) / len(planned)
        out["avg_planned_rr"] = round(rr, 2)
        out["breakeven_win_pct"] = round(100.0 / (1.0 + rr), 1)
    return out


# ---------------------------------------------------------------- data (Yahoo chart JSON)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_chart(symbol, query):
    """One chart request: query2 first, query1 on a 429 or 5xx, one retry after a wait on a 429."""
    path = "/v8/finance/chart/%s?%s" % (urllib.parse.quote(symbol, safe="=-.^"), query)
    last = None
    for attempt in range(2):
        for host in YAHOO_HOSTS:
            try:
                return http_get("https://%s%s" % (host, path))
            except urllib.error.HTTPError as e:
                last = "HTTP %s" % e.code
                if e.code == 404:
                    raise RuntimeError("Yahoo has no chart for %s (HTTP 404). Check the symbol: futures look like NQ=F, FX like EURUSD=X, crypto like BTC-USD." % symbol)
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError("Yahoo answered HTTP %s for %s." % (e.code, symbol))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError("Could not reach Yahoo for %s (%s). Free Yahoo data is unofficial and can be down or blocked." % (symbol, last))


def parse_chart(text):
    """Yahoo chart JSON -> (bars, meta). Bars with a missing price are dropped."""
    try:
        data = json.loads(text)
        chart = data["chart"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("That is not a Yahoo chart JSON document.")
    if chart.get("error"):
        raise RuntimeError("Yahoo: %s" % (chart["error"].get("description") or chart["error"]))
    res = (chart.get("result") or [None])[0]
    if not res:
        raise RuntimeError("Yahoo returned no data.")
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for k, t in enumerate(stamps):
        try:
            o, h, l, c = quote["open"][k], quote["high"][k], quote["low"][k], quote["close"][k]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        v = (quote.get("volume") or [0] * len(stamps))[k] or 0
        bars.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": v})
    return bars, res.get("meta") or {}


def tidy(bars, interval_min, now_ts):
    """Sort, drop duplicates, drop bars off the grid (Yahoo's live tick) and bars still forming."""
    step = interval_min * 60
    seen = {}
    for b in bars:
        if b["t"] % step == 0 and b["t"] + step <= now_ts:
            seen[b["t"]] = b
    return [seen[t] for t in sorted(seen)]


def load_offline(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return parse_chart(f.read())


def fetch(symbol, interval_min, now_ts, days, offline=None):
    """Bars for `symbol` up to `now_ts`: (bars, meta, notes)."""
    notes = []
    if offline:
        bars, meta = load_offline(offline)
        return tidy(bars, interval_min, now_ts), meta, notes
    interval = "%dm" % interval_min
    if interval_min == 1 and days > 7:
        # Yahoo keeps about 30 days of 1-minute bars and serves at most about a week a request.
        bars, meta, end = [], {}, int(time.time())
        for k in range(int(math.ceil(min(days, 28) / 7.0))):
            p2 = end - k * 7 * 86400
            try:
                got, m = parse_chart(yahoo_chart(symbol, "interval=1m&period1=%d&period2=%d&includePrePost=true" % (p2 - 7 * 86400, p2)))
                bars += got
                meta = meta or m
            except RuntimeError as e:
                notes.append("One week of 1-minute bars was not available (%s)." % e)
            time.sleep(PAUSE)
        if not bars:
            raise RuntimeError("Yahoo returned no 1-minute bars for %s." % symbol)
        return tidy(bars, interval_min, now_ts), meta, notes
    rng = "%dd" % max(2, min(days, 7 if interval_min == 1 else 60))
    bars, meta = parse_chart(yahoo_chart(symbol, "interval=%s&range=%s&includePrePost=true" % (interval, rng)))
    return tidy(bars, interval_min, now_ts), meta, notes


def tick_size(symbol, meta, override=None):
    if override:
        return float(override)
    if symbol.upper() in TICKS:
        return TICKS[symbol.upper()]
    hint = meta.get("priceHint")
    if isinstance(hint, int) and 0 <= hint <= 8:
        return 10.0 ** -hint
    return 0.01


def is_us_stock(meta):
    return (str(meta.get("instrumentType", "")).upper() in ("EQUITY", "ETF")
            and meta.get("exchangeTimezoneName") == "America/New_York")


# ---------------------------------------------------------------- reports


def money(x, tick):
    if x is None:
        return "none"
    places = max(0, min(8, int(round(-math.log10(tick))))) if tick < 1 else 0
    if tick in (0.25, 0.005):
        places = max(places, 2 if tick == 0.25 else 3)
    return "%.*f" % (places, x)


def handoff(symbol, trade, meta):
    """How this signal goes on into the day-trader pack, or why it cannot."""
    if trade["side"] != "long":
        return ["The day-trader pack plans buys only, so this sell stays a signal: there is no hand-off to /trader-exit."]
    if not is_us_stock(meta):
        return ["The day-trader pack sizes and checks US stocks and ETFs only, so this stays a signal: there is no hand-off to /trader-exit for %s." % symbol]
    close_ts = trade["entry_t"] + trade["bar_min"] * 60
    grid_ts = int(math.ceil(close_ts / 300.0) * 300)
    d = ny(grid_ts)
    exit_py = os.path.join(os.path.dirname(HERE), "trader-exit", "exit.py")
    cmd = ('python3 "%s" plan --symbol %s --setup level --entry %s --date %s --time "%s" --interval 5m --stop price:%s'
           % (exit_py, symbol, trade["entry"], d.date().isoformat(), clock(grid_ts), trade["stop"]))
    cmd += (" --target price:%s" % trade["target"]) if trade["target"] is not None else " --target 2R"
    out = ["To take it on into the pack (a buy on a US stock or ETF): run this, then /trader-timemachine, /trader-budget, /trader-risk:",
           "  " + cmd + " --write"]
    if grid_ts != close_ts:
        out.append("  (The Time Machine works on 5-minute bars, so the plan's time is moved from %s to %s.)" % (clock(close_ts), clock(grid_ts)))
    if trade.get("rr") is not None and trade["rr"] < 1:
        out.append("  Expect /trader-risk to BLOCK it: reward:risk is %.2f and the pack's default minimum is 1.0." % trade["rr"])
    return out


def describe_trade(trade, tick, last_i):
    side = "BUY" if trade["side"] == "long" else "SELL"
    age = last_i - trade["entry_i"]
    lines = []
    if trade["result"] == "open":
        head = "TRIGGERED" if age == 0 else "IN A TRADE (triggered %d bar%s ago)" % (age, "" if age == 1 else "s")
    else:
        head = "DONE"
    how = ""
    if trade["setup"] == "sweep":
        how = " after %s was swept, on %s" % (" and ".join(trade["swept"]), "a fair value gap" if trade["model"] == "fvg" else "an inverse fair value gap")
    else:
        how = " on a close %s the range" % ("above" if trade["side"] == "long" else "below")
    lines.append("%s  %s at %s, %s%s." % (head, side, money(trade["entry"], tick), clock(trade["entry_t"] + trade["bar_min"] * 60), how))
    rr = "" if trade["rr"] is None else ", reward:risk %.2f" % trade["rr"]
    lines.append("  Stop %s. Target %s (%s). Risk %s a unit%s." % (
        money(trade["stop"], tick), money(trade["target"], tick), trade["target_name"], money(trade["risk"], tick), rr))
    if trade["result"] != "open":
        word = {"target": "hit the target", "stop": "hit the stop", "flat": "was closed at the flat-by time"}[trade["result"]]
        lines.append("  It %s at %s (%s): %+.2fR." % (word, money(trade["exit"], tick), clock(trade["exit_t"] + trade["bar_min"] * 60), trade["r"]))
    elif age > 1:
        lines.append("  That trigger is %d bars old. A late entry is a different trade." % age)
    return lines


def report_sweep(symbol, eng, tick, meta, now_ts):
    c = eng.cfg
    bars = eng.bars
    last_i = len(bars) - 1
    out = ["%s  %s ET  session sweep + FVG on %d-minute bars (Yahoo, free, can lag)" % (
        symbol, ny(now_ts).strftime("%a %Y-%m-%d %H:%M"), eng.bar_min)]
    if not eng.cycles:
        out.append("NO DATA  Not enough bars yet for a full trading day (the day starts with the Asia session at %s New York)." % hhmm(eng.day_start))
        return out, 1, None
    cyc = eng.cycles[-1]
    out.append("Trading day %s (it starts with the Asia session at %s New York)." % (cyc["date"], hhmm(eng.day_start)))
    for nm in ("AH", "AL", "LH", "LL"):
        v = cyc["levels"][nm]
        sess = "asia" if nm[0] == "A" else "london"
        if v is None:
            state = "no bars in that session"
        elif nm in cyc["swept_at"]:
            state = "swept at %s" % clock(cyc["swept_at"][nm])
        elif not cyc["done"][sess]:
            state = "still forming"
        else:
            state = "not swept"
        out.append("  %-12s %12s   %s" % (LEVEL_NAMES[nm], money(v, tick), state))
    if cyc["levels"]["AH"] is None and cyc["levels"]["LH"] is None:
        out.append("No Asia or London bars: this symbol does not trade overnight on Yahoo. Use a futures, FX or crypto symbol (NQ=F, ES=F, EURUSD=X, BTC-USD).")
    code, fresh = 1, None
    for t in cyc["trades"]:
        t["bar_min"] = eng.bar_min
        out += describe_trade(t, tick, last_i)
        if t["result"] == "open":
            code, fresh = 0, t
    if eng.trade is None:
        tod = ny(bars[last_i]["t"]).hour * 60 + ny(bars[last_i]["t"]).minute
        for side, s, word in (("buy", eng.bull, "low"), ("sell", eng.bear, "high")):
            if s is None:
                continue
            left = min(c["max_bars"] - (last_i - s["ext_bar"]), c["max_setup_bars"] - (last_i - s["armed_i"]))
            names = " and ".join(LEVEL_NAMES[nm] for nm in s["names"])
            need = []
            if c["model"] in ("fvg", "either"):
                if c["require_mss"] and not s["mss"]:
                    need.append("a close %s the last swing %s%s, then a fair value gap" % (
                        "above" if side == "buy" else "below", "high" if side == "buy" else "low",
                        "" if s["ref"] is None else " " + money(s["ref"], tick)))
                else:
                    need.append("a %s fair value gap" % ("bullish" if side == "buy" else "bearish"))
            if c["model"] in ("ifvg", "either"):
                need.append("a close through the last opposing gap (inverse FVG)")
            out.append("ARMED (%s side)  %s swept; the %s so far is %s at %s. Waiting for %s. %d bar%s left." % (
                side, names, word, money(s["ext"], tick), clock(bars[s["ext_bar"]]["t"]), " or ".join(need), max(left, 0), "" if left == 1 else "s"))
        if eng.bull is None and eng.bear is None and code == 1:
            if eng.trades_today >= c["max_trades"]:
                out.append("DONE FOR TODAY  %d of %d trades taken." % (eng.trades_today, c["max_trades"]))
            else:
                out.append("WAITING  No level has just been swept. Entries are taken %s New York%s." % (
                    " and ".join("%s-%s" % (hhmm(s), hhmm(e)) for s, e in eng.entry),
                    "" if in_sessions(tod, eng.entry) else " (outside that window now)"))
    if fresh is not None:
        out += handoff(symbol, fresh, meta)
    out.append("A signal is information, not an order. Nothing was placed.")
    return out, code, fresh


def report_orb(symbol, eng, tick, meta, now_ts):
    c = eng.cfg
    last_i = len(eng.bars) - 1
    out = ["%s  %s ET  10 AM range breakout on %d-minute bars (Yahoo, free, can lag)" % (
        symbol, ny(now_ts).strftime("%a %Y-%m-%d %H:%M"), eng.bar_min)]
    if not eng.days:
        out.append("NO DATA  Yahoo returned no bars.")
        return out, 1, None
    day = eng.days[-1]
    rs, re_ = eng.rng[0]
    window = "%s-%s" % (hhmm(rs), hhmm(re_))
    code, fresh = 1, None
    if day["high"] is None:
        if eng.range_bars:
            out.append("FORMING  The %s range is still building: high %s, low %s so far." % (window, money(eng.hi, tick), money(eng.lo, tick)))
        else:
            out.append("NOT YET  No bar has opened in the %s New York range window on %s." % (window, day["date"]))
        out.append("Nothing was placed.")
        return out, 1, None
    width = day["high"] - day["low"]
    out.append("Range %s on %s: high %s, low %s (%s points wide)." % (window, day["date"], money(day["high"], tick), money(day["low"], tick), money(width, tick)))
    tp = float(c["tp_points"])
    if tp > 0 and width > 0:
        rr = tp / width
        out.append("A %g-point target against a stop at least %s points away pays at most %.2f : 1. It has to win at least %.0f%% of the time just to break even." % (
            tp, money(width, tick), rr, 100.0 / (1.0 + rr)))
    if day["note"]:
        out.append("SKIPPED  %s today." % day["note"].capitalize())
    for t in day["trades"]:
        t["bar_min"] = eng.bar_min
        out += describe_trade(t, tick, last_i)
        if t["result"] == "open":
            code, fresh = 0, t
    if not day["trades"] and not day["note"]:
        last = eng.bars[last_i]
        d = ny(last["t"])
        tod = d.hour * 60 + d.minute
        if eng.trades_today >= c["max_trades"]:
            out.append("SKIPPED  The first close outside the range did not meet the reward:risk minimum.")
        elif d.date().isoformat() == day["date"] and not in_sessions(tod, eng.entry) and tod >= eng.entry[-1][1]:
            out.append("NO TRADE  The entry window closed without a %d-minute close outside the range." % eng.bar_min)
        else:
            out.append("WAITING  No %d-minute bar has closed outside the range yet (last close %s at %s). A wick outside does not count." % (
                eng.bar_min, money(last["c"], tick), clock(last["t"] + eng.bar_min * 60)))
    if fresh is not None:
        out += handoff(symbol, fresh, meta)
    out.append("A signal is information, not an order. Nothing was placed.")
    return out, code, fresh


def report_history(symbol, setup, eng, tick, notes):
    bars = eng.bars
    n_days = len(eng.cycles) if setup == "sweep" else len([d for d in eng.days if d["high"] is not None])
    out = ["%s  %s history on %d-minute bars: %s to %s New York, %d trading day%s (Yahoo, free, unofficial)." % (
        symbol, "session sweep + FVG" if setup == "sweep" else "10 AM range breakout", eng.bar_min,
        ny(bars[0]["t"]).strftime("%Y-%m-%d %H:%M"), ny(bars[-1]["t"]).strftime("%Y-%m-%d %H:%M"), n_days, "" if n_days == 1 else "s")]
    out += notes
    for t in eng.trades:
        t["bar_min"] = eng.bar_min
        res = "open" if t["result"] == "open" else "%-6s %+.2fR" % (t["result"], t["r"])
        out.append("  %s %-5s %-4s entry %s  stop %s  target %s  risk %s  -> %s" % (
            ny(t["entry_t"] + eng.bar_min * 60).strftime("%a %m-%d %H:%M"), "buy" if t["side"] == "long" else "sell",
            t.get("model", ""), money(t["entry"], tick), money(t["stop"], tick), money(t["target"], tick), money(t["risk"], tick), res))
    s = summarize(eng.trades)
    if not s["closed"]:
        out.append("The rule did not fire in this stretch.")
        return out, s
    out.append("The rule fired %d time%s: %d target, %d stop, %d closed at the flat-by time. Average R %+.3f, total %+.2fR." % (
        s["closed"], "" if s["closed"] == 1 else "s", s["target"], s["stop"], s["flat"], s["avg_r"], s["total_r"]))
    if s["avg_planned_rr"]:
        out.append("Planned reward:risk averaged %.2f, so the target has to come first about %.0f%% of the time to break even. It came first %.0f%% of the time. Average stop distance %s." % (
            s["avg_planned_rr"], s["breakeven_win_pct"], s["target_pct"], money(s["avg_risk"], tick)))
    out.append("This is what the rule did on a short stretch of free, unofficial bars, with no costs or slippage. It is far too few trades to prove anything, and it is not a prediction.")
    return out, s


# ---------------------------------------------------------------- command line


def coerce(key, text, defaults):
    if key not in defaults:
        raise ValueError("unknown rule %r. Rules: %s" % (key, ", ".join(sorted(defaults))))
    cur = defaults[key]
    if isinstance(cur, bool):
        if text.lower() in ("1", "true", "yes", "on"):
            return True
        if text.lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError("%s wants true or false" % key)
    if isinstance(cur, int):
        return int(text)
    if isinstance(cur, float):
        return float(text)
    return text


def build_cfg(a):
    defaults = SWEEP_DEFAULTS if a.setup == "sweep" else ORB_DEFAULTS
    cfg = {}
    for item in a.set or []:
        if "=" not in item:
            raise ValueError("--set wants KEY=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        cfg[k.strip()] = coerce(k.strip(), v.strip(), defaults)
    if a.setup == "sweep":
        if a.rr is not None:
            cfg["rr"] = a.rr
        if a.model:
            cfg["model"] = a.model
        if a.target:
            cfg["target"] = a.target
    else:
        if a.points is not None:
            cfg["tp_points"] = a.points
    if a.long_only:
        cfg["short"] = False
    if a.short_only:
        cfg["long"] = False
    return cfg


def now_from(a):
    if not a.now:
        return int(time.time())
    try:
        return ny_to_ts(datetime.datetime.strptime(a.now.strip(), "%Y-%m-%d %H:%M"))
    except ValueError:
        raise ValueError('--now wants New York time like "2026-11-20 10:15"')


def main(argv=None):
    p = argparse.ArgumentParser(prog="playbook.py", description="Two personal day-trading setups, checked on free Yahoo bars. It never places an order.")
    sub = p.add_subparsers(dest="cmd")
    for name, text in (("check", "today's levels, sweeps, range and any trigger"),
                       ("history", "every past signal the available bars allow")):
        s = sub.add_parser(name, help=text)
        s.add_argument("symbol", help="Yahoo symbol: NQ=F, ES=F, EURUSD=X, BTC-USD, QQQ ...")
        s.add_argument("--setup", choices=["sweep", "orb"], required=True, help="sweep = session sweep + FVG (1-minute); orb = 10 AM range breakout (15-minute)")
        s.add_argument("--set", action="append", metavar="KEY=VALUE", help="override any rule, e.g. --set asia=1800-0000 (repeatable)")
        s.add_argument("--points", type=float, help="orb: take profit in price points (default 10)")
        s.add_argument("--rr", type=float, help="sweep: fixed reward:risk (default 2)")
        s.add_argument("--model", choices=["fvg", "ifvg", "either"], help="sweep: entry trigger (default fvg, after a structure shift)")
        s.add_argument("--target", choices=["rr", "liquidity"], help="sweep: fixed reward:risk, or the next liquidity level")
        s.add_argument("--long-only", action="store_true")
        s.add_argument("--short-only", action="store_true")
        s.add_argument("--tick", type=float, help="tick size, if Yahoo's price precision is wrong for this market")
        s.add_argument("--days", type=int, help="history: how many days of bars to ask for (sweep: up to 28, orb: up to 60)")
        s.add_argument("--now", help='pretend it is this New York time, e.g. "2026-11-20 10:15"')
        s.add_argument("--offline", metavar="PATH", help="a saved Yahoo chart JSON (.json or .json.gz) instead of the network")
        s.add_argument("--write", nargs="?", const="trader/playbook.json", metavar="PATH", help="save the result (default trader/playbook.json)")
        s.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        cfg = build_cfg(a)
        now_ts = now_from(a)
        interval = 1 if a.setup == "sweep" else 15
        if a.cmd == "check":
            days = 5
        else:
            days = a.days or (28 if a.setup == "sweep" else 60)
        symbol = a.symbol.upper()
        bars, meta, notes = fetch(symbol, interval, now_ts, days, a.offline)
        if not bars:
            print("NO DATA  Yahoo returned no completed %d-minute bars for %s up to %s New York." % (interval, symbol, ny(now_ts).strftime("%Y-%m-%d %H:%M")))
            return 1
        tick = tick_size(symbol, meta, a.tick)
        eng = (SweepEngine if a.setup == "sweep" else OrbEngine)(bars, tick, cfg).run()
    except (ValueError, RuntimeError) as e:
        print("playbook.py: %s" % e)
        return 1

    if a.cmd == "check":
        lines, code, fresh = (report_sweep if a.setup == "sweep" else report_orb)(symbol, eng, tick, meta, now_ts)
        result = {"created_by": "trader-playbook", "command": "check", "symbol": symbol, "setup": a.setup,
                  "checked_at": datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "status": "triggered" if fresh is not None else "waiting", "rules": eng.cfg, "tick": tick,
                  "signal": fresh, "text": lines}
        if a.setup == "sweep" and eng.cycles:
            cyc = eng.cycles[-1]
            result["day"] = {"date": cyc["date"], "levels": {LEVEL_NAMES[k]: v for k, v in cyc["levels"].items()},
                             "swept_at": {LEVEL_NAMES[k]: clock(v) for k, v in cyc["swept_at"].items()}}
        elif a.setup == "orb" and eng.days:
            d = eng.days[-1]
            result["day"] = {"date": d["date"], "range_high": d["high"], "range_low": d["low"], "note": d["note"]}
    else:
        lines, stats = report_history(symbol, a.setup, eng, tick, notes)
        code = 0 if stats["closed"] else 1
        result = {"created_by": "trader-playbook", "command": "history", "symbol": symbol, "setup": a.setup,
                  "rules": eng.cfg, "tick": tick, "summary": stats, "trades": eng.trades, "text": lines}
    lines.append("Education and tooling, not financial advice.")

    if a.write:
        folder = os.path.dirname(a.write)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        lines.append("Wrote %s." % a.write)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
