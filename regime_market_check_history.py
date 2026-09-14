"""
Regime + market check, side by side, across the three crisis run-ups --
dot-com, GFC, and the 2022 inflation surge. NOT part of the live dashboard.

Why this exists rather than reusing earlier output: regime_persistence.py
was run against an OLDER, smaller signal set (before GROWTH_MOM/INFLATION_MOM
were expanded this session). market_check_backtest.py's deep-dive covered
only the dot-com/GFC peaks, not the 2022 run-up, and didn't print the regime
name at all. Rather than splice numbers from two different methodology
vintages, this computes both regime (historical_check.regime_at) and market
check (the same functions from market_check_backtest.py, reused verbatim)
together, in one run, under the CURRENT signal set -- one consistent answer.

Run: python regime_market_check_history.py   (needs FRED_API_KEY; must sit
                                                next to macro_dashboard.py
                                                and historical_check.py)
"""
import historical_check as hc
import macro_dashboard as md
from datetime import datetime, timedelta

THEMES_IDS = {i["id"] for lst in md.THEMES.values() for i in lst}


def _percentile_state(sid, ind):
    if sid in THEMES_IDS:
        default = md.SIGNAL_THEME.get(sid) in ("Liquidity", "Housing")
    else:
        default = True
    return ind.get("pctile", default)


def eval_state(sid, as_of):
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
        if net > 1e-9: lean_out = "Overweight"
        elif net < -1e-9: lean_out = "Underweight"
        elif (ow + uw) > 0: lean_out = "Balanced"
        else: lean_out = "No signal"
        results[b] = {"lean": lean_out, "net": round(net, 2)}
    return results, val_cond, cons_cond


def equity_trend_hot(sp500_raw, as_of):
    vals = [(d, v) for d, v in sp500_raw if d <= as_of and isinstance(v, (int, float))]
    if len(vals) < 230:
        return None
    v = [x[1] for x in vals]
    sma_now = sum(v[-200:]) / 200.0
    sma_prev = sum(v[-221:-21]) / 200.0
    return not (v[-1] > sma_now)


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
    if verdict == "Confirmed risk-on" and val_condition == "extreme":
        return "Risk-on, but valuations extreme"
    return verdict


def weekly_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return out


print("Fetching SP500 (for the equity-trend gauge)...")
SP500_RAW = md.fetch("SP500", "2015-01-01")
print(f"  {len(SP500_RAW)} points")

WINDOWS = [
    ("DOT-COM run-up", datetime(1998, 6, 1), datetime(2000, 6, 1), "2000-03-10"),
    ("GFC run-up", datetime(2006, 6, 1), datetime(2008, 6, 1), "2007-10-09"),
    ("2022 INFLATION SURGE run-up", datetime(2020, 6, 1), datetime(2022, 6, 1), "2022-01-03"),
]

for label, start, end, peak_dt in WINDOWS:
    dates = weekly_dates(start, end)
    print(f"\n{'=' * 100}")
    print(f"{label} -- weekly, {dates[0]} to {dates[-1]} (actual peak: {peak_dt})")
    print(f"{'=' * 100}")
    hdr = f"{'date':<12}{'regime':<26}{'market check':<28}{'valuation':<10}{'consumer':<10}"
    print(hdr)
    print("-" * len(hdr))
    for k, dt in enumerate(dates):
        name, *_ = hc.regime_at(dt)
        alloc, val_c, cons_c = allocation_at(dt)
        verdict, macro, n_hot, total = market_check_at(dt, SP500_RAW, alloc)
        gv = gated_verdict(verdict, val_c["condition"])
        peak_flag = "  <-- PEAK WEEK" if dt <= peak_dt < (
            datetime.strptime(dt, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d") else ""
        print(f"{dt:<12}{name:<26}{gv:<28}{val_c['condition']:<10}{cons_c['condition']:<10}{peak_flag}")
        if (k + 1) % 25 == 0:
            print(f"  ... {k + 1}/{len(dates)}")
