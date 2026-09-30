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
production; it is gone.

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

# The market check's FRED-sourced gauges, as the dashboard lists them. Its fifth,
# the S&P 500 200-day trend, is excluded: FRED serves only ~10 years of S&P data
# and the dashboard never stores it, so no backtest has ever had it.
MARKET_GAUGES = [("BAMLH0A0HYM2", "Credit spreads"), ("CP minus T-bill", "Funding stress"),
                 ("VIXCLS", "Volatility"), ("STLFSI4", "Financial stress")]

SHOWN = {"alert": "danger", "caution": "caution", "calm": "calm", "neutral": "unscored"}

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


def market_riskoff(as_of, gauges):
    """The market check's rule: risk-off needs at least two hot gauges."""
    n_hot = n = 0
    for sid in gauges:
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        n += 1
        n_hot += _hot(e)
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
#  STEP 1b CONFIRMATION GATE
#  Every expectation below was measured before the build, on the live published
#  series plus FRED histories pulled during Step 1b. This run repeats it on the
#  exact production series, fetched live, through the production code.
# ===========================================================================
def gate_1b():
    print("\n" + "=" * 100)
    print("STEP 1b CONFIRMATION GATE -- production code, production series, live FRED")
    print("=" * 100)

    print("\n-- A. Funding stress (commercial paper minus T-bill) at the moments that matter --\n")
    print(f"{'moment':<28}{'date':<12}{'reading':<38}expected from the pre-build test")
    for lbl, d, exp in [
            ("2001 recession", "2001-03-01", "unscored: under the 5-yr floor (history from 1997)"),
            ("ABCP freeze, first crack", "2007-08-01", "danger"),
            ("GFC onset", "2007-12-01", "danger"),
            ("pre-Lehman", "2008-06-01", "caution or danger"),
            ("Lehman", "2008-10-01", "danger"),
            ("COVID", "2020-03-01", "danger"),
            ("2005 rate hikes", "2005-06-01", "calm -- hikes are not funding stress"),
            ("2018 rate hikes", "2018-03-01", "calm"),
            ("2022 rate shock", "2022-07-01", "calm"),
            ("2022 rate shock", "2022-10-01", "calm")]:
        print(f"{lbl:<28}{d:<12}{_fmt(eval_signal('CP minus T-bill', d)):<38}{exp}")
    for lbl, a, b in [("2004-06 hiking cycle", "2004-06-01", "2006-06-01"),
                      ("2016-18 hiking cycle", "2016-01-01", "2018-12-01"),
                      ("2022-23 hiking cycle", "2022-03-01", "2023-07-01"),
                      ("GFC, Aug 2007 - Mar 2009", "2007-08-01", "2009-03-01")]:
        ms = _months(a, b)
        hot = sum(1 for m in ms if (eval_signal("CP minus T-bill", m) or {}).get("state")
                  in ("caution", "alert"))
        print(f"   months at caution/danger, {lbl:<26} {hot}/{len(ms)}")
    print("   (pre-build test: 0/25, 4/36, 2/17 in the hiking cycles; 18/20 through the GFC)")

    print("\n-- B. Housing: mortgage-debt growth minus home-price growth --\n")
    for d, exp in [("2003-07-01", "danger (2003-04 refinancing boom -- a known early fire)"),
                   ("2006-01-01", "caution"), ("2006-07-01", "danger"), ("2007-07-01", "danger"),
                   ("2008-04-01", "danger"), ("2013-01-01", "calm (equity rebuilding)"),
                   ("2021-07-01", "calm"), ("2026-04-01", "calm")]:
        print(f"   {d:<12}{_fmt(eval_signal('Mortgage debt vs prices', d)):<38}expected: {exp}")

    print("\n-- C. Regime at every date, with and without the curve-momentum vote --\n")
    old_g = [s for s in md.GROWTH_MOM if s != "Curve momentum"]
    same = 0
    print(f"{'date':<12}{'without curve':<36}{'with curve (as shipped)':<36}curve vote")
    for dt in DATES:
        a, b = regime_at(dt, old_g), regime_at(dt)
        e = eval_signal("Curve momentum", dt)
        vote = "worsening" if e and e["det"] else "improving" if e and e["imp"] else "none"
        same += a[0] == b[0]
        print(f"{dt:<12}{a[0] + f' g{a[3]}w/{a[4]}b':<36}{b[0] + f' g{b[3]}w/{b[4]}b':<36}{vote}"
              + ("" if a[0] == b[0] else "   <-- label differs"))
    print(f"   {same}/{len(DATES)} regime labels identical (pre-build test: the curve vote "
          f"changes the growth read in 2 of 441 months)")

    print("\n-- D. Market check through the 2008 crisis, with and without the funding gauge --\n")
    base = [s for s, _ in MARKET_GAUGES if s != "CP minus T-bill"]
    full = [s for s, _ in MARKET_GAUGES]
    for lbl, g in (("without funding", base), ("with funding", full)):
        segs = []
        for yr, a, b in (("2007", "2007-06-01", "2007-12-01"), ("2008", "2008-01-01", "2008-12-01"),
                         ("2009", "2009-01-01", "2009-06-01")):
            segs.append(yr + " " + "".join("R" if market_riskoff(m, g)[0] else "."
                                           for m in _months(a, b)))
        print(f"   {lbl:<16} " + " | ".join(segs))
    print("   (R = risk-off; 2007 runs Jun-Dec, 2009 Jan-Jun. Pre-build test: without funding it\n"
          "    drops out in spring 2008 -- the Bear Stearns relief rally -- and with funding it holds.)")
    calm = [m for a, b in [("2003-06-01", "2006-12-01"), ("2012-01-01", "2014-12-01"),
                           ("2016-06-01", "2018-06-01"), ("2023-06-01", "2026-08-01")]
            for m in _months(a, b)]
    for lbl, g in (("without funding", base), ("with funding", full)):
        r = sum(market_riskoff(m, g)[0] for m in calm)
        print(f"   calm-period risk-off months, {lbl:<16} {r}/{len(calm)}")
    print("   (pre-build test on VIX + stress index alone: 8/143 -> 13/143. The credit-spread\n"
          "    gauge, which FRED serves only from 2023, is included above; offline it adds 2-3.)")

    print("\n-- F. Daily noise: the pre-build test used MONTHLY averages; the dashboard reads "
          "DAILY values --\n")
    ind, raw = INDS["CP minus T-bill"], SERIES.get("CP minus T-bill")
    if not raw:
        print("   (funding series did not load -- see the failure list at the top)")
    else:
        windows = {"calm stretches": [("2004-06-01", "2006-06-01"), ("2012-01-01", "2014-12-31"),
                                      ("2016-01-01", "2018-12-31"), ("2023-06-01", "2026-08-31")],
                   "year-ends, Dec 15-Jan 15": [(f"{y}-12-15", f"{y + 1}-01-15")
                                                for y in list(range(2004, 2006)) + list(range(2012, 2015))
                                                + list(range(2016, 2019)) + list(range(2023, 2026))],
                   "GFC, Aug 2007-Mar 2009": [("2007-08-01", "2009-03-31")]}
        print(f"   share of sampled trading days 'hot' (moving risk-off, or at caution/danger)")
        print(f"   {'':<22}" + "".join(f"{k:<28}" for k in windows))
        for lbl, series in (("daily (as shipped)", raw), ("5-day average", _trailing_mean(raw, 5)),
                            ("20-day average", _trailing_mean(raw, 20))):
            cells = []
            for spans in windows.values():
                days = [d for d, _ in series if any(a <= d <= b for a, b in spans)][::5]
                hot = sum(_hot_sc(_score(ind, series, {}, d)) for d in days)
                cells.append(f"{hot}/{len(days)} = {100 * hot / max(1, len(days)):.0f}%")
            print(f"   {lbl:<22}" + "".join(f"{c:<28}" for c in cells))
        print("   If daily reads materially hotter in calm stretches or at year-ends than the\n"
              "   averages while the GFC column holds, a short average is the fix -- chosen from\n"
              "   this table, not guessed.")

    print("\n-- E. Today --\n")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for sid in ("Curve momentum", "CP minus T-bill", "Mortgage debt vs prices"):
        print(f"   {sid:<26}{_fmt(eval_signal(sid, today))}")
    r = market_riskoff(today, full)
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

    gate_1b()


if __name__ == "__main__":
    main()
