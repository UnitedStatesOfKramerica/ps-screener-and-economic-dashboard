# =============================================================================
#  SPLIT / SHARE-COUNT FIXTURE  -- the reliability report's market-cap offenders
# =============================================================================
#  The reliability cross-check flagged these rows as disagreeing with Yahoo's
#  market cap by a lot. Booking at -96% is a ~25x gap, the signature of a missed
#  split (Booking's own 6-for-1-era history). This pulls the offenders untrimmed
#  so each can be traced against real filings. Writes nothing back; the nightly
#  cache is untouched.
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

ps._keep_concept = lambda n: True     # keep everything
ps.CACHE = Path("split_probe_cache"); ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

# Likely-real market-cap errors from the reliability report, plus two controls
# that agree with Yahoo (AAPL, MSFT) so a regression would show.
SUSPECT = ["BKNG", "WAT", "CVNA", "LITE", "MLM", "DVN", "SPG", "ALB"]
CONTROL = ["AAPL", "MSFT"]
WANT = SUSPECT + CONTROL

edgar = ps.Edgar(EMAIL)
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()
missing = [t for t in WANT if t not in cik_map]
if missing:
    print(f"  no CIK for: {', '.join(missing)}")

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


def trace_shares(t, facts):
    print("=" * 90); print(t)
    # every share concept, yearly, to expose a split discontinuity
    node = facts.get("facts", {}).get("us-gaap", {})
    for concept in ("WeightedAverageNumberOfDilutedSharesOutstanding",
                    "CommonStockSharesOutstanding",
                    "WeightedAverageNumberOfSharesOutstandingBasic"):
        e = node.get(concept)
        if not e:
            continue
        yv = {}
        for u, rs in e.get("units", {}).items():
            if u != "shares":
                continue
            for r in rs:
                st = r.get("end") or r.get("instant")
                if st and r.get("val") and float(r["val"]) > 0:
                    yv[ps._d(st).year] = float(r["val"])
        if yv:
            prev = None
            line = []
            for y in sorted(yv):
                ratio = f"(x{yv[y]/prev:.1f})" if prev and prev > 0 and abs(yv[y]/prev-1) > 0.4 else ""
                line.append(f"{y}:{yv[y]/1e6:,.0f}M{ratio}")
                prev = yv[y]
            print(f"  {concept[:46]:<48} {' '.join(line[-8:])}")
    # what the tool actually picks
    sh = None; srcname = None
    for c in ps.SHARE_MARKETCAP_TAGS:
        g = ps.collect_instants(facts, "us-gaap", [c])
        if g and (date.today() - g[-1][0]).days <= ps.SHARE_STALE_DAYS:
            sh, srcname = g, c; break
    if sh:
        print(f"  --> tool picks {srcname[:40]}: newest {sh[-1][1]/1e6:,.0f}M on {sh[-1][0]}")


bundle = {}
for t in [x for x in WANT if x in cik_map]:
    try:
        facts = edgar.company_facts(cik_map[t], refresh=True)
    except Exception:
        traceback.print_exc(limit=2); continue
    if not facts:
        print(f"{t}: empty"); continue
    bundle[t] = {"cik": cik_map[t], "facts": slim(facts)}
    try:
        trace_shares(t, facts)
    except Exception:
        traceback.print_exc(limit=2)

code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("split.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code,
               "suspect": SUSPECT, "control": CONTROL}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'split' artifact and send me the file inside.")
