#!/usr/bin/env python3
"""build_quality_fixture.py -- pull REAL filings for the quality-score work.

The quality score needs three SEC concepts the screener does not yet read
(operating income, depreciation/amortisation, interest expense). Before any of
those tag lists is hard-coded, they have to be confirmed against real filings --
that is the whole discipline of this repo. This probe fetches companyfacts for a
spread of companies that stress the score and writes a small fixture in the
exact shape replay.py already uses: {ticker: {"facts": ..., "prices": ...,
"splits": ...}}.

It reuses the screener's own EDGAR client (ps.Edgar) and price fetcher
(ps.fetch_prices), so the fixture can never diverge from the real pipeline. It
pulls companyfacts RAW (bypassing company_facts(), which trims to the concepts
the screener reads today) and keeps a superset: everything the screener already
reads, plus any concept in the operating-income / D&A / interest / tax family,
so the real tag each filer uses is visible rather than guessed.

Run in GitHub Actions -- SEC_EMAIL comes from the repository secret, exactly
like the nightly run. No local setup required.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
from datetime import date, timedelta

import ps_screener as ps

OUT = "quality_fixture.json.gz"

# A deliberate spread. Each name exercises one behaviour of the score.
TICKERS = [
    "OXY",   # levered energy -- the value-trap archetype
    "DVN",   # energy, cyclical margins
    "MSFT",  # high-quality compounder, high ROIC
    "V",     # ~40% operating margin, asset-light
    "PG",    # staple, steady margins
    "VZ",    # capital-intensive: big debt, big D&A, real interest expense
    "CAT",   # cyclical industrial with substantial interest expense
    "INTC",  # margins collapsing -- a live "cheap or trap?" question
    "CRM",   # software; also the net-income-drift guard case
    "MCD",   # negative book equity from years of buybacks
    "MTD",   # thin equity from buybacks (ROE once printed 7,096%)
    "ADM",   # ASC-606 revenue edge, kept for continuity with the tests
    "JPM",   # bank -- quality should read N/A
    "CPT",   # residential REIT -- quality should read N/A
    "O",     # net-lease REIT -- second N/A case
]
TICKERS = [t.upper().replace(".", "-") for t in TICKERS]

# Confirm-against-real-filings: keep everything the screener already reads, plus
# any concept whose name is in the operating-income / D&A / interest / tax
# family, so the real tag each filer uses is visible rather than guessed.
DISCOVERY = re.compile(
    r"operatingincomeloss|grossprofit|costof"
    r"|deprecia|amortiz|depletion"
    r"|interestexpense|interestanddebt|interestpaid"
    r"|incometaxexpense|beforeincometax|beforeprovisionforincometax",
    re.I,
)


def keep_concept(name: str) -> bool:
    return name in ps.FACTS_WE_READ or bool(DISCOVERY.search(name))


def trim(data: dict) -> dict:
    """Keep the quality-relevant concepts; dei is kept whole, as the real
    client does. Returns the {"facts": {...}} wrapper collect_periods expects."""
    out: dict = {"facts": {}}
    for tax, concepts in (data.get("facts") or {}).items():
        if tax == "dei":
            out["facts"]["dei"] = concepts
            continue
        kept = {k: v for k, v in concepts.items() if keep_concept(k)}
        if kept:
            out["facts"][tax] = kept
    return out


def latest_usd(gaap: dict, name: str):
    """Newest USD value for a concept, for the log-only discovery table."""
    rows = (gaap.get(name, {}).get("units", {}) or {}).get("USD") or []
    rows = [r for r in rows if r.get("val") is not None and r.get("end")]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["end"])
    return r["val"], r["end"]


def report_menu(t: str, gaap: dict) -> None:
    """Print which operating-income / D&A / interest / tax concepts this filer
    reports, so the tag choice can be checked from the Actions log alone."""
    fams = {
        "operating income": [k for k in gaap if k == "OperatingIncomeLoss"],
        "D&A":              [k for k in gaap if re.search(r"deprecia|amortiz|depletion", k, re.I)],
        "interest expense": [k for k in gaap if re.search(r"interestexpense|interestanddebt", k, re.I)],
        "pretax income":    [k for k in gaap if re.search(r"beforeincometax|beforeprovisionforincometax", k, re.I)],
        "income tax":       [k for k in gaap if re.search(r"incometaxexpense", k, re.I)],
    }
    print(f"\n  {t} -- concepts this filer reports:")
    for label, names in fams.items():
        if not names:
            print(f"      {label:<17} (none reported)")
            continue
        for n in sorted(names)[:5]:
            lv = latest_usd(gaap, n)
            shown = f"${lv[0] / 1e9:,.2f}B to {lv[1]}" if lv else "(empty)"
            print(f"      {label:<17} {n} = {shown}")


def main() -> None:
    email = os.environ.get("SEC_EMAIL", "")
    if "@" not in email:
        sys.exit("SEC_EMAIL secret is missing or is not an email address; "
                 "cannot call EDGAR. Check the repository secret.")

    edgar = ps.Edgar(email)
    print("Mapping tickers to CIKs...")
    cik = edgar.ticker_to_cik()

    have = [t for t in TICKERS if t in cik]
    missing = [t for t in TICKERS if t not in cik]
    if missing:
        print("  not in the SEC map (skipped):", ", ".join(missing))

    print("Pulling companyfacts (raw, then trimmed to the quality concept set)...")
    facts_by_t: dict[str, dict] = {}
    for t in have:
        data = edgar.get_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik[t]}.json")
        if not data:
            print(f"  {t}: companyfacts came back empty -- skipped")
            continue
        facts_by_t[t] = trim(data)
        report_menu(t, (data.get("facts") or {}).get("us-gaap", {}))

    if not facts_by_t:
        sys.exit("No companyfacts fetched; nothing to write.")

    print("\nDownloading prices/splits (reusing the screener's own fetch_prices)...")
    prices: dict[str, dict] = {}
    splits: dict[str, dict] = {}
    try:
        start = date.today() - timedelta(days=365 * 12 + 400)
        closes, sp = ps.fetch_prices(list(facts_by_t), start)
        for t in facts_by_t:
            if t in closes and len(closes[t]):
                prices[t] = {str(k.date()): round(float(v), 4)
                             for k, v in closes[t].items()}
            if t in sp and len(sp[t]):
                splits[t] = {str(k.date()): float(v) for k, v in sp[t].items()}
    except Exception as e:
        print(f"  price fetch failed ({type(e).__name__}: {e}); writing a "
              f"facts-only fixture, which still confirms the concept tags.")

    bundle = {
        t: {"facts": facts_by_t[t], "prices": prices.get(t, {}), "splits": splits.get(t, {})}
        for t in facts_by_t
    }
    with gzip.open(OUT, "wt") as fh:
        json.dump(bundle, fh)

    size_mb = os.path.getsize(OUT) / 1e6
    have_px = sum(1 for t in bundle if bundle[t]["prices"])
    print(f"\nWrote {OUT}: {len(bundle)} companies, {have_px} with price history, "
          f"{size_mb:.1f} MB")


if __name__ == "__main__":
    main()
