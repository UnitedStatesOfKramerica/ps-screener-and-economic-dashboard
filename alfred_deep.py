"""
Deep-vintage backtest -- "sitting in 2000 / 2007 / 2021, would we have seen it?"

The regular ALFRED check went blank before ~2020 because the live signals either
didn't exist then (Sahm, WEI, JOLTS) or are convenience series FRED recomputes
instead of archiving (the pre-made yield-curve spread, 4-week claims). This script
rebuilds the read from RAW primary series that FRED archives point-in-time back to
the 1990s, and tests each crisis through the lens that should have caught it:

  * 2000 dot-com  -> VALUATION      (was CAPE screaming overvaluation?)
  * 2008 GFC      -> CREDIT/HOUSING (curve inverted? lending tightening? housing rolling over?)
  * 2022 inflation-> INFLATION      (was CPI/core-PCE visibly accelerating?)

Everything except CAPE is fetched AS IT WAS REPORTED on each date (realtime params),
so publication lag and revisions are honest. CAPE is truncated current data --
valuation barely revises, so that is a fair proxy. All series are public domain.

Run:  python alfred_deep.py     (needs FRED_API_KEY)
"""
import os
import requests
from datetime import datetime, timedelta

FRED = "https://api.stlouisfed.org/fred/series/observations"
MULTPL_CAPE = "https://www.multpl.com/shiller-pe/table/by-month"
KEY = os.environ.get("FRED_API_KEY")
_UA = {"User-Agent": "Mozilla/5.0"}
_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def vintage(sid, as_of, start="1990-01-01"):
    """Series exactly as reported on `as_of` (publication lag + un-revised)."""
    if not KEY:
        raise SystemExit("FRED_API_KEY is not set.")
    try:
        r = requests.get(FRED, params={
            "series_id": sid, "api_key": KEY, "file_type": "json",
            "observation_start": start, "realtime_start": as_of, "realtime_end": as_of,
            "sort_order": "asc", "limit": 100000}, timeout=30)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception:
        return []
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    return out


def _yoy(series):
    d = dict(series)
    out = []
    for date, v in series:
        y, m = int(date[:4]), date[5:7]
        prev = f"{y-1}-{m}-01"
        if prev in d and d[prev]:
            out.append((date, (v / d[prev] - 1) * 100))
    return out


_CAPE_CACHE = None
def cape_current():
    global _CAPE_CACHE
    if _CAPE_CACHE is not None:
        return _CAPE_CACHE
    try:
        import re
        html = requests.get(MULTPL_CAPE, headers=_UA, timeout=60).text
        out = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            dm = re.search(r"([A-Z][a-z]{2})\s+\d{1,2},\s+(\d{4})", row)
            vm = re.search(r"(\d{1,3}\.\d+)", row)
            if dm and vm and _MON.get(dm.group(1)):
                val = float(vm.group(1))
                if 3 <= val <= 80:
                    out.append((f"{int(dm.group(2)):04d}-{_MON[dm.group(1)]:02d}-01", val))
        _CAPE_CACHE = sorted(set(out))
    except Exception:
        _CAPE_CACHE = []
    return _CAPE_CACHE


# ---- signal builders: each returns (value, flag_string or "") given an as-of date ----
def sig_curve(as_of):
    g10 = dict(vintage("GS10", as_of)); g3 = dict(vintage("TB3MS", as_of))
    common = sorted(set(g10) & set(g3))
    if not common:
        return None
    v = g10[common[-1]] - g3[common[-1]]
    return v, ("INVERTED" if v < 0 else "flat" if v < 0.5 else "")

def sig_lending(as_of):
    s = vintage("DRTSCILM", as_of)
    if not s:
        return None
    v = s[-1][1]
    return v, ("TIGHTENING" if v >= 20 else "firming" if v >= 5 else "")

def _yoy_last(sid, as_of, warn, worse):
    y = _yoy(vintage(sid, as_of))
    if not y:
        return None
    v = y[-1][1]
    return v, ("WEAK" if v <= worse else "soft" if v <= warn else "")

def sig_hstarts(as_of):  return _yoy_last("HOUST", as_of, -5, -15)
def sig_permits(as_of):  return _yoy_last("PERMIT", as_of, -5, -15)
def sig_payrolls(as_of): return _yoy_last("PAYEMS", as_of, 1.0, 0.0)
def sig_indpro(as_of):   return _yoy_last("INDPRO", as_of, 0.5, -1.0)

def sig_sahm(as_of):
    u = vintage("UNRATE", as_of)
    if len(u) < 15:
        return None
    vals = [v for _, v in u]
    ma3 = [sum(vals[i-2:i+1]) / 3 for i in range(2, len(vals))]
    cur = ma3[-1]; lo = min(ma3[-12:])
    v = cur - lo
    return v, ("TRIGGERED" if v >= 0.5 else "rising" if v >= 0.3 else "")

def sig_claims(as_of):
    c = vintage("ICSA", as_of)
    if len(c) < 60:
        return None
    vals = [v for _, v in c]
    ma4_now = sum(vals[-4:]) / 4
    ma4_yr = sum(vals[-56:-52]) / 4
    v = (ma4_now / ma4_yr - 1) * 100 if ma4_yr else 0
    return v, ("RISING" if v >= 15 else "up" if v >= 5 else "")

def sig_cpi(as_of):
    y = _yoy(vintage("CPIAUCSL", as_of))
    if not y:
        return None
    v = y[-1][1]
    return v, ("HOT" if v >= 5 else "high" if v >= 4 else "")

def sig_pce(as_of):
    y = _yoy(vintage("PCEPILFE", as_of))
    if not y:
        return None
    v = y[-1][1]
    return v, ("HOT" if v >= 4 else "high" if v >= 3 else "")

def sig_cape(as_of):
    s = [(d, v) for d, v in cape_current() if d <= as_of]
    if not s:
        return None
    v = s[-1][1]
    return v, ("EXTREME" if v >= 35 else "elevated" if v >= 28 else "")


def show(title, dates, cols):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)
    hdr = f"{'as-of date':<13}" + "".join(f"{name:<20}" for name, _ in cols)
    print(hdr)
    print("-" * 92)
    for dt in dates:
        row = f"{dt:<13}"
        for _, fn in cols:
            r = fn(dt)
            if r is None:
                row += f"{'(no vintage)':<20}"
            else:
                val, flag = r
                cell = f"{val:+.1f}" + (f" {flag}" if flag else "")
                row += f"{cell:<20}"
        print(row)


def main():
    print("DEEP-VINTAGE BACKTEST -- raw series as reported then. CAPS = signal flashing.")

    show("2000 DOT-COM  ->  valuation lens (overvaluation/speculation)",
         ["1998-06-01", "1999-01-01", "1999-06-01", "2000-01-01", "2000-03-01"],
         [("Shiller CAPE", sig_cape)])

    show("2008 GFC  ->  credit / housing / lending lens (loose lending, housing bust)",
         ["2005-06-01", "2006-06-01", "2006-12-01", "2007-06-01", "2007-09-01", "2007-12-01"],
         [("Yield curve", sig_curve), ("Lending stds", sig_lending),
          ("Housing starts", sig_hstarts), ("Permits", sig_permits),
          ("Payrolls YoY", sig_payrolls), ("Sahm", sig_sahm),
          ("Claims", sig_claims), ("IndProd YoY", sig_indpro)])

    show("2022 INFLATION  ->  inflation lens (40-year-high CPI)",
         ["2021-01-01", "2021-04-01", "2021-07-01", "2021-10-01", "2022-01-01", "2022-04-01"],
         [("CPI YoY", sig_cpi), ("Core PCE YoY", sig_pce)])

    print("\nRead: a flag (CAPS) means that signal was already saying something on that "
          "date, in real time.\nThe question for each crisis is whether its lens lit up "
          "BEFORE the market broke.")


if __name__ == "__main__":
    main()
