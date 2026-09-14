"""
Does regime PERSISTENCE predict anything -- or does the type of regime alone
carry all the information? NOT part of the live dashboard.

The specific question this answers: if you see a regime change, does it
matter whether it's day 1 of that regime or week 20 of it, for what happens
to the market next? Tests this with NO LOOKAHEAD -- at each date, "how long
has this regime already persisted" only counts backward from that date,
exactly what you'd actually know in real time, never how long the episode
eventually turns out to last.

DATA NOTE: FRED's own SP500 series only goes back to 2015 (discovered
earlier this session), so it can't test forward returns around 2000 or
2008 -- the two episodes that matter most here. This pulls the S&P 500
directly via yfinance (^GSPC) instead, which has real history back through
the 1990s -- same source already used for the concentration ratio, just
fetched here for its own sake. Computed and used internally only; nothing
raw is published, same treatment as everywhere else this index is touched
in this project.

Run: python regime_actionability_backtest.py   (needs FRED_API_KEY; must
                                                  sit next to
                                                  macro_dashboard.py and
                                                  historical_check.py)
"""
import collections
from datetime import datetime, timedelta

import historical_check as hc
import macro_dashboard as md

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


print("Fetching S&P 500 (^GSPC) full history via yfinance...")
sp500 = yf.Ticker("^GSPC").history(start="1996-01-01", auto_adjust=True)
SP_SORTED = sorted((d.strftime("%Y-%m-%d"), float(c))
                    for d, c in zip(sp500.index, sp500["Close"]))
print(f"  {len(SP_SORTED)} points, {SP_SORTED[0][0]} to {SP_SORTED[-1][0]}")


def sp_on_or_after(date_str):
    for d, c in SP_SORTED:
        if d >= date_str:
            return d, c
    return None, None


def forward_return(as_of, days):
    _, c0 = sp_on_or_after(as_of)
    if c0 is None:
        return None
    target = (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    _, c1 = sp_on_or_after(target)
    if c1 is None:
        return None
    return (c1 / c0 - 1) * 100


DATES = weekly_dates(datetime(1998, 1, 1), datetime(2026, 3, 1))  # leaves 180d forward room
print(f"\nComputing regime at {len(DATES)} weekly dates (reusing historical_check.regime_at)...")
seq = []
for k, dt in enumerate(DATES):
    name, *_ = hc.regime_at(dt)
    seq.append((dt, name))
    if (k + 1) % 200 == 0:
        print(f"  ... {k + 1}/{len(DATES)}")

# Persistence-so-far, in weeks. NO LOOKAHEAD: persisted[i] depends only on
# seq[0..i], counted backward from that date -- exactly what would be known
# in real time, never the eventual full length of the episode.
persisted = []
run_len, prev_name = 0, None
for dt, name in seq:
    run_len = run_len + 1 if name == prev_name else 1
    persisted.append(run_len)
    prev_name = name

BUCKETS = [(1, 1, "1 week (just changed)"), (2, 4, "2-4 weeks"),
           (5, 12, "5-12 weeks"), (13, 26, "13-26 weeks"), (27, 9999, "27+ weeks")]


def bucket_for(w):
    for lo, hi, label in BUCKETS:
        if lo <= w <= hi:
            return label
    return "?"


ORDER = [b[2] for b in BUCKETS]


def print_bucket_table(rows_by_bucket):
    print(f"{'persistence bucket':<24}{'n':>6}{'fwd 30d avg':>14}{'fwd 90d avg':>14}{'fwd 180d avg':>14}")
    print("-" * 72)
    for b in ORDER:
        n = len(rows_by_bucket[b][30])
        if n == 0:
            continue
        a30 = sum(rows_by_bucket[b][30]) / n
        a90 = sum(rows_by_bucket[b][90]) / len(rows_by_bucket[b][90])
        a180 = sum(rows_by_bucket[b][180]) / len(rows_by_bucket[b][180])
        print(f"{b:<24}{n:>6}{a30:>13.2f}%{a90:>13.2f}%{a180:>13.2f}%")


print(f"\n{'=' * 100}")
print("PART 1 -- forward S&P 500 return, ALL regimes, bucketed by how long the")
print("current regime has already persisted (no lookahead)")
print(f"{'=' * 100}")
by_bucket = collections.defaultdict(lambda: collections.defaultdict(list))
for (dt, name), w in zip(seq, persisted):
    b = bucket_for(w)
    for days in (30, 90, 180):
        r = forward_return(dt, days)
        if r is not None:
            by_bucket[b][days].append(r)
print_bucket_table(by_bucket)

print(f"\n{'=' * 100}")
print("PART 2 -- SAME, but ONLY the two 'negative' regimes (Stagflation,")
print("Slowdown/Disinflation). This is the actionable case: does persisting")
print("longer inside a bad regime predict worse forward returns, or is the")
print("first week just as informative as the twentieth?")
print(f"{'=' * 100}")
NEG = {"Stagflation", "Slowdown / Disinflation"}
by_bucket_neg = collections.defaultdict(lambda: collections.defaultdict(list))
for (dt, name), w in zip(seq, persisted):
    if name not in NEG:
        continue
    b = bucket_for(w)
    for days in (30, 90, 180):
        r = forward_return(dt, days)
        if r is not None:
            by_bucket_neg[b][days].append(r)
print_bucket_table(by_bucket_neg)

print(f"\n{'=' * 100}")
print("PART 3 -- the specific case: regime persistence in the run-up to the")
print("actual dot-com and pre-GFC peaks. 'X weeks before' counts backward")
print("from the real peak date -- what would you have seen in real time?")
print(f"{'=' * 100}")
for peak_dt, label in [("2000-03-10", "dot-com peak"), ("2007-10-09", "pre-GFC peak")]:
    print(f"\n{label} ({peak_dt}):")
    for weeks_before in (0, 1, 4, 12, 26):
        target = (datetime.strptime(peak_dt, "%Y-%m-%d")
                  - timedelta(weeks=weeks_before)).strftime("%Y-%m-%d")
        cand = [i for i, (dt, _) in enumerate(seq) if dt <= target]
        if not cand:
            continue
        idx = cand[-1]
        dt, name = seq[idx]
        w = persisted[idx]
        print(f"  {weeks_before:>3} weeks before ({dt}): regime={name:<24} "
              f"persisted {w} week(s) so far")

print(f"\n{'=' * 100}")
print("SUMMARY")
print(f"{'=' * 100}")
print("Read Part 1 and Part 2 for whether the AVERAGE forward return actually")
print("differs across persistence buckets. If the numbers are roughly flat")
print("across buckets, persistence-so-far isn't adding predictive information")
print("beyond what the regime type itself already tells you -- appearance would")
print("be the real signal, not duration. If forward returns get monotonically")
print("worse (for negative regimes) as persistence increases, that's evidence")
print("duration itself is informative. Part 3 shows what this would have looked")
print("like in real time at the two actual historical peaks.")
