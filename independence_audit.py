"""
Three-layer independence audit -- NOT part of the live dashboard.

THE PREREQUISITE before any confluence backtesting. The whole actionability
thesis is "act only when regime AND market check AND allocation align" -- three
green lights. But alignment is only a meaningful reliability check if the three
lights can turn green INDEPENDENTLY. If they're wired to agree (shared inputs
forcing lockstep), their agreement proves nothing -- it's circular.

The operational test of independence, agreed with the user: NOT "do they share
inputs" but "CAN THEY GENUINELY DISAGREE?" Two layers are independent enough to
cross-check each other if there are real historical moments where one said
danger and the other said calm. If they always agree, they're redundant. If
they diverge meaningfully, that divergence both proves independence AND is
itself informative (regime vs market-check disagreement in 2022 was the "market
topping before the data rolls over" signature).

So this reduces each layer to a directional call at each weekly date, using its
OWN native output, then measures:
  1. how often all three agree vs. split
  2. the pairwise agreement rate for each pair (which pair is most redundant?)
  3. WHEN they diverged (the dates -- so we can eyeball whether divergences are
     informative or just noise)
  4. specifically: has the cross-wiring introduced this session (allocation's
     macro component = the equity lean; valuation/consumer conditions feeding
     allocation) forced artificial agreement between market-check and allocation?

Each layer's directional call (its OWN logic, not an imposed one):
  REGIME:  danger if growth decelerating (Stagflation or Slowdown/Disinflation);
           benign if growth accelerating (Reflation or Goldilocks). Growth is
           the axis that matters for equity risk.
  MARKET CHECK: danger if market_riskoff (>=2 of the market gauges hot);
           benign otherwise. Uses ONLY the market-pricing gauges, NOT the
           allocation-derived macro component -- so this is the market check's
           own independent read.
  ALLOCATION: danger if Overall-equity-exposure lean is Underweight; benign if
           Overweight; neutral if Balanced/No-signal.

Reuses regime_at / allocation_at / market gauges verbatim. Revised-data (this
is a structural audit of the live logic, not a predictive backtest, so the
revised-vs-point-in-time distinction matters less here -- we're asking whether
the layers CAN disagree, which is a property of their construction).

Run: python independence_audit.py   (needs FRED_API_KEY; must sit next to
                                       macro_dashboard.py, historical_check.py,
                                       market_check_backtest.py)
"""
import collections
from datetime import datetime, timedelta

import macro_dashboard as md
import historical_check as hc
import market_check_backtest as mcb


def weekly_dates(start, end):
    d, out = start, []
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=7)
    return out


print("Fetching SP500 (FRED, for the market-check 200-day gauge)...")
SP500_FRED = md.fetch("SP500", "2015-01-01")
print(f"  {len(SP500_FRED)} points")


def regime_call(as_of):
    """danger / benign, from the regime's OWN growth axis."""
    name, growth, _, _, _, _, _ = hc.regime_at(as_of)
    return "danger" if growth == "decel" else "benign"


def market_call(as_of):
    """danger / benign, from the market check's OWN gauges only (NOT the
    allocation-derived macro component -- that's the whole point: this must be
    market check's independent read)."""
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
    return "danger" if n_hot >= 2 else "benign"


def alloc_call(as_of):
    """danger / neutral / benign, from the Overall-equity-exposure lean."""
    alloc, _, _ = mcb.allocation_at(as_of)
    lean = alloc["Overall equity exposure"]["lean"]
    if lean == "Underweight":
        return "danger"
    if lean == "Overweight":
        return "benign"
    return "neutral"


DATES = weekly_dates(datetime(1998, 1, 1), datetime(2026, 3, 1))
print(f"\nEvaluating {len(DATES)} weekly dates...")

rows = []
for k, dt in enumerate(DATES):
    r = regime_call(dt)
    m = market_call(dt)
    a = alloc_call(dt)
    rows.append((dt, r, m, a))
    if (k + 1) % 200 == 0:
        print(f"  ... {k + 1}/{len(DATES)}")

n = len(rows)

# ---- 1. three-way agreement ------------------------------------------------
print(f"\n{'=' * 100}")
print("1. THREE-WAY: how often do all three point the same way?")
print(f"{'=' * 100}")
all_danger = sum(1 for _, r, m, a in rows if r == "danger" and m == "danger" and a == "danger")
all_benign = sum(1 for _, r, m, a in rows if r == "benign" and m == "benign" and a == "benign")
# a "split" = not all three the same (treating neutral alloc as its own state)
split = sum(1 for _, r, m, a in rows if len({r, m, a}) > 1)
print(f"  all three DANGER:  {all_danger} weeks ({all_danger/n*100:.0f}%)  <- the 'act' signal")
print(f"  all three BENIGN:  {all_benign} weeks ({all_benign/n*100:.0f}%)")
print(f"  SPLIT (disagree):  {split} weeks ({split/n*100:.0f}%)")
print(f"\n  Read: if SPLIT is high, the layers ARE independent (they diverge often) --")
print(f"  good for alignment-as-check. If near zero, they move in lockstep = redundant.")

# ---- 2. pairwise agreement -------------------------------------------------
print(f"\n{'=' * 100}")
print("2. PAIRWISE agreement -- which pair is most redundant (agree most)?")
print("(agreement = both 'danger' or both 'benign'; neutral alloc counts as")
print("disagreement with a danger/benign call)")
print(f"{'=' * 100}")


def agree(x, y):
    return x == y and x in ("danger", "benign")


pairs = [("regime", "market", lambda row: (row[1], row[2])),
         ("regime", "alloc", lambda row: (row[1], row[3])),
         ("market", "alloc", lambda row: (row[2], row[3]))]
for a_name, b_name, getter in pairs:
    ag = sum(1 for row in rows if agree(*getter(row)))
    # also count how often one says danger while the other says benign (hard disagreement)
    hard = sum(1 for row in rows
               if {getter(row)[0], getter(row)[1]} == {"danger", "benign"})
    print(f"  {a_name:<8} vs {b_name:<8}: agree {ag/n*100:>3.0f}%   "
          f"hard-disagree (one danger, one benign) {hard/n*100:>3.0f}%")
print(f"\n  Read: the market-vs-alloc pair is the one to watch -- I wired a")
print(f"  dependency there this session (alloc's macro component IS the equity")
print(f"  lean, which feeds the market check). If that pair agrees MUCH more than")
print(f"  the others, the cross-wiring has made them partly redundant.")

# ---- 3. when did they diverge? ---------------------------------------------
print(f"\n{'=' * 100}")
print("3. WHEN did regime and market check DISAGREE? (the informative case --")
print("one sees danger the other doesn't). Showing contiguous stretches.")
print(f"{'=' * 100}")
prev_state = None
run_start = None
divergences = []
for dt, r, m, a in rows:
    diverged = {r, m} == {"danger", "benign"}
    if diverged and run_start is None:
        run_start = dt
        run_dir = "regime-danger/market-calm" if r == "danger" else "market-danger/regime-calm"
    elif not diverged and run_start is not None:
        divergences.append((run_start, prev_dt, run_dir))
        run_start = None
    prev_dt = dt
if run_start is not None:
    divergences.append((run_start, rows[-1][0], run_dir))
print(f"  {len(divergences)} contiguous divergence stretches between regime and market check:")
for start, end, direction in divergences:
    print(f"    {start} to {end:<12} {direction}")

# ---- 4. the confluence-danger dates ----------------------------------------
print(f"\n{'=' * 100}")
print("4. THE 'ALL THREE DANGER' DATES -- when the actionable signal actually")
print("fired historically. Contiguous stretches. Do these line up with real")
print("trouble, or fire in calm periods too?")
print(f"{'=' * 100}")
run_start = None
confluence = []
for dt, r, m, a in rows:
    fired = (r == "danger" and m == "danger" and a == "danger")
    if fired and run_start is None:
        run_start = dt
    elif not fired and run_start is not None:
        confluence.append((run_start, prev_dt2))
        run_start = None
    prev_dt2 = dt
if run_start is not None:
    confluence.append((run_start, rows[-1][0]))
print(f"  {len(confluence)} contiguous 'all three danger' stretches:")
for start, end in confluence:
    print(f"    {start} to {end}")

print(f"\n{'=' * 100}")
print("BOTTOM LINE: this audit decides whether 'alignment' is a real cross-check")
print("or circular. High divergence + the all-three-danger dates lining up with")
print("real trouble = the three-green-lights structure is sound and worth")
print("backtesting for reliability. Low divergence (esp. market-vs-alloc) = we")
print("must de-couple the layers before alignment means anything.")
print(f"{'=' * 100}")
