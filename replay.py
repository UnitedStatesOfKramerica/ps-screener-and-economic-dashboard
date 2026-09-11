"""Run the real pipeline against real filings from a fixture.

Every regression in this project came from testing a fix against invented data.
This runs the actual functions over the actual facts. The fixture is a build
artifact (build_quality_fixture.py and friends), not committed to the repo, so
this resolves whichever one is present rather than hard-coding a path that goes
stale the next time it is regenerated.
"""
import glob, gzip, json, os, sys
from datetime import date
import numpy as np, pandas as pd
import ps_screener as ps


def _find_fixture():
    env = os.environ.get("QS_FIXTURE")
    cands = ([env] if env else []) + [
        "quality_fixture.json.gz", "fixture.json.gz",
        "/mnt/user-data/uploads/quality_fixture.json.gz",
        "/mnt/user-data/uploads/quality_fixture_json.gz",
    ]
    cands += sorted(glob.glob("/mnt/user-data/uploads/*fixture*json*.gz"))
    cands += sorted(glob.glob("*fixture*json*.gz"))
    for p in cands:
        if p and os.path.exists(p):
            return p
    sys.exit("No fixture found. Set QS_FIXTURE=/path/to/fixture.json.gz, or put "
             "quality_fixture.json.gz beside this script.")


FIX = _find_fixture()

# Sector only gates the revenue-based figures and the quality score (banks and
# property companies read N/A). Mapped for the names the quality fixture ships
# with; anything else defaults to a plain operating company.
SECTOR = {
    "OXY": "Energy", "DVN": "Energy", "MSFT": "Information Technology",
    "V": "Information Technology", "PG": "Consumer Staples",
    "VZ": "Communication Services", "CAT": "Industrials",
    "INTC": "Information Technology", "CRM": "Information Technology",
    "MCD": "Consumer Discretionary", "MTD": "Health Care",
    "ADM": "Consumer Staples", "JPM": "Financials", "CPT": "Real Estate",
    "O": "Real Estate",
}


def load(path=None):
    return json.load(gzip.open(path or FIX, "rt"))


def series(d, key):
    if not d.get(key):
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in d[key].items()}).sort_index()
    s.index = s.index.astype("datetime64[ns]")
    return s


def _shares(facts):
    # Mirror the live pipeline exactly (see main()): consolidated us-gaap counts
    # in priority order, first one that is present AND current wins; repair a
    # power-of-ten units slip and splice a longer concept backward before
    # trusting it; dei only as a last resort. (The old code called
    # ps.SHARE_TAGS_GAAP, since renamed to SHARE_MARKETCAP_TAGS, and skipped the
    # repairs -- which is why McDonald's used to print 712 shares.)
    shares, src = None, None
    for concept in ps.SHARE_MARKETCAP_TAGS:
        got = ps.collect_instants(facts, "us-gaap", [concept])
        if got and (date.today() - got[-1][0]).days <= ps.SHARE_STALE_DAYS:
            shares, src = got, concept
            break
    if shares is not None:
        shares = ps._correct_units_error(shares, facts)
        shares = ps._splice_share_history(shares, src, facts)
    if shares is None:
        dei = ps.collect_instants(facts, "dei", ps.SHARE_TAGS_DEI)
        if dei and (date.today() - dei[-1][0]).days <= ps.SHARE_STALE_DAYS:
            shares = dei
    return shares


def run_one(t, d, years=12):
    ps.CURRENT_TICKER = t
    facts = d["facts"]
    px, sp = series(d, "prices"), series(d, "splits")
    periods = ps.collect_periods(facts, "us-gaap", ps.REVENUE_TAGS)
    quarters = ps.derive_quarters(periods)
    ttm = ps.trailing_twelve(quarters, periods)          # annuals arg restored
    shares = _shares(facts)
    hist = ps.monthly_ps(px, ttm, shares, sp if len(sp) else None, years)
    return dict(ticker=t, px=px, splits=sp, periods=periods, ttm=ttm,
                shares=shares, hist=hist, facts=facts)


def quality_of(t, r):
    """Full research + quality on one real company: the 0-100 score, or None."""
    hist, ttm = r["hist"], r["ttm"]
    if hist is None or hist.empty:
        return None
    sector = SECTOR.get(t, "Industrials")
    mktcap_b = float(hist["mktcap"].iloc[-1]) / 1e9
    rs = ps.research(t, r["facts"], hist, ttm, mktcap_b, sector=sector)
    rs["ticker"] = t
    rs["sector"] = sector
    v = ps.quality_score(pd.DataFrame([rs]))["quality"].iloc[0]
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else int(v)


if __name__ == "__main__":
    bundle = load()
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else list(bundle)
    ps.REJECTED_RATIOS.clear()
    print(f"fixture: {FIX}\n")
    print(f"{'tkr':6s}{'shares now':>15s}{'shares 10y ago':>16s}{'chg':>8s}"
          f"{'rev now':>11s}{'P/S':>8s}{'10y med':>9s}{'quality':>9s}")
    for t in only:
        if t not in bundle:
            print(f"{t:6s}  (not in fixture)")
            continue
        r = run_one(t, bundle[t])
        h = r["hist"]
        if h.empty:
            why = ("no current share count in fixture (e.g. a multi-class filer "
                   "whose count is only tagged per share class)"
                   if not r["shares"] else "no overlapping price history")
            print(f"{t:6s}  ({why})")
            continue
        chg = h["shares"].iloc[-1] / h["shares"].iloc[0] - 1
        q = quality_of(t, r)
        print(f"{t:6s}{h['shares'].iloc[-1]:>15,.0f}{h['shares'].iloc[0]:>16,.0f}"
              f"{chg:>7.0%}{r['ttm']['ttm'].iloc[-1]/1e9:>10,.1f}B"
              f"{h['ps'].iloc[-1]:>8.2f}{h['ps'].tail(120).median():>9.2f}"
              f"{(str(q) if q is not None else 'N/A'):>9s}")
    print()
    if ps.REJECTED_RATIOS:
        print("split factor decisions:")
        for tk, when, ratio, why in ps.REJECTED_RATIOS:
            print(f"  {tk:6s} {when}  {ratio:>9.4f}  {why[:88]}")


def assert_real_data():
    """What the real filings say, asserted. Anything touching split handling,
    share counts or revenue concepts has to come through here before it ships:
    every regression in this project passed invented data first. Only the tickers
    present in the loaded fixture are checked; the rest are reported as skipped,
    so this keeps working whichever fixture is on disk."""
    bundle = load()
    expect = {   # ticker: (dilution_1y within, share-base audit expected)
        "HON":  (2.0,  False),   # 1-for-2 reverse split Yahoo never filed
        "MNST": (2.0,  False),   # two-for-one, three weeks old
        "AMD":  (2.0,  True),    # Xilinx: real issuance, worth flagging
        "BG":   (5.0,  True),    # Viterra: must NOT read as a split, but real
        "AON":  (3.0,  False),   # Hewitt: 1.23x, must not read as five-for-four
        "ADM":  (2.0,  False),
        "DD":   (5.0,  False),   # 1-for-3 filed by Yahoo as 0.4725
        "CPT":  (8.0,  False),
    }
    present = {t: v for t, v in expect.items() if t in bundle}
    skipped = [t for t in expect if t not in bundle]
    bad, checks = [], 0
    for t, (tol, want_flag) in present.items():
        ps.CURRENT_TICKER = t
        r = run_one(t, bundle[t])
        h = r["hist"]
        sh = ps.shares_in_todays_units(h, h.attrs.get("splits") or {})
        older = h["date"] <= h["date"].iloc[-1] - pd.DateOffset(years=1)
        d1 = (sh.iloc[-1] / sh[older].iloc[-1] - 1) * 100 if older.any() else 0.0
        sp = h.attrs.get("splits") or {}
        flag = any("share count" in i for i in
                   ps.audit_series(h, r["ttm"], sp,
                                   ps.detect_corporate_actions(h, sp)))
        checks += 2
        if abs(d1) > tol:
            bad.append(f"{t}: dilution_1y {d1:+.1f}%, expected within {tol}%")
        if flag != want_flag:
            bad.append(f"{t}: share-base flag {flag}, expected {want_flag}")
    if "ADM" in bundle:
        adm = run_one("ADM", bundle["ADM"])
        rev = adm["ttm"]["ttm"].iloc[-1] / 1e9
        checks += 1
        if not 70 < rev < 95:
            bad.append(f"ADM revenue ${rev:,.1f}B - the ASC-606 slice is back")
    for line in bad:
        print("  FAIL", line)
    if skipped:
        print(f"  (skipped, not in this fixture: {', '.join(skipped)})")
    print(f"\n{checks - len(bad)} of {checks} checks pass against real filings")
    return not bad
