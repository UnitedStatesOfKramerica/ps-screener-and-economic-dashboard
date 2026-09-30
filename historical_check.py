"""
Historical check for the macro dashboard.

Re-runs the dashboard's OWN signal logic (regime classifier, recession signals,
valuation, market check) at a set of past dates, so you can see whether the
signals fired IN TIME around the 2001, 2008, 2020 and 2022 episodes -- or only
after the fact.

IMPORTANT caveat, read it: this uses FRED's *latest-vintage* values truncated to
each date -- today's revised numbers, not what was actually known then. It is a
sanity check, NOT a true point-in-time backtest (that needs ALFRED vintages).
Revisions flatter the results, so read "it caught it" with salt. Signals that
did not exist yet in real time (WEI from 2008, breakevens from 2003) simply drop
out of the earlier dates.

Since Step 1b this file has NO scoring code of its own. Fetching, transforming
and scoring all come from macro_dashboard.py (fetch_raw, transform, fetch_legs,
score_series), so what this measures is exactly what ships -- the precondition
for Step 3's retest. Its earlier copy of the scoring could drift from
production; it is gone. The market check's gauge list is imported too.

After the report, "OPEN QUESTIONS" answers what the Step 1b gate left open:
Q1 the smoothed funding card, Q2 funding stress as a market-check input (for
Step 2), Q3 whether valuation should be judged against its full history.

Run:  python historical_check.py     (needs FRED_API_KEY, like the dashboard)
"""
import macro_dashboard as md
from collections import deque
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

# The market check's FRED-sourced gauges, straight from the dashboard. Its fifth,
# the S&P 500 200-day trend, is excluded: FRED serves only ~10 years of S&P data
# and the dashboard never stores it, so no backtest has ever had it.
MARKET_GAUGES = [(sid, lbl) for sid, lbl, _ in md.MARKET_CHECK_GAUGES]

SHOWN = {"alert": "danger", "caution": "caution", "calm": "calm", "neutral": "unscored"}

INDS = {}
for _grp in (md.THEMES, md.DRILLDOWNS):
    for _lst in _grp.values():
        for _ind in _lst:
            INDS[_ind["id"]] = _ind

print("Fetching full history for", len(INDS), "series ...")
SERIES, LEGS, LOAD_FAILED, RAW = {}, {}, [], {}
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
    if ind.get("smooth_obs"):
        RAW[sid] = raw                      # kept so Q1 can compare against the unsmoothed daily

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
        if sid in dict(MARKET_GAUGES):
            harm.append("market check")
        print(f"!!   {sid:<24} {lbl[:34]:<36} "
              + ("-> " + "; ".join(harm) if harm else "-> display only"))
    print("!" * 78 + "\n")
else:
    print("All series loaded.\n")


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
    """Allocation's and the market check's shared rule: moving the risk-off way, or
    already at caution/danger."""
    return bool(e and (e["det"] or e["state"] in ("caution", "alert")))


def _momentum(ids, as_of):
    worse = better = 0
    for sid in ids:
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        worse += e["det"]
        better += e["imp"]
    return worse, better


def regime_at(as_of, growth_ids=None):
    g_worse, g_better = _momentum(growth_ids or md.GROWTH_MOM, as_of)
    i_worse, i_better = _momentum(md.INFLATION_MOM, as_of)
    growth = ("decelerating" if (g_worse - g_better) >= md.REGIME_MARGIN
              else "accelerating")
    inflation = ("accelerating" if (i_worse - i_better) >= md.REGIME_MARGIN
                 else "decelerating")
    name = md.REGIMES[(growth, inflation)][0]
    return name, growth[:5], inflation[:5], g_worse, g_better, i_worse, i_better


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


def market_riskoff(as_of, gauges, extra=()):
    """The market check's rule: risk-off needs at least two hot gauges. `extra` adds
    candidate gauges as (indicator, series) pairs, for testing inputs not shipped."""
    n_hot = n = 0
    for sid in gauges:
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        n += 1
        n_hot += _hot(e)
    for ind, series in extra:
        sc = _score(ind, series, {}, as_of)
        if sc is None:
            continue
        n += 1
        n_hot += _hot_sc(sc)
    return n_hot >= 2, n_hot, n


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


def _fmt(e):
    if e is None:
        return "(no data)"
    z = f"z{e['z']:+.1f} " if e["z"] is not None else ""
    return (f"{e['latest']:+7.2f}  {z}{SHOWN.get(e['state'], e['state']):<9}"
            f"{'worsening' if e['det'] else ''}")


def _trailing_mean(series, n):
    out, win, tot = [], deque(), 0.0
    for d, v in series:
        win.append(v)
        tot += v
        if len(win) > n:
            tot -= win.popleft()
        if len(win) == n:
            out.append((d, tot / n))
    return out


# ===========================================================================
#  OPEN QUESTIONS left by the Step 1b gate (30 Sept 2026 run)
# ===========================================================================
def _state_of(ind, series, d):
    sc = _score(ind, series, {}, d)
    return sc["state"] if sc else None


def _fmt_sc(sc):
    if sc is None:
        return "(no data)"
    return _fmt({"latest": sc["latest"], "z": sc["z"], "state": sc["state"],
                 "det": sc["deteriorating"]})


def open_questions():
    import statistics
    print("\n" + "=" * 100)
    print("OPEN QUESTIONS FROM THE STEP 1b GATE")
    print("=" * 100)
    fid = "CP minus T-bill"
    ind20, s20, raw = INDS[fid], SERIES.get(fid), RAW.get(fid)
    ind1 = {**ind20, "smooth_obs": None}
    s1 = md.transform(ind1, raw) if raw else None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("\n-- Q1. Funding card, now a 20-day average: does it keep the crises and drop the "
          "distortions? --\n")
    if not (s1 and s20):
        print("   (funding series did not load -- see the failure list at the top)")
    else:
        print(f"   {'moment':<30}{'date':<12}{'daily (last run)':<32}{'20-day avg (as shipped)'}")
        for lbl, d in [("before the ABCP freeze", "2007-08-01"), ("two weeks into the freeze", "2007-08-24"),
                       ("2008 recession starts", "2007-12-01"), ("pre-Lehman", "2008-06-01"),
                       ("Lehman", "2008-10-01"), ("before COVID", "2020-03-01"),
                       ("COVID funding seizure", "2020-03-23"), ("2022, day after quarter-end", "2022-07-01"),
                       ("2022, later", "2022-10-01"), ("2005 rate hikes", "2005-06-01"),
                       ("2018 rate hikes", "2018-03-01")]:
            print(f"   {lbl:<30}{d:<12}{_fmt_sc(_score(ind1, s1, {}, d)):<32}"
                  f"{_fmt_sc(_score(ind20, s20, {}, d))}")
        print("\n   How often the badge reads caution/danger, 2010-2026. A quarter-end effect would")
        print("   show up as quarter-starts reading hotter than other month-starts:")
        qs = [f"{y}-{m:02d}-01" for y in range(2010, 2027) for m in (1, 4, 7, 10)
              if f"{y}-{m:02d}-01" <= "2026-07-01"]
        other = [f"{y}-{m:02d}-01" for y in range(2010, 2027) for m in (2, 3, 5, 6, 8, 9, 11, 12)
                 if f"{y}-{m:02d}-01" <= "2026-08-01"]
        for lbl, ind, ser in (("daily", ind1, s1), ("20-day avg", ind20, s20)):
            hq = sum(_state_of(ind, ser, d) in ("caution", "alert") for d in qs)
            ho = sum(_state_of(ind, ser, d) in ("caution", "alert") for d in other)
            print(f"   {lbl:<12} quarter-starts {hq}/{len(qs)} = {100 * hq / len(qs):.0f}%   "
                  f"other month-starts {ho}/{len(other)} = {100 * ho / len(other):.0f}%")
        for lbl, a2, b2 in [("2004-06 hiking cycle", "2004-06-01", "2006-06-01"),
                            ("2016-18 hiking cycle", "2016-01-01", "2018-12-01"),
                            ("2022-23 hiking cycle", "2022-03-01", "2023-07-01"),
                            ("GFC, Aug 2007 - Mar 2009", "2007-08-01", "2009-03-01")]:
            ms = _months(a2, b2)
            h1 = sum(_state_of(ind1, s1, m) in ("caution", "alert") for m in ms)
            h20 = sum(_state_of(ind20, s20, m) in ("caution", "alert") for m in ms)
            print(f"   months at caution/danger, {lbl:<26} daily {h1}/{len(ms)}   20-day {h20}/{len(ms)}")
        print("   (last run, daily: 0/25, 6/36, 3/17; GFC 17/20)")

    print("\n-- Q2. Funding stress as a market-check input (evidence for Step 2; not shipped) --\n")
    base = [sid for sid, _ in MARKET_GAUGES]
    variants = [("market check as shipped", ())]
    if s20:
        variants.append(("+ funding, 20-day", ((ind20, s20),)))
    if s1:
        variants.append(("+ funding, daily", ((ind1, s1),)))
    for lbl, extra in variants:
        segs = []
        for yr, a2, b2 in (("2007", "2007-06-01", "2007-12-01"), ("2008", "2008-01-01", "2008-12-01"),
                           ("2009", "2009-01-01", "2009-06-01")):
            segs.append(yr + " " + "".join("R" if market_riskoff(m, base, extra)[0] else "."
                                           for m in _months(a2, b2)))
        print(f"   {lbl:<26} " + " | ".join(segs))
    print("   (R = risk-off; 2007 runs Jun-Dec, 2009 Jan-Jun)")
    calm = [m for a2, b2 in [("2003-06-01", "2006-12-01"), ("2012-01-01", "2014-12-01"),
                             ("2016-06-01", "2018-06-01"), ("2023-06-01", "2026-08-01")]
            for m in _months(a2, b2)]
    for lbl, extra in variants:
        r = sum(market_riskoff(m, base, extra)[0] for m in calm)
        print(f"   calm-period risk-off months, {lbl:<26} {r}/{len(calm)}")
    print("   (last run: 10/143 as shipped now; 19/143 with the daily gauge)")

    print("\n-- Q3. Valuation: judged against history since 1990 (as shipped) or its full "
          "history? --\n")
    print("   The robust score measures each reading against its own history. The dashboard")
    print("   starts most valuation series in 1990, so 'normal' is set by an era that was")
    print("   itself richly valued. Where the source goes back further, this refetches it.\n")
    val_ids = [i["id"] for g in (md.THEMES, md.DRILLDOWNS) for i in g.get("Valuation", [])]
    extend = {}
    for sid in val_ids:
        ind = INDS[sid]
        if ind.get("compute") in (None, "ratio", "cape", "multpl") and ind["start"] <= "1990-01-01" \
                and SERIES.get(sid):
            full = {**ind, "start": "1871-01-01"}
            raw_f = md.fetch_raw(full)
            ser_f = md.transform(full, raw_f) if raw_f else []
            if ser_f and ser_f[0][0] < SERIES[sid][0][0]:
                extend[sid] = (full, ser_f)
    if not extend:
        print("   (no valuation series returned longer history -- nothing to compare)")
    else:
        print(f"   {'signal':<20}{'full history from':<19}{'normal since 1990':>18}{'full-history normal':>21}"
              f"   today: since-1990 / full")
        for sid, (full, ser_f) in extend.items():
            shp = [v for d, v in SERIES[sid] if d <= today]
            fl = [v for d, v in ser_f if d <= today]
            a1, a2 = eval_signal(sid, today), _score(full, ser_f, {}, today)
            if a1 and a2 and a1["z"] is not None and a2["z"] is not None:
                today_txt = (f"{SHOWN.get(a1['state'], a1['state'])} (z{a1['z']:+.1f}) / "
                             f"{SHOWN.get(a2['state'], a2['state'])} (z{a2['z']:+.1f})")
            else:
                today_txt = "(not comparable today)"
            print(f"   {sid[:19]:<20}{ser_f[0][0]:<19}{statistics.median(shp):>18.2f}"
                  f"{statistics.median(fl):>21.2f}   {today_txt}")
        print(f"\n   The Valuation condition -- the part that feeds allocation (extreme = 3+ in danger):")
        print(f"   {'date':<12}{'history since 1990 (as shipped)':<36}{'full history':<36}CAPE: 1990 / full")
        for d in ["1995-06-01", "2000-03-01", "2003-03-01", "2007-10-01", "2009-03-01", "2012-06-01",
                  "2016-02-01", "2020-03-01", "2021-12-01", today]:
            rows = {}
            for mode in ("shipped", "full"):
                panels = []
                for sid in val_ids:
                    if mode == "full" and sid in extend:
                        st_ = _state_of(extend[sid][0], extend[sid][1], d)
                    else:
                        e = eval_signal(sid, d)
                        st_ = e["state"] if e else None
                    if st_ is not None:
                        panels.append({"state": st_, "label": sid})
                c = md.theme_condition(panels)
                rows[mode] = f"{c['condition']} ({c['alert']} of {c['total']} in danger)"
            cs = eval_signal("Shiller CAPE", d)
            cf = (_state_of(extend["Shiller CAPE"][0], extend["Shiller CAPE"][1], d)
                  if "Shiller CAPE" in extend else None)
            print(f"   {d:<12}{rows['shipped']:<36}{rows['full']:<36}"
                  f"{SHOWN.get(cs['state'], cs['state']) if cs else '-'} / {SHOWN.get(cf, cf) if cf else '-'}")

    print("\n-- Today --\n")
    for sid in ("Curve momentum", "CP minus T-bill", "Mortgage debt vs prices"):
        print(f"   {sid:<26}{_fmt(eval_signal(sid, today))}")
    r = market_riskoff(today, base)
    print(f"   regime {regime_at(today)[0]} | market check risk-off from FRED gauges: {r[0]} "
          f"({r[1]} of {r[2]} hot)")


def _hot_sc(sc):
    return bool(sc and (sc["deteriorating"] or sc["state"] in ("caution", "alert")))


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

    open_questions()


if __name__ == "__main__":
    main()
