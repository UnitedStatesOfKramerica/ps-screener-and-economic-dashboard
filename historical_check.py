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

STEP 1a GATE (new): this file now also carries the candidate robust-z magnitude
scoring and reports it SIDE BY SIDE with the scoring the dashboard uses today,
at every signal and every date. Nothing in macro_dashboard.py is changed -- this
run is purely a measurement, so the sigma bands get chosen from real data rather
than from a guess, and the blast radius on recession flags and allocation is
visible BEFORE production moves. See report sections A-F at the bottom.

Run:  python historical_check.py     (needs FRED_API_KEY, like the dashboard)
"""
import macro_dashboard as md
import statistics
from datetime import datetime, timedelta
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

THEME_IDS = {i["id"] for lst in md.THEMES.values() for i in lst}
INDS = {}
for _grp in (md.THEMES, md.DRILLDOWNS):
    for _lst in _grp.values():
        for _ind in _lst:
            INDS[_ind["id"]] = _ind


# ---------------------------------------------------------------------------
# Percentile-state resolution, matched to the dashboard exactly.
#
# build() resolves percentile_state as:
#     theme signals -> ind.get("pctile", theme in ("Liquidity", "Housing"))
#     drill signals -> ind.get("pctile", True)
# The previous version of this file approximated that with `sid not in
# THEME_IDS`, which silently DISAGREED with the dashboard for every Liquidity
# and Housing theme signal and for the seven signals carrying an explicit
# "pctile" flag -- so the historical report was not re-running the dashboard's
# own logic for those, despite the docstring saying it was. (valuation_at and
# concentration_at hardcoded their own overrides to work around it.) Fixed here
# so the old-vs-new comparison below is honest on both sides.
# ---------------------------------------------------------------------------
PCTILE_OF = {}
for _theme, _lst in md.THEMES.items():
    for _ind in _lst:
        PCTILE_OF[_ind["id"]] = _ind.get("pctile", _theme in ("Liquidity", "Housing"))
for _theme, _lst in md.DRILLDOWNS.items():          # drilldowns win, as in INDS
    for _ind in _lst:
        PCTILE_OF[_ind["id"]] = _ind.get("pctile", True)


def pctile_for(sid):
    return PCTILE_OF.get(sid, False)


# ---------------------------------------------------------------------------
# CANDIDATE SCORING (Step 1a) -- lives here, not in macro_dashboard.py, so this
# run cannot affect the nightly build. It graduates into the dashboard only
# after the numbers below are reviewed.
# ---------------------------------------------------------------------------
ROBUST_Z_CAP = 4.0          # beyond ~4 sigma "very alarmed" is "very alarmed"
MIN_Z_HISTORY = 8           # too few points for a stable median/MAD

# Candidate sigma bands, tested as a sweep in section A2 before one is chosen.
Z_CAUTION = 1.0
Z_ALERT = 2.0
Z_CALM = -1.0

# Signals whose thresholds are EXTERNALLY defined, not invented by this project.
# These keep absolute-level scoring; a z-score against their own history would
# destroy real information:
#   T10Y3M / T10Y2Y -- zero is inversion, an economically meaningful line, not a
#       percentile of anything.
#   SAHMREALTIME    -- 0.50 is the published Sahm rule trigger, externally
#       validated; re-deriving it from the series' own spread would be worse.
#   NFCI            -- the Chicago Fed already publishes this standardised to
#       mean 0 / sd 1, so it IS a z-score; re-standardising it against its own
#       median is double-standardisation, and 0.0 is the publisher's own
#       "average financial conditions" line.
# Everything else is scored on robust z. This is the "binary flags only where
# genuinely meaningful" rule from the design, widened slightly: the test is not
# "is it binary" but "did somebody outside this project define the line".
ABSOLUTE = {"T10Y3M", "T10Y2Y", "SAHMREALTIME", "NFCI"}


def robust_z(vals, latest, worry, cap=ROBUST_Z_CAP):
    """How many robust standard deviations `latest` sits from normal, in the
    WORRYING direction. Positive = toward danger, negative = toward safe.

    Uses median + MAD*1.4826 (the constant makes MAD comparable to stdev for
    normally distributed data). Outlier-resistant, unlike mean/stdev, and uses
    the full history so it never forgets a crisis. worry='up': high readings are
    dangerous. worry='down': low readings are. Result is capped at +/-cap so one
    extreme signal cannot dominate a composite.
    """
    if worry is None or len(vals) < MIN_Z_HISTORY:
        return None
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    if mad == 0:
        sd = statistics.pstdev(vals)          # MAD degenerate (many identical)
        if sd == 0:
            return 0.0
        raw = (latest - med) / sd
    else:
        raw = (latest - med) / (mad * 1.4826)
    z = raw if worry == "up" else -raw        # orient: positive = toward danger
    return max(-cap, min(cap, z))


def zstate_of(z, caution=None, alert=None):
    if z is None:
        return "neutral"
    c = Z_CAUTION if caution is None else caution
    a = Z_ALERT if alert is None else alert
    return "alert" if z >= a else "caution" if z >= c else "calm"


def distance_phrase(z):
    """Plain English for the magnitude. No sigma is ever shown to the reader.

    Six bands, not four: the extra two on the safe side exist because the
    dashboard is not allowed to be an echo chamber -- a signal that is genuinely
    better than normal has to be able to say so, not just fail to alarm.
    """
    if z is None:
        return "not scored"
    if z < -1.0:
        return "better than normal"
    if z < 0.5:
        return "normal"
    if z < 1.0:
        return "slightly worse than normal"
    if z < 2.0:
        return "somewhat worse than normal"
    if z < 3.0:
        return "far worse than normal"
    return "at a historic extreme"


def _fetch_raw(ind):
    if ind.get("compute") == "cape":
        return md.fetch_cape(ind["start"])
    if ind.get("compute") == "concentration":
        return md.fetch_concentration_ratio(ind["start"])
    if ind.get("compute") == "issuance":
        return md.fetch_fed_issuance(ind["issuance_col"])
    if ind.get("compute") == "combine":
        return md.combine_series(ind["parts"], ind["start"], ind.get("ratio_scale", 1.0))
    if ind.get("compute") == "margin":
        return md.fetch_finra_margin(ind["start"])
    if ind.get("compute") == "ecy":
        return md.fetch_ecy(ind["start"])
    if ind.get("compute") == "top10":
        return []   # no historical source exists -- accumulates live only, see its own docstring
    if ind.get("compute") == "multpl":
        return md.fetch_multpl(ind["url"], ind["start"], ind.get("lo", 3.0),
                               ind.get("hi", 80.0), tag=ind.get("tag", "x"))
    if ind.get("compute") == "ratio":
        num = (md.fetch_sum(ind["nums"], ind["start"]) if ind.get("nums")
               else md.fetch(ind["num"], ind["start"]))
        return md.ratio_align(num, md.fetch(ind["den"], ind["start"]),
                              ind.get("ratio_scale", 1.0))
    return md.fetch(ind["id"], ind["start"])


# Full universe. Previously this was a subset (regime + recession + allocation +
# Valuation/Consumer); the scoring gate needs EVERY indicator, because the
# old-vs-new comparison has to cover every signal that carries a state, not just
# the ones that vote. Extra cost is a few dozen FRED calls.
_VAL_CONS_IDS = set()
for _t in ("Valuation", "Consumer"):
    for _lst in (md.THEMES.get(_t, []), md.DRILLDOWNS.get(_t, [])):
        _VAL_CONS_IDS |= {_i["id"] for _i in _lst}

NEEDED = (set(INDS.keys())
          | set(md.GROWTH_MOM) | set(md.INFLATION_MOM) | set(RECESSION)
          | set(md.ALLOC.keys()) | _VAL_CONS_IDS
          | {"Shiller CAPE", "Market cap / GDP", "SPY/RSP",
             "IPO issuance", "SEO issuance", "VIXCLS", "STLFSI4"})
print("Fetching full history for", len(NEEDED), "series ...")
RAW, LOAD_FAILED = {}, []
for sid in NEEDED:
    ind = INDS.get(sid)
    if ind:
        RAW[sid] = _fetch_raw(ind)
        if not RAW[sid] and ind.get("compute") != "top10":
            LOAD_FAILED.append((sid, ind.get("label", "")))

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
            harm.append(f"{len(votes)} allocation vote(s): "
                        + ", ".join(b for b, _ in votes))
        if sid in RECESSION:
            harm.append("recession flag count")
        print(f"!!   {sid:<16} {lbl[:34]:<36} "
              + ("-> " + "; ".join(harm) if harm else "-> display only"))
    print("!" * 78 + "\n")
else:
    print("All series loaded.\n")


@lru_cache(maxsize=None)
def eval_signal(sid, as_of, percentile_state=None):
    """Evaluate one signal at a date under BOTH scorings.

    percentile_state defaults to the dashboard's own resolution for this signal;
    pass it explicitly only to force a comparison.
    """
    ind, raw = INDS.get(sid), RAW.get(sid)
    if not ind or not raw:
        return None
    if percentile_state is None:
        percentile_state = pctile_for(sid)
    raw = [(d, v) for d, v in raw if d <= as_of]
    if ind.get("scale"):
        raw = [(d, v * ind["scale"]) for d, v in raw]
    series = md.yoy(raw) if ind["kind"] == "yoy" else raw
    if len(series) < 8:
        return None
    tr = md.trend(series)
    latest = series[-1][1]

    # --- scoring as it works TODAY -------------------------------------
    if percentile_state:
        old_state = md.substate_of(ind["worry"], tr["pct_of_range"] if tr else None)
        old_mode = "percentile"
    else:
        old_state = md.state_of(ind["worry"], latest, ind.get("caution"), ind.get("alert"))
        old_mode = ("threshold" if (ind.get("caution") is not None
                                    and ind.get("alert") is not None
                                    and ind["worry"]) else "unscored")

    # --- candidate scoring ---------------------------------------------
    z = robust_z([v for _, v in series], latest, ind["worry"])
    if sid in ABSOLUTE and ind.get("caution") is not None and ind.get("alert") is not None:
        new_state = md.state_of(ind["worry"], latest, ind["caution"], ind["alert"])
        new_mode = "absolute"
    elif z is not None:
        new_state = zstate_of(z)
        new_mode = "robust_z"
    else:
        new_state = old_state                 # too little history -- keep as-is
        new_mode = "fallback"

    # --- direction axis: UNCHANGED, shared by both -----------------------
    sig = bool(tr and tr.get("typical", 0) > 0
               and abs(tr["delta"]) >= md.DEADBAND_K * tr["typical"])
    moved_bad = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    det = bool(sig and moved_bad)
    imp = bool(sig and ind["worry"] and not moved_bad and tr["delta"] != 0)
    return {"state": old_state, "old_state": old_state, "old_mode": old_mode,
            "new_state": new_state, "new_mode": new_mode, "z": z,
            "phrase": distance_phrase(z),
            "det": det, "imp": imp, "latest": latest, "worry": ind["worry"]}


def _momentum(ids, as_of):
    worse = better = 0
    for sid in ids:
        e = eval_signal(sid, as_of)
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


def recession_flags(as_of, which="old"):
    key = "old_state" if which == "old" else "new_state"
    hot, tot = [], 0
    for sid in RECESSION:
        e = eval_signal(sid, as_of)
        if e is None:
            continue
        tot += 1
        if e["det"] or e[key] in ("caution", "alert"):
            hot.append(sid)
    return hot, tot


def valuation_at(as_of):
    e = eval_signal("Shiller CAPE", as_of)
    if e is None:
        e = eval_signal("Market cap / GDP", as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def concentration_at(as_of):
    e = eval_signal("SPY/RSP", as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def issuance_at(sid, as_of):
    e = eval_signal(sid, as_of)
    return (e["state"], round(e["latest"], 1)) if e else ("n/a", None)


def _counts(states):
    return {k: sum(1 for s in states if s == k)
            for k in ("alert", "caution", "calm", "neutral")}


def _cfmt(c):
    return f"A{c['alert']:>2} C{c['caution']:>2} c{c['calm']:>2} n{c['neutral']:>2}"


# ===========================================================================
#  STEP 1a SCORING GATE
# ===========================================================================
def scoring_gate(today):
    print("\n" + "=" * 100)
    print("STEP 1a SCORING GATE -- candidate robust-z vs the scoring in production today")
    print("=" * 100)

    rows = []
    for sid in sorted(INDS):
        e = eval_signal(sid, today)
        if e is None:
            continue
        rows.append((sid, INDS[sid].get("label", sid), e))

    # ---- A1: every signal, side by side --------------------------------
    print("\n-- A1. Every signal today: OLD state -> NEW state "
          "(* = the read changes) --\n")
    print(f"{'signal':<16}{'label':<34}{'old':<10}{'new':<10}{'z':>7}  "
          f"{'mode':<11}{'plain English'}")
    print("-" * 100)
    changed = 0
    for sid, lbl, e in rows:
        mark = " " if e["old_state"] == e["new_state"] else "*"
        changed += (mark == "*")
        zs = f"{e['z']:+.2f}" if e["z"] is not None else "   --"
        print(f"{mark}{sid:<15}{lbl[:32]:<34}{e['old_state']:<10}{e['new_state']:<10}"
              f"{zs:>7}  {e['new_mode']:<11}{e['phrase']}")
    print("-" * 100)
    print(f"{changed} of {len(rows)} signals change state.")
    print(f"  OLD totals: {_cfmt(_counts([e['old_state'] for _, _, e in rows]))}")
    print(f"  NEW totals: {_cfmt(_counts([e['new_state'] for _, _, e in rows]))}")
    print("  (A=alert/danger, C=caution, c=calm, n=neutral/unscored)")

    zs = [e["z"] for _, _, e in rows if e["z"] is not None]
    if zs:
        pinned = [(sid, e["z"]) for sid, _, e in rows
                  if e["z"] is not None and abs(e["z"]) >= ROBUST_Z_CAP - 0.01]
        print(f"\n  z distribution: min {min(zs):+.2f}  median {statistics.median(zs):+.2f}"
              f"  max {max(zs):+.2f}")
        print(f"  pinned at the +/-{ROBUST_Z_CAP} cap: {len(pinned)} of {len(zs)}")
        if len(pinned) > len(zs) * 0.15:
            print("  !! WARNING: more than 15% of signals are pinned at the cap. The")
            print("     score has lost resolution where it matters most -- either the")
            print("     cap is too tight, or MAD is too narrow on these series and the")
            print("     denominator needs a floor. Do NOT ship the bands until this is")
            print("     resolved.")
        for sid, z in pinned[:10]:
            print(f"       {sid:<16}{z:+.2f}")

    unscored = [sid for sid, _, e in rows if e["old_mode"] == "unscored"]
    if unscored:
        print(f"\n  Signals carrying NO level read at all today, which robust-z "
              f"gives one to for the first time:\n    {', '.join(unscored)}")

    # ---- A2: band sweep -------------------------------------------------
    print("\n-- A2. Sigma-band sweep: what each candidate band pair would produce "
          "today --\n")
    print("  Picking bands from this table rather than from a guess. The target is "
          "not 'match the old counts'\n  -- the old counts come from thresholds we "
          "now think are wrong -- but a distribution where danger is\n  genuinely "
          "rare and caution is meaningful rather than constant.\n")
    print(f"{'caution/alert':<18}{'alert':<9}{'caution':<10}{'calm':<8}"
          f"{'% of signals hot'}")
    print("-" * 60)
    global Z_CAUTION, Z_ALERT
    keep_c, keep_a = Z_CAUTION, Z_ALERT
    for c, a in [(0.75, 1.75), (1.0, 2.0), (1.25, 2.25), (1.5, 2.5), (1.5, 3.0)]:
        Z_CAUTION, Z_ALERT = c, a
        sts = []
        for sid, _, e in rows:
            if e["new_mode"] == "robust_z":
                sts.append(zstate_of(e["z"]))
            else:
                sts.append(e["new_state"])
        cc = _counts(sts)
        hot = cc["alert"] + cc["caution"]
        print(f"{c:.2f} / {a:.2f}      {cc['alert']:<9}{cc['caution']:<10}"
              f"{cc['calm']:<8}{hot / max(1, len(sts)) * 100:.0f}%")
    Z_CAUTION, Z_ALERT = keep_c, keep_a
    print(f"\n  (the run above and below uses {keep_c:.2f} / {keep_a:.2f})")

    # ---- B: regime, old vs new -----------------------------------------
    print("\n-- B. Regime at every date: must be IDENTICAL --\n")
    print("  The regime is computed from the DIRECTION axis only (6-month delta vs")
    print("  the deadband), never from signal state -- verified by reading")
    print("  regime_at -> _momentum, which reads only det/imp. So a magnitude-only")
    print("  rework cannot move it. This section exists to PROVE that, not to test it:")
    print("  any difference here means the change leaked somewhere it shouldn't.\n")
    print(f"{'date':<12}{'regime':<26}{'momentum(w-b)'}")
    print("-" * 60)
    for dt in DATES:
        name, g, i, gw, gb, iw, ib = regime_at(dt)
        print(f"{dt:<12}{name:<26}g{gw}-{gb} i{iw}-{ib}")
    print("\n  -> Regime is UNCHANGED by Step 1a by construction. The magnitude axis")
    print("     belongs to the confidence meter (Step 2), not to the regime label.")

    # ---- C: recession flags, old vs new ---------------------------------
    print("\n-- C. Recession flag count, OLD vs NEW, at every date --\n")
    print(f"{'date':<12}{'old':<28}{'new':<28}{'delta'}")
    print("-" * 82)
    for dt in DATES:
        ho, to = recession_flags(dt, "old")
        hn, tn = recession_flags(dt, "new")
        so = f"{len(ho)}/{to} " + ",".join(s[:5] for s in ho)
        sn = f"{len(hn)}/{tn} " + ",".join(s[:5] for s in hn)
        d = len(hn) - len(ho)
        print(f"{dt:<12}{so:<28}{sn:<28}{d:+d}")

    print("\n  Did the recession read fire BEFORE each onset (>=2 flags in any of "
          "the 6 months prior)?")
    print(f"  {'event':<24}{'old':<26}{'new'}")
    for label, onset in EVENTS.items():
        od = datetime.strptime(onset, "%Y-%m-%d")
        res = {}
        for which in ("old", "new"):
            fired = []
            for k in range(1, 7):
                cd = (od - timedelta(days=30 * k)).strftime("%Y-%m-01")
                hot, _ = recession_flags(cd, which)
                if len(hot) >= 2:
                    fired.append(cd)
            res[which] = f"YES from {min(fired)}" if fired else "no"
        print(f"  {label:<24}{res['old']:<26}{res['new']}")

    # ---- D: allocation blast radius -------------------------------------
    print("\n-- D. Allocation blast radius: signals that actually cast votes --\n")
    print("  Allocation counts a signal when its state is caution/alert (or it is")
    print("  deteriorating), so a shift in the band changes real leans. This is the")
    print("  number most likely to come back uncomfortable.\n")
    alloc_ids = [s for s in md.ALLOC if s in INDS]
    print(f"{'date':<12}{'old hot':<12}{'new hot':<12}{'of':<6}{'delta'}")
    print("-" * 50)
    for dt in DATES:
        o = n = t = 0
        for sid in alloc_ids:
            e = eval_signal(sid, dt)
            if e is None:
                continue
            t += 1
            o += e["old_state"] in ("caution", "alert")
            n += e["new_state"] in ("caution", "alert")
        print(f"{dt:<12}{o:<12}{n:<12}{t:<6}{n - o:+d}")

    # ---- E: the 2008 / 2022 gate ----------------------------------------
    print("\n-- E. The 2008 / 2022 gate, signal by signal --\n")
    for label, dt in [("2007-12 (GFC onset)", "2007-12-01"),
                      ("2008-09 (Lehman)", "2008-09-01"),
                      ("2021-12 (pre-2022 top)", "2021-12-01"),
                      ("2022-06 (bear)", "2022-06-01")]:
        name, g, i, gw, gb, iw, ib = regime_at(dt)
        ho, to = recession_flags(dt, "old")
        hn, tn = recession_flags(dt, "new")
        print(f"  {label:<24} regime={name}  "
              f"recession flags old {len(ho)}/{to} -> new {len(hn)}/{tn}")
        for sid in RECESSION:
            e = eval_signal(sid, dt)
            if e is None:
                print(f"      {sid:<14} (no data at this date)")
                continue
            zs = f"{e['z']:+.2f}" if e["z"] is not None else "  --"
            print(f"      {sid:<14} {e['old_state']:<9}-> {e['new_state']:<9}"
                  f"z={zs:<8}{e['phrase']}")
        print()

    # ---- F: verdict ------------------------------------------------------
    print("-- F. What this run decides --\n")
    print("  1. Which sigma bands to ship (section A2).")
    print("  2. Whether the new bands leave the 2008/2022 recession read at least")
    print("     as strong as today's (section C and E). If NEW fires later or")
    print("     weaker than OLD at those dates, the bands are too loose and")
    print("     section A2 says which pair to move to.")
    print("  3. How far allocation moves (section D), so the shift is a known")
    print("     quantity rather than a surprise on the next nightly build.")
    print("  Nothing in macro_dashboard.py has changed. Production is untouched.")


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

    print("\nEquity issuance around the dot-com peak -- this is the one signal in "
          "this whole check with real data reaching back that far (starts 1994), "
          "so it should actually show 1999-2000 as elevated/alert, not just cite it. "
          "Note: the Fed only refreshes this source a few times a year, so don't be "
          "surprised if nearby dates show identical values -- that's the data, not a bug:")
    for label, dt in [("1998 (before)", "1998-06-01"), ("1999 (boom)", "1999-06-01"),
                       ("2000 peak", "2000-03-01"), ("2001 (bust)", "2001-06-01"),
                       ("2021 peak", "2021-12-01"), ("latest", DATES[-1])]:
        ist, ival = issuance_at("IPO issuance", dt)
        sst, sval = issuance_at("SEO issuance", dt)
        print(f"  {label:<16} {dt}: IPO {ist} (${ival}B/12mo) | "
              f"SEO {sst} (${sval}B/12mo)")

    print("\nMarket concentration (SPY/RSP, rebased=100 at 2003 launch) -- no data "
          "before 2003, so dot-com can't be checked directly; 2021-2025 should "
          "read high/alert (Magnificent 7 era narrowing the rally):")
    for label, dt in [("2007 (pre-GFC)", "2007-06-01"), ("2018 (calm)", "2018-06-01"),
                       ("2020 low", "2020-03-01"), ("2021 peak", "2021-12-01"),
                       ("2024", "2024-01-01"), ("latest", DATES[-1])]:
        cst, cval = concentration_at(dt)
        print(f"  {label:<16} {dt}: {cst} ({cval})")

    scoring_gate(datetime.utcnow().strftime("%Y-%m-%d"))


if __name__ == "__main__":
    main()
