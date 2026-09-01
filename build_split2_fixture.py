# =============================================================================
#  SPLIT-RECONCILIATION FIXTURE  -- SEC shares AND Yahoo's split/price feed
# =============================================================================
#  The last fixture had only SEC data, so it could not show how the tool's
#  split reconciliation reacts to Yahoo's split feed -- which is exactly what
#  misled me into calling Booking's real 25-for-1 split "corruption". This one
#  captures BOTH sides for the split/merger names the reliability report flagged:
#  the SEC share history AND Yahoo's actual price series and split events. That
#  is the only way to see, and fix, the market-cap error where the share count
#  jumps correctly but the price/split reconciliation does not agree.
#
#  Confirmed real events (not corruption):
#    BKNG  25-for-1 forward split, 2026-04-06
#    WAT   merger with BD Biosciences (new shares issued), 2026-02-09
#    CVNA  real dilution
#  Writes nothing back; nightly cache untouched.
# =============================================================================
import gzip, hashlib, importlib.util, json, os, sys, traceback
from datetime import date, timedelta
from pathlib import Path

src = next((p for p in [Path("ps_screener.py")] + sorted(Path(".").rglob("ps_screener.py"))
            if p.exists()), None)
if src is None:
    raise SystemExit("ps_screener.py is not in this repository checkout.")
print(f"using {src.resolve()}\n  sha256 {hashlib.sha256(src.read_bytes()).hexdigest()}\n")
spec = importlib.util.spec_from_file_location("ps", src)
ps = importlib.util.module_from_spec(spec); sys.modules["ps"] = ps
spec.loader.exec_module(ps)

EMAIL = os.environ.get("SEC_EMAIL", "")
if "@" not in EMAIL:
    raise SystemExit("SEC_EMAIL is not set.")
ps._keep_concept = lambda n: True
ps.CACHE = Path("split2_probe_cache"); ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

# The flagged split/merger names, plus controls: NKE (clean past split, must
# stay correct), AAPL (clean, splits handled), MSFT (no recent event).
SUSPECT = ["BKNG", "WAT", "CVNA"]
CONTROL = ["NKE", "AAPL", "MSFT"]
WANT = SUSPECT + CONTROL

edgar = ps.Edgar(EMAIL)
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()

KEEP = ("start", "end", "val", "filed", "form", "instant")
def slim(facts):
    out = {"facts": {}}
    for tax, concepts in (facts.get("facts") or {}).items():
        kept = {}
        for name, entry in concepts.items():
            if tax == "dei" and "Shares" not in name:
                continue
            units = {u: [{k: r[k] for k in KEEP if k in r} for r in rs]
                     for u, rs in (entry.get("units") or {}).items()}
            if any(units.values()):
                kept[name] = {"units": units}
        if kept:
            out["facts"][tax] = kept
    return out


# ----- Yahoo side: real price history AND declared splits -----
import yfinance as yf
import pandas as pd

def yahoo_bundle(t):
    tk = yf.Ticker(t)
    start = (date.today() - timedelta(days=365 * 13 + 60)).isoformat()
    hist = tk.history(start=start, auto_adjust=False, actions=True)
    prices, splits = {}, {}
    if hist is not None and len(hist):
        close = hist["Close"] if "Close" in hist else hist.get("close")
        for ts, v in close.items():
            if pd.notna(v):
                prices[str(pd.Timestamp(ts).date())] = round(float(v), 4)
        if "Stock Splits" in hist:
            for ts, v in hist["Stock Splits"].items():
                if v and float(v) != 0.0:
                    splits[str(pd.Timestamp(ts).date())] = float(v)
    info = {}
    try:
        raw = tk.info or {}
        for k in ("marketCap", "sharesOutstanding", "totalRevenue",
                  "currentPrice", "regularMarketPrice"):
            if k in raw:
                info[k] = raw[k]
    except Exception:
        pass
    return {"prices": prices, "splits": splits, "info": info}


bundle = {}
for t in [x for x in WANT if x in cik_map]:
    print("=" * 80); print(t)
    try:
        facts = edgar.company_facts(cik_map[t], refresh=True)
    except Exception:
        traceback.print_exc(limit=2); continue
    yb = yahoo_bundle(t)
    bundle[t] = {"cik": cik_map[t], "facts": slim(facts) if facts else {"facts": {}},
                 "yahoo": yb}
    # log what Yahoo says about splits, and the SEC share tail
    print(f"  Yahoo splits: {yb['splits']}")
    print(f"  Yahoo marketCap: {yb['info'].get('marketCap')}, "
          f"sharesOutstanding: {yb['info'].get('sharesOutstanding')}")
    if facts:
        sh = ps.collect_instants(facts, "us-gaap",
                                 ["WeightedAverageNumberOfDilutedSharesOutstanding"])
        if sh:
            print("  SEC diluted share tail:", [(str(d), round(v/1e6, 1)) for d, v in sh[-5:]])


code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("split2.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code,
               "suspect": SUSPECT, "control": CONTROL}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'split2' artifact and send me the file inside.")
