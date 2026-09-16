"""
Point-in-time, confidence-graded equity-risk validation -- the "DEFCON" test.
NOT part of the live dashboard.

Three things this session forced, folded into one test:

  1. POINT-IN-TIME. Every prior backtest used revised data. This uses
     alfred_deep.py's vintage() -- each date sees only what was actually
     reported then, un-revised -- for the raw primary series ALFRED archives
     back to the 1990s. (The full 70-signal engine can't be rebuilt
     point-in-time pre-2020; this is an honest PROXY of the core equity-risk
     read from the deep-archived recession/credit/growth signals, labelled as
     such -- not a claim to reconstruct the whole 9-bucket engine.)

  2. CONFIDENCE TIERS (the DEFCON idea). Prior tests treated risk-off as
     binary and measured the binary's false-alarm rate -- which lumped a
     1-signal warning in with a 5-signal one. This grades each date by HOW
     MANY of the risk signals are flashing, and measures false-alarm rate
     SEPARATELY per tier. The hypothesis: higher-confidence readings have
     materially lower false-alarm rates. If true, the fix to false alarms is
     "only act on high tiers," measured rather than guessed.

  3. RIGHT HORIZON. Prior tests used 90 days -- wrong for a tool meant to
     surface conditions held 6-18 months. This tests forward returns at 6,
     12, and 18 months. (Stats caveat, stated up front: 18mo gives very few
     independent windows over 28 years -- ~18 -- so 18mo tier splits are
     suggestive, not conclusive; 6mo is the most trustworthy.)

BENCHMARK: absolute (did the S&P fall) -- correct here, since the equity-risk
read is a market-wide call, not a relative tilt. (Relative-to-index tests for
the individual bucket tilts -- energy, value, treasuries -- are a separate
follow-up.)

Forward returns via yfinance ^GSPC. The risk signals are the deep-archived
subset from alfred_deep.py. Same public-domain series; CAPE is truncated
current data (valuation barely revises -- a fair proxy, per alfred_deep).

Run: python pit_confidence_validation.py   (needs FRED_API_KEY; heavy on
                                              ALFRED calls -- allow time;
                                              must sit next to alfred_deep.py
                                              and macro_dashboard.py)
"""
import collections
from datetime import datetime, timedelta

import alfred_deep as ad

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("yfinance not installed -- pip install yfinance")


# ---- forward returns -------------------------------------------------------
print("Fetching S&P 500 (^GSPC) via yfinance...")
sp = yf.Ticker("^GSPC").history(start="1996-01-01", auto_adjust=True)
SP_SORTED = sorted((d.strftime("%Y-%m-%d"), float(c))
                    for d, c in zip(sp.index, sp["Close"]))
print(f"  {len(SP_SORTED)} points, {SP_SORTED[0][0]} to {SP_SORTED[-1][0]}")


def sp_on_or_after(date_str):
    for d, c in SP_SORTED:
        if d >= date_str:
            return c
    return None


def fwd_return(as_of, months):
    c0 = sp_on_or_after(as_of)
    if c0 is None:
        return None
    tgt = (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=int(30.44 * months))).strftime("%Y-%m-%d")
    c1 = sp_on_or_after(tgt)
    if c1 is None:
        return None
    return (c1 / c0 - 1) * 100


# ---- the point-in-time risk score ------------------------------------------
# Each builder from alfred_deep returns (value, flag_string). A non-empty flag
# means that signal was actively warning on that date, IN REAL TIME. We count
# how many are warning -> that count IS the confidence tier. These are the
# deep-archived signals that carry real point-in-time history to the 1990s;
# each is a recognised recession/credit/growth-stress indicator.
RISK_SIGNALS = [
    ("Yield curve inverted", ad.sig_curve),
    ("Lending standards tightening", ad.sig_lending),
    ("Housing starts weak", ad.sig_hstarts),
    ("Permits weak", ad.sig_permits),
    ("Payrolls weak", ad.sig_payrolls),
    ("Sahm rising/triggered", ad.sig_sahm),
    ("Jobless claims rising", ad.sig_claims),
    ("Industrial production weak", ad.sig_indpro),
]


def risk_count(as_of):
    """How many risk signals were flashing on `as_of`, point-in-time.
    Returns (n_flashing, n_available, [flashing labels])."""
    n_flash = n_avail = 0
    flashing = []
    for label, fn in RISK_SIGNALS:
        r = fn(as_of)
        if r is None:
            continue
        n_avail += 1
        _, flag = r
        if flag:                       # non-empty flag = actively warning
            n_flash += 1
            flashing.append(label)
    return n_flash, n_avail, flashing


def monthly_dates(start, end):
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


ALL = monthly_dates(datetime(1998, 1, 1), datetime(2026, 1, 1))
HORIZONS = (6, 12, 18)

print(f"\nEvaluating {len(ALL)} monthly dates point-in-time (ALFRED vintages)...")
print("(each date = one round of ALFRED calls; this is the slow part)")

records = []   # (date, n_flash, {h: fwd})
for k, dt in enumerate(ALL):
    nf, na, _ = risk_count(dt)
    fwds = {h: fwd_return(dt, h) for h in HORIZONS}
    records.append((dt, nf, fwds))
    if (k + 1) % 24 == 0:
        print(f"  ... {k + 1}/{len(ALL)}")


# ---- base rates ------------------------------------------------------------
def base_rates(h):
    vals = [r[2][h] for r in records if r[2][h] is not None]
    neg = sum(1 for v in vals if v < 0) / len(vals) * 100
    avg = sum(vals) / len(vals)
    return neg, avg, len(vals)


print(f"\n{'=' * 100}")
print("BASE RATES (all months, any tier) -- the bar every tier must beat")
print(f"{'=' * 100}")
for h in HORIZONS:
    neg, avg, n = base_rates(h)
    print(f"  {h:>2}mo forward:  {neg:.0f}% of all months are followed by a DECLINE, "
          f"avg return {avg:+.2f}%  (n={n})")


# ---- the DEFCON table ------------------------------------------------------
print(f"\n{'=' * 100}")
print("CONFIDENCE-TIER FALSE-ALARM TABLE -- the core result")
print("For each tier (how many risk signals were flashing that month, point-in-")
print("time), the % of the time the market DECLINED over each horizon, and the")
print("average forward return. If higher tiers show higher decline-rates / more")
print("negative returns, confidence grading WORKS and the false-alarm fix is")
print("'only act on high tiers'. If flat across tiers, the signals don't carry")
print("gradeable information even point-in-time.")
print(f"{'=' * 100}")

# tier buckets: 0, 1, 2, 3, 4+ flashing
def tier_of(nf):
    return nf if nf < 4 else 4   # 4 = "4 or more"

TIER_LABEL = {0: "0 flashing (all clear)", 1: "1 flashing", 2: "2 flashing",
              3: "3 flashing", 4: "4+ flashing (max alarm)"}

for h in HORIZONS:
    print(f"\n--- {h}-month forward horizon ---")
    print(f"{'tier':<26}{'n':>6}{'% declined':>13}{'avg fwd ret':>14}{'vs base':>12}")
    print("-" * 71)
    base_neg, base_avg, _ = base_rates(h)
    by_tier = collections.defaultdict(list)
    for dt, nf, fwds in records:
        if fwds[h] is not None:
            by_tier[tier_of(nf)].append(fwds[h])
    for t in (0, 1, 2, 3, 4):
        vals = by_tier.get(t, [])
        if not vals:
            continue
        n = len(vals)
        decl = sum(1 for v in vals if v < 0) / n * 100
        avg = sum(vals) / n
        edge = decl - base_neg
        tag = ""
        if n >= 10:
            tag = "  <-- real edge" if edge >= 15 else "  (noise)" if abs(edge) < 6 else ""
        print(f"{TIER_LABEL[t]:<26}{n:>6}{decl:>12.0f}%{avg:>13.2f}%{edge:>+11.0f}{tag}")
    print(f"  (base rate this horizon: {base_neg:.0f}% decline, {base_avg:+.2f}% avg)")


# ---- the three crises, real-time, graded ----------------------------------
print(f"\n{'=' * 100}")
print("THE THREE CRISES -- what confidence tier was showing in the run-up,")
print("point-in-time (un-revised). Did the alarm level rise BEFORE each top?")
print(f"{'=' * 100}")
CRISES = [
    ("DOT-COM (peak 2000-03)", ["1998-06-01", "1999-01-01", "1999-06-01",
                                 "2000-01-01", "2000-03-01"]),
    ("GFC (peak 2007-10)", ["2006-06-01", "2007-01-01", "2007-06-01",
                             "2007-09-01", "2007-12-01"]),
    ("2022 INFLATION (peak 2022-01)", ["2021-01-01", "2021-06-01", "2021-09-01",
                                        "2021-12-01", "2022-03-01"]),
]
for label, dates in CRISES:
    print(f"\n{label}:")
    for dt in dates:
        nf, na, flashing = risk_count(dt)
        print(f"  {dt}: tier {nf}/{na}  " +
              (", ".join(flashing) if flashing else "(nothing flashing)"))

print(f"\n{'=' * 100}")
print("READ: the DEFCON table is the answer to the false-alarm problem. If tier 3")
print("and 4+ decline materially more than the base rate while tier 0-1 sit at or")
print("below it, then a high-confidence-only trigger is both rare AND meaningful --")
print("exactly the 'harder to trigger, fires seldom, means something' design. The")
print("crisis section shows whether the alarm escalated in real time before each")
print("top. Remember the 18mo column rests on few independent windows -- weight 6")
print("and 12mo more heavily.")
print(f"{'=' * 100}")
