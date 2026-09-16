"""
Market-check false-alarm validation -- NOT part of the live dashboard.

The missing measurement. We validated the market check's LEAD TIME at three
crises (did it turn cautious before each top), but NEVER its false-alarm rate
(at every point it flagged risk, how often did the market actually fall). That
is the same test that just showed the raw allocation lean is barely better
than a coin flip. This runs it for the market check, because the market check
is a heavily-weighted INPUT to the allocation engine -- if it is also noisy,
"fix allocation by leaning on the market check" would just import the noise.

CRUCIAL SEPARATION: the market-check verdict combines two things --
  (a) market_riskoff: the pure market-pricing gauges (credit spreads, VIX,
      financial stress, S&P 200-day trend). This is the market check's OWN
      signal.
  (b) macro: which is literally the allocation engine's Overall-equity lean
      (already shown noisy this session).
Testing the full verdict would partly re-test the allocation layer. So this
reports THREE things separately:
  1. market_riskoff alone  -- the market check's own gauges, isolated
  2. "Confirmed risk-off"  -- the full combined verdict (both must agree)
  3. the base rate         -- how often the market falls over any window
So we can see exactly which component, if any, carries real signal.

Reuses eval_state / equity_trend_hot / allocation_at VERBATIM from
market_check_backtest.py. Forward returns via yfinance ^GSPC, same as the
allocation validation. Same revised-data asterisk (point-in-time is the
agreed next step regardless of this result).

Run: python market_check_validation.py   (needs FRED_API_KEY; must sit next
                                            to macro_dashboard.py,
                                            historical_check.py, and
                                            market_check_backtest.py)
"""
import collections
from datetime import datetime, timedelta

import macro_dashboard as md
import market_check_backtest as mcb

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

print("Fetching SP500 (FRED, for the 200-day equity-trend gauge)...")
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


def market_riskoff_at(as_of):
    """The market check's OWN signal, isolated from the allocation-derived
    macro component. Copied exactly from market_check_at's gauge logic."""
    n_hot = total = 0
    for sid in ("BAMLH0A0HYM2", "VIXCLS", "STLFSI4"):
        e = mcb.eval_state(sid, as_of)
        if e:
            total += 1
            n_hot += bool(e["deteriorating"] or e["state"] in ("caution", "alert"))
    et = mcb.equity_trend_hot(SP500_FRED, as_of)
    if et is not None:
        total += 1
        n_hot += et
    return (n_hot >= 2), n_hot, total


def full_verdict_at(as_of):
    alloc, _, _ = mcb.allocation_at(as_of)
    verdict, macro, n_hot, total = mcb.market_check_at(as_of, SP500_FRED, alloc)
    return verdict


ALL_DATES = weekly_dates(datetime(1998, 1, 1), datetime(2026, 3, 1))
THRESH_FLAT = 0.0
THRESH_DRAW = -5.0

print(f"\nEvaluating {len(ALL_DATES)} weekly dates (1998 to 2026)...")

# collect once
rows = []   # (date, market_riskoff_bool, full_verdict, fwd90)
for k, dt in enumerate(ALL_DATES):
    mro, _, _ = market_riskoff_at(dt)
    fv = full_verdict_at(dt)
    f90 = forward_return(dt, 90)
    rows.append((dt, mro, fv, f90))
    if (k + 1) % 200 == 0:
        print(f"  ... {k + 1}/{len(ALL_DATES)}")

valid = [(d, m, v, f) for d, m, v, f in rows if f is not None]
all_fwd = [f for _, _, _, f in valid]
base_avg = sum(all_fwd) / len(all_fwd)
base_neg = sum(1 for f in all_fwd if f < THRESH_FLAT) / len(all_fwd) * 100


def report(label, flagged):
    n = len(flagged)
    if not n:
        print(f"\n{label}: never flagged.")
        return
    corr_flat = sum(1 for f in flagged if f < THRESH_FLAT)
    corr_draw = sum(1 for f in flagged if f < THRESH_DRAW)
    avg = sum(flagged) / n
    print(f"\n{label}")
    print(f"  flagged: {n} of {len(valid)} weeks ({n/len(valid)*100:.0f}%)")
    print(f"  correct, market fell (<0%):     {corr_flat} ({corr_flat/n*100:.0f}%)")
    print(f"  correct, real drawdown (<-5%):  {corr_draw} ({corr_draw/n*100:.0f}%)")
    print(f"  FALSE ALARM (rose anyway):      {n-corr_flat} ({(n-corr_flat)/n*100:.0f}%)")
    print(f"  avg fwd-90d when flagged:       {avg:+.2f}%   (baseline all weeks {base_avg:+.2f}%)")
    edge = corr_flat / n * 100 - base_neg
    print(f"  -> hit rate {corr_flat/n*100:.0f}% vs base rate {base_neg:.0f}%  "
          f"= {edge:+.0f} pts of edge  {'(REAL SIGNAL)' if edge >= 8 else '(barely better than noise)' if edge < 4 else '(marginal)'}")


print(f"\n{'=' * 100}")
print("MARKET-CHECK FALSE-ALARM RESULTS")
print(f"base rate: {base_neg:.0f}% of all weeks are followed by a negative 90d return; "
      f"avg 90d return {base_avg:+.2f}%")
print(f"{'=' * 100}")

# 1. the market check's OWN signal, isolated
report("[1] market_riskoff ALONE (pure market gauges: spreads/VIX/stress/200d)",
       [f for _, m, _, f in valid if m])

# 2. the full combined verdict
report("[2] 'Confirmed risk-off' (full verdict: market gauges AND allocation agree)",
       [f for _, _, v, f in valid if v == "Confirmed risk-off"])

# 3. for contrast: any risk-off-flavoured verdict (incl. unconfirmed)
report("[3] ANY risk-off verdict (Confirmed OR Unconfirmed -- the loosest read)",
       [f for _, _, v, f in valid if "risk-off" in v])

print(f"\n{'=' * 100}")
print("READ: compare each hit rate to the base rate. The market check EARNS the")
print('"reliable input" label only if [1] or [2] clears the base rate by a real')
print("margin. If [1] (its own gauges) is barely above base rate like the raw")
print("allocation lean was, then the market check is ALSO noise, and the whole")
print("approach -- not just allocation -- needs rethinking before real money.")
print("If [1]/[2] show real edge, the market check is a sound foundation to")
print("rebuild allocation on.")
print(f"{'=' * 100}")
