# =============================================================================
#  SHARE-COUNT FIXTURE
#  Run once from the Actions tab. Prints a table, writes shares.json.gz.
# =============================================================================
#  _facts_we_read() scans globals whose name ends in "_TAGS". The two share
#  lists are called SHARE_TAGS_DEI and SHARE_TAGS_GAAP, so they are never
#  scanned and all three us-gaap share concepts are deleted from every
#  company's filings at download. The documented fallback for a stale dei count
#  has therefore never run.
#
#  This pulls those companies again WITHOUT that trim, so the replacement data
#  can be checked before the way every row is valued gets changed. It writes
#  nothing back and touches no cache the nightly run uses.
# =============================================================================

import gzip, hashlib, importlib.util, json, os, sys, time, traceback
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

# Keep every share-ish concept for this run only. Nothing is written back, so
# the nightly cache is unaffected.
_orig_keep = ps._keep_concept
ps._keep_concept = lambda name: _orig_keep(name) or "Shares" in name
ps.CACHE = Path("share_probe_cache")
ps.CACHE.mkdir(exist_ok=True)
ps._FACTS = {}

# The 30 that produced no share count at all, plus Nike (dei stops in 2015),
# Berkshire (market cap came out at $0.48bn), and four single-class controls
# that work today and must go on working.
DROPPED = ["ABNB", "APP", "BF-B", "COIN", "CVNA", "DASH", "DDOG", "DELL", "ECHO",
           "EL", "ERIE", "EXPE", "GOOG", "GOOGL", "HOOD", "HRL", "LEN", "META",
           "MKC", "NWS", "NWSA", "PLTR", "RDDT", "RL", "STZ", "TKO", "TSN",
           "TTD", "UHS", "XYZ"]
PROBLEM = ["NKE", "BRK-B", "IBKR", "CME", "MA", "BX"]
CONTROL = ["HON", "JNJ", "PG", "CAT"]
WANT = DROPPED + PROBLEM + CONTROL

edgar = ps.Edgar(EMAIL)
print("Mapping tickers to CIKs...")
cik_map = edgar.ticker_to_cik()
missing = [t for t in WANT if t not in cik_map]
if missing:
    print(f"  no CIK for: {', '.join(missing)}")

KEEP_FIELDS = ("start", "end", "val", "filed", "form", "instant")


def slim(facts):
    out = {"facts": {}}
    for tax, concepts in (facts.get("facts") or {}).items():
        kept = {}
        for name, entry in concepts.items():
            if tax == "dei" and "Shares" not in name:
                continue
            units = {u: [{k: r[k] for k in KEEP_FIELDS if k in r} for r in rs]
                     for u, rs in (entry.get("units") or {}).items()}
            if any(units.values()):
                kept[name] = {"units": units}
        if kept:
            out["facts"][tax] = kept
    return out


bundle, rows = {}, []
t0 = time.time()
for i, t in enumerate([x for x in WANT if x in cik_map], 1):
    cik = cik_map[t]
    try:
        facts = edgar.company_facts(cik, refresh=True)   # bypass the trimmed cache
    except Exception:
        traceback.print_exc(limit=2)
        continue
    if not facts:
        print(f"  {t}: companyfacts empty")
        continue
    bundle[t] = {"cik": cik, "facts": slim(facts)}

    found = {}
    for tax, concepts in (facts.get("facts") or {}).items():
        for name, entry in concepts.items():
            if "Shares" not in name:
                continue
            for unit, rs in (entry.get("units") or {}).items():
                if unit != "shares":
                    continue
                dated = []
                for r in rs:
                    stamp = r.get("end") or r.get("instant")
                    if stamp and r.get("val") is not None:
                        dated.append((stamp, float(r["val"])))
                if dated:
                    dated.sort()
                    found[f"{tax}:{name}"] = (len(dated), dated[-1][0], dated[-1][1])
    rows.append((t, found))
    if i % 10 == 0:
        print(f"  {i}/{len(WANT)}  {time.time()-t0:.0f}s")

print("\n" + "=" * 104)
print("SHARE CONCEPTS AVAILABLE PER COMPANY  (rows, newest date, newest value in millions)")
print("=" * 104)
for t, found in rows:
    tag = ("DROPPED" if t in DROPPED else "problem" if t in PROBLEM else "control")
    print(f"\n{t}  [{tag}]")
    if not found:
        print("    nothing at all -- no share concept of any kind")
        continue
    for name, (n, when, val) in sorted(found.items(), key=lambda kv: -kv[1][0]):
        star = " *" if name.split(":")[1] in (
            set(ps.SHARE_TAGS_DEI) | set(ps.SHARE_TAGS_GAAP)) else "  "
        print(f"  {star}{name[:66]:<68}{n:>5} rows  {when}  {val/1e6:>12,.1f}M")

print("\n" + "=" * 104)
print("WHICH CONCEPT COULD REPLACE THE MISSING dei COUNT")
print("=" * 104)
cand = {}
for t, found in rows:
    for name in found:
        cand.setdefault(name.split(":")[1], []).append(t)
for name, ts in sorted(cand.items(), key=lambda kv: -len(kv[1])):
    covers_dropped = [x for x in ts if x in DROPPED]
    print(f"  {name[:60]:<62}{len(ts):>4} of {len(rows)} companies, "
          f"{len(covers_dropped):>3} of {len(DROPPED)} dropped ones")

code = {}
for rel in ["ps_screener.py", "replay.py", "test_current_build.py"]:
    f = Path(rel)
    if f.exists():
        raw = f.read_bytes()
        code[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                     "text": raw.decode("utf-8", "replace")}

out = Path("shares.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump({"built": str(date.today()), "companies": bundle, "code": code,
               "dropped": DROPPED, "problem": PROBLEM, "control": CONTROL}, fh)
print(f"\nwrote {out.resolve()}  {out.stat().st_size/1e6:.1f} MB, {len(bundle)} companies")
print("Download the 'shares' artifact from this run and send me the file inside.")
