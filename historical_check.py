"""
Historical check for the macro dashboard.

Re-runs the dashboard's OWN decision stack -- signal scoring, regime, market check,
allocation and, since Step 2, the action meter -- at past dates. It shows how the
dashboard behaved around the 2001, 2008, 2020 and 2022 episodes, and how the UNTUNED
meter scores against the S&P 500's declines of 18% or more.

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
#  S&P 500 declines of 18%+ and the baseline score of the untuned meter
# ---------------------------------------------------------------------------
def sp500_events(closes, drop=EVENT_DROP):
    """Declines of `drop` or more, closing basis, measured from the running all-time
    closing high. An episode starts at that high and ends at its lowest close before
    the index makes a new high. Baseline definition only: it does not see a decline
    from a LOCAL peak that never reached a new all-time high (e.g. 2011), which Step 3
    takes from the existing confluence sweep's own event list instead.
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


def meter_report():
    print("\n" + "=" * 100)
    print("ACTION METER (Step 2) -- the dashboard's own meter, replayed monthly, UNTUNED")
    print("=" * 100)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    grid = _months(GRID_START, today[:8] + "01")
    series, layers_used = [], []
    for m in grid:
        d = decision_at(m)
        if d["meter"]:
            series.append((m, d["meter"]["score"]))
            layers_used.append(d["meter"]["layers_used"])
    if not series:
        print("  (no meter readings -- see the failure list at the top)")
        return
    n = len(series)
    print(f"\n  {n} monthly readings, {series[0][0]} -> {series[-1][0]}.  Layers available: "
          f"{statistics.mean(layers_used):.1f} of 3 on average.")
    print("  Market layer uses: " + ("FRED gauges plus the S&P 200-day trend. " if SP else
          "FRED gauges only -- S&P 500 data did not load, so the equity-trend gauge is missing. ")
          + "The HY-spread gauge exists on FRED only from 2023.")
    print("  Constants: equal layer weights; regime saturates at 50% of growth signals net-worsening and "
          f"mean z {md.R_FULL_Z}; market at z {md.M_FULL_Z}; allocation at {md.A_FULL_CONVICTION:.0f}% conviction.")
    print("  Nothing here has been tuned on any outcome.")

    print("\n-- 1. Where the meter sits, and how often each layer is 'on' (50 or more) --\n")
    cols = [("all months", lambda m: True), ("before 2011", lambda m: m < OOS_START),
            ("2011 onward", lambda m: m >= OOS_START)]
    print(f"   {'':<26}" + "".join(f"{c:<22}" for c, _ in cols))
    for lbl, key in (("Clear (under 30)", lambda s: s < 30), ("Caution (30-54)", lambda s: 30 <= s < 55),
                     ("Elevated (55-74)", lambda s: 55 <= s < 75), ("High (75+)", lambda s: s >= 75)):
        cells = []
        for _, sel in cols:
            ss = [s for m, s in series if sel(m)]
            cells.append(f"{sum(1 for s in ss if key(s))}/{len(ss)} = {100 * sum(1 for s in ss if key(s)) / max(1, len(ss)):.0f}%")
        print(f"   {lbl:<26}" + "".join(f"{c:<22}" for c in cells))
    for i, name in enumerate(("Regime layer", "Market layer", "Allocation layer")):
        cells = []
        for _, sel in cols:
            vals = [(decision_at(m)["layers"][i] or {}).get("score") for m, _ in series if sel(m)]
            vals = [v for v in vals if v is not None]
            cells.append(f"{sum(1 for v in vals if v >= 50)}/{len(vals)} = {100 * sum(1 for v in vals if v >= 50) / max(1, len(vals)):.0f}%")
        print(f"   {name + ' >= 50':<26}" + "".join(f"{c:<22}" for c in cells))

    if not SP:
        print("\n-- 2/3. S&P 500 declines and baseline score: SKIPPED (S&P 500 data did not load) --")
    else:
        events = [e for e in sp500_events(SP) if e["peak"] >= GRID_START]
        print(f"\n-- 2. S&P 500 declines of 18% or more from an all-time closing high, since {GRID_START[:4]} --")
        print("   (levels are never printed; 'ahead' = decline still to come from that month to the trough)\n")
        print(f"   {'peak':<12}{'trough':<12}{'decline':>8}  {'sample':<8}{'growth sigs':>12}   "
              f"meter at: -12m   -6m   -3m  peak  +3m trough")
        meter_at = lambda d: next((s for m, s in reversed(series) if m <= d), None)
        for ev in events:
            nsig = (decision_at(max([m for m, _ in series if m <= ev["peak"]] or [series[0][0]]))["layers"][0] or {}).get("n", 0)
            offs = [meter_at(_shift(ev["peak"], k)) for k in (-365, -183, -91, 0, 91)] + [meter_at(ev["trough"])]
            print(f"   {ev['peak']:<12}{ev['trough']:<12}{-100 * ev['depth']:>7.0f}%  "
                  f"{'in' if ev['peak'] < OOS_START else 'OUT':<8}{nsig:>12}   "
                  + "".join(f"{('-' if o is None else o):>6}" for o in offs)
                  + ("   (ongoing)" if ev["ongoing"] else ""))
        in_win = {m for m, _ in series
                  if any(_shift(e["peak"], -TOO_EARLY_DAYS) <= m <= e["trough"] for e in events)}
        print(f"\n   Base rate: {len(in_win)} of {n} months ({100 * len(in_win) / n:.0f}%) fall inside a decline "
              f"window (a year before the peak through the trough), so a random alarm is 'right' that often.")

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
                print(f"     {lbl:<12} events caught {len(caught)}/{len(evs)}"
                      f" | median lead {_med([r['lead_months'] for r in caught]) if caught else float('nan'):+.1f} mo"
                      f" | median still ahead {100 * (_med([r['ahead'] for r in caught]) or 0):.0f}%"
                      f" | alarm runs: {kinds.count('GOOD')} good, {kinds.count('TOO EARLY')} too early, {kinds.count('FALSE')} false")
            alarm = [m for m, s in series if s >= thr]
            false_m = [m for m in alarm if m not in in_win]
            print(f"     months in alarm: {len(alarm)}/{n} ({100 * len(alarm) / n:.0f}%); of those, outside every decline "
                  f"window (false alarm): {len(false_m)} ({100 * len(false_m) / max(1, len(alarm)):.0f}%)")

    print("\n-- 4. Today --\n")
    d = decision_at(today)
    m = d["meter"]
    if m:
        print(f"   meter {m['score']} -> {m['band']}  (regime {m['regime']}, market {m['market']}, "
              f"allocation {m['allocation']}; {m['layers_used']} of 3 layers)")
    print(f"   regime {d['regime']} | valuation condition {d['val']['condition']} | "
          f"market gauges hot: {sum(1 for c in d['comps'] if c['hot'])} of {len(d['comps'])}")


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


if __name__ == "__main__":
    main()
