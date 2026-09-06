"""
ALFRED point-in-time check.

Same evaluation as historical_check.py, but instead of truncating today's revised
series, it pulls each series EXACTLY as it was reported on each past date, using
FRED's ALFRED archive (realtime_start / realtime_end). That respects two things
at once: publication lag (as of June 2007 you only had April payrolls) and later
revisions (you get the number first reported, not today's revised one).

Run it, then compare its output line-by-line against historical_check.py's: where
the recession flags or regime differ, revisions were flattering the revised run.

Caveats: ALFRED only archives vintages back to when FRED started keeping them, so
some series have no vintage for the earliest dates and simply drop out (fewer
signals early -- honest, not a bug). Computed/scraped signals (CAPE, Buffett, net
liquidity) have no vintages, so this check covers the regime and recession read
only -- which is exactly the revision-sensitive macro data that matters here.

Run:  python alfred_check.py     (needs FRED_API_KEY; ~350 API calls, ~3-4 min)
"""
import os
import requests
import macro_dashboard as md
from datetime import datetime, timedelta

FRED = "https://api.stlouisfed.org/fred/series/observations"
KEY = os.environ.get("FRED_API_KEY")

EVENTS = {
    "2001 recession": "2001-03-01",
    "2008 recession": "2007-12-01",
    "2020 recession": "2020-02-01",
    "2022 inflation/bear": "2022-01-01",
}
DATES = ["1999-06-01", "2000-01-01", "2000-07-01", "2001-03-01",
         "2006-06-01", "2007-06-01", "2007-12-01", "2008-09-01",
         "2019-06-01", "2020-01-01", "2020-03-01",
         "2021-06-01", "2021-12-01", "2022-06-01",
         "2024-01-01", "2025-06-01", "2026-01-01"]
RECESSION = ["T10Y3M", "SAHMREALTIME", "IC4WSA", "BAMLH0A0HYM2", "NFCI", "DRTSCILM",
             "WEI", "JTSJOL", "HPIPONM226S", "MSACSR"]

INDS = {}
for _grp in (md.THEMES, md.DRILLDOWNS):
    for _lst in _grp.values():
        for _ind in _lst:
            INDS[_ind["id"]] = _ind
PCTILE_IDS = {i["id"] for lst in md.DRILLDOWNS.values() for i in lst}
for _th in ("Liquidity", "Housing"):
    for _i in md.THEMES.get(_th, []):
        PCTILE_IDS.add(_i["id"])
for _lst in (*md.THEMES.values(), *md.DRILLDOWNS.values()):
    for _i in _lst:
        if _i.get("pctile"):
            PCTILE_IDS.add(_i["id"])


def fetch_vintage(sid, start, as_of):
    """Series as it existed on `as_of` (respects publication lag AND revisions)."""
    if not KEY:
        raise SystemExit("FRED_API_KEY is not set.")
    try:
        r = requests.get(FRED, params={
            "series_id": sid, "api_key": KEY, "file_type": "json",
            "observation_start": start, "realtime_start": as_of,
            "realtime_end": as_of, "sort_order": "asc", "limit": 100000,
        }, timeout=30)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception:
        return []
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    return out


def eval_vintage(sid, as_of):
    ind = INDS.get(sid)
    if not ind or ind.get("compute"):        # computed/scraped -> no vintage
        return None
    raw = fetch_vintage(sid, ind["start"], as_of)
    if not raw:
        return None
    if ind.get("scale"):
        raw = [(d, v * ind["scale"]) for d, v in raw]
    series = md.yoy(raw) if ind["kind"] == "yoy" else raw
    if len(series) < 8:
        return None
    tr = md.trend(series)
    pctile = sid in PCTILE_IDS
    if pctile:
        st = md.substate_of(ind["worry"], tr["pct_of_range"] if tr else None)
    else:
        st = md.state_of(ind["worry"], series[-1][1], ind.get("caution"), ind.get("alert"))
    sig = bool(tr and tr.get("typical", 0) > 0
               and abs(tr["delta"]) >= md.DEADBAND_K * tr["typical"])
    bad = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    det = bool(sig and bad)
    imp = bool(sig and ind["worry"] and not bad and tr["delta"] != 0)
    return {"state": st, "det": det, "imp": imp, "asof_last": series[-1][0]}


def _momentum(ids, as_of):
    worse = better = 0
    for sid in ids:
        e = eval_vintage(sid, as_of)
        if e is None:
            continue
        worse += e["det"]
        better += e["imp"]
    return worse, better


def regime_at(as_of):
    gw, gb = _momentum(md.GROWTH_MOM, as_of)
    iw, ib = _momentum(md.INFLATION_MOM, as_of)
    growth = "decelerating" if (gw - gb) >= md.REGIME_MARGIN else "accelerating"
    inflation = "accelerating" if (iw - ib) >= md.REGIME_MARGIN else "decelerating"
    return md.REGIMES[(growth, inflation)][0], gw, gb, iw, ib


def recession_flags(as_of):
    hot, tot = [], 0
    for sid in RECESSION:
        e = eval_vintage(sid, as_of)
        if e is None:
            continue
        tot += 1
        if e["det"] or e["state"] in ("caution", "alert"):
            hot.append(sid)
    return hot, tot


def main():
    print("=" * 100)
    print("ALFRED POINT-IN-TIME CHECK -- each date sees only what was reported then, "
          "un-revised.")
    print("Compare to historical_check.py (revised) to see how much revisions "
          "flattered the timing.")
    print("=" * 100)
    print(f"{'date':<12}{'regime':<24}{'momentum(w-b)':<16}{'recession flags'}")
    print("-" * 100)
    for dt in DATES:
        name, gw, gb, iw, ib = regime_at(dt)
        hot, tot = recession_flags(dt)
        mom = f"g{gw}-{gb} i{iw}-{ib}"
        flags = f"{len(hot)}/{tot} " + ",".join(s[:5] for s in hot)
        print(f"{dt:<12}{name:<24}{mom:<16}{flags}")

    print("\nDid the recession read fire BEFORE each onset, IN REAL TIME "
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


if __name__ == "__main__":
    main()
