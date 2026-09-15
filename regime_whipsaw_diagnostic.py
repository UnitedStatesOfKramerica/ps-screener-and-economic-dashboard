"""
Two follow-ups from the three-crisis comparison, NOT part of the live
dashboard:

PART 1: diagnoses the regime whipsaw found in both dot-com (2000-03-13 to
2000-04-24) and 2022 (2022-02-07 to 2022-04-25) -- the regime flipped back
to "Reflation" mid-crash, and the 3-day persistence rule wouldn't have
caught it (these ran 6-12 weeks, not 1-2 days). This prints DAILY, with
which specific growth/inflation signals were worse vs better on the days
right before and during each whipsaw -- so we can see the actual mechanism
rather than guess whether it's classifier noise or a genuine realized-data
vs. market-pricing divergence.

PART 2: extends the GFC window back to find where the negative regime
actually started, since the prior test found it was "already negative"
before the window began and never pinned down a real number.

Run: python regime_whipsaw_diagnostic.py   (needs FRED_API_KEY; must sit
                                             next to macro_dashboard.py and
                                             historical_check.py)
"""
import historical_check as hc
import macro_dashboard as md
from datetime import datetime, timedelta


def momentum_detail(ids, as_of):
    worse_l, better_l = [], []
    for sid in ids:
        e = hc.eval_signal(sid, as_of, percentile_state=(sid not in hc.THEME_IDS))
        if e is None:
            continue
        if e["det"]:
            worse_l.append(hc.INDS[sid]["label"])
        elif e["imp"]:
            better_l.append(hc.INDS[sid]["label"])
    return worse_l, better_l


def daily_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


print("=" * 100)
print("PART 1 -- daily driver detail through both whipsaw windows")
print("=" * 100)

WHIPSAWS = [
    ("DOT-COM whipsaw", datetime(2000, 2, 25), datetime(2000, 4, 30)),
    ("2022 whipsaw", datetime(2022, 1, 25), datetime(2022, 5, 5)),
]

for label, start, end in WHIPSAWS:
    dates = daily_dates(start, end)
    print(f"\n{'-' * 100}")
    print(f"{label}: {dates[0]} to {dates[-1]}")
    print(f"{'-' * 100}")
    prev_name = None
    for dt in dates:
        name, g, i, gw, gb, iw, ib = hc.regime_at(dt)
        if name != prev_name:
            gwl, gbl = momentum_detail(md.GROWTH_MOM, dt)
            iwl, ibl = momentum_detail(md.INFLATION_MOM, dt)
            print(f"\n{dt}  ->  {name}  (growth {g}: {gw}w/{gb}b, "
                  f"inflation {i}: {iw}w/{ib}b)")
            print(f"    growth worse:     {', '.join(gwl) or '(none)'}")
            print(f"    growth better:    {', '.join(gbl) or '(none)'}")
            print(f"    inflation worse:  {', '.join(iwl) or '(none)'}")
            print(f"    inflation better: {', '.join(ibl) or '(none)'}")
            prev_name = name

print(f"\n{'=' * 100}")
print("PART 2 -- how far back does the GFC-era negative regime actually go?")
print("Extending the window back from 2006-06-01 in 3-month steps until we")
print("find a positive (Reflation/Goldilocks) regime, or hit a floor.")
print(f"{'=' * 100}")

FLOOR = datetime(2003, 1, 1)
d = datetime(2006, 6, 1)
NEGATIVE = {"Stagflation", "Slowdown / Disinflation"}
last_negative = None
while d >= FLOOR:
    dt = d.strftime("%Y-%m-%d")
    name, *_ = hc.regime_at(dt)
    is_neg = name in NEGATIVE
    print(f"  {dt}: {name}{'  (negative)' if is_neg else '  <-- POSITIVE, stopping here'}")
    if not is_neg:
        break
    last_negative = dt
    d -= timedelta(days=90)

if last_negative:
    print(f"\nEarliest date tested that was still negative: {last_negative}")
    print("If this printed all the way to the floor without ever hitting a")
    print("positive regime, the true start is even earlier than 2003 -- worth")
    print("extending further if so.")
