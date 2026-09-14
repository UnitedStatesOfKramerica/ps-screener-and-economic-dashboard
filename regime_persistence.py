"""
Regime persistence + margin calibration backtest -- NOT part of the live
dashboard. Run once, paste the full output back.

WHY THIS IS BEING RE-RUN: the earlier version of this script was run when
GROWTH_MOM had 13 signals and INFLATION_MOM had 7. Wiring up previously
unused signals took those to 23 and 8. DEADBAND_K, REGIME_MARGIN and the
3-day persistence rule were all calibrated against the OLD set, so those
numbers no longer describe the classifier that is actually running.

It now answers three questions in one pass instead of one:

  1. With the CURRENT signal set, how often does the regime flip for only a
     day or two? (Re-measures what the 3-day persistence rule was based on.)

  2. Is a flat REGIME_MARGIN=2 right for both axes now that growth has 23
     signals and inflation has 8? A net-2 margin is 8.7% of the growth axis
     but 25% of the inflation axis -- growth is now ~3x easier to flip. This
     grid-tests margin combinations so the constant can be chosen from data
     rather than guessed.

  3. For whichever margin looks best, what persistence threshold actually
     filters the noise?

Efficiency note: momentum counts are evaluated ONCE per date (the expensive
part), then every margin combination is re-derived from those cached counts.
So testing 12 configurations costs barely more than testing one.

Run: python regime_persistence.py   (needs FRED_API_KEY; must sit next to
                                      macro_dashboard.py and historical_check.py)
"""
import historical_check as hc
import macro_dashboard as md
from datetime import datetime, timedelta

START = datetime(2000, 1, 1)
END = datetime(2026, 9, 12)

dates = []
d = START
while d <= END:
    dates.append(d.strftime("%Y-%m-%d"))
    d += timedelta(days=1)

print(f"Signal set under test: {len(md.GROWTH_MOM)} growth, "
      f"{len(md.INFLATION_MOM)} inflation")
print(f"Evaluating {len(dates)} daily dates, {dates[0]} to {dates[-1]} ...")

counts = []
for k, dt in enumerate(dates):
    _, _, _, gw, gb, iw, ib = hc.regime_at(dt)
    counts.append((dt, gw, gb, iw, ib))
    if (k + 1) % 1000 == 0:
        print(f"  ... {k + 1}/{len(dates)}")


def regimes_for(g_margin, i_margin):
    seq = []
    for dt, gw, gb, iw, ib in counts:
        growth = "decelerating" if (gw - gb) >= g_margin else "accelerating"
        infl = "accelerating" if (iw - ib) >= i_margin else "decelerating"
        seq.append((dt, md.REGIMES[(growth, infl)][0]))
    return seq


def runs_of(seq):
    runs = []
    cur, start, length = seq[0][1], seq[0][0], 1
    for k in range(1, len(seq)):
        dt, name = seq[k]
        if name == cur:
            length += 1
        else:
            runs.append((cur, start, seq[k - 1][0], length))
            cur, start, length = name, dt, 1
    runs.append((cur, start, seq[-1][0], length))
    return runs


print("\n" + "=" * 78)
print("PART 1 -- MARGIN GRID (how stable is each configuration?)")
print("=" * 78)
print(f"{'g_margin':>9}{'i_margin':>9}{'runs':>7}{'median':>8}{'1-day':>8}"
      f"{'<=2day':>8}{'<=7day':>8}")
print("-" * 78)
grid = {}
for gm in (2, 3, 4, 5):
    for im in (2, 3):
        seq = regimes_for(gm, im)
        runs = runs_of(seq)
        L = [r[3] for r in runs]
        med = sorted(L)[len(L) // 2]
        one = sum(1 for x in L if x == 1)
        two = sum(1 for x in L if x <= 2)
        wk = sum(1 for x in L if x <= 7)
        grid[(gm, im)] = runs
        mark = "  <-- CURRENT" if (gm, im) == (md.REGIME_MARGIN, md.REGIME_MARGIN) else ""
        print(f"{gm:>9}{im:>9}{len(runs):>7}{med:>8}"
              f"{one:>7}{'':1}{two:>7}{'':1}{wk:>7}{mark}")

print("\nProportional alternative (margin scaled to axis size, ~15% of signals):")
gm_prop = max(2, round(0.15 * len(md.GROWTH_MOM)))
im_prop = max(2, round(0.15 * len(md.INFLATION_MOM)))
print(f"  would be g_margin={gm_prop}, i_margin={im_prop}")
seq = regimes_for(gm_prop, im_prop)
runs = runs_of(seq)
L = [r[3] for r in runs]
print(f"  runs={len(runs)}  median={sorted(L)[len(L)//2]}d  "
      f"1-day={sum(1 for x in L if x==1)}  <=2day={sum(1 for x in L if x<=2)}")
grid[(gm_prop, im_prop)] = runs

print("\n" + "=" * 78)
print(f"PART 2 -- RUN DETAIL at the CURRENT setting "
      f"(g={md.REGIME_MARGIN}, i={md.REGIME_MARGIN})")
print("=" * 78)
cur_runs = grid[(md.REGIME_MARGIN, md.REGIME_MARGIN)]
for regime, start, end, days in cur_runs:
    flag = "  <-- BLIP" if days <= 2 else ""
    print(f"{regime:<24}{start:<12}{end:<12}{days:>5}{flag}")

L = [r[3] for r in cur_runs]
print(f"\nTotal runs: {len(L)}   median: {sorted(L)[len(L)//2]} days   "
      f"longest: {max(L)} days")
for n in (2, 3, 4, 5, 8):
    caught = sum(1 for x in L if x < n)
    print(f"  requiring {n} consecutive days would filter {caught} of "
          f"{len(L)} episodes ({caught/len(L)*100:.0f}%)")

print("\n" + "=" * 78)
print("PART 3 -- SIGNAL AVAILABILITY DRIFT (caveat check)")
print("=" * 78)
print("Some newly-wired signals start late (e.g. average weekly hours ~2006,")
print("JOLTS ~2000), so the early part of this backtest runs on a smaller")
print("growth axis than today's. Total signals reporting at each sample date:")
for dt in ("2000-06-01", "2004-06-01", "2008-06-01", "2012-06-01",
           "2016-06-01", "2020-06-01", "2026-06-01"):
    row = next((c for c in counts if c[0] == dt), None)
    if row:
        _, gw, gb, iw, ib = row
        print(f"  {dt}: growth {gw + gb:>2} of {len(md.GROWTH_MOM)} moving, "
              f"inflation {iw + ib:>2} of {len(md.INFLATION_MOM)} moving")
