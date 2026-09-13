"""
Regime persistence backtest -- NOT part of the live dashboard, and separate
from historical_check.py's own crisis-anchored checks.

The question this answers: historically, how often has this regime
classifier flipped for only a day or two before reverting? DEADBAND_K and
REGIME_MARGIN were tuned against known crisis dates (does the classifier
read correctly at 2000/2008/2020/2022) -- that is a DIFFERENT question from
"how long does an assigned regime typically last before flipping again,"
which has never been measured. If short-lived regimes turn out to be
common, a persistence requirement (only change the displayed regime after
N consecutive daily readings agree) is justified, and this shows what N
would actually filter out. If they're rare, the current instant-flip
design already matches what the backtest validated, and adding a delay
would just slow down real signal for no real benefit.

This evaluates the regime DAILY (not weekly) over ~26 years, because the
concrete worry is day-to-day noise: two of the seven INFLATION_MOM signals
(T5YIE, T5YIFR) update daily while the rest are monthly/quarterly, so a
regime can mechanically flip between two calendar days even though nothing
else in the picture changed. Weekly sampling would hide exactly that.

Runtime: a few thousand dates x ~20 signals each -- expect low single-digit
minutes, not seconds. Progress prints every 500 dates so it's clear it's
still working.

Run: python regime_persistence.py   (needs FRED_API_KEY, same as
                                      historical_check.py; must sit next to
                                      macro_dashboard.py and
                                      historical_check.py)
"""
import historical_check as hc
from datetime import datetime, timedelta

START = datetime(2000, 1, 1)
END = datetime(2026, 9, 12)     # today
STEP_DAYS = 1

dates = []
d = START
while d <= END:
    dates.append(d.strftime("%Y-%m-%d"))
    d += timedelta(days=STEP_DAYS)

print(f"Evaluating regime at {len(dates)} daily dates, {dates[0]} to {dates[-1]} ...")
seq = []
for k, dt in enumerate(dates):
    name, *_ = hc.regime_at(dt)
    seq.append((dt, name))
    if (k + 1) % 500 == 0:
        print(f"  ... {k + 1}/{len(dates)}")

# ---- Run-length analysis: consecutive stretches of the same regime ----
runs = []
cur_regime, cur_start = seq[0][1], seq[0][0]
length = 1
for k in range(1, len(seq)):
    dt, name = seq[k]
    if name == cur_regime:
        length += 1
    else:
        runs.append((cur_regime, cur_start, seq[k - 1][0], length))
        cur_regime, cur_start, length = name, dt, 1
runs.append((cur_regime, cur_start, seq[-1][0], length))

print(f"\n{len(runs)} total regime runs over {len(dates)} daily readings "
      f"({dates[0]} to {dates[-1]})")
print(f"{'regime':<24}{'start':<12}{'end':<12}{'days':<8}")
print("-" * 60)
for regime, start, end, days in runs:
    flag = "  <-- <=2-DAY BLIP" if days <= 2 else ""
    print(f"{regime:<24}{start:<12}{end:<12}{days:<8}{flag}")

lengths = [r[3] for r in runs]
one_day = sum(1 for l in lengths if l == 1)
two_or_fewer = sum(1 for l in lengths if l <= 2)
week_or_fewer = sum(1 for l in lengths if l <= 7)
print("\n--- Summary ---")
print(f"Total runs: {len(runs)}")
print(f"1-day runs (caught by requiring 2 consecutive daily readings to agree): "
      f"{one_day} ({one_day / len(runs) * 100:.0f}%)")
print(f"<=2-day runs (caught by requiring 3 consecutive readings): "
      f"{two_or_fewer} ({two_or_fewer / len(runs) * 100:.0f}%)")
print(f"<=1-week runs (caught by requiring 8 consecutive readings): "
      f"{week_or_fewer} ({week_or_fewer / len(runs) * 100:.0f}%)")
print(f"Median run length: {sorted(lengths)[len(lengths) // 2]} days")
longest = max(lengths)
print(f"Longest run: {longest} days "
      f"({[r for r in runs if r[3] == longest][0][0]})")
