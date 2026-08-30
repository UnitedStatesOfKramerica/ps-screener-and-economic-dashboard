# =============================================================================
#  REVENUE-FAULT FIXTURE v2 -- keeps EVERY concept, untrimmed
# =============================================================================
#  The first revenue fixture slimmed to revenue-named concepts, which hid the
#  one thing I most needed for Exxon: the concept its real top line is filed
#  under is NOT revenue-named, so it was filtered out before I could see it.
#  This keeps every us-gaap concept for the affected names, so all three faults
#  -- Exxon's missing concept, Costco's quarter dedup, Alphabet's short share
#  history -- can be worked from one file. Writes nothing back; does not touch
#  the nightly cache.
# =============================================================================

import gzip, hashlib, importlib.util, json, os, sys, traceback
from datetime import date
from pathlib import Path

src = next((p for p in [Path("ps_screener.py")] + sorted(Path(".").rglob("ps_screener.py"))
            if p.exists()), None)
if src is None:
    raise SystemExit("ps_screener.py is not in this repository checkout.")
print(f"using {src.resolve()}")
print(f"  sha256 {hashlib.sha256(src.read_bytes()).hexdigest()}\n")

spec = importlib.util.spec_from_file_location("ps", src)
ps = importlib.util.module_from_spec(spec)
sys.modules["ps"] = ps
spec.loader.exec_module(ps)

EMAIL = os.environ.get("SEC_EMAIL", "")
if "@" not in EMAIL:
    raise SystemExit("SEC_EMAIL is not set.")

# Keep EVERYTHING for this run. This is the whole point of v2.
ps._keep_concept = lambda n: True
ps.CACHE = Path("rev2_probe_cache"); ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

ZERO   = ["XOM", "APA", "RF", "SYF"]
NO_TTM = ["COST", "PEP", "KR", "AZO", "DPZ"]
SHORT  = ["GOOGL", "GOOG"]
CONTROL = ["HON", "PG"]
WANT = ZERO + NO_TTM + SHORT + CONTROL

edgar = ps.Edgar(EMAIL)
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()
missing = [t for t in WANT if t not in cik_map]
if missing:
    print(f"  no CIK for: {', '.join(missing)}")

KEEP = ("start", "end", "val", "filed", "form", "instant", "frame")


def slim(facts):
    # keep every concept, only trim each fact row to the fields the tool reads
    out = {"facts": {}}
    for tax, concepts in (facts.get("facts") or {}).items():
        kept = {}
        for name, entry in concepts.items():
            units = {u: [{k: r[k] for k in KEEP if k in r} for r in rs]
                     for u, rs in (entry.get("units") or {}).items()}
            if any(units.values()):
                kept[name] = {"units": units}
        if kept:
            out["facts"][tax] = kept
    return out


# For XOM/APA specifically, hunt the concept its real revenue lives under, so
# the log names it even if you never open the file.
def find_revenue_concept(t, facts):
    node = facts.get("facts", {}).get("us-gaap", {})
    hits = []
    for tag, entry in node.items():
        annual = []
        for u, rs in entry.get("units", {}).items():
            if u != "USD":
                continue
            for r in rs:
                if r.get("val") is None or "start" not in r or "end" not in r:
                    continue
                try:
                    s, e = ps._d(r["start"]), ps._d(r["end"])
                except Exception:
                    continue
                # revenue-scale: a full year worth tens of billions
                if 330 <= (e - s).days <= 400 and float(r["val"]) > 20e9:
                    annual.append((e.year, float(r["val"])))
        if len(annual) >= 3:
            annual.sort()
            hits.append((len(annual), tag, annual[0][0], annual[-1][0], annual[-1][1]))
    hits.sort(reverse=True)
    print(f"  {t}: revenue-scale annual concepts (>=3 yrs, >$20B):")
    for n, tag, y0, y1, newest in hits[:6]:
        marked = " <-- in REVENUE_TAGS" if tag in ps.REVENUE_TAGS else ""
        print(f"     {tag[:60]:<62}{n:>3} yrs {y0}-{y1}  ${newest/1e9:.0f}B{marked}")
    if not hits:
        print("     none found even untrimmed -- revenue may be a company extension")


bundle = {}
for t in [x for x in WANT if x in cik_map]:
    try:
        facts = edgar.company_facts(cik_map[t], refresh=True)
    except Exception:
        traceback.print_exc(limit=2); continue
    if not facts:
        print(f"\n{t}: companyfacts empty"); continue
    bundle[t] = {"cik": cik_map[t], "facts": slim(facts)}
    if t in ZERO:
        print("=" * 90)
        find_revenue_concept(t, facts)

code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("rev2.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code,
               "zero": ZERO, "no_ttm": NO_TTM, "short": SHORT,
               "control": CONTROL}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'rev2' artifact from this run and send me the file inside.")
