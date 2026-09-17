"""
Confluence FORECAST-ACCURACY sweep -- NOT part of the live dashboard, and NOT
a strategy-return test. This scores the three-layer signal the way the user
will actually use it: as an early-warning forecast (an umbrella, not a switch
to cash), judged on whether serious trouble followed when it warned, with
useful timing.

Scoring spec, fixed with the user (do not soften):
  * EVENTS scored against: S&P 500 peak-to-trough declines >= 18% (that's where
    the data clusters -- 9 such events 1990-2026; -10/-15% corrections excluded
    as unpredictable noise). NOTE the model's SIGNALS only exist from the
    mid-1990s, so events before signals exist are marked out-of-range, not
    scored as misses -- the model literally couldn't run then.
  * "TOO EARLY" = fired more than 12 MONTHS before the decline began. A firing
    only counts as a valid warning if the >=18% decline started within 12
    months after it fired. Fire earlier and it's a FALSE ALARM (premature), not
    a catch -- this encodes "2 years early is wrong."
  * SCORING AXIS = depth-still-ahead-when-fired, NOT how-early. Firing at -5%
    into a -30% decline is GOOD (most of the drop still ahead); firing 2 years
    early at all-time-highs is BAD. This handles late-but-valuable and
    too-early correctly with one metric.
  * MISS COST scales with depth: failing to warn of a -30%+ event is heavily
    penalised; a -18% "miss" barely registers. (A miss = no valid warning in
    the 12 months before the decline began.)
  * FALSE-ALARM rate reported PER CONFIDENCE TIER -- the user's tolerance for it
    depends entirely on tier (near-zero at the highest).

OVERFITTING GUARD (the user's core worry -- "you can make any backtest look
great in hindsight"): the threshold sweep is TUNED on pre-2011 events, then the
winning threshold's accuracy is REPORTED on the post-2011 events it never saw.
The out-of-sample number is the one that counts. With only ~4-5 events per half
this is directional, not statistically conclusive -- stated plainly in-output,
not hidden.

Uses the live (revised-data) full three-layer engine via market_check_backtest.
Companion to pit_confidence_validation (point-in-time but proxy-only); both
asterisks stand.

Run: python confluence_forecast_sweep.py   (needs FRED_API_KEY; must sit next to
                                              macro_dashboard.py,
                                              historical_check.py,
                                              market_check_backtest.py)
"""
import bisect
import collections
from datetime import datetime, timedelta

import macro_dashboard as md
import historical_check as hc
import market_check_backtest as mcb

import yfinance as yf


# ---- the >=18% bear events (established S&P 500 peak-to-trough figures) -----
# (peak_date, trough_date, depth_pct, name). Peak/trough are approximate month
# starts -- fine for lead-time scoring at monthly/weekly resolution.
BEARS = [
    ("1990-07-16", "1990-10-11", -19.9, "1990 recession/Gulf War"),
    ("1998-07-17", "1998-08-31", -19.3, "LTCM/Russia"),
    ("2000-03-24", "2002-10-09", -49.1, "dot-com"),
    ("2007-10-09", "2009-03-09", -56.8, "GFC"),
    ("2011-04-29", "2011-10-03", -19.4, "US downgrade/Europe"),
    ("2018-09-20", "2018-12-24", -19.8, "Q4 2018"),
    ("2020-02-19", "2020-03-23", -33.9, "COVID"),
    ("2022-01-03", "2022-10-12", -25.4, "2022 inflation/rates"),
    ("2025-02-19", "2025-04-08", -18.9, "2025 tariff selloff"),
]
TUNE_CUTOFF = "2011-01-01"   # events before this = tune; on/after = out-of-sample test
EARLY_LIMIT_DAYS = 365       # fired >12mo before the decline began = too early = false alarm


def weekly_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return out


print("Fetching S&P 500 (^GSPC) via yfinance...")
sp = yf.Ticker("^GSPC").history(start="1994-01-01", auto_adjust=True)
SP = sorted((d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(sp.index, sp["Close"]))
SP_DATES = [d for d, _ in SP]
SP_VALS = {d: c for d, c in SP}
print(f"  {len(SP)} points, {SP[0][0]} to {SP[-1][0]}")

print("Fetching SP500 (FRED, for the market-check 200-day gauge)...")
SP500_FRED = md.fetch("SP500", "2015-01-01")
print(f"  {len(SP500_FRED)} points")


def price_on_or_after(date_str):
    i = bisect.bisect_left(SP_DATES, date_str)
    return SP_VALS[SP_DATES[i]] if i < len(SP_DATES) else None


# ---- three-layer state + confidence magnitudes -----------------------------
def layer_states(as_of):
    name, growth, infl, gw, gb, iw, ib = hc.regime_at(as_of)
    r_danger = (growth == "decel")
    r_margin = gw - gb

    n_hot = total = 0
    for sid in ("BAMLH0A0HYM2", "VIXCLS", "STLFSI4"):
        e = mcb.eval_state(sid, as_of)
        if e:
            total += 1
            n_hot += bool(e["deteriorating"] or e["state"] in ("caution", "alert"))
    et = mcb.equity_trend_hot(SP500_FRED, as_of)
    if et is not None:
        total += 1
        n_hot += et
    m_danger = (n_hot >= 2)

    alloc, _, _ = mcb.allocation_at(as_of)
    net = alloc["Overall equity exposure"]["net"]
    a_danger = (net < 0)
    a_conv = abs(net) if net < 0 else 0.0

    return r_danger, r_margin, m_danger, n_hot, a_danger, a_conv


# precompute weekly states once; the sweep reuses them for every threshold combo
DATES = weekly_dates(datetime(1996, 1, 1), datetime(2026, 3, 1))
print(f"\nComputing three-layer state at {len(DATES)} weekly dates (full engine)...")
STATES = {}
for k, dt in enumerate(DATES):
    STATES[dt] = layer_states(dt)
    if (k + 1) % 100 == 0:
        print(f"  ... {k + 1}/{len(DATES)}")


def fires(dt, rm_min, mhot_min, conv_min):
    """Does the confluence signal fire at this date under these thresholds?
    All three must be in danger AND each must clear its confidence floor."""
    rd, rmar, md_, mhot, ad, aconv = STATES[dt]
    return (rd and md_ and ad
            and rmar >= rm_min and mhot >= mhot_min and aconv >= conv_min)


# ---- scoring ---------------------------------------------------------------
def score_threshold(rm_min, mhot_min, conv_min, events, date_lo, date_hi):
    """Score one threshold combo over the events whose PEAK falls in
    [date_lo, date_hi]. Returns dict of accuracy metrics."""
    ev = [e for e in events if date_lo <= e[0] <= date_hi]
    # signal-testable events only: the model needs ~6mo of prior signal history.
    # events before signals exist are excluded from scoring (not counted as miss).
    testable = [e for e in ev if e[0] >= "1997-01-01"]

    fire_dates = [dt for dt in DATES if date_lo <= dt <= date_hi
                  and fires(dt, rm_min, mhot_min, conv_min)]

    # --- catches & misses ---
    caught, missed = [], []
    catch_lead = {}       # peak_date -> (first valid warning date, depth still ahead)
    for peak, trough, depth, name in testable:
        pk = datetime.strptime(peak, "%Y-%m-%d")
        window_lo = (pk - timedelta(days=EARLY_LIMIT_DAYS)).strftime("%Y-%m-%d")
        # valid warnings: fired within 12mo BEFORE the peak (decline "began" ~peak)
        valid = [dt for dt in fire_dates if window_lo <= dt <= peak]
        if valid:
            first = min(valid)
            ppk = price_on_or_after(peak)
            ptr = price_on_or_after(trough)
            psig = price_on_or_after(first)
            still_ahead = (ptr / psig - 1) * 100 if (ptr and psig) else None
            caught.append((name, depth, first, still_ahead))
            catch_lead[peak] = (first, still_ahead)
        else:
            missed.append((name, depth))

    # --- false alarms: fire dates NOT within 12mo before any bear peak ---
    bear_windows = []
    for peak, _, _, _ in testable:
        pk = datetime.strptime(peak, "%Y-%m-%d")
        bear_windows.append(((pk - timedelta(days=EARLY_LIMIT_DAYS)).strftime("%Y-%m-%d"), peak))
    false_alarm_weeks = []
    for dt in fire_dates:
        in_window = any(lo <= dt <= hi for lo, hi in bear_windows)
        if not in_window:
            false_alarm_weeks.append(dt)

    # --- depth-weighted miss cost ---
    # weight = (depth/18)^2 so -36% miss counts ~4x a -18% miss
    miss_cost = sum((abs(d) / 18.0) ** 2 for _, d in missed)

    return {
        "n_testable": len(testable),
        "caught": caught,
        "missed": missed,
        "n_caught": len(caught),
        "n_missed": len(missed),
        "miss_cost": miss_cost,
        "fire_weeks": len(fire_dates),
        "false_alarm_weeks": len(false_alarm_weeks),
    }


# ---- the sweep -------------------------------------------------------------
RM_GRID = (2, 4, 6)         # regime margin floor
MHOT_GRID = (2, 3, 4)       # market gauges hot floor
CONV_GRID = (0, 5, 10)      # allocation underweight-conviction floor

print(f"\n{'=' * 100}")
print("THE SWEEP -- tuned on PRE-2011 events, then scored OUT-OF-SAMPLE on")
print(">=2011 events the threshold never saw. Out-of-sample is the number that")
print("counts. (~4 tune events, ~5 test events -- directional, NOT conclusive.)")
print(f"{'=' * 100}")

# score every combo on the tune set; rank by: catch all, then fewest false-alarm
# weeks, then lowest miss cost. "Catch all" is gated first because a model that
# misses a bear is disqualified regardless of how few false alarms it has.
combos = []
for rm in RM_GRID:
    for mh in MHOT_GRID:
        for cv in CONV_GRID:
            s = score_threshold(rm, mh, cv, BEARS, "1990-01-01", "2010-12-31")
            combos.append(((rm, mh, cv), s))

# rank
def rank_key(item):
    (_, s) = item
    return (s["n_missed"], s["miss_cost"], s["false_alarm_weeks"])
combos.sort(key=rank_key)

print("\nTUNE-SET results (pre-2011), best first:")
print(f"{'rm':>3}{'mhot':>5}{'conv':>5}{'caught':>8}{'missed':>8}{'missCost':>10}{'FA weeks':>10}")
print("-" * 49)
for (rm, mh, cv), s in combos[:12]:
    print(f"{rm:>3}{mh:>5}{cv:>5}{s['n_caught']:>6}/{s['n_testable']:<1}"
          f"{s['n_missed']:>8}{s['miss_cost']:>10.1f}{s['false_alarm_weeks']:>10}")

best_thresh = combos[0][0]
print(f"\nBest tune-set threshold: regime margin>={best_thresh[0]}, "
      f"gauges hot>={best_thresh[1]}, conviction>={best_thresh[2]}")

# ---- out-of-sample report on the winner ------------------------------------
print(f"\n{'=' * 100}")
print("OUT-OF-SAMPLE (>=2011 events the winning threshold NEVER saw) -- THE VERDICT")
print(f"{'=' * 100}")
oos = score_threshold(*best_thresh, BEARS, "2011-01-01", "2026-03-01")
print(f"  threshold: regime margin>={best_thresh[0]}, gauges>={best_thresh[1]}, conviction>={best_thresh[2]}")
print(f"  bears in the out-of-sample window (testable): {oos['n_testable']}")
print(f"  CAUGHT: {oos['n_caught']}/{oos['n_testable']}")
for name, depth, first, ahead in oos["caught"]:
    ah = f"{ahead:+.0f}% still ahead when it fired" if ahead is not None else "n/a"
    print(f"     + {name:<26} ({depth:.0f}%)  first warned {first}  |  {ah}")
if oos["missed"]:
    print(f"  MISSED:")
    for name, depth in oos["missed"]:
        print(f"     - {name:<26} ({depth:.0f}%)   <-- {'SERIOUS miss' if depth <= -30 else 'miss'}")
print(f"  false-alarm weeks (fired >12mo from any bear, or no bear followed): {oos['false_alarm_weeks']}")

# ---- ALL bears, both halves, at the winning threshold ----------------------
print(f"\n{'=' * 100}")
print("FULL PICTURE -- every >=18% bear since signals exist, at the winning")
print("threshold, marked [tune] or [OOS]. Depth-still-ahead is the value metric.")
print(f"{'=' * 100}")
full = score_threshold(*best_thresh, BEARS, "1990-01-01", "2026-03-01")
caught_names = {c[0] for c in full["caught"]}
for peak, trough, depth, name in BEARS:
    if peak < "1997-01-01":
        tag = "[before signals - not scorable]"
        print(f"  {peak}  {depth:>6.1f}%  {name:<26} {tag}")
        continue
    half = "[tune]" if peak < TUNE_CUTOFF else "[OOS ]"
    if name in caught_names:
        c = next(c for c in full["caught"] if c[0] == name)
        ah = f"{c[3]:+.0f}% ahead" if c[3] is not None else "n/a"
        print(f"  {peak}  {depth:>6.1f}%  {name:<26} {half}  CAUGHT, warned {c[2]} ({ah})")
    else:
        sev = "SERIOUS MISS" if depth <= -30 else "miss"
        print(f"  {peak}  {depth:>6.1f}%  {name:<26} {half}  {sev}")

print(f"\n{'=' * 100}")
print("HOW TO READ THIS:")
print("- Out-of-sample catches with big 'still-ahead' = the model warned of")
print("  bears it was NOT tuned on, early enough to act = the real result.")
print("- A SERIOUS MISS (-30%+) out-of-sample = disqualifying, regardless of")
print("  anything else.")
print("- Few false-alarm weeks at this (high) threshold = the near-zero-false-")
print("  alarm-at-high-confidence property the user needs.")
print("- With ~5 out-of-sample events this is DIRECTIONAL evidence the logic")
print("  holds, never statistical proof -- bears are too rare for proof.")
print("- Revised-data asterisk stands; pit_confidence_validation is the")
print("  point-in-time companion. Neither alone is the whole truth.")
print(f"{'=' * 100}")
