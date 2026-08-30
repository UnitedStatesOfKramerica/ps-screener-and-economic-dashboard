# =============================================================================
#  REVENUE-FAULT FIXTURE
#  Run once from the Actions tab. Prints traces, writes rev.json.gz.
# =============================================================================
#  The drop ledger shows two revenue faults that are NOT about share counts:
#    * "no revenue series -- 0 periods"      Exxon, Apache: nothing collected
#    * "no revenue series -- 54 quarters"    Costco, Pepsi: quarters collected
#                                            but no 4 of them span ~365 days
#  and one puzzle: Alphabet has a share count now but "fewer than 60 usable
#  months", though it plainly has a decade of history -- its revenue series is
#  being cut short somewhere.
#
#  This pulls those names untrimmed so the collection can be traced against real
#  filings. Writes nothing back; does not touch the nightly cache.
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

# keep everything revenue-ish for this run, untrimmed
_orig = ps._keep_concept
ps._keep_concept = lambda n: _orig(n) or "Revenue" in n or "Sales" in n
ps.CACHE = Path("rev_probe_cache"); ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

ZERO   = ["XOM", "APA", "RF", "SYF"]          # 0 periods collected
NO_TTM = ["COST", "PEP", "KR", "AZO", "DPZ"]   # quarters collected, no TTM
SHORT  = ["GOOGL", "GOOG"]                      # history cut short
CONTROL = ["HON", "PG"]                         # work today
WANT = ZERO + NO_TTM + SHORT + CONTROL

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


def trace(t, facts):
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)
    ps.CONCEPT_TRACE.clear()
    periods = ps.collect_periods(facts, "us-gaap", ps.REVENUE_TAGS)
    for ln in ps.CONCEPT_TRACE.get("revenue", []):
        print("  ", ln)
    print(f"  periods collected: {len(periods)}")
    if periods:
        yrs = sorted({p.end.year for p in periods})
        print(f"  span: {yrs[0]}-{yrs[-1]}")
        # duration histogram -- the ANNUAL/QUARTER windows are the gate
        from collections import Counter
        buckets = Counter()
        for p in periods:
            d = p.days
            b = ("annual" if 330 <= d <= 400 else "quarter" if 80 <= d <= 100
                 else "half" if 170 <= d <= 190 else "9mo" if 260 <= d <= 285
                 else f"other({d}d)")
            buckets[b] += 1
        print("  period durations:", dict(buckets))
    q = ps.derive_quarters(periods)
    print(f"  quarters derived: {len(q)}")
    if q:
        spans = sorted((qq.end - qq.start).days for qq in q)
        print(f"  quarter span range: {spans[0]}-{spans[-1]} days "
              f"(the TTM gate wants 4 in a row spanning 330-400)")
        # show the last few quarters concretely
        for qq in q[-4:]:
            print(f"     {qq.start} -> {qq.end}  {(qq.end-qq.start).days}d  "
                  f"${qq.val/1e9:.2f}B  {qq.tag}")
    ttm = ps.trailing_twelve(q, periods)
    print(f"  TTM rows: {len(ttm)}")
    if len(ttm):
        print(f"     newest TTM: {ttm['period_end'].iloc[-1].date()}  "
              f"${ttm['ttm'].iloc[-1]/1e9:.1f}B")


bundle = {}
for t in [x for x in WANT if x in cik_map]:
    try:
        facts = edgar.company_facts(cik_map[t], refresh=True)
    except Exception:
        traceback.print_exc(limit=2); continue
    if not facts:
        print(f"\n{t}: companyfacts empty"); continue
    bundle[t] = {"cik": cik_map[t], "facts": slim(facts)}
    try:
        trace(t, facts)
    except Exception:
        traceback.print_exc(limit=3)

code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("rev.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code,
               "zero": ZERO, "no_ttm": NO_TTM, "short": SHORT,
               "control": CONTROL}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'rev' artifact from this run and send me the file inside.")
