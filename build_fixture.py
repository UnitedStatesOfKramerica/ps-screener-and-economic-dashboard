# =============================================================================
#  ORPHAN SURVEY + FIXTURE BUILDER
#  Runs inside GitHub Actions, against the repo's own ps_screener.py.
# =============================================================================
#  What it does:
#    1. runs YOUR CURRENT CODE over all 500 companies and records, for each one,
#       which revenue concepts it dropped for "no comparable overlap"
#    2. prints the exact list, with the fiscal years each drop is costing
#    3. writes fixture.json.gz holding the real SEC filings + prices for the
#       affected companies and a control set -- AND a verbatim copy of the repo
#       files that produced them, so the code and the data can never drift apart
#       again. That drift is what broke the first attempt at this.
#
#  It changes nothing and ships nothing. It contains no new logic -- every
#  judgement in it is made by your own collect_periods.
# =============================================================================

import gzip, importlib.util, json, hashlib, os, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

# --- 1. find ps_screener.py --------------------------------------------------
src = next((p for p in [Path("ps_screener.py")] + sorted(Path(".").rglob("ps_screener.py"))
            if p.exists()), None)
if src is None:
    raise SystemExit("ps_screener.py is not in this repository checkout.")

actual = hashlib.sha256(src.read_bytes()).hexdigest()
print(f"using {src.resolve()}")
print(f"  sha256 {actual}\n")

# Earlier this pinned an expected hash and stopped when it did not match, which
# turned a drift between the repo and my copy into a failed run and another
# round trip. Carrying the file itself in the artifact is strictly better: the
# survey always runs, and whatever produced it arrives with it.
CODE_FILES = ["ps_screener.py", "replay.py", "test_current_build.py",
              "requirements.txt", ".github/workflows/screen.yml",
              ".github/workflows/fixture.yml"]
code = {}
for rel in CODE_FILES:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}
        print(f"  bundling {rel}  ({len(raw):,} bytes)")
print()

spec = importlib.util.spec_from_file_location("ps", src)
ps = importlib.util.module_from_spec(spec)
sys.modules["ps"] = ps
spec.loader.exec_module(ps)

# --- 2. email ----------------------------------------------------------------
EMAIL = os.environ.get("SEC_EMAIL", "")
if "@" not in EMAIL:
    raise SystemExit("SEC_EMAIL is not set. The workflow should pass the repository "
                     "secret through as an environment variable, exactly as the "
                     "nightly run does.")

# Companies whose behaviour is already pinned by a comment in the code. If any
# of these changes, the fix has broken something that was previously paid for.
CONTROLS = ["PFE", "GOOGL", "AMZN", "ADM", "HUM", "BF-B", "TAP", "ROL", "CPT",
            "NVDA", "DTE", "XEL", "FIX", "GIS", "HON", "WAT", "BKNG", "MNST",
            "AON", "NKE", "MAR", "HLT", "PAYX", "LHX", "WMB"]

edgar = ps.Edgar(EMAIL)
print("Fetching constituents...")
const = ps.sp500_constituents()
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()
rows = [(r.ticker, r["name"], r.sector, cik_map[r.ticker])
        for _, r in const.iterrows() if r.ticker in cik_map]
print(f"  {len(rows)} companies\n")

# A few Financials / Real Estate names so the research-panel work (item 3) has
# real filings to test against without a second download.
fin = [t for t, _, s, _ in rows if s in ps.EXCLUDED_SECTORS]
FIN_SAMPLE = fin[::max(1, len(fin) // 18)][:18]


def annual_years(facts, tag):
    node = (facts.get("facts", {}).get("us-gaap", {}).get(tag) or {})
    out = set()
    for r in node.get("units", {}).get("USD") or []:
        if r.get("val") is None or "start" not in r or "end" not in r:
            continue
        try:
            s, e = ps._d(r["start"]), ps._d(r["end"])
        except Exception:
            continue
        if ps.ANNUAL_DAYS[0] <= (e - s).days <= ps.ANNUAL_DAYS[1]:
            out.add(e.year)
    return out


def slim(facts):
    """Same data, only the six fields the tool ever reads off a fact row."""
    keep = ("start", "end", "val", "filed", "form", "instant")
    out = {"facts": {}}
    for tax, concepts in (facts.get("facts") or {}).items():
        if tax == "dei":
            concepts = {k: v for k, v in concepts.items()
                        if k in ps.SHARE_TAGS_DEI or "Shares" in k}
        kept = {}
        for name, entry in concepts.items():
            units = {u: [{k: r[k] for k in keep if k in r} for r in rs]
                     for u, rs in (entry.get("units") or {}).items()}
            if any(units.values()):
                kept[name] = {"units": units}
        if kept:
            out["facts"][tax] = kept
    return out


# --- 3. the survey -----------------------------------------------------------
THIS_YEAR = date.today().year
survey, fixture_facts, fixture_meta, failed = [], {}, {}, []
t0 = time.time()

for i, (tkr, name, sector, cik) in enumerate(rows, 1):
    if i % 25 == 0 or i == len(rows):
        done = time.time() - t0
        eta = done / i * (len(rows) - i)
        print(f"  {i}/{len(rows)}  {done/60:.1f} min elapsed, "
              f"~{eta/60:.0f} min left, {len(survey)} orphans so far")
    try:
        facts = edgar.company_facts(cik)
        if not facts:
            failed.append((tkr, "no companyfacts"))
            continue
        ps.CONCEPT_TRACE.clear()
        periods = ps.collect_periods(facts, "us-gaap", ps.REVENUE_TAGS)
        trace = list(ps.CONCEPT_TRACE.get("revenue", []))
        dropped = [ln.split(":")[0].strip() for ln in trace
                   if "no comparable overlap" in ln]
        anchor = next((ln.split("anchor: ")[1].split(" (")[0]
                       for ln in trace if ln.startswith("anchor: ")), "?")
        have = {p.end.year for p in periods
                if ps.ANNUAL_DAYS[0] <= p.days <= ps.ANNUAL_DAYS[1]}
        interesting = False
        for tag in dropped:
            years = annual_years(facts, tag)
            gain = sorted(y for y in years - have if y >= THIS_YEAR - 13)
            survey.append({"ticker": tkr, "name": name, "sector": sector,
                           "anchor": anchor, "dropped": tag,
                           "dropped_years": sorted(years),
                           "merged_years": sorted(have),
                           "would_gain": gain})
            if gain:
                interesting = True
        if interesting or tkr in CONTROLS or tkr in FIN_SAMPLE:
            fixture_facts[cik] = slim(facts)
            fixture_meta[tkr] = {"cik": cik, "name": name, "sector": sector}
    except Exception:
        failed.append((tkr, traceback.format_exc(limit=2)))
    finally:
        if isinstance(ps._FACTS, dict):   # keep peak memory flat
            ps._FACTS.pop(cik, None)

print(f"\ndone in {(time.time()-t0)/60:.1f} min\n")

# --- 4. report ---------------------------------------------------------------
gains = [s for s in survey if s["would_gain"]]
print("=" * 78)
print(f"{len(survey)} dropped concepts across "
      f"{len({s['ticker'] for s in survey})} companies")
print(f"{len(gains)} of them cost real fiscal years, across "
      f"{len({s['ticker'] for s in gains})} companies")
print("=" * 78)
hdr = f"{'tkr':<6} {'anchor':<44} {'dropped concept':<44} years lost"
print(hdr)
print("-" * len(hdr))
for s in sorted(gains, key=lambda x: (-len(x["would_gain"]), x["ticker"])):
    print(f"{s['ticker']:<6} {s['anchor'][:44]:<44} {s['dropped'][:44]:<44} "
          f"{','.join(str(y) for y in s['would_gain'])}")

no_gain = [s for s in survey if not s["would_gain"]]
if no_gain:
    print(f"\n({len(no_gain)} further drops cost nothing -- the years were "
          f"already covered by another concept)")
if failed:
    print(f"\n{len(failed)} companies errored:")
    for t, why in failed[:10]:
        print(f"  {t}: {why.splitlines()[-1] if why else ''}")

# --- 5. prices for the fixture set -------------------------------------------
prices = {}
try:
    want = sorted(fixture_meta)
    print(f"\nDownloading prices for {len(want)} fixture companies...")
    closes, splits = ps.fetch_prices(
        want, date.today() - timedelta(days=365 * 13 + 400))
    for t, ser in closes.items():
        sp = splits.get(t)
        prices[t] = {
            "prices": {str(k.date()): round(float(v), 4) for k, v in ser.items()},
            "splits": ({str(k.date()): float(v) for k, v in sp.items()}
                       if sp is not None and len(sp) else {}),
        }
    print(f"  {len(prices)} price histories")
except Exception:
    print("  prices failed -- writing the fixture without them, the SEC side "
          "is what matters for the merge work:")
    traceback.print_exc(limit=3)

# --- 6. write ----------------------------------------------------------------
out = Path("fixture.json.gz")
payload = {"built": str(date.today()), "screener_sha256": actual,
           "code": code,
           "meta": fixture_meta, "facts": fixture_facts,
           "prices": prices, "survey": survey,
           "failed": [t for t, _ in failed]}
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump(payload, fh)
mb = out.stat().st_size / 1e6
print(f"\nwrote {out.resolve()}  {mb:.1f} MB, {len(fixture_facts)} companies")
if mb > 20:
    print("  NOTE: that is large for a chat upload. Tell me the size and I will "
          "re-issue this with a narrower company list.")
print("Download the 'fixture' artifact from this run and send me the file inside.")
