"""
Historical check for the macro dashboard.

Re-runs the dashboard's OWN decision stack -- signal scoring, regime, market check,
allocation and, since Step 2, the action meter -- at past dates. It shows how the
dashboard behaved around the 2001, 2008, 2020 and 2022 episodes, and how the UNTUNED
meter scores against the S&P 500's declines of 18% or more.
Step 3a adds what the S&P actually did after each reading, which meter layers carry
information, and a list of the false alarms. Nothing is tuned: it measures, it does not fit.
Step 3b tests the nine allocation leans against what each bucket's proxy actually earned.

IMPORTANT caveats, read them:
  * Data is FRED's latest-vintage values truncated to each date: today's revised
    numbers, not what was known then. A true point-in-time test needs ALFRED vintages.
  * A monthly or quarterly figure is dated on the 1st of its period but covers the
    whole period and is published weeks later, so a reading "as of the 1st" has
    seen data that did not exist yet. This also flatters the results.
  * Both effects make lead times look better than they were. Read "it caught it" with
    salt; Step 3 and the point-in-time rebuild are where this gets resolved.
  * Signals that did not exist yet (WEI from 2008, breakevens from 2003, the HY
    spread index before 2023 on FRED) simply drop out of the earlier dates.

This file has no scoring or decision code of its own. Fetching, transforming, scoring,
the regime, the allocation, the market check and the meter all come from
macro_dashboard.py, so what is measured is exactly what ships.

S&P 500 data (yfinance) is used only to derive the equity-trend gauge and the decline
episodes. It is licensed data, so index levels are never printed here.

Run:  python historical_check.py     (needs FRED_API_KEY, like the dashboard)
"""
import macro_dashboard as md
import math
import statistics
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone, date
from functools import lru_cache

# NBER recession peaks (onset) + the inflation break, used for scoring.
EVENTS = {
    "2001 recession": "2001-03-01",
    "2008 recession": "2007-12-01",
    "2020 recession": "2020-02-01",
    "2022 inflation/bear": "2022-01-01",
}

# Evaluation grid: run-up -> event -> aftermath.
DATES = ["1999-06-01", "2000-01-01", "2000-07-01", "2001-03-01",
         "2006-06-01", "2007-06-01", "2007-12-01", "2008-09-01",
         "2019-06-01", "2020-01-01", "2020-03-01",
         "2021-06-01", "2021-12-01", "2022-06-01",
         "2024-01-01", "2025-06-01", "2026-01-01"]

# Signals for the recession read (leading/coincident risk gauges with long history).
RECESSION = ["T10Y3M", "SAHMREALTIME", "IC4WSA", "BAMLH0A0HYM2", "NFCI", "DRTSCILM", "WEI"]

SHOWN = {"alert": "danger", "caution": "caution", "calm": "calm", "neutral": "unscored"}

GRID_START = "1990-01-01"      # monthly grid for the meter replay
EVENT_DROP = 0.18              # an S&P decline counts at 18% or more, closing basis
TOO_EARLY_DAYS = 365           # an alarm that starts more than a year before the peak is a false alarm
OOS_START = "2011-01-01"       # events peaking before this are in-sample; from it, out-of-sample
BAND_STARTS = [(30, "Caution"), (55, "Elevated"), (75, "High")]

INDS = {}
for _grp in (md.THEMES, md.DRILLDOWNS):
    for _lst in _grp.values():
        for _ind in _lst:
            INDS[_ind["id"]] = _ind

print("Fetching full history for", len(INDS), "series ...")
SERIES, LEGS, LOAD_FAILED = {}, {}, []
for sid, ind in INDS.items():
    if ind.get("compute") == "top10":
        # No historical source exists, and its live fetch rewrites
        # docs/top10_history.json, which this read-only check must not touch.
        continue
    raw = md.fetch_raw(ind)
    if not raw:
        LOAD_FAILED.append((sid, ind.get("label", "")))
        continue
    # Transform once on the full history, then truncate per date: exactly
    # equivalent (year-over-year only ever looks backward) and far faster.
    SERIES[sid] = md.transform(ind, raw)
    LEGS[sid] = md.fetch_legs(ind)

if LOAD_FAILED:
    print("\n" + "!" * 78)
    print(f"!! {len(LOAD_FAILED)} SERIES FAILED TO LOAD -- every number below that "
          f"depends on them is wrong.")
    for sid, lbl in LOAD_FAILED:
        votes = md.ALLOC.get(sid, [])
        axes = [ax for ax, lst in (("growth", md.GROWTH_MOM),
                                   ("inflation", md.INFLATION_MOM)) if sid in lst]
        harm = []
        if axes:
            harm.append("regime " + "/".join(axes) + " axis")
        if votes:
            harm.append(f"{len(votes)} allocation vote(s)")
        if sid in RECESSION:
            harm.append("recession flag count")
        if sid in [g[0] for g in md.MARKET_CHECK_GAUGES]:
            harm.append("market check")
        print(f"!!   {sid:<24} {lbl[:34]:<36} "
              + ("-> " + "; ".join(harm) if harm else "-> display only"))
    print("!" * 78 + "\n")
else:
    print("All series loaded.\n")


def load_sp500():
    """Daily S&P 500 closes since 1985, [(date, close)]. Used only for the equity-trend
    gauge and the decline episodes; levels are never printed (licensed data)."""
    try:
        import yfinance as yf
        h = yf.Ticker("^GSPC").history(start="1985-01-01")["Close"].dropna()
        out = md.finite_only([(d.strftime("%Y-%m-%d"), float(v)) for d, v in h.items()], "S&P 500")
    except Exception as exc:
        print(f"  [sp500] unavailable ({exc})")
        return []
    if out:
        print(f"  [sp500] {len(out)} daily closes, {out[0][0]} -> {out[-1][0]}")
    else:
        print("  [sp500] empty result")
    return out


SP = load_sp500()
SP_DATES = [d for d, _ in SP]
SP_VALS = [v for _, v in SP]


def sp_close_on(d):
    """Close on the first trading day on or after d (the last one if d is later)."""
    if not SP:
        return None
    return SP_VALS[min(bisect_left(SP_DATES, d), len(SP) - 1)]


def equity_trend_at(as_of):
    """The market check's equity-trend gauge as of a past date, from the same
    function the live build uses. None if S&P data did not load."""
    if not SP:
        return None
    i = bisect_right(SP_DATES, as_of)
    return md.equity_trend_status(SP_VALS[max(0, i - 260):i])


def equity_gap_at(as_of):
    """How far the S&P 500 sits below its own 200-day average, as a share of that
    average (positive = below, negative = above). The graded form of the equity-trend
    gauge, used only as a benchmark. None before 200 closes exist."""
    if not SP:
        return None
    i = bisect_right(SP_DATES, as_of)
    vals = SP_VALS[max(0, i - 200):i]
    if len(vals) < 200:
        return None
    sma = sum(vals) / 200.0
    return (sma - vals[-1]) / sma


def _score(ind, series, legs, as_of):
    sub = [x for x in series if x[0] <= as_of]
    if len(sub) < 8:
        return None
    return md.score_series(ind, sub, {k: [x for x in v if x[0] <= as_of]
                                      for k, v in legs.items()})


@lru_cache(maxsize=None)
def eval_signal(sid, as_of):
    """The dashboard's own score for one signal, on history truncated to as_of."""
    ind, full = INDS.get(sid), SERIES.get(sid)
    if not ind or not full:
        return None
    sc = _score(ind, full, LEGS.get(sid, {}), as_of)
    if sc is None:
        return None
    return {"state": sc["state"], "det": sc["deteriorating"], "imp": sc["improving"],
            "latest": sc["latest"], "z": sc["z"], "phrase": sc["phrase"],
            "mode": sc["score_mode"], "worry": ind["worry"]}


def _hot(e):
    """Moving the risk-off way, or already at caution/danger."""
    return bool(e and (e["det"] or e["state"] in ("caution", "alert")))


@lru_cache(maxsize=None)
def decision_at(as_of):
    """The whole decision stack as of a past date, through the dashboard's own
    functions: per-signal readings -> regime votes -> Valuation/Consumer conditions ->
    allocation -> market gauges -> the three meter layers -> the meter. Cached; callers
    must not modify what it returns."""
    by_id = {}
    for sid, ind in INDS.items():
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        by_id[sid] = {"series_id": sid, "label": ind["label"], "state": e["state"],
                      "deteriorating": e["det"], "improving": e["imp"],
                      # the live page stores z to 2 decimals and the meter reads that value
                      "z": None if e["z"] is None else round(e["z"], 2)}
    themed = lambda name: [by_id[i["id"]] for g in (md.THEMES, md.DRILLDOWNS)
                           for i in g.get(name, []) if i["id"] in by_id]
    val_c = md.theme_condition(themed("Valuation"))
    cons_c = md.theme_condition(themed("Consumer"), labels=("stressed", "mixed", "healthy"))
    allocation = md.compute_allocation(by_id, val_c, cons_c)
    comps, _ = md.market_gauges(by_id)
    et = equity_trend_at(as_of)
    if et:
        comps.append(et)
    rl, ml, al = md.regime_layer(by_id), md.market_layer(by_id, comps), md.allocation_layer(allocation)
    gv, iv = md.mom_votes(by_id, md.GROWTH_MOM), md.mom_votes(by_id, md.INFLATION_MOM)
    growth, inflation = md.regime_axes(gv[0], gv[1], iv[0], iv[1])
    return {"by_id": by_id, "val": val_c, "cons": cons_c, "allocation": allocation,
            "comps": comps, "equity_trend": et, "layers": (rl, ml, al),
            "meter": md.action_meter(rl, ml, al), "votes": (gv, iv),
            "growth": growth, "inflation": inflation,
            "regime": md.REGIMES[(growth, inflation)][0]}


def regime_at(as_of):
    d = decision_at(as_of)
    (gw, gb, _, _), (iw, ib, _, _) = d["votes"]
    return d["regime"], d["growth"][:5], d["inflation"][:5], gw, gb, iw, ib


def recession_flags(as_of):
    hot, tot = [], 0
    for sid in RECESSION:
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        tot += 1
        if _hot(e):
            hot.append(sid)
    return hot, tot


def valuation_at(as_of):
    e = eval_signal("Shiller CAPE", as_of) or eval_signal("Market cap / GDP", as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def concentration_at(as_of):
    e = eval_signal("SPY/RSP", as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def issuance_at(sid, as_of):
    e = eval_signal(sid, as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def _months(a, b):
    out, d = [], date.fromisoformat(a)
    while d <= date.fromisoformat(b):
        out.append(d.isoformat())
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _shift(d, days):
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
#  S&P 500 declines of 18%+, and how the UNTUNED meter does against them
# ---------------------------------------------------------------------------
EVENT_RALLY = 0.30             # a decline episode ends when the index rallies this much off its low
THIN_GROWTH = 15               # fewer growth signals than this at a date = thin evidence
FWD_HORIZONS = (3, 6, 12)      # months ahead for the forward-return tables


def sp500_events(closes, drop=EVENT_DROP):
    """Declines of `drop` or more, closing basis, measured from the running all-time
    closing high. An episode starts at that high and ends at its lowest close before
    the index makes a new high. Kept only to show how much the event list depends on
    the definition: it does not see a decline from a LOCAL high (e.g. 2011).
    closes: [(date, close)] oldest first."""
    events, peak_i, trough_i, in_ep = [], 0, 0, False
    for i in range(1, len(closes)):
        v = closes[i][1]
        if v > closes[peak_i][1]:
            if in_ep:
                events.append((peak_i, trough_i, False))
                in_ep = False
            peak_i = trough_i = i
            continue
        if v < closes[trough_i][1]:
            trough_i = i
        if not in_ep and v <= closes[peak_i][1] * (1 - drop):
            in_ep = True
    if in_ep:
        events.append((peak_i, trough_i, True))
    return [{"peak": closes[p][0], "trough": closes[t][0],
             "depth": 1 - closes[t][1] / closes[p][1], "ongoing": og}
            for p, t, og in events]


def sp500_events_zigzag(closes, drop=EVENT_DROP, rally=EVENT_RALLY):
    """Declines of `drop` or more, closing basis, from a LOCAL high. The peak is the
    highest close since the previous episode ended; an episode starts once the close is
    `drop` below it, and ends -- at its lowest close -- when the index either rallies
    `rally` off that low or closes above the peak. Unlike the all-time-high version it
    sees a decline from a high that never got back to a record (2011). `rally` is the
    one judgement call: too low splits a long bear market at its interim rallies, so the
    report prints the event list at several values -- if it changes between neighbours
    the definition is fragile. closes: [(date, close)] oldest first."""
    events, peak_i, trough_i = [], 0, None
    for i in range(1, len(closes)):
        v = closes[i][1]
        if trough_i is None:
            if v > closes[peak_i][1]:
                peak_i = i
            elif v <= closes[peak_i][1] * (1 - drop):
                trough_i = i
        else:
            if v < closes[trough_i][1]:
                trough_i = i
            if v > closes[peak_i][1] or v >= closes[trough_i][1] * (1 + rally):
                events.append((peak_i, trough_i, False))
                trough_i, peak_i = None, i
    if trough_i is not None:
        events.append((peak_i, trough_i, True))
    return [{"peak": closes[p][0], "trough": closes[t][0],
             "depth": 1 - closes[t][1] / closes[p][1], "ongoing": og}
            for p, t, og in events]


def runs_at_or_above(series, thr):
    """Maximal runs of consecutive grid months with score >= thr: [(first, last)]."""
    runs, start, prev = [], None, None
    for m, s in series:
        if s >= thr:
            start = start or m
            prev = m
        elif start is not None:
            runs.append((start, prev))
            start = None
    if start is not None:
        runs.append((start, prev))
    return runs


def score_baseline(events, series, thr):
    """Score 'the meter at or above thr' against the events. A run of alarm months is
    GOOD for an event if it overlaps [peak - 1 year, trough] and starts no more than a
    year before the peak; TOO EARLY if it overlaps but started sooner (a false alarm);
    FALSE if it overlaps no event window. Per event: the first GOOD run, its lead over
    the peak, and the decline still ahead from its start to the trough."""
    runs = runs_at_or_above(series, thr)
    per_event, run_class = [], {r: "FALSE" for r in runs}
    for ev in events:
        ws = _shift(ev["peak"], -TOO_EARLY_DAYS)
        good = early = None
        for r in runs:
            a, b = r
            if a > ev["trough"] or b < ws:
                continue
            if a >= ws:
                good = good or r
                run_class[r] = "GOOD"
            else:
                early = early or r
                if run_class[r] == "FALSE":
                    run_class[r] = "TOO EARLY"
        rec = {"ev": ev, "good": good, "early": early}
        if good:
            c0, c1 = sp_close_on(good[0]), sp_close_on(ev["trough"])
            rec["lead_months"] = _days(good[0], ev["peak"]) / 30.44
            rec["ahead"] = (c1 / c0 - 1) if c0 and c1 else None
        per_event.append(rec)
    return runs, run_class, per_event


def _med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def forward_returns(closes, h):
    """Return from each month's close to the close h months later (None near the end)."""
    n = len(closes)
    return [closes[i + h] / closes[i] - 1 if i + h < n and closes[i] and closes[i + h] else None
            for i in range(n)]


def circular_shift_p(alarm, ret):
    """Is the average return in alarm months lower than chance? Slide the alarm pattern
    against the returns through every circular alignment (so clusters of consecutive
    alarm months stay intact, which a plain shuffle would destroy) and ask what share of
    alignments give an alarm-month average as low as the real one (alignment 0).
    alarm: [bool] per month; ret: [float|None] per month (None = not yet known).
    Exact and deterministic: no random numbers."""
    n = len(alarm)
    valid = [i for i in range(n) if ret[i] is not None]
    if not valid:
        return None
    base = sum(ret[i] for i in valid) / len(valid)

    def stat(k):
        sel = [ret[i] for i in valid if alarm[(i - k) % n]]
        return (sum(sel) / len(sel) - base) if sel else None

    obs = stat(0)
    if obs is None:
        return None
    dist = [s for s in (stat(k) for k in range(n)) if s is not None]
    return {"alarm_mean": obs + base, "base": base, "p": sum(1 for s in dist if s <= obs + 1e-12) / len(dist),
            "n_alignments": len(dist)}


def quantile_threshold(scores, frac):
    """The score line that alarms on about `frac` of months: the k-th largest score,
    k = ceil(frac * n). Ties can push the alarm share a little above frac."""
    s = sorted(scores, reverse=True)
    return s[max(1, math.ceil(frac * len(s))) - 1]


def window_months(events, months):
    """Months inside any decline window (a year before the peak through the trough)."""
    return {m for m in months if any(_shift(e["peak"], -TOO_EARLY_DAYS) <= m <= e["trough"] for e in events)}


def _inflation_layer(by_id):
    """EXPERIMENT, not shipped: the regime layer's own formula run on the inflation
    signals instead of the growth ones."""
    saved = md.GROWTH_MOM
    md.GROWTH_MOM = md.INFLATION_MOM
    try:
        return md.regime_layer(by_id)
    finally:
        md.GROWTH_MOM = saved


def _layer_rows(months):
    """Each month's three layer scores plus the experimental variants' ingredients."""
    rows = []
    for m in months:
        d = decision_at(m)
        rl, ml, al = d["layers"]
        if rl is None or ml is None or al is None:
            continue
        by_id, comps = d["by_id"], d["comps"]
        fs = by_id.get("CP minus T-bill")
        ml_f = (md.market_layer(by_id, comps + [{"label": "Funding stress", "sid": "CP minus T-bill",
                                                 "hot": md.market_gauge_status(fs, "widening")[1]}])
                if fs else ml) or ml
        ml_n = md.market_layer(by_id, [c for c in comps if c.get("sid") != "SP500"]) or ml
        ri = _inflation_layer(by_id)
        et = d["equity_trend"]
        rows.append({"m": m, "R": rl["score"], "M": ml["score"], "A": al["score"], "Mf": ml_f["score"],
                     "Mn": ml_n["score"], "Ri": ri["score"] if ri else 0.0,
                     "T": 100.0 if (et and et["hot"]) else 0.0, "G": equity_gap_at(m)})
    return rows


VARIANTS = [
    ("regime only", lambda r: r["R"]),
    ("market only", lambda r: r["M"]),
    ("allocation only", lambda r: r["A"]),
    ("regime + market", lambda r: (r["R"] + r["M"]) / 2),
    ("regime + allocation", lambda r: (r["R"] + r["A"]) / 2),
    ("market + allocation", lambda r: (r["M"] + r["A"]) / 2),
    ("all three (as shipped)", lambda r: (r["R"] + r["M"] + r["A"]) / 3),
    ("all three + funding-stress gauge", lambda r: (r["R"] + r["Mf"] + r["A"]) / 3),
    ("all three, market w/o S&P trend", lambda r: (r["R"] + r["Mn"] + r["A"]) / 3),
    ("all three, regime counts inflation", lambda r: (max(r["R"], r["Ri"]) + r["M"] + r["A"]) / 3),
]


def _print_occupancy(series):
    n = len(series)
    print("\n-- 1. Where the meter sits, and how often each layer is 'on' (50 or more) --\n")
    cols = [("all months", lambda m: True), ("before 2011", lambda m: m < OOS_START),
            ("2011 onward", lambda m: m >= OOS_START)]
    print(f"   {'':<26}" + "".join(f"{c:<22}" for c, _ in cols))
    for lbl, key in (("Clear (under 30)", lambda s: s < 30), ("Caution (30-54)", lambda s: 30 <= s < 55),
                     ("Elevated (55-74)", lambda s: 55 <= s < 75), ("High (75+)", lambda s: s >= 75)):
        cells = []
        for _, sel in cols:
            ss = [s for m, s in series if sel(m)]
            k = sum(1 for s in ss if key(s))
            cells.append(f"{k}/{len(ss)} = {100 * k / max(1, len(ss)):.0f}%")
        print(f"   {lbl:<26}" + "".join(f"{c:<22}" for c in cells))
    for i, name in enumerate(("Regime layer", "Market layer", "Allocation layer")):
        cells = []
        for _, sel in cols:
            vals = [(decision_at(m)["layers"][i] or {}).get("score") for m, _ in series if sel(m)]
            vals = [v for v in vals if v is not None]
            k = sum(1 for v in vals if v >= 50)
            cells.append(f"{k}/{len(vals)} = {100 * k / max(1, len(vals)):.0f}%")
        print(f"   {name + ' >= 50':<26}" + "".join(f"{c:<22}" for c in cells))


def _print_events(events, series, n, in_win):
    months = [m for m, _ in series]
    print(f"\n-- 2. S&P 500 declines of 18% or more since {GRID_START[:4]}, from a local closing high --")
    print(f"   An episode ends at its low when the index rallies {EVENT_RALLY:.0%} off it or closes above the peak.")
    print("   How the event list depends on that choice (peak years):")
    for r in (0.20, 0.25, 0.30, 0.35, 0.40):
        ev = [e["peak"][:4] for e in sp500_events_zigzag(SP, rally=r) if e["peak"] >= GRID_START]
        print(f"     rally {r:.0%}: {len(ev)} events: {' '.join(ev)}")
    ath = [e["peak"][:4] for e in sp500_events(SP) if e["peak"] >= GRID_START]
    print(f"     all-time-high anchored (no local peaks): {len(ath)} events: {' '.join(ath)}")
    print("   (index levels are never printed; * = fewer than "
          f"{THIN_GROWTH} growth signals existed then, so the reading is thin)\n")
    print(f"   {'peak':<12}{'trough':<12}{'decline':>8}  {'sample':<8}{'growth sigs':>12}   "
          f"meter at: -12m   -6m   -3m  peak  +3m trough")
    meter_at = lambda d: next((s for m, s in reversed(series) if m <= d), None)
    for ev in events:
        nsig = (decision_at(max([m for m in months if m <= ev["peak"]] or [months[0]]))["layers"][0] or {}).get("n", 0)
        offs = [meter_at(_shift(ev["peak"], k)) for k in (-365, -183, -91, 0, 91)] + [meter_at(ev["trough"])]
        print(f"   {ev['peak']:<12}{ev['trough']:<12}{-100 * ev['depth']:>7.0f}%  "
              f"{'in' if ev['peak'] < OOS_START else 'OUT':<8}{str(nsig) + ('*' if nsig < THIN_GROWTH else ''):>12}   "
              + "".join(f"{('-' if o is None else o):>6}" for o in offs)
              + ("   (ongoing)" if ev["ongoing"] else ""))
    print(f"\n   Base rate: {len(in_win)} of {n} months ({100 * len(in_win) / n:.0f}%) fall inside a decline "
          f"window (a year before the peak through the trough), so a random alarm is 'right' that often.")


def _print_baseline(events, series, n, in_win):
    print("\n-- 3. Baseline score of the UNTUNED meter (first-principles settings, nothing fitted) --")
    print("   Run = consecutive months at or above the line. GOOD = overlaps a decline window and starts")
    print("   within a year of the peak; TOO EARLY = overlaps but started sooner (counts as a false alarm);")
    print("   FALSE = overlaps no window. 'ahead' < 0 means that much decline was still to come.")
    for thr, name in BAND_STARTS:
        runs, cls, per_event = score_baseline(events, series, thr)
        print(f"\n   Meter >= {thr} ({name})")
        for rec in per_event:
            ev = rec["ev"]
            tag = "in " if ev["peak"] < OOS_START else "OUT"
            if rec["good"]:
                lead = rec["lead_months"]
                when = (f"{lead:.0f} mo before the peak" if lead >= 0.5 else
                        f"{-lead:.0f} mo after the peak" if lead <= -0.5 else "at the peak")
                ahead = f"{100 * rec['ahead']:.0f}% still ahead" if rec["ahead"] is not None else "n/a"
                print(f"     [{tag}] {ev['peak']} ({-100 * ev['depth']:.0f}%): first alarm {rec['good'][0]}, {when}; {ahead}")
            elif rec["early"]:
                print(f"     [{tag}] {ev['peak']} ({-100 * ev['depth']:.0f}%): on since {rec['early'][0]} -- "
                      f"{_days(rec['early'][0], ev['peak']) / 30.44:.0f} mo before the peak, too early")
            else:
                print(f"     [{tag}] {ev['peak']} ({-100 * ev['depth']:.0f}%): MISSED -- never at or above {thr} in the window")
        for lbl, sel in (("before 2011", lambda e: e["peak"] < OOS_START), ("2011 onward", lambda e: e["peak"] >= OOS_START)):
            evs = [r for r in per_event if sel(r["ev"])]
            caught = [r for r in evs if r["good"]]
            rr = [r for r in runs if (r[0] < OOS_START) == (lbl == "before 2011")]
            kinds = [cls[r] for r in rr]
            lead_m = _med([r["lead_months"] for r in caught])
            ahead_m = _med([r["ahead"] for r in caught])
            print(f"     {lbl:<12} events caught {len(caught)}/{len(evs)}"
                  f" | median lead {('n/a' if lead_m is None else f'{lead_m:+.1f} mo')}"
                  f" | median still ahead {('n/a' if ahead_m is None else f'{100 * ahead_m:.0f}%')}"
                  f" | alarm runs: {kinds.count('GOOD')} good, {kinds.count('TOO EARLY')} too early, {kinds.count('FALSE')} false")
        alarm = [m for m, s in series if s >= thr]
        false_m = [m for m in alarm if m not in in_win]
        print(f"     months in alarm: {len(alarm)}/{n} ({100 * len(alarm) / n:.0f}%); of those, outside every decline "
              f"window (false alarm): {len(false_m)} ({100 * len(false_m) / max(1, len(alarm)):.0f}%)")


def _print_forward(series, closes):
    months, scores, n = [m for m, _ in series], [s for _, s in series], len(series)
    print("\n-- 4. What the S&P 500 did AFTER each reading (price index; dividends ignored) --")
    print("   Average return over the next 3, 6 and 12 months, and how often it was negative, by the")
    print("   meter's band in the starting month ('Elevated+High' = 55 and above). Windows overlap, so")
    print("   read the pattern, not the decimals.\n")
    bands = [("Clear (under 30)", lambda s: s < 30), ("Caution (30-54)", lambda s: 30 <= s < 55),
             ("Elevated+High (55+)", lambda s: s >= 55), ("every month", lambda s: True)]
    samples = [("all months", lambda m: True), ("before 2011", lambda m: m < OOS_START),
               ("2011 onward", lambda m: m >= OOS_START)]
    print(f"   {'':<24}" + "".join(f"{c:<32}" for c, _ in samples))
    for h in FWD_HORIZONS:
        ret = forward_returns(closes, h)
        print(f"   {h} months ahead")
        for bl, bsel in bands:
            cells = []
            for _, ssel in samples:
                v = [ret[i] for i in range(n) if ret[i] is not None and bsel(scores[i]) and ssel(months[i])]
                cells.append(f"{100 * _mean(v):+.1f}%  neg {100 * sum(1 for x in v if x < 0) / len(v):.0f}%  n={len(v)}"
                             if v else "-")
            print(f"     {bl:<22}" + "".join(f"{c:<32}" for c in cells))
    print("\n   Is the gap bigger than chance? Exact circular-shift test: the alarm months are slid against the")
    print("   returns through every possible alignment (which keeps clusters of alarms intact) and p is the")
    print("   share of alignments whose alarm-month average return is as low or lower than the real one.")
    print("   Only about nine separate declines sit behind all of this, so a small p is suggestive, not proof.")
    for thr in (30, 55):
        alarm = [s >= thr for s in scores]
        for h in (3, 12):
            r = circular_shift_p(alarm, forward_returns(closes, h))
            if r:
                print(f"     meter >= {thr}, {h}m ahead: alarm months {100 * r['alarm_mean']:+.1f}% "
                      f"vs every month {100 * r['base']:+.1f}%  ->  p = {r['p']:.2f}")
    print("\n   Exploratory only (chosen after seeing the numbers above, so NOT a test): when the meter is")
    print("   55 or more, is it higher or lower than three months earlier?")
    r6, r12 = forward_returns(closes, 6), forward_returns(closes, 12)
    for lbl, sel in (("rising", lambda i: scores[i] > scores[i - 3]), ("falling", lambda i: scores[i] < scores[i - 3]),
                     ("unchanged", lambda i: scores[i] == scores[i - 3])):
        idx = [i for i in range(3, n) if scores[i] >= 55 and sel(i)]
        a6, a12 = [r6[i] for i in idx if r6[i] is not None], [r12[i] for i in idx if r12[i] is not None]
        print(f"     {lbl:<10} n={len(idx):<4}"
              + (f" next 6m {100 * _mean(a6):+.1f}%   next 12m {100 * _mean(a12):+.1f}%" if a6 and a12 else ""))


def _print_ablation(series, events, in_win, closes):
    months, n = [m for m, _ in series], len(series)
    rows = _layer_rows(months)
    if len(rows) != n:
        print("\n-- 5. Layer ablation skipped: a layer was missing in some months --")
        return
    frac = sum(1 for _, s in series if s >= 55) / n
    ret12 = forward_returns(closes, 12)
    n_in = sum(1 for e in events if e["peak"] < OOS_START)
    n_out = len(events) - n_in
    print("\n-- 5. Which parts carry information? Every variant alarms on the same share of months --")
    print(f"   Each variant's alarm line is set so it alarms on about {100 * frac:.0f}% of months (the shipped")
    print("   meter's share at 55+), so they are compared on discrimination, not on how often they speak.")
    print("   'in window' = share of alarm months inside a decline window (a random month: "
          f"{100 * len(in_win) / n:.0f}%).")
    print("   'caught' = declines with a GOOD alarm run (before 2011 + 2011 on).")
    print("   Variants marked + / w/o / counts are experiments, not shipped. With about nine declines, one")
    print("   event either way is noise, and about 15 comparisons are shown here and in section 4, so a few")
    print("   p-values of 0.03-0.05 are expected by chance alone.\n")
    print(f"   {'variant':<38}{'alarm':>6}{'in window':>11}{'caught':>14}{'med lead':>10}   next-12m return in alarm months (p)")

    def row(name, vs, thr):
        runs, cls, per_event = score_baseline(events, vs, thr)
        alarm_m = [m for m, s in vs if s >= thr]
        inw = sum(1 for m in alarm_m if m in in_win)
        ci = sum(1 for p in per_event if p["good"] and p["ev"]["peak"] < OOS_START)
        co = sum(1 for p in per_event if p["good"] and p["ev"]["peak"] >= OOS_START)
        lead = _med([p["lead_months"] for p in per_event if p["good"]])
        r = circular_shift_p([s >= thr for _, s in vs], ret12)
        print(f"   {name:<38}{100 * len(alarm_m) / n:>5.0f}%{100 * inw / max(1, len(alarm_m)):>10.0f}%"
              f"{f'{ci}/{n_in} + {co}/{n_out}':>14}{('n/a' if lead is None else f'{lead:+.1f} mo'):>10}"
              + (f"   {100 * r['alarm_mean']:+.1f}%  (p = {r['p']:.2f})" if r else ""))

    for name, fn in VARIANTS:
        vs = [(r["m"], fn(r)) for r in rows]
        row(name, vs, quantile_threshold([s for _, s in vs], frac))
    gaps = [(r["m"], r["G"]) for r in rows]
    if all(g is not None for _, g in gaps):
        row("benchmark: S&P furthest below 200-day", gaps, quantile_threshold([g for _, g in gaps], frac))
    row("benchmark: S&P below 200-day (on/off)", [(r["m"], r["T"]) for r in rows], 100.0)
    print(f"   {'every month (base rate)':<38}{'':>6}{100 * len(in_win) / n:>10.0f}%{'':>14}{'':>10}   "
          f"{100 * _mean(ret12):+.1f}%")


def _print_false_alarms(events, series, closes):
    months = [m for m, _ in series]
    idx = {m: i for i, m in enumerate(months)}
    ret12 = forward_returns(closes, 12)
    print("\n-- 6. The alarm runs that were not GOOD, listed --")
    for thr in (55, 30):
        runs, cls, _ = score_baseline(events, series, thr)
        bad = [(r, cls[r]) for r in runs if cls[r] != "GOOD"]
        print(f"\n   Meter >= {thr}: {len(bad)} of {len(runs)} alarm runs")
        for (a, b), k in bad:
            r12 = ret12[idx[a]]
            print(f"     {k:<10} {a[:7]} -> {b[:7]} ({len(_months(a, b))} mo); S&P over the 12 months from the start: "
                  + ("n/a" if r12 is None else f"{100 * r12:+.0f}%"))


def _print_today(today):
    print("\n-- 7. Today --\n")
    d = decision_at(today)
    m = d["meter"]
    if m:
        print(f"   meter {m['score']} -> {m['band']}  (regime {m['regime']}, market {m['market']}, "
              f"allocation {m['allocation']}; {m['layers_used']} of 3 layers)")
    print(f"   regime {d['regime']} | valuation condition {d['val']['condition']} | "
          f"market gauges hot: {sum(1 for c in d['comps'] if c['hot'])} of {len(d['comps'])}")


def meter_report():
    print("\n" + "=" * 100)
    print("ACTION METER -- the dashboard's own meter, replayed monthly, UNTUNED  (Step 3a diagnostics)")
    print("=" * 100)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    series, layers_used = [], []
    for m in _months(GRID_START, today[:8] + "01"):
        d = decision_at(m)
        if d["meter"]:
            series.append((m, d["meter"]["score"]))
            layers_used.append(d["meter"]["layers_used"])
    if not series:
        print("  (no meter readings -- see the failure list at the top)")
        return
    n = len(series)
    months = [m for m, _ in series]
    print(f"\n  {n} monthly readings, {months[0]} -> {months[-1]}.  Layers available: "
          f"{statistics.mean(layers_used):.1f} of 3 on average.")
    print("  Market layer uses: " + ("FRED gauges plus the S&P 200-day trend. " if SP else
          "FRED gauges only -- S&P 500 data did not load, so the equity-trend gauge is missing. ")
          + "The HY-spread gauge exists on FRED only from 2023.")
    print("  Constants: equal layer weights; regime saturates at 50% of growth signals net-worsening and "
          f"mean z {md.R_FULL_Z}; market at z {md.M_FULL_Z}; allocation at {md.A_FULL_CONVICTION:.0f}% conviction.")
    print("  Nothing here has been tuned on any outcome. Monthly figures are dated on the 1st but cover the")
    print("  whole month and are published later, so every lead time below is flattered (see the file header).")
    _print_occupancy(series)
    if not SP:
        print("\n-- 2-6. S&P 500 declines, baseline score, forward returns, ablation: SKIPPED (S&P 500 data did not load) --")
    else:
        closes = [sp_close_on(m) for m in months]
        events = [e for e in sp500_events_zigzag(SP) if e["peak"] >= GRID_START]
        in_win = window_months(events, months)
        _print_events(events, series, n, in_win)
        _print_baseline(events, series, n, in_win)
        _print_forward(series, closes)
        _print_ablation(series, events, in_win, closes)
        _print_false_alarms(events, series, closes)
    _print_today(today)


# ---------------------------------------------------------------------------
#  Step 3b: do the nine allocation LEANS point the right way?
# ---------------------------------------------------------------------------
# The dashboard's actual recommendation is the nine leans (Overweight / Underweight),
# and until now nothing tested them. Each bucket is judged by the forward return of a
# tradeable proxy against a benchmark, both FIXED HERE IN ADVANCE: 12 months ahead is
# the primary test; 3 and 6 are shown for context. A lean has skill if the proxy beats
# its benchmark by more after Overweight months than after Underweight ones.
# Proxies are total-return (adjusted) prices from yfinance, used only to compute these
# statistics -- never published. Each "leg" lists tickers to try in order (a long-history
# fund first, a shorter ETF as fallback); a basket is the equal-weight average of its legs.
LEAN_PROXIES = {
    "Overall equity exposure":   {"legs": [["SPY"]], "bench": "cash"},
    "Long-duration Treasuries":  {"legs": [["VUSTX", "TLT"]], "bench": "cash"},
    "High-yield credit":         {"legs": [["VWEHX", "HYG"]], "bench": "cash"},
    "Gold":                      {"legs": [["GLD", "GC=F"]], "bench": "cash"},
    "Real assets & commodities": {"legs": [["DBC", "GSG"], ["VNQ"], ["TIP"]], "bench": "cash"},
    "Energy":                    {"legs": [["XLE"]], "bench": "spy"},
    "Defensive equities":        {"legs": [["XLP"], ["XLU"], ["XLV"]], "bench": "spy"},
    "Cyclicals & small caps":    {"legs": [["IWM"], ["XLI"], ["XLB"], ["XLY"]], "bench": "spy"},
    "Value over Growth":         {"legs": [["IWD"]], "bench": ["IWF"]},
}
LEAN_MOMENTS = [("2000-03-01", "Mar 2000 top"), ("2007-10-01", "Oct 2007 top"), ("2009-03-01", "Mar 2009 low"),
                ("2020-02-01", "Feb 2020 top"), ("2022-01-01", "Jan 2022 top")]
LEAN_HORIZONS = (3, 6, 12)     # months; 12 is the primary test


def month_closes(series, months):
    """Close on the first trading day on or after each month start. None before the
    series begins (more than 10 days ahead of its first observation) or after it ends."""
    dates, vals = [d for d, _ in series], [v for _, v in series]
    out = []
    for m in months:
        i = bisect_left(dates, m)
        if i >= len(dates) or (i == 0 and dates[0] > _shift(m, 10)):
            out.append(None)
        else:
            out.append(vals[i])
    return out


def basket_forward(leg_closes, h):
    """Equal-weight average of each leg's h-month return from each month; None unless
    every leg has both end points."""
    n, out = len(leg_closes[0]), []
    for i in range(n):
        rs = []
        for c in leg_closes:
            if i + h >= n or c[i] is None or c[i + h] is None or c[i] <= 0:
                rs = None
                break
            rs.append(c[i + h] / c[i] - 1)
        out.append(None if not rs else sum(rs) / len(rs))
    return out


def cash_forward(yields, h):
    """h-month cash return from the 3-month T-bill yield (percent) at each month start."""
    return [None if y is None else y / 100.0 * h / 12.0 for y in yields]


def excess(a, b):
    return [None if x is None or y is None else x - y for x, y in zip(a, b)]


def circular_shift_spread(signs, ret):
    """Do Overweight months precede better returns than Underweight months? signs: +1 /
    -1 / 0 per month (0 = no lean); ret: forward excess return per month (None = not
    known). Spread = mean(ret after +1) - mean(ret after -1). The lean pattern is slid
    against the returns through every circular alignment (clusters of the same lean
    stay intact) and p = share of alignments whose spread is at least as high as the
    real one. Exact and deterministic."""
    n = len(signs)
    valid = [i for i in range(n) if ret[i] is not None]

    def parts(k):
        up = [ret[i] for i in valid if signs[(i - k) % n] == 1]
        dn = [ret[i] for i in valid if signs[(i - k) % n] == -1]
        return up, dn

    up0, dn0 = parts(0)
    if not up0 or not dn0:
        return None
    obs = sum(up0) / len(up0) - sum(dn0) / len(dn0)
    dist = []
    for k in range(n):
        u, d = parts(k)
        if u and d:
            dist.append(sum(u) / len(u) - sum(d) / len(d))
    return {"spread": obs, "p": sum(1 for s in dist if s >= obs - 1e-12) / len(dist),
            "n_ow": len(up0), "n_uw": len(dn0), "mean_ow": sum(up0) / len(up0),
            "mean_uw": sum(dn0) / len(dn0), "hit_ow": sum(1 for r in up0 if r > 0) / len(up0),
            "hit_uw": sum(1 for r in dn0 if r < 0) / len(dn0)}


def _spread_in(signs, ret, months, sel):
    up = [ret[i] for i in range(len(signs)) if ret[i] is not None and signs[i] == 1 and sel(months[i])]
    dn = [ret[i] for i in range(len(signs)) if ret[i] is not None and signs[i] == -1 and sel(months[i])]
    return (sum(up) / len(up) - sum(dn) / len(dn), len(up), len(dn)) if up and dn else None


def load_lean_proxies():
    """Fetch every ticker the proxy table names. Returns {ticker: [(date, close)]}; a
    ticker that fails is simply absent (and the bucket is skipped, loudly)."""
    out = {}
    want = []
    for spec in LEAN_PROXIES.values():
        for leg in spec["legs"]:
            want += leg
        if isinstance(spec["bench"], list):
            want += spec["bench"]
    want += ["SPY"]
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"  [proxy] yfinance unavailable ({exc})")
        return out
    for t in dict.fromkeys(want):
        try:
            h = yf.Ticker(t).history(start="1985-01-01", auto_adjust=True)["Close"].dropna()
            ser = md.finite_only([(d.strftime("%Y-%m-%d"), float(v)) for d, v in h.items()], t)
            if len(ser) > 250:
                out[t] = ser
                print(f"  [proxy] {t:<7} {len(ser):>5} closes, {ser[0][0]} -> {ser[-1][0]}")
            else:
                print(f"  [proxy] {t:<7} too little history ({len(ser)} closes) -- not used")
        except Exception as exc:
            print(f"  [proxy] {t:<7} unavailable ({str(exc)[:60]})")
    return out


def _lean_code(a):
    lean = {"Overweight": "OW", "Underweight": "UW", "Balanced": "bal", "No signal": "--"}[a["lean"]]
    conv = {"strong": " S", "moderate": " M", "slight": " w", "none": ""}.get(a["conviction"], "")
    return lean + (conv if lean in ("OW", "UW") else "")


def _lean_sign(a, min_conviction=False):
    if a["lean"] not in ("Overweight", "Underweight"):
        return 0
    if min_conviction and a["conviction"] not in ("strong", "moderate"):
        return 0
    return 1 if a["lean"] == "Overweight" else -1


def allocation_report():
    print("\n" + "=" * 100)
    print("ALLOCATION LEANS (Step 3b) -- do the nine Overweight / Underweight calls point the right way?")
    print("=" * 100)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    months = _months(GRID_START, today[:8] + "01")
    alloc = {}
    for m in months:
        alloc[m] = {a["bucket"]: a for a in decision_at(m)["allocation"]}
    buckets = list(LEAN_PROXIES)
    print("  Nothing here is tuned. Revised data and month-start dating flatter it (see the file header).")

    print("\n-- A. The nine leans at five known moments (S strong, M moderate, w slight conviction) --\n")
    print(f"   {'bucket':<27}" + "".join(f"{lbl:<15}" for _, lbl in LEAN_MOMENTS))
    for b in buckets:
        cells = [(_lean_code(alloc[m][b]) if m in alloc and b in alloc[m] else "n/a") for m, _ in LEAN_MOMENTS]
        print(f"   {b:<27}" + "".join(f"{c:<15}" for c in cells))

    print("\n-- B. How the leans behave over time, monthly 1995 on --\n")
    print("   Flips = a change from Overweight to Underweight or back. A lean that flips every few months")
    print("   is noise to act on; one stuck on a side for years is a standing opinion, not a signal.\n")
    print(f"   {'bucket':<27}{'Overweight':>11}{'Underweight':>13}{'neither':>9}{'flips/yr':>10}{'avg run (mo)':>14}")
    ms95 = [m for m in months if m >= "1995-01-01"]
    for b in buckets:
        s = [_lean_sign(alloc[m][b]) for m in ms95]
        nz = [x for x in s if x != 0]
        flips = sum(1 for a_, b_ in zip(nz, nz[1:]) if a_ != b_)
        runs = flips + 1 if nz else 0
        print(f"   {b:<27}{100 * s.count(1) / len(s):>10.0f}%{100 * s.count(-1) / len(s):>12.0f}%"
              f"{100 * s.count(0) / len(s):>8.0f}%{flips / (len(ms95) / 12):>10.1f}"
              f"{(len(nz) / runs if runs else 0):>14.1f}")

    print("\n-- C. The skill test: what the bucket's proxy earned AFTER each lean --")
    print("   Excess return of the proxy over its benchmark (cash for asset classes, the S&P 500 for stock")
    print("   sub-groups, growth for value). 'spread' = average after Overweight minus average after")
    print("   Underweight: positive means the leans pointed the right way. p = exact circular-shift test (the")
    print("   share of alignments of the lean pattern with at least that spread); 'hit' = how often the call")
    print("   was right (proxy beat its benchmark after Overweight; fell short after Underweight).\n")
    px = load_lean_proxies()
    if "SPY" not in px:
        print("  (SPY did not load: every benchmark depends on it -- section C skipped)")
        return
    cash_raw = md.fetch("DTB3", "1985-01-01")
    if not cash_raw:
        print("  (the T-bill yield did not load from FRED: buckets judged against cash will be skipped)")
    cdates = [d for d, _ in cash_raw]
    yields = []
    for m in months:
        i = bisect_right(cdates, m) - 1
        yields.append(cash_raw[i][1] if i >= 0 and m >= cdates[0] else None)
    cl = {t: month_closes(s, months) for t, s in px.items()}
    spy_f = {h: basket_forward([cl["SPY"]], h) for h in LEAN_HORIZONS}
    cash_f = {h: cash_forward(yields, h) for h in LEAN_HORIZONS}
    results = {}
    print(f"   {'bucket (12 months ahead)':<27}{'proxy history':<22}{'OW n':>5}{'UW n':>6}{'after OW':>10}{'after UW':>10}"
          f"{'spread':>9}{'p':>6}{'hit OW':>8}{'hit UW':>8}  {'before 2011':>12}{'2011 on':>10}")
    for b in buckets:
        spec = LEAN_PROXIES[b]
        legs, names = [], []
        for leg in spec["legs"]:
            tk = next((t for t in leg if t in cl), None)
            if tk is None:
                legs = None
                break
            legs.append(cl[tk]); names.append(tk)
        if not legs:
            print(f"   {b:<27}(proxy did not load -- skipped)")
            continue
        bench_names = []
        if spec["bench"] == "cash":
            bench = {h: cash_f[h] for h in LEAN_HORIZONS}
        elif spec["bench"] == "spy":
            bench = spy_f
        else:
            if not all(t in cl for t in spec["bench"]):
                print(f"   {b:<27}(benchmark did not load -- skipped)")
                continue
            bench = {h: basket_forward([cl[t] for t in spec["bench"]], h) for h in LEAN_HORIZONS}
        proxy_f = {h: basket_forward(legs, h) for h in LEAN_HORIZONS}
        ex = {h: excess(proxy_f[h], bench[h]) for h in LEAN_HORIZONS}
        signs = [_lean_sign(alloc[m][b]) for m in months]
        signs_c = [_lean_sign(alloc[m][b], True) for m in months]
        first = next((m for m, c in zip(months, [all(c[i] is not None for c in legs) for i in range(len(months))]) if c), "?")
        r = circular_shift_spread(signs, ex[12])
        results[b] = {h: circular_shift_spread(signs, ex[h]) for h in LEAN_HORIZONS}
        results[b]["conv"] = circular_shift_spread(signs_c, ex[12])
        if r is None:
            print(f"   {b:<27}{('+'.join(names) + ' ' + first[:7]):<22}(not enough Overweight and Underweight months)")
            continue
        a = _spread_in(signs, ex[12], months, lambda m: m < OOS_START)
        c = _spread_in(signs, ex[12], months, lambda m: m >= OOS_START)
        print(f"   {b:<27}{('+'.join(names) + ' ' + first[:7]):<22}{r['n_ow']:>5}{r['n_uw']:>6}"
              f"{100 * r['mean_ow']:>+9.1f}%{100 * r['mean_uw']:>+9.1f}%{100 * r['spread']:>+8.1f}%{r['p']:>6.2f}"
              f"{100 * r['hit_ow']:>7.0f}%{100 * r['hit_uw']:>7.0f}%  "
              f"{('n/a' if a is None else f'{100 * a[0]:+.1f}%'):>12}{('n/a' if c is None else f'{100 * c[0]:+.1f}%'):>10}")

    done = {b: v for b, v in results.items() if v.get(12)}
    if done:
        pos = [b for b, v in done.items() if v[12]["spread"] > 0]
        sig = [b for b, v in done.items() if v[12]["spread"] > 0 and v[12]["p"] < 0.10]
        wrong = [b for b, v in done.items() if v[12]["spread"] < 0 and v[12]["p"] > 0.90]
        print(f"\n   Right sign (Overweight months did better than Underweight) in {len(pos)} of {len(done)} buckets; "
              f"p < 0.10 in {len(sig)}; clearly the WRONG way (p > 0.90) in {len(wrong)}.")
        print("   With no skill about half would be positive and about one in ten would reach p < 0.10 by chance;")
        print("   the buckets overlap (stress moves several at once), so read this as a pattern, not nine tests.")
        print("\n   Context -- the same spread over shorter horizons, and counting only moderate/strong calls:")
        print(f"   {'bucket':<27}{'3 months':>16}{'6 months':>16}{'12m, moderate+strong':>26}")
        for b, v in done.items():
            def cell(x):
                return "n/a" if not x else f"{100 * x['spread']:+.1f}% (p {x['p']:.2f})"
            print(f"   {b:<27}{cell(v.get(3)):>16}{cell(v.get(6)):>16}{cell(v.get('conv')):>26}")


def _fmt(e):
    if e is None:
        return "(no data)"
    z = f"z{e['z']:+.1f} " if e["z"] is not None else ""
    return (f"{e['latest']:+7.2f}  {z}{SHOWN.get(e['state'], e['state']):<9}"
            f"{'worsening' if e['det'] else ''}")


def main():
    print("=" * 100)
    print("HISTORICAL CHECK -- latest-vintage data truncated to each date "
          "(revisions flatter this; not point-in-time).")
    print("=" * 100)
    print(f"{'date':<12}{'regime':<24}{'momentum(w-b)':<16}"
          f"{'recession flags':<30}{'valuation'}")
    print("-" * 100)
    for dt in DATES:
        name, g, i, gw, gb, iw, ib = regime_at(dt)
        hot, tot = recession_flags(dt)
        vst, vval = valuation_at(dt)
        flags = f"{len(hot)}/{tot} " + ",".join(s[:5] for s in hot)
        mom = f"g{gw}-{gb} i{iw}-{ib}"
        print(f"{dt:<12}{name:<24}{mom:<16}{flags:<30}{vst} ({vval})")

    print("\nDid the recession read fire BEFORE each onset "
          "(>=2 flags in any of the 6 months prior)?")
    for label, onset in EVENTS.items():
        od = datetime.strptime(onset, "%Y-%m-%d")
        fired = []
        for k in range(1, 7):
            cd = (od - timedelta(days=30 * k)).strftime("%Y-%m-01")
            hot, _ = recession_flags(cd)
            if len(hot) >= 2:
                fired.append(cd)
        verdict = f"YES, from {min(fired)}" if fired else "no (missed or only coincident)"
        print(f"  {label:<24} onset {onset}: {verdict}")

    print("\nValuation at the two bubble peaks (should read danger):")
    for label, dt in [("dot-com 2000", "2000-03-01"), ("2021 peak", "2021-12-01")]:
        vst, vval = valuation_at(dt)
        print(f"  {label:<16} {dt}: {vst} ({vval})")

    print("\nEquity issuance around the dot-com peak (real data from 1994; the Fed "
          "refreshes this source a few times a year, so nearby dates can match):")
    for label, dt in [("1998 (before)", "1998-06-01"), ("1999 (boom)", "1999-06-01"),
                       ("2000 peak", "2000-03-01"), ("2001 (bust)", "2001-06-01"),
                       ("2021 peak", "2021-12-01"), ("latest", DATES[-1])]:
        ist, ival = issuance_at("IPO issuance", dt)
        sst, sval = issuance_at("SEO issuance", dt)
        print(f"  {label:<16} {dt}: IPO {ist} (${ival}B/12mo) | "
              f"SEO {sst} (${sval}B/12mo)")

    print("\nMarket concentration (SPY/RSP, rebased=100 at 2003 launch; no data before "
          "2003; 2021-2025 should read high):")
    for label, dt in [("2007 (pre-GFC)", "2007-06-01"), ("2018 (calm)", "2018-06-01"),
                       ("2020 low", "2020-03-01"), ("2021 peak", "2021-12-01"),
                       ("2024", "2024-01-01"), ("latest", DATES[-1])]:
        cst, cval = concentration_at(dt)
        print(f"  {label:<16} {dt}: {cst} ({cval})")

    meter_report()
    allocation_report()


if __name__ == "__main__":
    main()
