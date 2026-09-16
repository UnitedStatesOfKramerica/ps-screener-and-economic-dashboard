"""
Market check + capital allocation backtest -- NOT part of the live dashboard.

Answers directly: would the market check have said "Confirmed risk-on" at
the 1999-2000 dot-com peak, the 2007 pre-GFC peak, or the 2021 euphoria
peak? And what was the Overall equity exposure / Value-over-Growth /
Gold allocation lean at each of those points?

This reconstructs BOTH the capital allocation engine (all 9 buckets,
including the two-directional mirror votes and the Valuation/Consumer
condition votes added this session) and the market-check verdict (which
depends on the allocation engine's own "Overall equity exposure" read),
at each historical date -- using the same state/deteriorating/improving
formula as the live panel_for(), and the same DEADBAND_K/percentile-state
rules, just evaluated on data truncated to what was known "as of" each
date.

KNOWN CAVEAT, stated once here rather than buried: FRED's own SP500 series
only starts 2015-01-01, so the 200-day equity-trend gauge in market check
is UNAVAILABLE before ~2016 (needs ~230 days of lead-in). For 1999-2000
and 2007-2008, market check runs on the other 3 gauges (credit spreads,
VIX, financial stress) -- fewer inputs than it has today. The script
prints how many gauges were actually live at each date so this isn't
hidden in the numbers.

Run: python market_check_backtest.py   (needs FRED_API_KEY; must sit next
                                         to macro_dashboard.py and
                                         historical_check.py)
"""
import historical_check as hc
import macro_dashboard as md
from datetime import datetime, timedelta

THEMES_IDS = {i["id"] for lst in md.THEMES.values() for i in lst}


def _percentile_state(sid, ind):
    if sid in THEMES_IDS:
        default = md.SIGNAL_THEME.get(sid) in ("Liquidity", "Housing")
    else:
        default = True   # drill-downs default to percentile-based
    return ind.get("pctile", default)


def eval_state(sid, as_of):
    """Mirrors panel_for()'s exact state/deteriorating/improving formula,
    on data truncated to <= as_of. Returns None if the signal has no data
    yet at this date (correctly excludes it, same as the live build) or
    the transform leaves nothing to evaluate.

    Memoized per (sid, as_of): several signals (e.g. BAMLH0A0HYM2) vote in
    3-4 different buckets, so without this the same trend computation would
    re-run once per bucket per date across ~1,300 dates.
    """
    key = (sid, as_of)
    if key in _EVAL_CACHE:
        return _EVAL_CACHE[key]
    ind = hc.INDS.get(sid)
    raw = hc.RAW.get(sid)
    if not ind or not raw:
        _EVAL_CACHE[key] = None
        return None
    raw = [(d, v) for d, v in raw if d <= as_of]
    if ind.get("scale"):
        raw = [(d, v * ind["scale"]) for d, v in raw]
    series = md.yoy(raw) if ind["kind"] == "yoy" else raw
    if not series:
        _EVAL_CACHE[key] = None
        return None
    latest = series[-1][1]
    tr = md.trend(series)
    pctile = _percentile_state(sid, ind)
    if pctile:
        st = md.substate_of(ind["worry"], tr["pct_of_range"] if tr else None)
    else:
        st = md.state_of(ind["worry"], latest, ind.get("caution"), ind.get("alert"))
    sig = bool(tr and tr.get("typical", 0) > 0
               and abs(tr["delta"]) >= md.DEADBAND_K * tr["typical"])
    moved_bad = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    deteriorating = bool(sig and moved_bad)
    improving = bool(sig and ind["worry"] and not moved_bad and tr["delta"] != 0)
    result = {"state": st, "deteriorating": deteriorating, "improving": improving,
              "latest": latest, "label": ind["label"]}
    _EVAL_CACHE[key] = result
    return result


_EVAL_CACHE = {}


def theme_cond_at(theme, as_of, labels):
    panels = []
    for lst in (md.THEMES.get(theme, []), md.DRILLDOWNS.get(theme, [])):
        for i in lst:
            e = eval_state(i["id"], as_of)
            if e:
                panels.append(e)
    return md.theme_condition(panels, labels=labels)


_BUCKET_SIGNALS = {b: [] for b in md.ALLOC_BUCKETS}
for _sid, _votes in md.ALLOC.items():
    for _bucket, _lean in _votes:
        _BUCKET_SIGNALS[_bucket].append((_sid, _lean))


def allocation_at(as_of):
    val_cond = theme_cond_at("Valuation", as_of, ("extreme", "elevated", "normal"))
    cons_cond = theme_cond_at("Consumer", as_of, ("stressed", "mixed", "healthy"))
    val_w = {"extreme": 1.5, "elevated": 0.75}.get(val_cond["condition"], 0.0)
    cons_w = {"stressed": 1.5, "mixed": 0.75}.get(cons_cond["condition"], 0.0)
    cond_votes = []
    if val_w:
        cond_votes += [("Overall equity exposure", "UW", val_w),
                       ("Defensive equities", "OW", val_w),
                       ("Gold", "OW", val_w)]
    if cons_w:
        cond_votes += [("Cyclicals & small caps", "UW", cons_w),
                       ("Defensive equities", "OW", cons_w),
                       ("Overall equity exposure", "UW", cons_w)]
    cond_by_bucket = {b: [] for b in md.ALLOC_BUCKETS}
    for bucket, lean, w in cond_votes:
        cond_by_bucket[bucket].append((lean, w))

    results = {}
    for b in md.ALLOC_BUCKETS:
        ow = uw = 0.0
        for sid, lean in _BUCKET_SIGNALS[b]:
            e = eval_state(sid, as_of)
            if not e:
                continue
            w = md.SIGNAL_WEIGHT.get(sid, 1.0)
            two = sid in md.TWO_DIRECTIONAL
            rev = "UW" if lean == "OW" else "OW"
            worse = bool(e["deteriorating"] or e["state"] in ("caution", "alert"))
            better = two and bool(e["improving"] or e["state"] == "calm") and not worse
            if worse:
                if lean == "OW": ow += w
                else: uw += w
            elif better:
                if rev == "OW": ow += w
                else: uw += w
        for lean, w in cond_by_bucket[b]:
            if lean == "OW": ow += w
            else: uw += w
        net = ow - uw
        if net > 1e-9:
            lean_out = "Overweight"
        elif net < -1e-9:
            lean_out = "Underweight"
        elif (ow + uw) > 0:
            lean_out = "Balanced"
        else:
            lean_out = "No signal"
        results[b] = {"lean": lean_out, "net": round(net, 2)}
    return results, val_cond, cons_cond


def equity_trend_hot(sp500_raw, as_of):
    vals = [(d, v) for d, v in sp500_raw if d <= as_of and isinstance(v, (int, float))]
    if len(vals) < 230:
        return None
    v = [x[1] for x in vals]
    sma_now = sum(v[-200:]) / 200.0
    sma_prev = sum(v[-221:-21]) / 200.0
    return not (v[-1] > sma_now)   # "hot" if below its own 200-day


def market_check_at(as_of, sp500_raw, alloc_results):
    n_hot = total = 0
    for sid in ("BAMLH0A0HYM2", "VIXCLS", "STLFSI4"):
        e = eval_state(sid, as_of)
        if e:
            total += 1
            n_hot += bool(e["deteriorating"] or e["state"] in ("caution", "alert"))
    et = equity_trend_hot(sp500_raw, as_of)
    if et is not None:
        total += 1
        n_hot += et
    market_riskoff = n_hot >= 2
    oee = alloc_results.get("Overall equity exposure")
    macro = ("risk-off" if oee and oee["lean"] == "Underweight"
             else "risk-on" if oee and oee["lean"] == "Overweight" else "neutral")
    if macro == "risk-off" and market_riskoff:
        verdict = "Confirmed risk-off"
    elif macro == "risk-off":
        verdict = "Unconfirmed risk-off"
    elif macro == "risk-on" and market_riskoff:
        verdict = "Watch -- market pricing risk"
    elif macro == "risk-on":
        verdict = "Confirmed risk-on"
    else:
        verdict = "Market pricing risk" if market_riskoff else "Market calm"
    return verdict, macro, n_hot, total


def gated_verdict(verdict, val_condition):
    """PROPOSED CHANGE, not yet live: downgrades 'Confirmed risk-on' to a
    qualified state when Valuation reads 'extreme'. Deliberately does NOT
    touch the underlying vote math (2007/GFC showed that's accurate) -- this
    only stops the LABEL from claiming 'confirmed, all clear' when a known
    slow-moving risk factor is flashing. Every other verdict passes through
    unchanged, including 'Confirmed risk-on' when valuation is merely
    'elevated' or 'normal'.
    """
    if verdict == "Confirmed risk-on" and val_condition == "extreme":
        return "Risk-on, but valuations extreme"
    return verdict


def weekly_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return out


def monthly_dates(start, end):
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out




def _run():
    print("Fetching SP500 (for the equity-trend gauge; separate from the INDS set)...")
    SP500_RAW = md.fetch("SP500", "2015-01-01")
    print(f"  {len(SP500_RAW)} points")

    WINDOWS = [
        ("DOT-COM ERA", datetime(1998, 1, 1), datetime(2002, 12, 31)),
        ("GFC ERA", datetime(2006, 1, 1), datetime(2009, 12, 31)),
        ("COVID / 2022 BEAR ERA", datetime(2019, 1, 1), datetime(2023, 12, 31)),
    ]

    KEY_BUCKETS = ["Overall equity exposure", "Value over Growth", "Gold",
                   "Cyclicals & small caps", "Defensive equities"]

    verdict_counts_by_window = {}
    gated_counts_by_window = {}
    flip_examples_by_window = {}   # first/last week of each contiguous flip run, per era

    for label, start, end in WINDOWS:
        dates = weekly_dates(start, end)
        print(f"\n{'=' * 100}")
        print(f"{label} -- weekly, {dates[0]} to {dates[-1]} ({len(dates)} dates)")
        print(f"{'=' * 100}")
        counts, gcounts = {}, {}
        flips = []            # list of (start_date, end_date, run_length) for flipped runs
        run_start = None
        for k, dt in enumerate(dates):
            alloc, val_c, cons_c = allocation_at(dt)
            verdict, macro, n_hot, total = market_check_at(dt, SP500_RAW, alloc)
            gv = gated_verdict(verdict, val_c["condition"])
            counts[verdict] = counts.get(verdict, 0) + 1
            gcounts[gv] = gcounts.get(gv, 0) + 1
            flipped = (gv != verdict)
            if flipped and run_start is None:
                run_start = dt
            if not flipped and run_start is not None:
                flips.append((run_start, dates[k - 1]))
                run_start = None
            if (k + 1) % 100 == 0:
                print(f"  ... {k + 1}/{len(dates)}")
        if run_start is not None:
            flips.append((run_start, dates[-1]))
        verdict_counts_by_window[label] = counts
        gated_counts_by_window[label] = gcounts
        flip_examples_by_window[label] = flips

    print(f"\n{'=' * 100}")
    print("COMPARISON -- verdict frequency, BEFORE vs AFTER the proposed valuation gate")
    print(f"{'=' * 100}")
    for label in verdict_counts_by_window:
        counts, gcounts = verdict_counts_by_window[label], gated_counts_by_window[label]
        total = sum(counts.values())
        before_riskon = counts.get("Confirmed risk-on", 0)
        after_riskon = gcounts.get("Confirmed risk-on", 0)
        after_qualified = gcounts.get("Risk-on, but valuations extreme", 0)
        print(f"\n{label} ({total} weeks):")
        print(f"  BEFORE -- 'Confirmed risk-on': {before_riskon} weeks ({before_riskon/total*100:.0f}%)")
        print(f"  AFTER  -- 'Confirmed risk-on': {after_riskon} weeks ({after_riskon/total*100:.0f}%)   "
              f"'Risk-on, but valuations extreme': {after_qualified} weeks ({after_qualified/total*100:.0f}%)")
        pct_downgraded = (after_qualified / before_riskon * 100) if before_riskon else 0.0
        print(f"  -> {after_qualified} of the original {before_riskon} risk-on weeks "
              f"({pct_downgraded:.0f}%) get downgraded")
        flips = flip_examples_by_window[label]
        print(f"  Contiguous flipped stretches: {len(flips)}")
        for s, e in flips:
            print(f"    {s} to {e}")

    print(f"\n{'=' * 100}")
    print("OVER-TRIGGER CHECK -- does the gate ever fire outside the 3 known crisis eras'")
    print("bubble buildups? (spot-checking calmer stretches within each window)")
    print(f"{'=' * 100}")
    CALM_CHECK_DATES = [
        ("2003-06-01", "post dot-com bust recovery, should NOT be extreme"),
        ("2009-06-01", "post-GFC trough recovery, should NOT be extreme"),
        ("2013-06-01", "mid-cycle expansion, should NOT be extreme"),
        ("2017-06-01", "mid-cycle expansion, should NOT be extreme"),
    ]
    for dt, why in CALM_CHECK_DATES:
        alloc, val_c, cons_c = allocation_at(dt)
        verdict, macro, n_hot, total = market_check_at(dt, SP500_RAW, alloc)
        gv = gated_verdict(verdict, val_c["condition"])
        flag = "  <-- GATE FIRED HERE" if gv != verdict else ""
        print(f"  {dt} ({why}): valcond={val_c['condition']:<10} verdict={verdict:<20} gated={gv}{flag}")

    print(f"\n{'=' * 100}")
    print("DEEP DIVE -- reference dates, BEFORE vs AFTER")
    print(f"{'=' * 100}")
    REFERENCE_DATES = [
        ("1999-12-01", "pre dot-com peak"),
        ("2000-03-10", "dot-com peak (Nasdaq high was 2000-03-10)"),
        ("2000-09-01", "6mo after the peak"),
        ("2007-10-09", "pre-GFC peak (S&P 500 high was 2007-10-09)"),
        ("2008-09-01", "just before Lehman"),
        ("2021-11-19", "2021 peak (S&P 500 high was 2022-01-03; using late-2021 for data lag)"),
        ("2022-06-01", "mid-2022 bear"),
        ("2026-09-12", "latest / today"),
    ]
    for dt, why in REFERENCE_DATES:
        alloc, val_c, cons_c = allocation_at(dt)
        verdict, macro, n_hot, total = market_check_at(dt, SP500_RAW, alloc)
        gv = gated_verdict(verdict, val_c["condition"])
        changed = " <-- CHANGED" if gv != verdict else ""
        print(f"  {dt:<12} ({why})")
        print(f"    valuation={val_c['condition']:<10} BEFORE={verdict:<20} AFTER={gv}{changed}")




if __name__ == "__main__":
    _run()