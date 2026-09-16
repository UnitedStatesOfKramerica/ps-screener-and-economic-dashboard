"""
Allocation-engine validation -- NOT part of the live dashboard.

The 9-bucket allocation leans have NEVER been validated -- only the regime
and market check were. This checks them through THREE deliberate lenses,
chosen specifically so the test can't just flatter the tool:

  LENS 1 -- lean at the three known tops (2000/2007/2022). The obvious
     check. On its own it's the survivorship trap: we picked those dates
     because we already know they were tops. Necessary but not sufficient.

  LENS 2 -- flip-frequency in calm periods. A tool that's correct at the
     tops but churns the allocation every few weeks in normal times would be
     actively harmful to act on (transaction costs, whipsaw). Measured over
     known-calm stretches (2013-2017, 2024-2025). Never checked before for
     allocation.

  LENS 3 -- false-alarm rate across ALL history. The honest reverse of the
     survivorship test: at EVERY point the Overall-equity-exposure bucket
     leaned defensive (underweight), did the market actually fall next, or
     was it a false alarm? A defensive call is "correct" if the forward
     return was negative (or below a small threshold), a "false alarm" if
     the market rose anyway. This is the number most likely to come back
     uncomfortable, and it's wanted regardless.

Reuses allocation_at() VERBATIM from market_check_backtest.py -- the same
reconstruction already validated this session -- so the allocation logic
under test is identical to what that script (and the live dashboard) use.

Forward returns use the S&P 500 via yfinance (^GSPC), real history back to
1996 -- same source and treatment as regime_actionability_backtest.py.

IMPORTANT ASTERISK (applies to this and every backtest so far): uses FRED
latest-vintage (revised) data, not point-in-time. See the validation
section of pending-items. Lead/false-alarm numbers here will shift once the
ALFRED rebuild is done; this is the current-data baseline.

Run: python allocation_validation.py   (needs FRED_API_KEY; must sit next
                                          to macro_dashboard.py,
                                          historical_check.py, and
                                          market_check_backtest.py)
"""
import collections
from datetime import datetime, timedelta

import macro_dashboard as md
import market_check_backtest as mcb   # reuse the validated allocation_at()

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("yfinance not installed -- pip install yfinance")


def weekly_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return out


print("Fetching S&P 500 (^GSPC) via yfinance...")
sp = yf.Ticker("^GSPC").history(start="1996-01-01", auto_adjust=True)
SP_SORTED = sorted((d.strftime("%Y-%m-%d"), float(c))
                    for d, c in zip(sp.index, sp["Close"]))
print(f"  {len(SP_SORTED)} points, {SP_SORTED[0][0]} to {SP_SORTED[-1][0]}")

print("Fetching SP500 (FRED, for market-check equity-trend gauge)...")
SP500_FRED = md.fetch("SP500", "2015-01-01")
print(f"  {len(SP500_FRED)} points")


def sp_on_or_after(date_str):
    for d, c in SP_SORTED:
        if d >= date_str:
            return c
    return None


def forward_return(as_of, days):
    c0 = sp_on_or_after(as_of)
    if c0 is None:
        return None
    target = (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    c1 = sp_on_or_after(target)
    if c1 is None:
        return None
    return (c1 / c0 - 1) * 100


def lean_of(as_of, bucket="Overall equity exposure"):
    alloc, _, _ = mcb.allocation_at(as_of)
    return alloc[bucket]["lean"], alloc[bucket]["net"]


# ============================================================================
print(f"\n{'=' * 100}")
print("LENS 1 -- full 9-bucket allocation at the three known market tops")
print("(survivorship-aware: necessary check, but we picked these dates knowing")
print("they were tops, so a good result here alone proves little)")
print(f"{'=' * 100}")
TOPS = [
    ("2000-03-10", "dot-com peak"),
    ("2007-10-09", "pre-GFC peak"),
    ("2022-01-03", "2022 inflation-surge peak"),
]
for dt, why in TOPS:
    alloc, val_c, cons_c = mcb.allocation_at(dt)
    print(f"\n--- {dt} ({why}) ---")
    print(f"  valuation={val_c['condition']}  consumer={cons_c['condition']}")
    oee = alloc["Overall equity exposure"]
    verdict = ("DEFENSIVE (correct direction at a top)" if oee["lean"] == "Underweight"
               else "RISK-ON (wrong direction at a top)" if oee["lean"] == "Overweight"
               else oee["lean"])
    print(f"  >> Overall equity exposure: {oee['lean']} (net {oee['net']:+.2f}) -- {verdict}")
    for b in md.ALLOC_BUCKETS:
        a = alloc[b]
        print(f"     {b:<32} {a['lean']:<12} net={a['net']:+.2f}")


# ============================================================================
print(f"\n{'=' * 100}")
print("LENS 2 -- flip-frequency of the Overall-equity lean during KNOWN-CALM")
print("periods. High churn here = harmful to act on, even if tops are right.")
print(f"{'=' * 100}")
CALM_WINDOWS = [
    ("2013-01-01", "2017-12-31", "2013-2017 mid-cycle bull"),
    ("2024-01-01", "2025-12-31", "2024-2025 expansion"),
]
for start, end, label in CALM_WINDOWS:
    dates = weekly_dates(datetime.strptime(start, "%Y-%m-%d"),
                         datetime.strptime(end, "%Y-%m-%d"))
    leans = [lean_of(dt)[0] for dt in dates]
    flips = sum(1 for i in range(1, len(leans)) if leans[i] != leans[i - 1])
    dist = collections.Counter(leans)
    weeks = len(leans)
    print(f"\n{label} ({weeks} weeks, {start} to {end}):")
    print(f"  lean changes: {flips}  (~1 flip every {weeks/flips:.1f} weeks)"
          if flips else f"  lean changes: 0 (never flipped)")
    print(f"  time in each lean: " +
          ", ".join(f"{k} {v} ({v/weeks*100:.0f}%)" for k, v in dist.most_common()))


# ============================================================================
print(f"\n{'=' * 100}")
print("LENS 3 -- FALSE-ALARM RATE across all history. At every weekly date the")
print("Overall-equity lean was DEFENSIVE (underweight), what did the S&P do over")
print("the next 90 days? 'Correct' = market fell; 'false alarm' = it rose anyway.")
print("This is the reliability-critical number.")
print(f"{'=' * 100}")
ALL_DATES = weekly_dates(datetime(1998, 1, 1), datetime(2026, 3, 1))
print(f"Evaluating {len(ALL_DATES)} weekly dates (1998 to 2026)...")

# threshold: a defensive call "pays off" if fwd 90d return < this. 0% is the
# strict version (market must actually fall); we also report at -5% (a real
# drawdown, not just flat) so a call that dodged a big drop isn't lumped with
# one that dodged a 1% wobble.
THRESH_FLAT = 0.0
THRESH_DRAW = -5.0

defensive = []   # (date, net, fwd90)
for k, dt in enumerate(ALL_DATES):
    lean, net = lean_of(dt)
    if lean == "Underweight":
        f90 = forward_return(dt, 90)
        if f90 is not None:
            defensive.append((dt, net, f90))
    if (k + 1) % 200 == 0:
        print(f"  ... {k + 1}/{len(ALL_DATES)}")

n = len(defensive)
if n:
    correct_flat = sum(1 for _, _, f in defensive if f < THRESH_FLAT)
    correct_draw = sum(1 for _, _, f in defensive if f < THRESH_DRAW)
    avg_fwd = sum(f for _, _, f in defensive) / n
    # baseline: what did the S&P do on average over ALL weeks, defensive or
    # not? If defensive weeks aren't meaningfully worse than average, the
    # signal isn't discriminating.
    all_fwd = [forward_return(dt, 90) for dt in ALL_DATES]
    all_fwd = [f for f in all_fwd if f is not None]
    base_avg = sum(all_fwd) / len(all_fwd)
    base_neg = sum(1 for f in all_fwd if f < THRESH_FLAT) / len(all_fwd) * 100

    print(f"\n  Weeks flagged DEFENSIVE (underweight equity): {n} of {len(ALL_DATES)} "
          f"({n/len(ALL_DATES)*100:.0f}%)")
    print(f"  Of those, fwd-90d return was:")
    print(f"    negative at all (<0%):        {correct_flat} ({correct_flat/n*100:.0f}%)  "
          f"-- 'correct, market fell'")
    print(f"    a real drawdown (<-5%):       {correct_draw} ({correct_draw/n*100:.0f}%)  "
          f"-- 'correct, dodged a real drop'")
    print(f"    FALSE ALARM (rose anyway):    {n-correct_flat} ({(n-correct_flat)/n*100:.0f}%)")
    print(f"  Avg fwd-90d return when defensive:  {avg_fwd:+.2f}%")
    print(f"  Avg fwd-90d return, ALL weeks:      {base_avg:+.2f}%  (baseline)")
    print(f"  -> defensive weeks were {'WORSE' if avg_fwd < base_avg else 'NOT worse'} "
          f"than average by {abs(avg_fwd-base_avg):.2f} pts")
    print(f"  (For reference: {base_neg:.0f}% of ALL weeks are followed by a negative")
    print(f"   90d return, so a coin-flip 'always defensive' would be 'correct' that often.)")

    print(f"\n  The discriminating question: {correct_flat/n*100:.0f}% correct when defensive")
    print(f"  vs {base_neg:.0f}% base rate. If those are close, the defensive signal is")
    print(f"  barely better than always crying wolf. If materially higher, it's real.")

    # Also break down by conviction: do STRONGER defensive leans do better?
    print(f"\n  Does conviction help? Defensive weeks bucketed by net magnitude:")
    for lo, hi, lbl in [(0, 3, "mild (net 0 to -3)"), (3, 8, "moderate (-3 to -8)"),
                        (8, 999, "strong (< -8)")]:
        sub = [f for _, net, f in defensive if lo <= -net < hi]
        if sub:
            corr = sum(1 for f in sub if f < THRESH_FLAT) / len(sub) * 100
            print(f"    {lbl:<24} n={len(sub):<5} avg fwd90 {sum(sub)/len(sub):+6.2f}%  "
                  f"correct {corr:.0f}%")
