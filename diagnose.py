"""
One-off diagnostics -- NOT part of the permanent codebase. Run once, paste
the full printed output back.

PART 1 answers: which RECESSION signal in historical_check.py is missing
from 1999 through 2022, and why does it start counting at 2024-01? (Every
signal's declared "start" and real FRED history I could check by hand say
it should already be available by 1999 -- so rather than guess further,
this prints the actual truncated-series length for each signal at the two
known transition dates plus the two dates around each.)

PART 2 answers: does Stooq actually serve clean daily OHLC for RSP and SPY,
in the format the market-breadth signal would need, back to RSP's 2003
launch? This is the redistribution-safe candidate discussed for the
equal-weight vs cap-weight concentration ratio.

Run:  python diagnose.py     (needs FRED_API_KEY, same as historical_check.py,
                              and must sit next to macro_dashboard.py /
                              historical_check.py)
"""
import historical_check as hc
import requests

print("=" * 70)
print("PART 1 -- RECESSION signal availability by date")
print("=" * 70)
for as_of in ["2007-12-01", "2008-09-01", "2022-06-01", "2024-01-01"]:
    print(f"\n--- as_of {as_of} ---")
    for sid in hc.RECESSION:
        ind = hc.INDS.get(sid)
        raw = hc.RAW.get(sid)
        if not ind:
            print(f"  {sid:<14} NO IND DEFINITION IN THEMES/DRILLDOWNS")
            continue
        if not raw:
            print(f"  {sid:<14} NO RAW DATA FETCHED AT ALL")
            continue
        trimmed = [(d, v) for d, v in raw if d <= as_of]
        first = trimmed[0] if trimmed else None
        last = trimmed[-1] if trimmed else None
        flag = "  <-- EXCLUDED (< 8 pts)" if len(trimmed) < 8 else ""
        print(f"  {sid:<14} {len(trimmed):>4} pts   first={first}   last={last}{flag}")

print("\n" + "=" * 70)
print("PART 2 -- Stooq feed check for RSP / SPY")
print("=" * 70)
for sym in ["rsp.us", "spy.us"]:
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        lines = r.text.strip().splitlines()
        print(f"\n{sym}: HTTP {r.status_code}, {len(lines)} lines")
        print("  header:   ", lines[0] if lines else "(empty)")
        print("  first row:", lines[1] if len(lines) > 1 else "(none)")
        print("  last row: ", lines[-1] if len(lines) > 1 else "(none)")
    except Exception as exc:
        print(f"\n{sym}: FAILED ({exc})")
