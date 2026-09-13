"""
Historical check for the macro dashboard.

Re-runs the dashboard's OWN signal logic (regime classifier, recession signals,
valuation) at a set of past dates, so you can see whether the signals fired IN
TIME around the 2001, 2008, 2020 and 2022 episodes -- or only after the fact.

IMPORTANT caveat, read it: this uses FRED's *latest-vintage* values truncated to
each date -- today's revised numbers, not what was actually known then. It is a
sanity check, NOT a true point-in-time backtest (that needs ALFRED vintages).
Revisions flatter the results, so read "it caught it" with salt. Signals that
did not exist yet in real time (WEI from 2008, breakevens from 2003) simply drop
out of the earlier dates.

Run:  python historical_check.py     (needs FRED_API_KEY, like the dashboard)
"""
import macro_dashboard as md
from datetime import datetime, timedelta

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

THEME_IDS = {i["id"] for lst in md.THEMES.values() for i in lst}
INDS = {}
for _grp in (md.THEMES, md.DRILLDOWNS):
    for _lst in _grp.values():
        for _ind in _lst:
            INDS[_ind["id"]] = _ind


def _fetch_raw(ind):
    if ind.get("compute") == "cape":
        return md.fetch_cape(ind["start"])
    if ind.get("compute") == "concentration":
        return md.fetch_concentration_ratio(ind["start"])
    if ind.get("compute") == "multpl":
        return md.fetch_multpl(ind["url"], ind["start"], ind.get("lo", 3.0),
                               ind.get("hi", 80.0), tag=ind.get("tag", "x"))
    if ind.get("compute") == "ratio":
        num = (md.fetch_sum(ind["nums"], ind["start"]) if ind.get("nums")
               else md.fetch(ind["num"], ind["start"]))
        return md.ratio_align(num, md.fetch(ind["den"], ind["start"]),
                              ind.get("ratio_scale", 1.0))
    return md.fetch(ind["id"], ind["start"])


NEEDED = (set(md.GROWTH_MOM) | set(md.INFLATION_MOM) | set(RECESSION)
          | {"Shiller CAPE", "Market cap / GDP", "SPY/RSP"})
print("Fetching full history for", len(NEEDED), "series ...")
RAW = {}
for sid in NEEDED:
    ind = INDS.get(sid)
    if ind:
        RAW[sid] = _fetch_raw(ind)


def eval_signal(sid, as_of, percentile_state):
    ind, raw = INDS.get(sid), RAW.get(sid)
    if not ind or not raw:
        return None
    raw = [(d, v) for d, v in raw if d <= as_of]
    if ind.get("scale"):
        raw = [(d, v * ind["scale"]) for d, v in raw]
    series = md.yoy(raw) if ind["kind"] == "yoy" else raw
    if len(series) < 8:
        return None
    tr = md.trend(series)
    if percentile_state:
        st = md.substate_of(ind["worry"], tr["pct_of_range"] if tr else None)
    else:
        st = md.state_of(ind["worry"], series[-1][1], ind.get("caution"), ind.get("alert"))
    sig = bool(tr and tr.get("typical", 0) > 0
               and abs(tr["delta"]) >= md.DEADBAND_K * tr["typical"])
    moved_bad = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    det = bool(sig and moved_bad)
    imp = bool(sig and ind["worry"] and not moved_bad and tr["delta"] != 0)
    return {"state": st, "det": det, "imp": imp, "latest": series[-1][1],
            "worry": ind["worry"]}


def _momentum(ids, as_of):
    worse = better = 0
    for sid in ids:
        e = eval_signal(sid, as_of, percentile_state=(sid not in THEME_IDS))
        if e is None:
            continue
        worse += e["det"]
        better += e["imp"]
    return worse, better


def regime_at(as_of):
    g_worse, g_better = _momentum(md.GROWTH_MOM, as_of)
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
        e = eval_signal(sid, as_of, percentile_state=(sid not in THEME_IDS))
        if e is None:
            continue
        tot += 1
        if e["det"] or e["state"] in ("caution", "alert"):
            hot.append(sid)
    return hot, tot


def valuation_at(as_of):
    e = eval_signal("Shiller CAPE", as_of, percentile_state=False)
    if e is None:
        e = eval_signal("Market cap / GDP", as_of, percentile_state=False)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def concentration_at(as_of):
    # Percentile-based like the live dashboard's "pctile": True treatment --
    # historical_check's generic eval_signal helper doesn't read that flag
    # (it only looks at THEME_IDS membership), so this hardcodes it, the
    # same way valuation_at() hardcodes its own two signals.
    e = eval_signal("SPY/RSP", as_of, percentile_state=True)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


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

    print("\nValuation at the two bubble peaks (should read danger/high percentile):")
    for label, dt in [("dot-com 2000", "2000-03-01"), ("2021 peak", "2021-12-01")]:
        vst, vval = valuation_at(dt)
        print(f"  {label:<16} {dt}: {vst} ({vval})")

    print("\nMarket concentration (SPY/RSP, rebased=100 at 2003 launch) -- no data "
          "before 2003, so dot-com can't be checked directly; 2021-2025 should "
          "read high/alert (Magnificent 7 era narrowing the rally):")
    for label, dt in [("2007 (pre-GFC)", "2007-06-01"), ("2018 (calm)", "2018-06-01"),
                       ("2020 low", "2020-03-01"), ("2021 peak", "2021-12-01"),
                       ("2024", "2024-01-01"), ("latest", DATES[-1])]:
        cst, cval = concentration_at(dt)
        print(f"  {label:<16} {dt}: {cst} ({cval})")


if __name__ == "__main__":
    main()
