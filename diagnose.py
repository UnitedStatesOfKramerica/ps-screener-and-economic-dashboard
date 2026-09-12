"""
One-off diagnostic -- NOT part of the permanent codebase. Run once, paste
the full output back.

Stooq is out: it now gates its CSV endpoint behind a JS proof-of-work
anti-bot challenge, so plain HTTP requests get an HTML challenge page
instead of data. This tests yfinance instead -- already used by
ps_screener.py elsewhere in this repo, no API key needed -- for RSP and
SPY back to RSP's April 2003 launch.
"""
import yfinance as yf

for sym in ["RSP", "SPY"]:
    print(f"\n=== {sym} ===")
    try:
        df = yf.download(sym, start="2003-01-01", progress=False)
        if df.empty:
            print("  EMPTY RESULT")
            continue
        print(f"  {len(df)} rows")
        print("  first:", df.index[0].date(), float(df["Close"].iloc[0]))
        print("  last: ", df.index[-1].date(), float(df["Close"].iloc[-1]))
    except Exception as exc:
        print(f"  FAILED: {exc}")
