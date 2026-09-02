# =============================================================================
#  FINANCIALS / REAL ESTATE FIXTURE
#  Untrimmed filings for a representative spread, so the research panel can be
#  un-blanked field-by-field against real bank and REIT balance sheets.
# =============================================================================
import gzip, hashlib, importlib.util, json, os, sys, traceback
from datetime import date
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
ps.CACHE = Path("fin_probe_cache"); ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

# A spread across the two excluded sectors:
BANKS   = ["JPM", "COF", "HBAN", "USB", "PNC"]        # deposit-funded banks
EXCH    = ["ICE", "NDAQ", "CME"]                      # exchanges (fee revenue)
ASSETM  = ["BLK", "KKR", "BX"]                        # asset managers
INSUR   = ["PGR", "AIG", "MET"]                       # insurers
REITS   = ["SPG", "PLD", "O", "AMT", "VMRK"]          # REITs (rent revenue)
CONTROL = ["AAPL", "HON"]                             # non-financial, must not change
WANT = BANKS + EXCH + ASSETM + INSUR + REITS + CONTROL

edgar = ps.Edgar(EMAIL)
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()
missing = [t for t in WANT if t not in cik_map]
if missing:
    print(f"  no CIK for: {', '.join(missing)}")

import yfinance as yf, pandas as pd
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

def yahoo_bundle(t):
    tk = yf.Ticker(t)
    info = {}
    try:
        raw = tk.info or {}
        for k in ("marketCap", "sharesOutstanding", "totalRevenue",
                  "trailingPE", "priceToBook", "returnOnEquity"):
            if k in raw:
                info[k] = raw[k]
    except Exception:
        pass
    return {"info": info}

bundle = {}
sector_of = {}
for grp, names in (("bank", BANKS), ("exchange", EXCH), ("asset_mgr", ASSETM),
                   ("insurer", INSUR), ("reit", REITS), ("control", CONTROL)):
    for t in names:
        sector_of[t] = grp

for t in [x for x in WANT if x in cik_map]:
    print("=" * 84); print(f"{t}  [{sector_of[t]}]")
    try:
        facts = edgar.company_facts(cik_map[t], refresh=True)
    except Exception:
        traceback.print_exc(limit=2); continue
    if not facts:
        print("  empty"); continue
    yb = yahoo_bundle(t)
    bundle[t] = {"cik": cik_map[t], "group": sector_of[t],
                 "facts": slim(facts), "yahoo": yb}
    # log the revenue concepts and their newest annual, plus key balance-sheet tags
    node = facts.get("facts", {}).get("us-gaap", {})
    rev_concepts = {}
    for tag, e in node.items():
        low = tag.lower()
        if "revenue" not in low and "interestanddividend" not in low and "noninterest" not in low:
            continue
        for u, rs in e.get("units", {}).items():
            if u != "USD":
                continue
            annual = [(ps._d(r["end"]).year, float(r["val"])) for r in rs
                      if r.get("val") is not None and "start" in r and "end" in r
                      and 330 <= (ps._d(r["end"]) - ps._d(r["start"])).days <= 400]
            if annual:
                annual.sort()
                rev_concepts[tag] = (len(annual), annual[-1][0], annual[-1][1])
    top = sorted(rev_concepts.items(), key=lambda kv: -kv[1][2])[:5]
    print(f"  Yahoo revenue: {yb['info'].get('totalRevenue')}, "
          f"marketCap: {yb['info'].get('marketCap')}, PE: {yb['info'].get('trailingPE')}")
    print("  top revenue-ish concepts (by newest annual $):")
    for tag, (n, y, v) in top:
        mark = " <-IN REVENUE_TAGS" if tag in ps.REVENUE_TAGS else ""
        print(f"    {tag[:52]:<54} {n} yrs, {y}: ${v/1e9:.1f}B{mark}")
    # balance-sheet tags research() reads
    for label, tags in (("equity", ps.EQUITY_TAGS), ("long_debt", ps.LONG_DEBT_TAGS),
                        ("cur_assets", ps.CURRENT_ASSETS_TAGS)):
        v = ps._latest_instant(facts, tags)
        print(f"    {label}: {'$'+format(v[1]/1e9,'.1f')+'B' if v else 'ABSENT'}")

code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("fin.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'fin' artifact and send me the file inside.")
