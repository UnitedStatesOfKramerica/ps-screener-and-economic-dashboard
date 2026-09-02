#!/usr/bin/env python3
"""
ps_screener.py -- Price/Sales vs. own history, for the S&P 500.

Builds a point-in-time monthly P/S series for every constituent from free data:
  revenue + share counts : SEC EDGAR XBRL companyfacts  (no key, 10 req/sec)
  prices + split history : Yahoo Finance via yfinance    (no key)
  constituents           : Wikipedia                     (no key)

Outputs ps_screen.db (SQLite), ps_screen.csv, and ps_screen.html (sortable UI).

    pip install requests pandas numpy yfinance lxml
    python ps_screener.py --email you@example.com

First run downloads ~1-3 GB of EDGAR JSON and takes 20-40 min. It is cached in
./cache, so later runs take a couple of minutes. Use --refresh to re-pull.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CACHE = Path("cache")
OUT = Path(".")
HISTORY_YEARS = 12          # how far back to build the monthly series
MIN_MONTHS_FOR_STATS = 60   # matches the UI's own threshold; below this a
                            # "historical average" is not one

# EDGAR tags revenue under several concepts. Order matters: the first tag with
# usable data wins. ASC 606 (2018+) pushed most filers onto the first two.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    # These two are components for a filer that sells both goods and services
    # (Amazon 2015: 79.3 goods + 27.7 services = 107.0 net) but are the WHOLE
    # company for a services-only one. Deleting them globally on Amazon's
    # evidence cost Rollins its entire pre-2016 history. They sit last, so they
    # are only reached when nothing better exists, and the overlap test refuses
    # them wherever they disagree with a fuller concept.
    "SalesRevenueServicesNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",   # banks
    "TotalRevenuesAndOtherIncome",
    # Utilities file under their own concepts; without these, DTE's revenue was
    # eight years stale and Xcel's nearly seven.
    "RegulatedAndUnregulatedOperatingRevenue",
    "UtilityRevenue",
    "ElectricUtilityRevenue",
    "PublicUtilitiesRevenue",
    "RevenueFromContractWithCustomerExcludingAssessedTaxAndRegulatedOperatingRevenue",
    # A landlord's rent is a LEASE under ASC 842, not a contract with a customer
    # under ASC 606, so for a residential or office REIT the ASC-606 concept
    # holds only fee income. Camden's came out at $13m of fees against roughly
    # $1.5bn of rent, giving a P/S of 824. These sit last: for anyone who is not
    # a lessor they are a small component and the overlap test refuses them.
    "OperatingLeaseLeaseIncome",
    "OperatingLeasesIncomeStatementLeaseRevenue",
    "RealEstateRevenueNet",
]
GROSS_PROFIT_TAGS = ["GrossProfit"]
COST_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]

# --- Research module concepts ------------------------------------------------
# Balance-sheet items are INSTANTS (a value at a date); income and cash-flow
# items are DURATIONS (a value over a period). They are collected differently.
# Each list is in priority order and goes through the same overlap-verified
# merge as revenue, so the lessons already paid for apply here too.
EXCLUDED_SECTORS = ("Financials", "Real Estate")

CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
CURRENT_LIAB_TAGS = ["LiabilitiesCurrent"]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashAndDueFromBanks",
]
SHORT_INVEST_TAGS = ["ShortTermInvestments", "AvailableForSaleSecuritiesCurrent",
                     "MarketableSecuritiesCurrent"]
LONG_DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt",
                  "LongTermDebtAndCapitalLeaseObligations"]
SHORT_DEBT_TAGS = ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
                   "OtherShortTermBorrowings"]
EQUITY_TAGS = ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss",
                   "NetIncomeLossAvailableToCommonStockholdersBasic"]
OPER_CASH_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
                  "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"]

# Which share count to multiply by the price for market cap. Named with the
# _TAGS suffix so _facts_we_read() keeps them: they were SHARE_TAGS_DEI and
# SHARE_TAGS_GAAP, ending in _DEI and _GAAP, so the scan missed them and all
# three us-gaap share concepts were deleted at download. That is why 30
# multi-class filers -- Alphabet, Meta, Tyson -- had no share count and no row,
# and why Nike's eleven-year-stale dei count was never replaced.
#
# Order is deliberate and was checked against the filings of 40 companies:
#   * Weighted-average DILUTED first. It is the consolidated figure -- for a
#     multi-class filer CommonStockSharesOutstanding is the sum of A+B+C, which
#     is wrong against one class's price (Alphabet 12,230 summed vs 12,309
#     consolidated), and for holding companies like IBKR it is only the
#     parent's slice (64M vs 450M). Diluted is right in both and current almost
#     everywhere.
#   * Shares OUTSTANDING second, only when diluted is absent.
#   * BASIC last (Tyson files no basic; some names' diluted rounds to zero).
# Its one weakness -- diluted averages over the period, so it lags by 1-7% for a
# single-class filer mid-buyback (Dell, Palantir) -- does not touch the ranking,
# which compares each company only against its own history on the same concept.
SHARE_MARKETCAP_TAGS = [
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "CommonStockSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]
# The dei cover-page count is kept as a LAST resort: it is per-class for
# multi-class filers and goes stale (Nike stops 2015), but for a plain filer it
# is a genuine as-of-filing snapshot when no us-gaap count survived.
SHARE_TAGS_DEI = ["EntityCommonStockSharesOutstanding"]
# A share count older than this is treated as unusable rather than carried
# forward. 15 months clears a normal annual gap but rejects a decade-stale
# figure like Berkshire's 2015 count or Nike's 2015 dei count.
SHARE_STALE_DAYS = 460

# Why collect_periods picked the concept it picked, written on every call and
# read back by diagnose(). Deducing the anchor from the merged output was
# guesswork: ADM's output was consistent with three different explanations.
CONCEPT_TRACE: dict[str, list[str]] = {}

QUARTER_DAYS = (80, 100)     # a "quarter" duration window
# Every concept read anywhere in this file, plus anything whose name mentions
# revenue or sales -- the diagnostics table scans those to find totals the tag
# list does not know about, and trimming them away would blind it.
def _facts_we_read() -> set[str]:
    names = set()
    for key, val in list(globals().items()):
        if key.endswith("_TAGS") and isinstance(val, (list, tuple, set)):
            names |= {str(v) for v in val}
    return names


RECENT_DAYS = 7      # how far back a top-up reaches, to cover a long weekend
ANNUAL_DAYS = (330, 400)


# ---------------------------------------------------------------------------
# EDGAR client
# ---------------------------------------------------------------------------

# Built here, after every *_TAGS list above is defined.
FACTS_WE_READ = _facts_we_read()


def _keep_concept(name: str) -> bool:
    """Worth keeping on disk?

    The 44 concepts the tool reads, plus anything whose name mentions revenue
    or sales. That second clause matters: the diagnostics table scans those to
    find a total the tag list has never heard of, which is how ADM's real
    revenue turned up under RevenueNotFromContractWithCustomer. Trimming them
    away would save a little space and cost the one diagnostic that has
    actually cracked things open.
    """
    if name in FACTS_WE_READ:
        return True
    low = name.lower()
    return "revenue" in low or "sales" in low


_FACTS: dict | None = None
_FACTS_DIRTY = False


def _facts_path():
    # The cache is keyed by the SET OF CONCEPTS the code keeps. When that set
    # changes -- as it did when the share concepts were finally kept -- a cache
    # written by the old code holds filings with those concepts already stripped
    # out, and reading it silently reproduces the old bug: every multi-class
    # filer dropped again because its share count was trimmed away before the
    # new code ever saw it. Folding a short hash of FACTS_WE_READ into the name
    # means any change to the kept set lands in a fresh file and the stale one is
    # simply ignored. This is the missing invalidation that made the share fix
    # look like it had failed on its first run.
    tag = hashlib.sha1(",".join(sorted(FACTS_WE_READ)).encode()).hexdigest()[:8]
    return CACHE / f"facts_{tag}.json"


def _facts_store() -> dict:
    """All companies' trimmed filings, read from disk once per run."""
    global _FACTS
    if _FACTS is None:
        _FACTS = {}
        f = _facts_path()
        if f.exists() and time.time() - f.stat().st_mtime < 24 * 3600 * 7:
            try:
                _FACTS = json.loads(f.read_text())
                print(f"  {len(_FACTS)} companies' filings loaded from cache")
            except (ValueError, OSError):
                _FACTS = {}
    return _FACTS


def save_facts_cache() -> None:
    if not _FACTS_DIRTY or _FACTS is None:
        return
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        _facts_path().write_text(json.dumps(_FACTS))
    except OSError:
        pass


class Edgar:
    """Polite EDGAR client. 10 req/sec cap, descriptive User-Agent required."""

    def __init__(self, email: str, delay: float = 0.12):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"ps-screener/1.0 ({email})",
            "Accept-Encoding": "gzip, deflate",
        })
        self.delay = delay
        self._last = 0.0

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def get_json(self, url: str, tries: int = 4):
        for attempt in range(tries):
            self._throttle()
            try:
                r = self.session.get(url, timeout=60)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            # 403 means the User-Agent was rejected; 429 means we went too fast.
            # Backing off hard is the only thing that shortens either.
            time.sleep(3 * (attempt + 1))
        return None

    def ticker_to_cik(self) -> dict[str, str]:
        data = self.get_json("https://www.sec.gov/files/company_tickers.json")
        if not data:
            raise SystemExit("Could not fetch company_tickers.json from SEC.")
        return {
            row["ticker"].upper().replace(".", "-"): str(row["cik_str"]).zfill(10)
            for row in data.values()
        }

    def company_facts(self, cik: str, refresh: bool = False) -> dict | None:
        """EDGAR's filing facts, trimmed to what is read and kept in ONE file.

        Two separate costs were hiding here. The files carry every concept a
        filer has ever tagged -- 350 to 780 of them, and this tool reads 44 --
        so most of what was stored was never looked at. And they were 500
        separate files, which Google Drive charges for per file: profiling put
        the actual computation at 48 seconds for the whole index, against runs
        of seven minutes. The work was never the work; it was the reading.
        """
        store = _facts_store()
        if cik in store and not refresh:
            return store[cik]

        # The per-company cf_/cs_ files hold ALREADY-TRIMMED data, so reading
        # them back yields filings with the share concepts already stripped --
        # and re-trimming cannot recover a concept that was deleted before the
        # file was written. That is the whole reason the share fix appeared to
        # do nothing: the versioned main cache was empty on the first run, the
        # code fell through to these legacy files, and got back exactly the
        # trimmed data the old code had left. They were a one-time migration and
        # are now actively harmful, so they are no longer read. The versioned
        # facts_<hash>.json is the only cache; when it is empty, re-fetch from
        # EDGAR untrimmed, which is the only source that still has every concept.
        data = self.get_json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        if not data:
            return None

        trimmed = {"facts": {}}
        for taxonomy, concepts in (data.get("facts") or {}).items():
            if taxonomy == "dei":
                trimmed["facts"]["dei"] = concepts
                continue
            kept = {k: v for k, v in concepts.items() if _keep_concept(k)}
            if kept:
                trimmed["facts"][taxonomy] = kept
        store[cik] = trimmed
        global _FACTS_DIRTY
        _FACTS_DIRTY = True
        for old in (CACHE / f"cs_{cik}.json", CACHE / f"cf_{cik}.json"):
            try:
                old.unlink(missing_ok=True)   # sweep the poisoned legacy files
            except OSError:
                pass
        return trimmed


@dataclass(frozen=True)
class Period:
    """One reported figure: what it covers, what it was, and when it was filed."""
    start: date
    end: date
    val: float
    filed: date
    tag: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _form_fits(days: int, form: str) -> int:
    """0 when the filing type matches the period length, 1 when it doesn't."""
    if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
        return 0 if form.startswith("10-K") or form.startswith("20-F") else 1
    if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
        return 0 if form.startswith("10-Q") else 1
    return 0


def collect_periods(facts: dict, taxonomy: str, tags: list[str],
                    screen_annuals: bool = True) -> list[Period]:
    """
    Pull duration facts, joining tags only where they demonstrably agree.

    Two separate traps here. Taking only the first tag with data freezes revenue
    when a filer abandons a concept -- that made Nvidia read 476x. But joining
    every tag end-to-end is worse: "Revenues" and "RevenueFromContractWith..."
    do not always cover the same thing for the same filer, so splicing them at
    the 2018 accounting-standard change invented revenue jumps of 60-70% for
    Alphabet and Amazon, neither of which grew anything like that.

    So: anchor on the highest-priority tag still being filed, then extend
    backwards only with tags that match it where the two overlap. A shorter
    honest series beats a longer invented one.
    """
    node = facts.get("facts", {}).get(taxonomy, {})

    per_tag: dict[int, dict[tuple[date, date], Period]] = {}
    for rank, tag in enumerate(tags):
        entry = node.get(tag)
        if not entry:
            continue
        rows = entry.get("units", {}).get("USD") or entry.get("units", {}).get("shares")
        if not rows:
            continue
        found: dict[tuple[date, date], Period] = {}
        form_fit: dict[tuple[date, date], int] = {}
        for r in rows:
            if "start" not in r or "end" not in r or r.get("val") is None:
                continue
            if r.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F"):
                continue
            try:
                p = Period(_d(r["start"]), _d(r["end"]), float(r["val"]), _d(r["filed"]), tag)
            except (ValueError, KeyError, TypeError):
                continue
            key = (p.start, p.end)
            prior = found.get(key)
            form = r.get("form") or ""
            # A full year belongs in a 10-K and a quarter in a 10-Q. Comfort
            # Systems files its Q1 figure carrying a full-year end date in a
            # 10-Q; "latest filing wins" then picked $1.8bn over the 10-K's
            # correct $9.1bn and collapsed the whole year. Match the form to the
            # period type first, and only then prefer the later filing.
            fit = _form_fits(p.days, form)
            if prior is None:
                found[key] = p
                form_fit[key] = fit
            elif fit < form_fit.get(key, 9):
                found[key] = Period(p.start, p.end, p.val,
                                    min(p.filed, prior.filed), tag)
                form_fit[key] = fit
            elif fit == form_fit.get(key, 9):
                # Two facts for one period. Normally that is a restatement, and
                # the later filing supersedes the VALUE while the earliest keeps
                # the disclosure DATE -- the market saw a number for this period
                # on the original filing.
                #
                # But it can also be a consolidated figure sitting beside a
                # segment or noncontrolling-interest line for the same period.
                # A restatement seldom moves a figure several-fold; a segment is
                # a fraction of the whole. So where the two differ by more than
                # 3x, take the larger and ignore filing order. General Mills'
                # -$88m line beat its $2.3bn of profit on filing date alone, and
                # since Q4 is derived as (annual minus nine months) that one
                # choice set the whole trailing total.
                if abs(p.val) > 3 * abs(prior.val) and abs(prior.val) > 0:
                    chosen = p.val
                elif abs(prior.val) > 3 * abs(p.val) and abs(p.val) > 0:
                    chosen = prior.val
                else:
                    chosen = p.val if p.filed > prior.filed else prior.val
                found[key] = Period(p.start, p.end, chosen,
                                    min(p.filed, prior.filed), tag)
        if found:
            per_tag[rank] = found

    if not per_tag:
        return []

    newest = max(max(p.end for p in d.values()) for d in per_tag.values())
    current = [rank for rank in sorted(per_tag)
               if (newest - max(p.end for p in per_tag[rank].values())).days <= 400]
    if not current:
        current = [min(per_tag)]

    def compare_scale(a: dict, b: dict, near: int | None = None) -> float | None:
        """
        How much larger concept `b` is than `a`, judged only where they overlap.

        Comparing overall medians was wrong: Alphabet's "Revenues" spans
        2013-2026 and medians $90bn, while its ASC 606 concept spans 2016-2025
        and medians $258bn. Same company, same measure, 3x apart purely because
        they cover different years. A concept that happens to start later looked
        "more complete" than one covering everything.

        Annual figures decide it. Comparing every shared period equally let a
        concept that CHANGED SCOPE mid-history pass as identical: ADM's ASC-606
        tag carries full revenue in its pre-2022 quarters and only the
        contracts-with-customers slice from 2022, so the agreeing early
        quarters outnumbered the disagreeing recent years and the comparison
        came back at exactly 1.000x while the two were 3.6x apart on every year
        they both reported. The audited annual figure is the one number a filer
        cannot quietly re-scope, so it is asked first.
        """
        def by_year(d):
            out: dict[int, float] = {}
            for p in d.values():
                if ANNUAL_DAYS[0] <= p.days <= ANNUAL_DAYS[1] and p.val > 0:
                    out[p.end.year] = max(out.get(p.end.year, 0.0), p.val)
            return out

        ay, by = by_year(a), by_year(b)
        common = [y for y in by if y in ay and ay[y] > 0]
        if len(common) >= 2:
            # A concept can change scope mid-life, and then one factor for the
            # whole overlap is wrong at both ends. Pfizer's ASC-606 tag IS its
            # total revenue from 2016 to 2021 and becomes a subset in 2022,
            # when collaborative revenue was broken out into its own concept.
            # Measured across the whole overlap that reads 0.907, and the 10%
            # was then applied to 2016-2019, where the concept had been the
            # correct total all along -- inflating four years of revenue and
            # depressing the multiple the median is built from.
            #
            # The years being CARRIED ACROSS decide which overlap to trust. Ask
            # the three shared years nearest to them, not all of them. Where a
            # concept never changed scope every year gives the same answer and
            # this changes nothing.
            ratios = {y: by[y] / ay[y] for y in common}
            if near is not None:
                # Walk outwards from the year nearest what is being carried
                # across, and stop at the first year that disagrees. A concept
                # whose scope never moved gives the same ratio everywhere and
                # the whole overlap is used, exactly as before. One that DID
                # move gives a step, and only the side of the step adjacent to
                # the contributed years is used.
                #
                # Pfizer restated its ASC-606 figure for 2021 downward in the
                # 2024 10-K, so its overlap reads 1.000, 0.906, 0.907, 0.855.
                # A median over all four says 0.907 and inflates 2016-2019 by
                # 10%, when 2020 -- the year adjacent to them -- says plainly
                # that the two concepts were the same measure then.
                order = sorted(common, key=lambda y: (abs(y - near), y))
                r0 = ratios[order[0]]
                run = [r0]
                for y in order[1:]:
                    if r0 > 0 and abs(ratios[y] / r0 - 1) <= 0.05:
                        run.append(ratios[y])
                    else:
                        break
                return float(np.median(run))
            return float(np.median(list(ratios.values())))

        shared = [k for k in b if k in a and a[k].val not in (0, None)]
        if len(shared) >= 2:
            return float(np.median([b[k].val / a[k].val for k in shared]))
        # No common periods. Comparing each concept's own most recent years was
        # wrong for the same reason overall medians were: a concept covering
        # 2013-2017 against one covering 2018-2026 is being judged on a decade of
        # growth rather than on scope, so anything that grew got refused. That
        # cost ~148 companies their transition-year quarters. Compare the two
        # ANNUAL figures closest in time instead, and only when they are close
        # enough for growth not to swamp the difference.
        def annuals(d):
            return sorted((p.end, p.val) for p in d.values()
                          if ANNUAL_DAYS[0] <= p.days <= ANNUAL_DAYS[1] and p.val > 0)
        aa, bb = annuals(a), annuals(b)
        if not aa or not bb:
            return None
        best = min(((abs((ea - eb).days), va, vb) for ea, va in aa for eb, vb in bb),
                   key=lambda x: x[0])
        gap_days, va, vb = best
        if gap_days > 400 or va <= 0:
            return None
        return vb / va

    # Anchor on the concept reporting the MOST COMPLETE revenue, not the one
    # highest in the list. Insurance premiums sit outside ASC 606, as do much of
    # equipment rental and commodity trading, so
    # RevenueFromContractWithCustomer... is only a slice of the total for some
    # filers while "Revenues" carries the whole. Priority alone kept the slice
    # and threw away the total: Humana came out at $6.2bn against roughly $145bn.
    primary = current[0]
    for rank in current[1:]:
        bigger = compare_scale(per_tag[primary], per_tag[rank])
        if bigger is not None and bigger > 1.2:
            primary = rank

    # ...with one exception. "IncludingAssessedTax" is the SAME revenue as
    # "ExcludingAssessedTax" with excise tax added back, so for a distiller or
    # a brewer one is reliably 20-30% larger and wins the size test on a
    # difference that is a tax, not sales. Brown-Forman came out 29% high.
    #
    # Which of the two is the net figure cannot be read off the tag name.
    # Molson Coors files them the other way round: its "Excluding" concept is
    # $13.0bn for 2025 and its "Including" concept $11.1bn, and the $1.9bn
    # between them is exactly its reported ExciseAndSalesTaxes. Net of tax is
    # always the SMALLER of the two, whatever the filer chose to call it, and
    # net is what every data provider publishes.
    _INC = "RevenueFromContractWithCustomerIncludingAssessedTax"
    _EXC = "RevenueFromContractWithCustomerExcludingAssessedTax"
    pair_ranks = [r for r in current if tags[r] in (_INC, _EXC)]
    if tags[primary] in (_INC, _EXC) and len(pair_ranks) == 2:
        lo, hi = pair_ranks
        gap = compare_scale(per_tag[lo], per_tag[hi])
        if gap is not None and gap > 0:
            smaller = hi if gap < 1 else lo
            if abs(gap - 1) > 0.05:
                primary = smaller

    tracing = list(tags) == list(REVENUE_TAGS)
    trace: list[str] = []
    if tracing:
        trace.append(f"anchor: {tags[primary]} "
                     f"({len(per_tag[primary])} periods, newest "
                     f"{max(p.end for p in per_tag[primary].values())})")

    # The assessed-tax pair is the one case where a large, stable gap is
    # definitional rather than a difference of scope: it is excise tax.
    # Rescaling onto the net basis keeps the history instead of leaving a
    # hole. It has to cover the filer's RETIRED concepts too: once the net
    # anchor is in force, Brown-Forman's pre-2018 SalesRevenueGoodsNet is
    # measured on the old gross basis and falls outside the ordinary band,
    # which cost it 34 months of history and lifted its ten-year median
    # from 5.9 to 7.8. Where both assessed-tax concepts exist, the filer
    # charges an excise, so every older concept is on one of the two bases.
    pair = {_INC, _EXC}
    excise_filer = pair.issubset(set(tags[r] for r in per_tag))

    # A concept is judged against the anchor, because the anchor is the scope
    # the whole series is stated on. But a RETIRED concept need not overlap the
    # anchor at all. Pfizer's SalesRevenueGoodsNet ends at FY2015 and its anchor
    # "Revenues" does not begin until years later, so there was nothing to
    # compare and the old tag was dropped, taking real years with it. The two
    # are not strangers: both overlap a third concept that has already been
    # merged and is already restated onto the anchor's basis. So where the
    # anchor is silent, judge the orphan against everything MERGED SO FAR. That
    # never compares two concepts which were not in force together -- it
    # compares the orphan against the anchor's own basis, carried back in time
    # by a concept that overlapped both.
    #
    # This only fires where the anchor comparison returns nothing at all. A
    # concept the anchor can see is judged by the anchor exactly as before, and
    # one the anchor refuses stays refused.
    #
    # Passes repeat because the bridging concept can be merged after the orphan
    # was first considered. Each pass that merges something opens new ground for
    # the rest, and the loop stops the moment a pass settles nothing.
    merged = dict(per_tag[primary])
    pending = [r for r in sorted(per_tag) if r != primary]
    while pending:
        settled: list[int] = []
        for rank in pending:
            candidate = per_tag[rank]
            # Only the periods this concept would actually add matter; the rest
            # is already covered and will not be used whatever the scale says.
            adds = [p.end.year for k, p in candidate.items() if k not in merged]
            near = int(np.median(adds)) if adds else None
            ratio = compare_scale(per_tag[primary], candidate, near)
            chained = False
            if ratio is None or ratio <= 0:
                ratio = compare_scale(merged, candidate, near)
                chained = True
            if ratio is None or ratio <= 0:
                continue        # may still become comparable on a later pass
            where = "merged history" if chained else "the anchor"
            siblings = {tags[primary], tags[rank]} == pair or (
                excise_filer and tags[primary] in pair)
            band = (0.5, 2.0) if siblings else (0.80, 1.25)
            if abs(ratio - 1) <= 0.05:
                scale = 1.0                 # the same measure; join it as filed
            elif band[0] <= ratio <= band[1]:
                # Close but not identical -- the same measure with a definitional
                # wrinkle. Rescale the older concept onto the current basis
                # instead of discarding it. Discarding was costing Rollins two
                # and a half years, and a short window drops the older, cheaper
                # years, lifts the median, and makes a stock look further below
                # its norm than it is. For a median, internal consistency
                # matters far more than the absolute level of a decade-old
                # figure.
                scale = 1.0 / ratio
            else:
                # Genuinely different measures. Amazon's older concept covered
                # product sales only against a newer one covering everything,
                # and joining them invented a 71% jump. Refuse; years_covered
                # reports the cost honestly.
                if tracing:
                    trace.append(f"  {tags[rank]}: REFUSED, runs {ratio:.2f}x "
                                 f"{where} where they overlap")
                settled.append(rank)
                continue
            if tracing:
                trace.append(f"  {tags[rank]}: merged at {ratio:.3f}x {where} "
                             f"(rescaled by {scale:.3f})")
            for k, period in candidate.items():
                if k not in merged:
                    merged[k] = Period(period.start, period.end, period.val * scale,
                                       period.filed, period.tag)
            settled.append(rank)
        if not settled:
            break
        pending = [r for r in pending if r not in settled]

    if tracing:
        for rank in pending:
            trace.append(f"  {tags[rank]}: no comparable overlap, not merged")

    # WHEN a period was first reported is a separate question from WHAT the
    # right figure for it is, and the two must be answered from different
    # evidence. The value has to come from a concept that agrees with the
    # anchor's scope. The date does not: a period was public the day any filing
    # first reported it, whatever concept carried it and whatever that concept
    # measured.
    #
    # Losing this cost the series about two years of freshness at every
    # accounting-standard change. The transition quarters survive under the new
    # concept only as restated comparatives filed a year or two later, while the
    # concept the filer was actually using at the time carries them filed on
    # time. Camden shows the sharper version: its Q3 2018 was reported on
    # 2018-10-26 under a concept that is REFUSED here, correctly, because after
    # 2019 that concept holds only a $6m non-lease slice against a $1bn anchor.
    # Refusing its value is right; refusing its date meant Camden was valued
    # through 2019 on revenue from mid-2018.
    first_seen: dict[tuple[date, date], date] = {}
    for d in per_tag.values():
        for k, p in d.items():
            if k not in first_seen or p.filed < first_seen[k]:
                first_seen[k] = p.filed
    for k, p in merged.items():
        earliest = first_seen.get(k)
        if earliest and earliest < p.filed:
            merged[k] = Period(p.start, p.end, p.val, earliest, p.tag)

    if tracing:
        CONCEPT_TRACE["revenue"] = trace

    if len(merged) < 8:
        return []
    ordered = sorted(merged.values(), key=lambda x: (x.end, x.start))
    if screen_annuals is True:
        return _screen_annuals(ordered)
    if screen_annuals == "upper_only":
        return _screen_annuals(ordered, lower=0.0)
    return ordered


def _screen_annuals(periods: list[Period], lower: float = 0.12,
                    upper: float = 8.0) -> list[Period]:
    """
    Drop annual facts the company's other years make impossible.

    This has to happen at collection, not later. Q4 is derived as (annual minus
    nine months), so a bad annual manufactures a matching bad quarter and the
    trailing total then AGREES with it -- every downstream plausibility check
    sees a consistent series and waves it through. General Mills' -$88m annual
    walked past two guards built specifically to stop it for exactly that
    reason, and then past a third because I screened one branch of the pipeline
    and handed the raw facts to the other.

    The band is deliberately wide: a genuinely terrible year must survive. Only
    a figure the rest of the decade rules out is removed, and never more than a
    third of the years.
    """
    years = [p for p in periods
             if ANNUAL_DAYS[0] <= p.days <= ANNUAL_DAYS[1] and p.val != 0]
    if len(years) < 5:
        return periods
    # Benchmark each year against its NEIGHBOURS in time, not the whole series.
    # A whole-history median is meaningless for a company that grew: Salesforce
    # earned $0.13bn a year through the 2010s and $7.5bn in 2026, so its real
    # recent profits sat 57x the median and were discarded as impossible. Local
    # comparison handles growth for free -- $7.5bn beside $6.2bn is unremarkable,
    # while a -$88m beside $2.3bn still stands out.
    years = sorted(years, key=lambda q: q.end)
    mags = [abs(q.val) for q in years]
    rejects = set()
    for i, q in enumerate(years):
        lo_i, hi_i = max(0, i - 2), min(len(mags), i + 3)
        near = [m for k, m in enumerate(mags[lo_i:hi_i], start=lo_i) if k != i]
        scale = float(np.median(near)) if near else 0.0
        if scale <= 0:
            continue
        # Asymmetric on purpose. A year far BELOW its neighbours is ordinary for
        # earnings -- impairments, restructurings, outright losses -- and cutting
        # those rewrites a company's history to look smoother than it was.
        # Callers dealing with lumpy series pass lower=0 to keep the floor open
        # while still cutting the ceiling.
        if not (lower <= abs(q.val) / scale < upper):
            rejects.add((q.start, q.end))
    # What matters is how many good years REMAIN, not how many bad ones go. The
    # old test bailed out whenever more than a third were rejected, which turned
    # the screen off entirely for filers carrying many segment-level facts at
    # annual durations -- the very companies that need it. General Mills has 362
    # net income facts across 18 years and this valve disabled the screen on
    # every one of them.
    if not rejects or (len(years) - len(rejects)) < 5:
        return periods
    return [p for p in periods if (p.start, p.end) not in rejects]


def _normalize_share_splits(series: list[tuple[date, float]], jump: float = 3.0
                            ) -> list[tuple[date, float]]:
    """Put a share-count series on ONE split basis, the newest.

    A raw share concept spanning a split carries pre-split and post-split
    counts on different bases -- Alphabet's CommonStockSharesOutstanding reads
    ~680M through 2020 and ~13,240M from 2021 after its 20-for-1. Walking from
    newest to oldest, wherever an adjacent pair jumps by >=3x with no economic
    cause, every older point is rescaled by the rounded ratio so the whole
    series is expressed in current shares. A series with no split is unchanged.
    """
    if len(series) < 2:
        return series
    out = [list(x) for x in sorted(series)]
    for i in range(len(out) - 1, 0, -1):
        newer, older = out[i][1], out[i - 1][1]
        # BOTH sides must be a real positive count. A zero share value -- some
        # filers report CommonStockSharesOutstanding as 0 in a stub period
        # (Carvana, Datadog, Robinhood) -- is not a 20-for-1 split, and dividing
        # by the resulting zero ratio is what crashed the full run.
        if older > 0 and newer > 0:
            r = newer / older
            if r >= jump:
                fac = round(r)
                for j in range(i):
                    out[j][1] *= fac
            elif r <= 1.0 / jump:
                fac = round(1.0 / r)
                for j in range(i):
                    out[j][1] /= fac
    return [(dt, v) for dt, v in out]


def _correct_units_error(shares: list[tuple[date, float]], facts: dict
                         ) -> list[tuple[date, float]]:
    """Fix a share reading that slipped by a power of ten, and ONLY that.

    A filing can tag a share count off by a round factor of ten -- Waters'
    post-merger diluted count reads 98,204M against a true 98.2M (x1000), while
    its shares-outstanding and dei cover-page both carry the correct 98.2M. The
    fix fires only under strict confirmation, because a naive version corrupted
    good data: it must be a power-of-ten off the concept's OWN recent median
    (so AutoZone's clean diluted is never touched even though AutoZone's
    shares-outstanding is itself corrupt in 2009), AND an independent concept
    near that date must sit at the RESCALED value and NOT at the raw one (so a
    real split, which moves every concept together, is never rescaled). Anything
    that does not clear both tests is left alone for the reliability report to
    flag rather than silently altered.
    """
    if len(shares) < 6:
        return shares
    own = sorted(v for _, v in shares if v > 0)
    med = own[len(own) // 2] if own else 0.0
    if med <= 0:
        return shares
    cross = (collect_instants(facts, "us-gaap", ["CommonStockSharesOutstanding"])
             or collect_instants(facts, "dei", ["EntityCommonStockSharesOutstanding"]))
    out = []
    for d, v in shares:
        acted = False
        if v > 0:
            r = v / med
            if r > 50 or r < 1 / 50:
                p10 = 10 ** round(math.log10(r)) if r > 0 else 1
                cand = v / p10 if p10 else v
                near = [cv for cd, cv in cross
                        if abs((cd - d).days) <= 100 and cv > 0] if cross else []
                if near and p10 not in (0, 1):
                    cm = float(np.median(near))
                    # independent concept confirms the RESCALED value, not the raw
                    if (0.5 <= cand / cm <= 2.0 and not (0.5 <= v / cm <= 2.0)
                            and 0.3 <= cand / med <= 3.0):
                        out.append((d, cand))
                        acted = True
        if not acted:
            out.append((d, v))
    return out


def _splice_share_history(chosen: list[tuple[date, float]], chosen_src: str,
                          facts: dict) -> list[tuple[date, float]]:
    """Extend a short diluted history backward from a longer concept.

    Weighted-average diluted is the right consolidated count but for some
    filers it is only tagged from ~2022, which truncated Alphabet's P/S history
    below the five-year minimum and dropped it entirely. Where a longer concept
    exists, split-normalise it, scale it to diluted on their overlap (absorbing
    the small multi-class offset), and backfill only the years diluted lacks.
    The overlap for these filers is post-split, so the scale is a clean ~1.0
    and the join introduces no step. Only fires when diluted was chosen and a
    materially longer concept is present, so nothing else is touched.
    """
    if "Diluted" not in chosen_src or not chosen:
        return chosen
    start = chosen[0][0]
    best = None
    for alt in ("CommonStockSharesOutstanding",
                "WeightedAverageNumberOfSharesOutstandingBasic"):
        raw = collect_instants(facts, "us-gaap", [alt])
        if raw and raw[0][0] < start - timedelta(days=200):
            if best is None or raw[0][0] < best[0][0]:
                best = raw
    if best is None:
        return chosen
    # Scale the backfill onto the diluted basis using ONLY overlap years where
    # both are on the same split basis. The overlap is recent (diluted starts
    # ~2022, post-split), so any pre-split years in the backfill keep their raw
    # basis and the pipeline's own split detection lifts them by the factor
    # afterwards -- exactly as it does for a single-concept series like Nike's.
    # We must NOT normalise the split out here: the reconciliation downstream
    # validates Yahoo's declared split against the reported count, and a
    # pre-adjusted count reads flat across the split date and gets the real
    # split wrongly rejected. Splicing raw and letting one machinery own the
    # split keeps the two from fighting.
    ad = {dt.year: v for dt, v in best}
    cd = {dt.year: v for dt, v in chosen}
    # Overlap ratio from the most recent shared years only, so a split sitting
    # inside the backfill span cannot distort the scale.
    overlap = sorted(y for y in cd if y in ad and ad[y] > 0)
    if len(overlap) < 2:
        return chosen
    recent = overlap[-3:]
    scale = float(np.median([cd[y] / ad[y] for y in recent]))
    if not (0.5 <= scale <= 2.0):     # refuse an implausible join
        return chosen
    backfill = [(dt, v * scale) for dt, v in best if dt < start]
    return sorted(backfill + list(chosen))


def collect_instants(facts: dict, taxonomy: str, tags: list[str]) -> list[tuple[date, float]]:
    """
    Pull instant facts (share counts), merging every listed tag.

    Stopping at the first tag with data was the same bug as the revenue path:
    Nike's dei count stops in 2015, so a count from eleven years ago was carried
    forward to value the company today. Tags are merged in priority order, and
    a lower-priority tag fills any date the preferred one never covered.
    """
    node = facts.get("facts", {}).get(taxonomy, {})
    out: dict[date, float] = {}
    seen_rank: dict[date, int] = {}
    candidates: dict[date, dict] = {}

    for rank, tag in enumerate(tags):
        entry = node.get(tag)
        if not entry:
            continue
        for unit_rows in entry.get("units", {}).values():
            for r in unit_rows:
                if r.get("val") is None:
                    continue
                stamp = r.get("end") or r.get("instant")
                if not stamp:
                    continue
                try:
                    when, val = _d(stamp), float(r["val"])
                except (ValueError, TypeError):
                    continue
                if val <= 0:
                    continue      # a zero/negative share count is a stub, not data
                # The dei count is stated on the filing's cover page, current as
                # of a date near FILING, not as of the period end EDGAR files it
                # under. Dating it to the period end put pre-split labels on
                # post-split values and double-counted the split -- that is what
                # made Alphabet's market cap appear to jump 1,837% in a day.
                if taxonomy == "dei" and r.get("filed"):
                    try:
                        when = _d(r["filed"])
                    except (ValueError, TypeError):
                        pass
                # Where several values share a date, keep the largest and record
                # that others existed. Summing them was a mistake: a date can
                # carry the same count twice (one fact carrying a filing date,
                # one falling back to the period end), and adding those doubled
                # the count for 50-odd companies. Adding real share classes is
                # wrong too -- a Berkshire A share is worth about 1,500 B shares,
                # so the counts are not additive against a single price. The
                # dominant class is the right number for market cap, and the rest
                # is a caveat rather than an adjustment.
                # Key on the FILING as well as the date. Several values from one
                # filing are share classes -- take the largest, since a Berkshire
                # A share is worth ~1,500 B shares and the counts are not
                # additive against one price. Values from DIFFERENT filings for
                # the same date are a restatement, and us-gaap counts are
                # retroactively restated for splits: a 2016 filing reports Nike's
                # 2014 count post-split. Taking that and then applying the split
                # factor doubles it. Keep the earliest filing's figure, which is
                # the count as it stood at the time, and let the split
                # adjustment do its job from there.
                slot = candidates.setdefault(when, {})
                stamp = r.get("filed") or "9999-99-99"
                slot[stamp] = max(slot.get(stamp, 0.0), val)
                if when not in seen_rank or rank < seen_rank[when]:
                    seen_rank[when] = rank

    for when, by_filing in candidates.items():
        earliest = min(by_filing)
        out[when] = float(by_filing[earliest])

    series = sorted(out.items())
    # Reject a single corrupt point. Some filings carry a share value orders of
    # magnitude wrong -- Booking's newest quarters read 782M against a real 33M,
    # Waters' read 98,000M against 60M -- usually a units slip in one XBRL fact.
    # A real split or real issuance moves the series and STAYS moved; a value
    # that is a large multiple of BOTH its neighbours and reverts is not an
    # event, it is bad data. Drop only such isolated spikes, and only when there
    # is a clean neighbour to fall back on, so genuine step-changes (a real
    # split, sustained dilution) are never touched. Deliberately narrow: this
    # fixes corruption, not the multi-class question, which the caller handles.
    return series


def derive_quarters(periods: list[Period], smooth: bool = True) -> list[Period]:
    """
    Reduce mixed-duration XBRL periods to discrete quarters.

    Filers report a grab-bag: standalone quarters in 10-Qs, cumulative
    year-to-date figures, and a full year in the 10-K with no Q4 line at all.
    Q4 has to be backed out as (full year - nine months), and YTD stacks have to
    be differenced. This does both by repeated subtraction until nothing new
    falls out.
    """
    have = {(p.start, p.end): p for p in periods}
    # A quarter is normally 13 weeks (~91 days), but a 52/53-week retailer runs a
    # 16-week quarter -- Kroger's Q1 is 111 days, Costco's Q4 ~119. Capping the
    # quarter set at 100 days dropped those quarters, left a hole in the year,
    # and no four consecutive quarters could span a trailing twelve. Accept up to
    # 120 days, which admits a 16-week quarter while staying well clear of a
    # half-year (183 days), so a cumulative stub still cannot masquerade as one.
    QUARTER_MAX = 120
    quarters = {k: p for k, p in have.items() if QUARTER_DAYS[0] <= p.days <= QUARTER_MAX}

    for _ in range(6):  # converges in 2-3 passes; bounded to avoid pathological data
        added = False
        by_start: dict[date, list[Period]] = {}
        for p in have.values():
            by_start.setdefault(p.start, []).append(p)

        for start, group in by_start.items():
            group.sort(key=lambda x: x.end)
            for i, longer in enumerate(group):
                for shorter in group[:i]:
                    gap = (longer.end - shorter.end).days
                    # Normally the gap between two same-start periods must be one
                    # calendar quarter to be a derivable quarter. But a 52/53-week
                    # retailer's FINAL quarter is 16 weeks, not 13: Costco closes
                    # its year with a Jun-Aug quarter of ~119 days, so backing it
                    # out as (annual - nine months) leaves a 119-day gap the
                    # 80-100 window rejected -- and with Q4 missing every year, no
                    # four consecutive quarters existed and the trailing twelve
                    # was empty. Where the LONGER period is a full year, allow the
                    # final stub to run up to 130 days so that Q4 can be recovered.
                    # The widening is gated on the longer period being annual so a
                    # stray long gap between two interior periods is still
                    # rejected, and the size and reconciliation guards below still
                    # discard the result if the backed-out value is implausible.
                    longer_is_annual = ANNUAL_DAYS[0] <= longer.days <= ANNUAL_DAYS[1]
                    hi = 130 if longer_is_annual else QUARTER_DAYS[1]
                    if not (QUARTER_DAYS[0] <= gap <= hi):
                        continue
                    # Differencing across concepts USED to be banned outright,
                    # which cost ~148 companies a full year of quarters at the
                    # 2017 accounting-standard boundary: the 10-K arrived under
                    # the new concept while that year's 10-Qs were still under the
                    # old one, so Q4 could never be derived. The ban predates
                    # collect_periods verifying that concepts agree before it
                    # joins them, and two guards now catch a bad derivation
                    # anyway -- a quarter far larger than its neighbours is
                    # rejected, and the year is reconciled against the filer's
                    # own reported annual. Verified-compatible concepts can be
                    # differenced.
                    key = (shorter.end, longer.end)
                    if key in have:
                        continue
                    derived = Period(
                        shorter.end,
                        longer.end,
                        longer.val - shorter.val,
                        max(longer.filed, shorter.filed),
                        longer.tag if longer.tag == shorter.tag else "mixed",
                    )
                    have[key] = derived
                    quarters[key] = derived
                    added = True
        if not added:
            break

    out = sorted(quarters.values(), key=lambda x: x.end)

    # Reject any "quarter" too large to be one. L3Harris files its full-year
    # figure under a 90-day period -- $21.9bn against a real Q4 of ~$5.6bn --
    # and taking that at face value inflated its trailing revenue to $39bn
    # against an actual $21bn. A quarter cannot be most of the year that
    # contains it, whatever the period dates claim.
    # Compare magnitudes, not signed values. Revenue is always positive, but the
    # same machinery now carries net income: a loss year has a negative annual,
    # so a signed ">" test is true for every loss quarter and would silently
    # delete them all.
    # These two guards assume revenue's shape: smooth, always positive, roughly
    # a quarter of the year in each quarter. Earnings and cash flow are neither.
    # Intuit is a tax business whose Feb-Apr quarter carries almost the entire
    # year -- $2.7bn against a $2.8bn annual -- so both guards fired on it every
    # year, deleted its main quarter, and left nothing to build a series from.
    annuals = [p for p in periods
               if ANNUAL_DAYS[0] <= p.days <= ANNUAL_DAYS[1] and abs(p.val) > 0]
    if annuals and smooth:
        checked = []
        for q in out:
            covering = [a for a in annuals if a.start <= q.start and q.end <= a.end]
            if covering and abs(q.val) > 0.70 * max(abs(a.val) for a in covering):
                continue
            checked.append(q)
        if len(checked) >= 8:
            out = checked

    # Same idea without needing an annual to compare against. L3Harris tagged
    # its whole 2025 year with Q4 dates and filed no correct annual at all, so
    # the check above had nothing to measure against and let a $21.9bn "quarter"
    # through beside neighbours of $5.5bn. A quarter several times the ones
    # around it is wrong whatever the period dates say.
    if len(out) >= 9 and smooth:
        vals = np.array([q.val for q in out], dtype=float)
        kept = []
        for i, q in enumerate(out):
            lo, hi = max(0, i - 4), min(len(vals), i + 5)
            neighbours = np.delete(vals[lo:hi], min(i, i - lo))
            local = float(np.median(np.abs(neighbours))) if len(neighbours) else 0.0
            if local > 0 and abs(q.val) > 3.0 * local:
                continue
            kept.append(q)
        if len(kept) >= 8:
            out = kept

    # A quarter's DISCLOSURE DATE is the earliest date it could be obtained, not
    # the filing date of whichever fact happens to carry it.
    #
    # Honeywell's Q4 2024 is a worked example. It is derivable on 2025-02-14 as
    # (FY2024 annual - nine months), both of which were public by then. But
    # Honeywell also tags an explicit Oct-Dec 2024 fact as the prior-year
    # comparative in its FY2025 10-K, filed 2026-02-17, and that fact carries
    # dates one day apart from the derived one -- a different key, so both
    # survive to here and the later-filed one won the overlap test. Its filing
    # date then set availability for the whole trailing year, and cummax dragged
    # every 2025 point up with it: sixteen months of Honeywell's P/S dividing by
    # revenue through September 2024 while pricing mid-2025. Revenue was rising
    # across that stretch, so the multiple was overstated for all of it, the
    # ten-year median came out high, and nothing flagged it -- the period ends
    # are a clean 92 days apart the whole way, which is all the old gap test
    # could see.
    #
    # Collapse quarters covering the same span to one: the value from the latest
    # filing, because a restatement supersedes, and the date from the earliest,
    # because that is when the market could first work it out. That is the rule
    # collect_periods already applies to duplicate facts; it was simply never
    # carried across to derived ones.
    by_span: dict[tuple[date, int], Period] = {}
    ends: list[date] = []
    for q in sorted(out, key=lambda x: (x.end, x.filed)):
        # A 52/53-week filer reports the same quarter under two different end
        # dates: Waters' Q1 2021 is Jan 1 - Apr 3 in the 10-Q filed on time, and
        # Jan 1 - Mar 31 in the 10-K that restates it to calendar quarters a year
        # later. Identical value, three days apart, so an exact-date key treats
        # them as two quarters and the overlap filter keeps whichever it met
        # first -- which was the year-late one. Match on the same few-days
        # tolerance the annual reconciliation already uses.
        anchor = next((e for e in ends if abs((q.end - e).days) <= 5), q.end)
        if anchor == q.end:
            ends.append(q.end)
        key = (anchor, round(q.days / 7))
        prior = by_span.get(key)
        if prior is None:
            by_span[key] = q
        else:
            newest = q if q.filed > prior.filed else prior
            by_span[key] = Period(newest.start, newest.end, newest.val,
                                  min(q.filed, prior.filed), newest.tag)
    out = sorted(by_span.values(), key=lambda x: x.end)

    # Drop overlaps: keep the first quarter, then only quarters starting at or
    # after the previous one ended.
    clean: list[Period] = []
    for q in out:
        if clean and q.start < clean[-1].end - timedelta(days=10):
            continue
        if q.val is None or not math.isfinite(q.val):
            continue
        clean.append(q)
    return clean


def trailing_twelve(quarters: list[Period],
                    annuals: list[Period] | None = None) -> pd.DataFrame:
    """
    Rolling 4-quarter sums, stamped with the date they became public, checked
    against the company's own reported annual totals.

    This closes the gap that let L3Harris read 80% high for several runs while
    passing every other check. The structural audit only looks for STEPS, so an
    error that is consistently wrong across the whole history is invisible to
    it. But the filer publishes the answer: four quarters must add up to the
    year they sit in. Where they do not, the quarters are wrong and the reported
    annual wins.
    """
    rows = []
    for i in range(3, len(quarters)):
        window = quarters[i - 3: i + 1]
        span = (window[-1].end - window[0].start).days
        if not (ANNUAL_DAYS[0] <= span <= ANNUAL_DAYS[1]):
            continue  # a gap in the quarters -- don't fabricate a TTM across it
        tags = {q.tag for q in window}
        rows.append({
            "period_end": window[-1].end,
            "available": max(q.filed for q in window),
            "ttm": sum(q.val for q in window),
            "tag": tags.pop() if len(tags) == 1 else "mixed",
        })
    if not rows:
        return pd.DataFrame(columns=["period_end", "available", "ttm"])
    df = pd.DataFrame(rows)
    # Force a single datetime resolution -- merge_asof refuses to join keys of
    # differing precision, and these come from three different sources.
    df["available"] = pd.to_datetime(df["available"]).astype("datetime64[ns]")
    df["period_end"] = pd.to_datetime(df["period_end"]).astype("datetime64[ns]")
    # Order by the period the revenue belongs to, not by when it was filed.
    # Sorting by filing date let a late-disclosed old quarter drop a small TTM
    # in between two large recent ones, which reads as a huge jump and back --
    # that is the +99% "step" reported for Alphabet, whose underlying filings
    # agree to the cent. Availability is then forced non-decreasing so the
    # point-in-time guarantee survives the reordering: nothing can appear to
    # have been knowable earlier than something already in the series.
    df = df.sort_values("period_end").reset_index(drop=True)
    df["available"] = df["available"].cummax()

    if annuals:
        by_end = {}
        for a in annuals:
            if ANNUAL_DAYS[0] <= a.days <= ANNUAL_DAYS[1] and abs(a.val) > 0:
                by_end.setdefault(pd.Timestamp(a.end), []).append(a.val)
        if by_end:
            reported = pd.Series({k: max(v, key=abs) for k, v in by_end.items()})
            # Match a TTM to an annual ending within a few days of it: fiscal
            # year ends drift by a day or two between years.
            fixed, mismatches, implausible = 0, 0, 0
            vals = df["ttm"].to_numpy(dtype=float).copy()
            for i, pe in enumerate(df["period_end"]):
                near = reported.index[(reported.index - pe).days.map(abs) <= 5]
                if not len(near):
                    continue
                target = float(reported[near[0]])
                if target == 0 or abs(vals[i] / target - 1) <= 0.02:
                    continue
                # Trusting the reported annual is right when the quarters are
                # wrong -- that is what fixed L3Harris. But it has to be the more
                # believable of the two: General Mills carries a -$88m annual
                # fact beside $2.3bn of trailing profit, and overriding good
                # quarters with it made that the company's headline margin.
                # Same bad fact the fill already refuses, arriving by a
                # different door.
                scale = float(np.median(np.abs(vals))) if len(vals) else 0.0
                if scale > 0 and not (0.2 < abs(target) / scale < 5.0):
                    implausible += 1
                    continue
                mismatches += 1
                vals[i] = target
                fixed += 1
            df["ttm"] = vals
            df.attrs["annual_mismatches"] = mismatches
            df.attrs["annual_implausible"] = implausible
            df.attrs["annual_checks"] = int(sum(
                1 for pe in df["period_end"]
                if len(reported.index[(reported.index - pe).days.map(abs) <= 5])))

            # Fill year ends the quarters could not produce at all. Alphabet was
            # missing five consecutive quarters, which left a fourteen-month hole
            # that the P/S series simply carried the old figure across. The
            # company published the answer for those years; there is no reason to
            # leave the hole open.
            have = set(df["period_end"])
            added = []
            nearby = df["ttm"].abs()
            typical = float(nearby.median()) if len(nearby) else 0.0
            for when, val in reported.items():
                if any(abs((when - h).days) <= 5 for h in have):
                    continue
                # Do not insert a figure the surrounding series contradicts.
                # General Mills had a -$88m annual fact filled in beside $2.3bn
                # of trailing profit; one bad fact at the end of the series
                # became the company's headline margin.
                if typical > 0 and not (0.2 < abs(val) / typical < 5.0):
                    continue
                added.append({"period_end": when,
                              # A 10-K lands about eight weeks after the year end;
                              # assume that rather than pretend it was knowable
                              # on the day.
                              "available": when + pd.Timedelta(days=57),
                              "ttm": float(val), "tag": "reported-annual"})
            if added:
                df = pd.concat([df, pd.DataFrame(added)], ignore_index=True)
                df = df.sort_values("period_end").reset_index(drop=True)
                df["available"] = df["available"].cummax()
                df.attrs["annual_mismatches"] = mismatches
                df.attrs["annual_checks"] = checks_total = int(sum(
                    1 for pe in df["period_end"]
                    if len(reported.index[(reported.index - pe).days.map(abs) <= 5])))
                df.attrs["filled_from_annual"] = len(added)
    return df


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str], start: date, *,
                 refresh: bool = False,
                 max_age_hours: int = 20) -> tuple[dict, dict]:
    """Month-end closes plus each ticker's split history, in one bulk pass.

    Kept on disk between runs. Thirteen years of daily data for 500 companies
    was being pulled again on every single run, which is a long wait to find
    out whether a split classifier behaves. The series is sampled monthly, so
    a few hours of staleness cannot move a multiple; --refresh forces a pull.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    store = CACHE / "prices.json"

    # Thirteen years of history does not change. Only the last few days do.
    # Expiring the whole cache after 20 hours meant every daily run pulled 13
    # years for 500 companies again -- eight minutes to learn about three new
    # closes. The history is kept indefinitely and only a short recent window
    # is refetched, which is one small bulk call.
    blob = {}
    if store.exists() and not refresh:
        try:
            blob = json.loads(store.read_text())
        except (ValueError, OSError):
            blob = {}

    closes, splits, needed, topup = {}, {}, [], []
    cutoff = date.today() - timedelta(days=RECENT_DAYS)
    for t in tickers:
        rec = blob.get(t)
        if not rec or not rec.get("prices"):
            needed.append(t)
            continue
        ser = pd.Series({pd.Timestamp(k): float(v)
                         for k, v in rec["prices"].items()}).sort_index()
        closes[t] = ser
        if rec.get("splits"):
            splits[t] = pd.Series({pd.Timestamp(k): float(v)
                                   for k, v in rec["splits"].items()}).sort_index()
        # Top up whenever the cache is a day or more behind. The earlier test
        # tolerated five days, which would have quietly priced the screen off a
        # week-old close and started spurious disagreements with Yahoo's live
        # market cap. The top-up is one bulk call over a short window, so there
        # is no reason to be stingy about it.
        if (date.today() - ser.index[-1].date()).days >= 1:
            topup.append(t)

    if closes:
        print(f"  {len(closes)} of {len(tickers)} price histories from cache; "
              f"{len(needed)} to fetch in full, {len(topup)} to bring up to date")
    if not needed and not topup:
        return closes, splits          # nothing to ask for; no network at all

    import yfinance as yf

    def keep(t, series, sp):
        closes[t] = series
        if sp is not None and len(sp):
            splits[t] = sp
        blob[t] = {
            "prices": {str(k.date()): round(float(v), 4) for k, v in series.items()},
            "splits": ({str(k.date()): float(v) for k, v in sp.items()}
                       if sp is not None and len(sp) else {}),
        }

    chunk = 40
    for i in range(0, len(needed), chunk):
        batch = needed[i: i + chunk]
        print(f"  prices {i + 1}-{min(i + chunk, len(needed))} of {len(needed)}")
        try:
            data = yf.download(
                batch, start=start.isoformat(), auto_adjust=False, actions=True,
                progress=False, group_by="ticker", threads=True,
            )
        except Exception as e:
            print(f"    batch failed ({e}); retrying once")
            time.sleep(10)
            try:
                data = yf.download(
                    batch, start=start.isoformat(), auto_adjust=False, actions=True,
                    progress=False, group_by="ticker", threads=True,
                )
            except Exception:
                continue
        for t in batch:
            try:
                frame = data[t] if len(batch) > 1 else data
            except (KeyError, TypeError):
                continue
            try:
                series = frame["Close"].dropna()
            except (KeyError, TypeError):
                continue
            if not len(series):
                continue
            # Splits ride along in the same response, so no extra requests.
            sp = None
            try:
                sp = frame["Stock Splits"]
                sp = sp[sp > 0]
            except (KeyError, TypeError):
                sp = None
            keep(t, series, sp)
        time.sleep(1)  # Yahoo throttles aggressively on back-to-back bulk pulls

    for i in range(0, len(topup), 200):
        batch = topup[i: i + 200]
        print(f"  topping up {len(batch)} histories from {cutoff}")
        try:
            data = yf.download(batch, start=cutoff.isoformat(), auto_adjust=False,
                               actions=True, progress=False, group_by="ticker",
                               threads=True)
        except Exception as e:
            print(f"    top-up failed ({e}); using cached history as-is")
            break
        for t in batch:
            try:
                frame = data[t] if len(batch) > 1 else data
                recent = frame["Close"].dropna()
            except (KeyError, TypeError):
                continue
            if not len(recent):
                continue
            merged = pd.concat([closes[t], recent])
            closes[t] = merged[~merged.index.duplicated(keep="last")].sort_index()
            try:
                sp = frame["Stock Splits"]
                sp = sp[sp > 0]
                if len(sp):
                    splits[t] = (pd.concat([splits.get(t, pd.Series(dtype=float)), sp])
                                 .pipe(lambda x: x[~x.index.duplicated(keep="last")])
                                 .sort_index())
            except (KeyError, TypeError):
                pass
            blob[t] = {
                "prices": {str(k.date()): round(float(v), 4)
                           for k, v in closes[t].items()},
                "splits": ({str(k.date()): float(v) for k, v in splits[t].items()}
                           if t in splits else {}),
            }
        time.sleep(1)

    try:
        store.write_text(json.dumps(blob))          # one write for the lot
    except OSError:
        pass
    return closes, splits



# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def shares_in_todays_units(hist: pd.DataFrame, splits: dict | None = None) -> pd.Series:
    """The share count restated so every point means the same thing.

    hist["shares"] is deliberately kept consistent with hist["price"]: where
    Yahoo has not yet back-adjusted a fresh split, both sides stay pre-split
    and the market cap is right. That is correct for a multiple and wrong for
    anything measuring a CHANGE in the share base -- Monster's two-for-one
    read as 100.6% dilution and tripped the share-base audit, when nothing had
    been issued at all.
    """
    sh = hist["shares"].astype(float).copy()
    # Passed in explicitly: DataFrame.attrs does not survive sort_values or
    # reset_index, so reading it here silently returned the raw counts and the
    # audit went on reporting Monster's split as 101% dilution.
    if splits is None:
        splits = hist.attrs.get("splits") or {}
    for when, (ratio, rescaled) in splits.items():
        if rescaled or not ratio or ratio <= 0:
            continue                     # already folded into the series
        mask = pd.DatetimeIndex(hist["date"]) < pd.Timestamp(when)
        sh[mask] = sh[mask] * ratio
    return sh


def monthly_ps(
    prices: pd.Series,
    ttm: pd.DataFrame,
    shares: list[tuple[date, float]],
    splits: pd.Series | None,
    years: int,
) -> pd.DataFrame:
    if ttm.empty or not shares or prices.empty:
        return pd.DataFrame()

    px = prices.copy()
    idx = pd.to_datetime(px.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    px.index = idx.astype("datetime64[ns]")
    # Every trading day, not month ends. Twelve samples a year is thin for a
    # median and, worse, systematic -- it can miss a whole move that begins and
    # ends inside one month. The daily series is already downloaded, so the only
    # cost is a bigger join, and it makes the range and percentile columns
    # meaningful rather than approximate.
    monthly = px.dropna()
    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(years=years)
    monthly = monthly[monthly.index >= cutoff]
    if len(monthly) < 60:
        return pd.DataFrame()

    dates = pd.DataFrame({
        "date": pd.DatetimeIndex(monthly.index).astype("datetime64[ns]"),
        "price": monthly.values,
    })

    sh = pd.DataFrame(shares, columns=["asof", "shares"])
    sh["asof"] = pd.to_datetime(sh["asof"]).astype("datetime64[ns]")
    sh = sh.sort_values("asof").reset_index(drop=True)

    # A share count must be expressed on the same split basis as the price it is
    # multiplied by. Getting this backwards silently doubles every pre-split
    # market cap while leaving today's correct, which corrupts the historical
    # median and is nearly invisible -- it was why Monster read 17x against a
    # true 8.7x. So: scale each count by the splits between when it was measured
    # and the basis the price series is quoted on, and determine that basis by
    # looking at whether the price series still contains the split drop.
    # Yahoo back-adjusts old splits into its price history but takes a while to
    # propagate a fresh one, so a single company's series can be on BOTH sides at
    # once -- Monster's 2023 split is rescaled while its 2026 split, eleven days
    # old, is not. Treating that as one convention per company is what produced a
    # 17.5x historical median against a true 8.7x. So each split is classified on
    # its own evidence: did the price actually drop, or not?
    split_info = classify_splits(px, splits)

    # A ratio test alone is not enough. A spin-off whose distribution factor
    # happens to land on a round number is indistinguishable from a split by
    # its ratio: Honeywell's separation arrives as 2.0, passes as a two-for-one,
    # doubles the historical share count and publishes a 50.1% share reduction
    # that never happened. Monster shows the same thing in reverse at +100.6%.
    #
    # There is independent evidence to settle it. In a split the company's own
    # reported share count changes; in a spin-off it does not. The counts come
    # from EDGAR cover pages, which is a different source from Yahoo's splits
    # column, so agreement between them is real corroboration.
    split_info = corroborate_splits(split_info, shares)
    split_info.update(splits_the_share_count_reveals(shares, split_info, px, ttm))

    # Every count is first expressed in today's post-all-splits units...
    sh["shares"] = sh["shares"] * cumulative_factor(
        pd.DatetimeIndex(sh["asof"]), split_info).values

    # Now that splits are accounted for, the series should be smooth: real share
    # counts drift a few percent a year. Anything several times its neighbours is
    # a unit error or a wrong-taxonomy value. This check only works AFTER the
    # adjustment -- on the raw series a genuine 20-for-1 is indistinguishable
    # from a unit mistake.
    # A real share count moves a few percent a quarter. The band has to be tight
    # enough to catch a halving or a doubling, because that is what a mis-dated
    # record around a split looks like: dei cover-page counts are current as of a
    # date near filing, so a 10-Q filed just after a split can carry a pre-split
    # figure, and no dating convention gets every record right.
    #
    # A CENTRED median is what makes this safe. A single bad point sits far from
    # the median of its neighbours on both sides and is dropped. A genuine
    # corporate action shifts the level permanently, so the centred median lands
    # between the old and new levels and every point stays inside the band. The
    # filter removes noise without erasing Honeywell's separation.
    if len(sh) >= 7:
        local = sh["shares"].rolling(7, center=True, min_periods=3).median()
        ratio = sh["shares"] / local
        keep = ratio.between(0.62, 1.60) | local.isna()
        if keep.sum() >= 5:
            sh = sh[keep].reset_index(drop=True)

    out = _assemble(dates, ttm, sh, split_info)
    if out.empty:
        return out
    out.attrs["splits"] = {str(k.date()): (r, adj) for k, (r, adj) in split_info.items()}
    cut = stale_after_days(ttm)
    out["rev_stale"] = out["rev_age"] > cut
    out.attrs["stale_cut_days"] = cut

    # Decide ONCE, here, whether the stale days can be dropped, because the
    # audit runs before the statistics do and the two must not disagree about
    # what the median was built on.
    fresh = out[~out["rev_stale"]]
    span = (out["date"].max() - out["date"].min()).days / 30.44
    fresh_span = ((fresh["date"].max() - fresh["date"].min()).days / 30.44
                  if len(fresh) else 0.0)
    used_fresh = len(fresh) >= 200 and fresh_span >= MIN_MONTHS_FOR_STATS
    stale_months = int(round((1.0 - len(fresh) / max(len(out), 1)) * span))
    out.attrs["used_fresh"] = used_fresh
    out.attrs["stale_months_dropped"] = stale_months if used_fresh else 0
    out.attrs["stale_months_kept"] = stale_months if not used_fresh else 0
    return out


def stale_after_days(ttm: pd.DataFrame) -> float:
    """
    The age past which this filer's revenue figure should already have been
    superseded, derived from its own filing behaviour rather than a constant.

    A quarterly filer's trailing figure is at most one quarter old plus however
    long that filer takes to file. Both come from the data: the quarter is 92
    days and the lag is the median of (available - period_end) across the whole
    series. A month of slack absorbs a late filing without opening the door to a
    missing one, so an ordinary series never trips this and a stretch built on a
    year-old annual always does.

    This replaces the old fixed-gap "hole" test. That test could not tell a
    stretch where the data is genuinely absent from one where it is merely
    annual, because it only looked at the spacing of period ends; both look like
    a 365-day step. Age at the point of use is the thing that actually matters
    to the multiple, and it separates them without a tuned threshold.
    """
    if ttm is None or ttm.empty or "period_end" not in ttm:
        return 365.0
    lag = (pd.to_datetime(ttm["available"]) - pd.to_datetime(ttm["period_end"])).dt.days
    lag = lag[lag.between(0, 400)]
    typical = float(lag.median()) if len(lag) else 60.0
    return 92.0 + typical + 31.0


# The exchange ratios companies actually announce. A split is a board
# resolution -- "three new shares for every two held" -- so the factor comes
# from a short list. A spin-off factor is set by what the two pieces were worth
# on the day and lands anywhere: DuPont's were 1.487, 0.4725 and 2.39.
_FORWARD_SPLITS = (1.25, 4/3, 1.5, 5/3, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 50)
SPLIT_RATIOS = sorted({round(r, 6) for r in _FORWARD_SPLITS}
                      | {round(1 / r, 6) for r in _FORWARD_SPLITS}
                      | {round(1 / k, 6) for k in (12, 25, 100)})


# Every factor the classifier refused, so a run can prove the ratio test is
# not discarding real splits. Rejecting a genuine split corrupts the share
# series just as badly as applying a spin-off did, and the only difference is
# which direction the error runs.
REJECTED_RATIOS: list[tuple[str, str, float]] = []
CURRENT_TICKER = "?"


def _is_split_ratio(ratio: float, tol: float = 0.005) -> bool:
    """Is this factor one a company could have announced as a split?"""
    return ratio > 0 and any(abs(ratio / r - 1) <= tol for r in SPLIT_RATIOS)


# A split halves or doubles; an acquisition paid in stock lands on a modest
# multiple. Across the real filings these two never overlap. Everything that
# turned out to be issuance sat between 1.2x and 1.65x -- AMD buying Xilinx at
# 1.35x, Bunge buying Viterra at 1.49x, Aon buying Hewitt at 1.23x, Waters
# taking BD's biosciences arm at 1.65x -- and every genuine split needing
# recovery was 0.5x or smaller, or 2x or larger. The band between them is
# where a hand-written ratio list does its damage, so nothing there is trusted
# to the share count alone.
REVERSE_SPLIT_MAX = 0.55        # at least a one-for-two
FORWARD_SPLIT_MIN = 1.90        # at least a two-for-one


def splits_the_share_count_reveals(shares: list[tuple[date, float]],
                                   known: dict,
                                   prices: pd.Series | None = None,
                                   ttm: pd.DataFrame | None = None) -> dict:
    """Splits EDGAR reports and Yahoo has not published.

    Yahoo is slow with a fresh split and sometimes never files one at all:
    Honeywell's 1-for-2 reverse split is simply absent, and while it was
    missing Honeywell was the cheapest name on the screen. DuPont's 1-for-3 is
    filed as 0.4725, which is the Corteva distribution, not the split. The
    company's own cover-page count knows both.

    What the count cannot do alone is tell a split from an acquisition paid in
    stock -- read that way it produced 113 false splits in one run. Two things
    keep it honest: only ratios outside the range acquisitions occupy, and,
    for a forward split, a revenue line that does not move. A company that
    doubles its share count to buy something also books the revenue.

    The price cannot help here. Yahoo back-adjusts its price history even for
    splits it does not list -- Honeywell's price runs straight through its
    reverse split -- so the absence of a move proves nothing.
    """
    pts = sorted((pd.Timestamp(d), float(v)) for d, v in (shares or []) if v and v > 0)
    if len(pts) < 4:
        return {}

    def revenue_near(when):
        if ttm is None or ttm.empty:
            return None, None
        before = ttm[ttm["period_end"] <= when]
        after = ttm[ttm["period_end"] > when]
        if before.empty or after.empty:
            return None, None
        # iloc[-1] on the AFTER slice is today's revenue, not the reading just
        # after the event: it turned Monster's 2012 two-for-one into a "441%
        # rise" by comparing 2011 against 2026.
        return float(before["ttm"].iloc[-1]), float(after["ttm"].iloc[0])

    found = {}
    for i in range(1, len(pts)):
        (d0, v0), (d1, v1) = pts[i - 1], pts[i]
        if v0 <= 0 or (d1 - d0).days > 200:
            continue
        step = v1 / v0
        if not (step <= REVERSE_SPLIT_MAX or step >= FORWARD_SPLIT_MIN):
            continue
        if not _is_split_ratio(step, tol=0.02):
            continue
        if any(abs(pd.Timestamp(k) - d1).days <= 120 for k in known):
            continue                     # Yahoo already accounts for this one

        note = ""
        if step >= FORWARD_SPLIT_MIN:
            r0, r1 = revenue_near(d1)
            if r0 and r1 and r1 / r0 > 1.35:
                REJECTED_RATIOS.append(
                    (CURRENT_TICKER, str(d1.date()), float(step),
                     f"count {v0:,.0f} -> {v1:,.0f} but revenue rose "
                     f"{r1 / r0 - 1:.0%} at the same time, so this is a "
                     f"stock-funded acquisition, not a split"))
                continue
            note = " with revenue flat through it"

        found[d1] = (step, "raw")
        REJECTED_RATIOS.append(
            (CURRENT_TICKER, str(d1.date()), float(step),
             f"SPLIT ADDED from EDGAR: count {v0:,.0f} -> {v1:,.0f}{note}; "
             f"Yahoo has no record of it"))
    return found


def corroborate_splits(split_info: dict, shares: list[tuple[date, float]]) -> dict:
    """Decide each candidate factor on the company's own reported share count.

    The ratio list below is a fallback, not the primary test, and it must stay
    that way. When it ran first it refused Booking's 25-for-1 because 25 was
    not on the list, the historical count never got scaled, and Booking came
    out at a $6.8bn market cap against a real $171bn. Any hand-written list of
    "ratios companies use" is the same guess as the magnitude band it replaced;
    it is only safe where there is no evidence to consult.

    The evidence: in a split the company's own reported count changes, in a
    spin-off it does not. EDGAR cover pages are a different source from Yahoo's
    splits column, so when they agree that is corroboration rather than a
    second opinion from the same place.
    """
    if not split_info:
        return split_info
    pts = sorted((pd.Timestamp(d), v) for d, v in (shares or []) if v and v > 0)
    kept = {}
    for when, (ratio, rescaled) in split_info.items():
        w = pd.Timestamp(when)
        before = [v for d, v in pts
                  if w - pd.Timedelta(days=400) <= d <= w - pd.Timedelta(days=10)]
        after = [v for d, v in pts
                 if w + pd.Timedelta(days=10) <= d <= w + pd.Timedelta(days=400)]

        if len(before) >= 2 and len(after) >= 2:
            observed = float(np.median(after)) / float(np.median(before))
            if observed > 0:
                # The count has to actually AGREE with the factor, not merely
                # sit closer to it than to standing still. Honeywell's count
                # halved while Yahoo's factor said 0.9535 -- a spin-off factor,
                # not the 1-for-2 reverse split that really happened -- and
                # 0.50 is a hair closer to 0.9535 than to 1.0 in log terms, so
                # the loose test let it through. History was then scaled by
                # 0.9535 instead of 0.5, and Honeywell became the cheapest
                # name on the screen on the strength of it.
                if abs(math.log(observed / ratio)) < math.log(1.25):
                    kept[when] = (ratio, rescaled)
                else:
                    REJECTED_RATIOS.append(
                        (CURRENT_TICKER, str(w.date()), float(ratio),
                         f"reported share count went {observed:.2f}x, not {ratio:.2f}x"))
                continue

        # No usable evidence. Fall back to whether the factor looks like a
        # ratio a board would announce.
        if _is_split_ratio(float(ratio)):
            kept[when] = (ratio, rescaled)
        else:
            REJECTED_RATIOS.append(
                (CURRENT_TICKER, str(w.date()), float(ratio),
                 "no share counts either side, and not a ratio boards announce"))
    return kept


def classify_splits(prices: pd.Series, splits: pd.Series | None
                    ) -> dict[pd.Timestamp, tuple[float, bool]]:
    """
    For each split: its ratio, and whether the price history has been rescaled
    to account for it.

    Read off the data rather than assumed. Across a genuine 2-for-1 an
    unrescaled series halves and a rescaled one does not.
    """
    if splits is None or not len(splits) or prices.empty:
        return {}
    s = splits.copy()
    s.index = pd.to_datetime(s.index)
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)

    info = {}
    for when, ratio in s.items():
        if not ratio or ratio <= 0:
            continue
        # Yahoo files spin-off distribution adjustments in the same column as
        # splits. A band around 1.0 caught Honeywell's 0.9535 but nothing else:
        # DuPont's Dow, Corteva and Qnity separations arrive as 1.487, 0.4725
        # and 2.39, sail through, and get applied to the share count. That is
        # why DuPont published a 59.5% one-year share reduction and Honeywell
        # 50.1% -- 1/2.39 and 1/2.0 respectively, arithmetic from a price
        # adjustment, not buybacks.
        #
        # Magnitude cannot separate the two. Structure can: a stock split is
        # always announced as a whole-number ratio, so its factor is a simple
        # fraction. A distribution factor is whatever the two share prices
        # happened to be worth on the day, and lands on an arbitrary decimal.
        # No judgement here any more -- corroborate_splits() decides, and it
        # decides on the share count wherever the share count can speak.
        before = prices[prices.index < when].tail(3)
        after = prices[prices.index >= when].head(3)
        if len(before) < 2 or len(after) < 2:
            info[when] = (float(ratio), True)   # can't tell; assume rescaled
            continue
        observed = float(after.median() / before.median())
        if observed <= 0:
            info[when] = (float(ratio), True)
            continue
        rescaled = abs(math.log(observed)) < abs(math.log(observed * ratio))
        info[when] = (float(ratio), rescaled)
    return info


def cumulative_factor(dates: pd.DatetimeIndex,
                      split_info: dict[pd.Timestamp, tuple[float, bool]],
                      unrescaled_only: bool = False) -> pd.Series:
    """Product of split ratios falling after each date."""
    factor = pd.Series(1.0, index=range(len(dates)))
    for when, (ratio, rescaled) in split_info.items():
        if unrescaled_only and rescaled:
            continue
        factor[dates < when] *= ratio
    return factor


def _assemble(dates: pd.DataFrame, ttm: pd.DataFrame, sh: pd.DataFrame,
              split_info: dict) -> pd.DataFrame:
    """Join prices, point-in-time revenue and share counts into a P/S series."""
    rev = pd.merge_asof(dates, ttm[["available", "ttm"]],
                        left_on="date", right_on="available", direction="backward")
    shr = pd.merge_asof(dates, sh, left_on="date", right_on="asof", direction="backward")

    df = pd.DataFrame({
        "date": dates["date"].values,
        "price": dates["price"].values,
        "ttm": rev["ttm"].values,
        # How old the revenue figure being divided by was, on the day it is
        # being used. Under normal quarterly filing this cycles 0 -> ~130 days
        # and resets; where a year of quarters is missing it climbs past 365.
        # Carrying it here is what lets the statistics tell a stretch of COARSE
        # coverage apart from a stretch of CURRENT coverage, which was the whole
        # content of the old "hole" warning.
        "rev_asof": rev["available"].values,
        "shares": shr["shares"].values,
    }).dropna()
    df = df[(df["ttm"] > 0) & (df["shares"] > 0)]
    if df.empty:
        return df

    # ...then stepped back out of any split the price series has not yet been
    # rescaled for, so shares and price are quoted on the same basis.
    df["shares"] = df["shares"] / cumulative_factor(
        pd.DatetimeIndex(df["date"]), split_info, unrescaled_only=True).values

    df["mktcap"] = df["price"] * df["shares"]
    df["ps"] = df["mktcap"] / df["ttm"]
    df["rev_age"] = (df["date"] - df["rev_asof"]).dt.days
    return df[np.isfinite(df["ps"]) & (df["ps"] > 0)].reset_index(drop=True)




def pct_off_52w_high(prices: pd.Series) -> float | None:
    """How far below its own 52-week peak the stock closed, as a negative percent."""
    if prices is None or prices.empty:
        return None
    px = prices.dropna()
    idx = pd.to_datetime(px.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    px.index = idx
    window = px[px.index >= px.index.max() - pd.Timedelta(days=365)]
    if len(window) < 30 or window.max() <= 0:
        return None
    return float((window.iloc[-1] / window.max() - 1) * 100)


def cross_check(tickers: list[str], delay: float = 0.20, *,
                refresh: bool = False,
                info_max_age_hours: int = 20) -> dict[str, dict]:
    """
    Verify today's figures against Yahoo's own reported revenue and share count.

    EDGAR and Yahoo derive these independently, so agreement is real evidence and
    disagreement is a genuine warning. This is the only automatic check available:
    nobody publishes historical multiples through an API, so it validates today's
    P/S but says nothing about whether the historical median is right.

    Deliberately not run on all 500 -- it costs one request per ticker and the
    rows worth verifying are the ones you would act on.
    """
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    # This used to be one request at a time, in order, for every ticker, with up
    # to three attempts and a growing sleep between them. On 459 names that was
    # most of the run. Two changes: ask for several at once, and keep what comes
    # back so a second run the same day asks for nothing at all. Debugging a
    # split classifier should not cost a Yahoo round trip per company.
    CACHE.mkdir(parents=True, exist_ok=True)
    store = CACHE / "yinfo.json"
    fresh_for = timedelta(hours=info_max_age_hours)

    saved = {}
    if store.exists() and not refresh:
        if datetime.now() - datetime.fromtimestamp(store.stat().st_mtime) < fresh_for:
            try:
                saved = json.loads(store.read_text())
            except (ValueError, OSError):
                saved = {}

    def cached(t):
        return saved.get(t)

    def fetch(t):
        got = cached(t)
        if got is not None:
            return t, got, True
        info = {}
        for attempt in range(3):
            try:
                info = yf.Ticker(t).info or {}
                if info:
                    break
            except Exception:
                pass
            time.sleep(1.5 * (attempt + 1))   # Yahoo throttles; back off and retry
        return t, info, False

    out: dict[str, dict] = {}
    done = hits = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, tickers))
    for t, info, was_cached in results:
        if info and not was_cached:
            saved[t] = {k: v for k, v in info.items()
                        if isinstance(v, (str, int, float, bool, type(None)))}
    try:
        store.write_text(json.dumps(saved))        # one write for the lot
    except OSError:
        pass
    print(f"  {len(results)} checked, {sum(1 for _, _, c in results if c)} "
          f"reused from cache")
    for t, info, was_cached in results:
        done += 1
        hits += was_cached
        if not info:
            continue
        rev = info.get("totalRevenue")
        shares = info.get("sharesOutstanding")
        if rev or shares:
            out[t] = {"y_revenue": rev, "y_shares": shares,
                      "y_mktcap": info.get("marketCap"),
                      "y_ps": info.get("priceToSalesTrailing12Months"),
                      # Free: the .info call is already being made for the
                      # cross-check, so these cost nothing extra.
                      "y_target": info.get("targetMeanPrice"),
                      "y_target_n": info.get("numberOfAnalystOpinions"),
                      "y_div_yield": info.get("dividendYield"),
                      # The rate is dollars per share per year, which has no
                      # unit ambiguity. Yahoo's dividendYield field has changed
                      # convention before -- it used to be a fraction (0.0047)
                      # and now returns a percentage (0.47) -- so the yield is
                      # computed from the rate wherever the rate exists.
                      "y_div_rate": (info.get("dividendRate")
                                     or info.get("trailingAnnualDividendRate")),
                      "y_price": (info.get("currentPrice")
                                  or info.get("regularMarketPrice")),
                      "y_forward_pe": info.get("forwardPE"),
                      # What the company actually does. Same free .info call,
                      # no extra request. Trimmed to the first few sentences
                      # because the full text runs to a page and the panel is
                      # meant to save a trip elsewhere, not become one.
                      "y_summary": info.get("longBusinessSummary"),
                      "y_industry": info.get("industry")}
        time.sleep(delay)
    return out


def apply_cross_check(summary: pd.DataFrame, series: dict[str, pd.DataFrame],
                      checks: dict[str, dict]) -> pd.DataFrame:
    """Attach agreement percentages and a plain verdict to each row."""
    rev_diff, sh_diff, verdict = [], [], []

    for _, row in summary.iterrows():
        t = row["ticker"]
        c, hist = checks.get(t), series.get(t)
        if not c or hist is None or hist.empty:
            rev_diff.append(None); sh_diff.append(None); verdict.append("unchecked")
            continue

        mine_rev = float(hist["ttm"].iloc[-1])
        mine_cap = float(hist["mktcap"].iloc[-1])
        rd = (mine_rev / c["y_revenue"] - 1) * 100 if c.get("y_revenue") else None
        # Compare market cap rather than share count: for a dual-class company
        # Yahoo's sharesOutstanding covers only the listed class while its
        # marketCap counts every class, so the two disagree with each other.
        # Nike tripped that and looked 23% wrong when it was right.
        sd = (mine_cap / c["y_mktcap"] - 1) * 100 if c.get("y_mktcap") else None
        rev_diff.append(rd); sh_diff.append(sd)

        problems = []
        if rd is not None and abs(rd) > 15:
            problems.append(f"revenue differs from Yahoo by {rd:+.0f}%")
        if sd is not None and abs(sd) > 12:
            problems.append(f"market cap differs from Yahoo by {sd:+.0f}%")
        verdict.append("; ".join(problems) if problems else "agrees")

    summary["xc_revenue_diff"] = rev_diff
    summary["xc_mktcap_diff"] = sh_diff
    summary["xc_verdict"] = verdict
    for field, col in [("y_target", "target_price"), ("y_target_n", "target_analysts"),
                       ("y_forward_pe", "forward_pe"), ("y_industry", "industry")]:
        summary[col] = [checks.get(t, {}).get(field) for t in summary["ticker"]]

    # The business summary, cut to whole sentences. Yahoo's runs to a page and
    # the point of the panel is to save a trip elsewhere, not become one: the
    # first two or three sentences say what the company sells and to whom,
    # which is the question being asked.
    def _blurb(text):
        if not isinstance(text, str) or not text.strip():
            return None
        parts, out, n = re.split(r"(?<=\.)\s+", text.strip()), [], 0
        for s in parts:
            if out and n + len(s) > 420:
                break
            out.append(s)
            n += len(s)
            if len(out) >= 3:
                break
        return " ".join(out)

    summary["description"] = [_blurb(checks.get(t, {}).get("y_summary"))
                              for t in summary["ticker"]]
    # Trailing P/E is withheld when earnings are negative, because a negative
    # multiple means nothing. Forward P/E was passed through from Yahoo with no
    # such test, so it published -144.2 for Healthpeak and -66.7 for Alexandria
    # and the drawer read them as cheap.
    summary["forward_pe"] = [v if v is not None and v > 0 else None
                             for v in summary["forward_pe"]]

    # Dividend yield, always as a percentage.
    #
    # This was being fixed at render time by guessing: multiply by 100 if the
    # value is below 1, leave it alone otherwise. That guess is wrong for every
    # genuinely low payer, because 0.47 is a perfectly good percentage as well
    # as a perfectly good fraction -- Nvidia was published at 47% and Apple at
    # 35%. A single number cannot tell you its own units.
    #
    # Two ways out, neither of which inspects the magnitude of the value being
    # converted. Preferred: divide the annual dollar rate by the price, which
    # is unambiguous. Failing that, settle the convention ONCE from the whole
    # population -- across 400+ payers the median yield is a couple of percent,
    # so a median near 0.02 means Yahoo is sending fractions and a median near
    # 2 means percentages. One reading per run, applied uniformly.
    raw = [checks.get(t, {}).get("y_div_yield") for t in summary["ticker"]]
    payers = [float(v) for v in raw if v is not None and float(v) > 0]
    scale = 100.0 if payers and statistics.median(payers) < 0.25 else 1.0

    yields = []
    for t, v in zip(summary["ticker"], raw):
        c = checks.get(t, {})
        rate, px = c.get("y_div_rate"), c.get("y_price")
        try:
            if rate and px and float(px) > 0:
                yields.append(round(float(rate) / float(px) * 100, 2))
                continue
        except (TypeError, ValueError):
            pass
        yields.append(round(float(v) * scale, 2) if v is not None else None)
    summary["dividend_yield"] = yields
    # Upside needs a price column, which not every caller supplies. Reaching for
    # it unconditionally broke the cross-check on any frame without one.
    prices = summary["price"] if "price" in summary.columns else [None] * len(summary)
    summary["target_upside"] = [
        round((tp / px - 1) * 100, 1) if tp and px and px > 0 else None
        for tp, px in zip(summary["target_price"], prices)]
    return summary


def detect_corporate_actions(hist: pd.DataFrame, split_info: dict,
                             threshold: float = 0.18,
                             window: int = 63) -> list[tuple[pd.Timestamp, float]]:
    """
    Share count moves that no split explains -- spin-offs, breakups, big
    all-stock acquisitions.

    These matter more than they look. When Honeywell separated, its share count
    halved, so market cap immediately reflected the smaller company while
    trailing revenue still counted the divested businesses for another four
    quarters. Both halves of the fraction are wrong in opposite directions, and
    the result is a large fake discount that sorts straight to the top. Buybacks
    and issuance are gradual and stay well under the threshold; a restructuring
    does not.
    """
    if len(hist) < 4:
        return []
    shares = hist["shares"].to_numpy(dtype=float)
    dates = pd.DatetimeIndex(hist["date"])
    split_dates = [pd.Timestamp(k) for k in split_info]

    events, cancelled = [], []
    # Full three-month windows on BOTH sides. A truncated window averages a
    # split part-way and then fails the "is this explained by a split" test --
    # that is how Monster's 2-for-1 got reported as a 50% restructuring. The
    # cost is that a restructuring in the last two months is invisible, which is
    # the right trade: a false alarm on a split hides a good company, while a
    # two-month delay on a genuine breakup costs almost nothing.
    step = max(1, window // 3)
    for i in range(2, len(shares) - 1, step):
        # Short of a full window on the left, take the earliest count outright
        # rather than a partial median -- the same reasoning as at the tail. Dell
        # acquiring EMC and Alcoa separating both sit inside the first sixty
        # points of their series, so the old range() never examined them.
        before = (np.median(shares[i - window:i]) if i >= window
                  else float(shares[0]))
        # Past the end of a full window, take the latest count outright rather
        # than the median of a partial one. A half-covered window averages a
        # split part-way -- 1.5x for a 2-for-1 -- which then fails the "is this
        # a split?" test and reports Monster's split as a 50% restructuring.
        after = (np.median(shares[i:i + window]) if i + window <= len(shares)
                 else float(shares[-1]))
        if before <= 0:
            continue
        change = after / before - 1
        if abs(change) < threshold:
            continue
        # Only skip if a split in this window can actually account for a move of
        # this size -- not merely because some split happened nearby.
        # The forward-looking median flags the change up to a quarter early, so
        # look for an explaining split anywhere in the comparison window.
        explained = False
        for sd, (ratio, _) in split_info.items():
            if abs((pd.Timestamp(sd) - dates[i]).days) <= 100:
                if abs(math.log((1 + change) / ratio)) < 0.15:
                    explained = True
        if explained:
            continue
        if events and (dates[i] - events[-1][0]).days < 250:
            continue      # same event, already recorded
        # A move that reverses itself shortly after is a data artifact, not two
        # corporate events. Nike, McCormick and A.O. Smith each showed +100%
        # followed by -50% around a split, which is one bad value, not a
        # doubling and a halving.
        if any(abs((dates[i] - w).days) <= 250 for w in cancelled):
            continue      # already cancelled against its own mirror image
        if events and abs(math.log((1 + change) * (1 + events[-1][1]))) < 0.20:
            # The sliding window detects the same reversal several times over, so
            # record where it happened: without that, the first detection cancels
            # the spike and the next one puts it straight back.
            cancelled.append(dates[i])
            events.pop()
            continue
        # Pin the event to the day the count actually steps, not to where the
        # forward-looking median first notices it -- otherwise the audit's
        # excuse window looks in the wrong place and re-flags what we just found.
        seg = shares[max(0, i - window):min(len(shares), i + window)]
        offset = int(np.argmax(np.abs(np.diff(np.log(np.maximum(seg, 1)))))) + 1
        when = dates[max(0, i - window) + offset]
        events.append((when, float(change)))
    return events


def audit_series(hist: pd.DataFrame, ttm: pd.DataFrame, split_info: dict,
                 actions: list) -> list[str]:
    """
    Structural checks on a company's whole history, needing no outside source.

    The medians cannot be validated against anyone -- nobody publishes historical
    multiples through an API. But every median error found so far announced
    itself as a break in the underlying series, so the series is what gets
    checked. Monster was a step in market cap; Nvidia was a frozen revenue
    denominator. Both are visible from the inside.
    """
    issues: list[str] = []
    if hist.empty or len(hist) < 60:
        return issues

    df = hist.sort_values("date").reset_index(drop=True)
    dates = pd.DatetimeIndex(df["date"])

    # -- 1. The SHARE count must be continuous. Auditing market cap was wrong:
    #       market cap is price times shares, so every violent but genuine price
    #       move tripped the alarm -- AMD's 52% earnings jump in 2016, Netflix's
    #       54% collapse in 2022. Neither is a data fault. A break in scale can
    #       only come from the share count, which moves in slow steps at filing
    #       dates and never leaps except at a split or a corporate action.
    caps = df["shares"].to_numpy(dtype=float)
    steps = np.abs(np.diff(np.log(np.maximum(caps, 1))))
    excused = [pd.Timestamp(k) for k in split_info] + [a[0] for a in actions]
    for i in np.where(steps > 0.22)[0]:
        when = dates[i + 1]
        # Share counts update on filing dates, and during a merger they move
        # over two reporting periods -- Raytheon's event was detected in May 2020
        # and the resulting step landed in July, 83 days later. The window has to
        # span both.
        if any(abs((when - e).days) <= 160 for e in excused):
            continue
        issues.append(f"share count jumps {np.expm1(steps[i]) * 100:+.0f}% in one day on "
                      f"{when.date()} with no split or corporate action to explain it")
        break

    # -- 2. Revenue steps at each filing, which is normal, but a step this large
    #       means the series switched between differently-scoped tags.
    if len(ttm) > 8 and "tag" in ttm.columns:
        rev = ttm["ttm"].to_numpy(dtype=float)
        tags = ttm["tag"].to_numpy()
        rsteps = np.abs(np.diff(np.log(np.maximum(rev, 1))))
        # Judge each step against how much THIS company's revenue normally moves
        # between filings. A fixed threshold cannot work: concept changes cluster
        # in 2017-2019, exactly when Nvidia and Tesla were compounding 50-60% a
        # year, so a 64% step is unremarkable for them and damning for Alphabet,
        # which grew 10%. Comparing a company only against itself separates the
        # two. The median is taken over steps away from any concept change so a
        # splice cannot inflate its own baseline.
        spans = np.diff(ttm["period_end"].to_numpy()).astype("timedelta64[D]").astype(int)
        clean_steps = np.array([rsteps[i] for i in range(len(rsteps))
                                if tags[i] == tags[i + 1] and spans[i] <= 130])
        # Too few clean steps to judge a revenue splice. That used to `return`,
        # which quietly skipped every check after this one -- the share-base
        # test among them, which is why Honeywell and Waters came through with
        # an empty audit column. Skip THIS check, not the rest of the function.
        skip_revenue_step_test = len(clean_steps) < 4
        typical = max(float(np.median(clean_steps)), 0.02) if not skip_revenue_step_test else 0.02

        gaps = np.diff(ttm["period_end"].to_numpy()).astype("timedelta64[D]").astype(int)
        for i in ([] if skip_revenue_step_test else np.where(rsteps > 0.45)[0]):
            if gaps[i] > 130:
                continue    # not a step at all -- see the coverage check below
            if tags[i] == tags[i + 1]:
                continue
            if rsteps[i] < typical * 4:
                continue    # large, but ordinary for this company
            # A recovery returns to a level the company has already occupied; a
            # splice invents one it has never been at. The cruise lines went to
            # near-zero revenue in 2020 and back in 2021, which is real however
            # violent it looks. Comparing against the company's own history
            # separates the two without any threshold to tune.
            after = float(rev[i + 1])
            earlier = rev[:max(i, 1)]
            if len(earlier) and 0.75 <= after / float(np.max(earlier)) <= 1.35:
                continue
            # rsteps is an absolute value, so both the sign and the size have to
            # come back from the raw series. Using expm1 on the absolute log
            # step turned a 68% collapse into "falls 209%" -- that is the size
            # of the rise that would undo it, not the fall.
            change = float(rev[i + 1]) / float(rev[i]) - 1.0 if rev[i] else 0.0
            direction = "jumps" if change >= 0 else "falls"
            issues.append(f"trailing revenue {direction} {abs(change) * 100:.0f}% around "
                          f"{ttm['period_end'].iloc[i + 1].date()}, {rsteps[i] / typical:.0f}x "
                          f"this company's usual move between filings, at the exact point the "
                          f"source concept changes from {tags[i]} to {tags[i + 1]}")
            break

    # -- 3. Revenue that stopped updating. This is what made Nvidia read 476x.
    if len(ttm):
        stale_days = (pd.Timestamp(date.today()) - ttm["available"].max()).days
        if stale_days > 200:
            issues.append(f"newest revenue figure was filed {stale_days} days ago, so the "
                          f"denominator is stale")

    # -- 3b. Stretches valued on a revenue figure that was already out of date.
    #        Alphabet was missing five consecutive quarters, so it was valued on
    #        2023 revenue through all of 2024 -- revenue too low, P/S too high,
    #        median inflated.
    #
    #        This used to be measured as a GAP between period ends of more than
    #        200 days, which was the wrong quantity and fired 53 times a run. A
    #        365-day step between two period ends means only that coverage there
    #        is annual rather than quarterly; the annual figure is real, audited
    #        and correctly dated. In 36 of those 53 the step was not even inside
    #        the series -- it was the reported-annual backfill reaching one year
    #        further back than the quarters go, so the "hole" was the space
    #        before the series started, where nothing is being carried across
    #        because there is nothing earlier to carry.
    #
    #        What actually damages the median is a day whose denominator was
    #        stale AT THE MOMENT IT WAS USED, and summarize() now drops those
    #        days outright. So there is only something to report when they could
    #        not be dropped -- when removing them would leave too little history
    #        to measure against.
    kept = hist.attrs.get("stale_months_kept", 0)
    if kept:
        worst = int(hist["rev_age"].max()) if "rev_age" in hist else 0
        issues.append(f"{kept} of the months in the window are valued on a revenue figure "
                      f"that was already up to {worst} days old, and there is not enough "
                      f"history left to measure without them, so the median rests on them")

    # -- 4. Quarters that persistently disagree with the filer's own annual
    #       totals. A level error produces no step, so nothing else here can see
    #       it: L3Harris read 80% high for several runs while passing every
    #       other check. Occasional mismatches are normal (a 53-week year, a
    #       restatement); most of them disagreeing means the quarters are wrong.
    checks = ttm.attrs.get("annual_checks", 0)
    bad = ttm.attrs.get("annual_mismatches", 0)
    if checks >= 4 and bad / checks > 0.5:
        issues.append(f"the derived quarters disagreed with the company's own reported annual "
                      f"total at {bad} of {checks} year ends; the reported figures have been "
                      f"used instead, but the quarterly series is unreliable")

    # -- 5. Revenue that has not moved across several consecutive filings. A real
    #       business almost never posts identical trailing revenue four quarters
    #       running; a carried-forward stale value does exactly that. Being at an
    #       all-time-high multiple is NOT itself evidence -- plenty of companies
    #       legitimately are -- so the denominator is tested directly.
    if len(ttm) >= 5:
        recent = ttm["ttm"].tail(5).to_numpy(dtype=float)
        # Exactly identical, not merely close. A carried-forward value repeats
        # the same number; Nike's revenue genuinely moves about 0.4% between
        # filings and was being flagged as stale for it.
        if recent.min() > 0 and (recent.max() / recent.min() - 1) < 1e-6:
            issues.append("trailing revenue is identical across the last five filings, "
                          "which means the figure is being carried forward rather than "
                          "updated")

    # -- 6. The absolute LEVEL of the result, which nothing above looks at.
    #       Every other check here looks for change: a jump, a hole, a step, a
    #       frozen value. A series that is wrong from its very first observation
    #       never changes, so it trips none of them. Berkshire came through at a
    #       $0.8bn market cap and Erie at $0.001bn with an entirely empty audit
    #       column, and both were still ranked with a Z-score and a percentile --
    #       Erie as the 78th cheapest name of 462. Only the Yahoo cross-check
    #       caught them, and that has no view of the history at all.
    if len(df) and len(ttm):
        try:
            cap_now = float(df["mktcap"].iloc[-1])
            ps_now = float(df["ps"].iloc[-1])
        except (KeyError, ValueError, TypeError, IndexError):
            cap_now = ps_now = float("nan")
        rev_now = float(ttm["ttm"].iloc[-1])
        if not np.isfinite(cap_now) or cap_now <= 0:
            issues.append("market cap does not compute at all, so every multiple "
                          "on this row is meaningless")
        elif cap_now < 2e9:
            issues.append(f"market cap comes out at ${cap_now / 1e9:,.2f}bn, far below "
                          f"anything an index constituent can be; the share count is "
                          f"wrong, most often a filer with several share classes where "
                          f"only one was collected")
        if not np.isfinite(rev_now) or rev_now <= 0:
            issues.append("revenue does not compute at all, so every multiple on "
                          "this row is meaningless")
        elif rev_now < 1e8:
            issues.append(f"revenue comes out at ${rev_now / 1e6:,.0f}m, too small for "
                          f"an index constituent; the chosen concept is a fragment of "
                          f"total revenue rather than the total")
        elif np.isfinite(ps_now) and ps_now > 200:
            issues.append(f"a P/S of {ps_now:,.0f} is not a valuation, it is "
                          f"a broken denominator")

    # -- 7. A share base today's multiple cannot be compared against.
    #       Check 1 excuses a jump whenever a corporate action sits near it,
    #       which is circular: detect_corporate_actions calls any large move an
    #       action, so the audit then reports nothing. Honeywell's count halves
    #       across the window, Monster's doubles and Waters' rises two thirds,
    #       and all three came through with an empty audit column -- Honeywell
    #       as the second cheapest name on the screen, on a ten-year median
    #       built from a share base twice today's.
    #
    #       Whether the cause is a split we missed, a spin-off or a genuine
    #       restructuring changes nothing here. If the share base moved this
    #       far, the old multiples are measuring a different company.
    if len(df) >= 14:
        sh = shares_in_todays_units(df, split_info).to_numpy(dtype=float)
        if sh[0] > 0 and sh[-1] > 0:
            # Measured exactly the way dilution_1y is, so the audit and the
            # research drawer can never disagree about the same company.
            # Index into the RESTATED array, not back into the raw column --
            # taking the year-ago figure from df["shares"] undid the whole
            # point of restating it.
            older = np.flatnonzero(
                np.asarray(dates <= dates[-1] - pd.DateOffset(years=1)))
            year_ago = float(sh[older[-1]]) if len(older) else float(sh[0])
            over_window = sh[-1] / sh[0] - 1
            over_year = sh[-1] / year_ago - 1 if year_ago > 0 else 0.0
            steps7 = np.diff(np.log(np.maximum(sh, 1)))
            worst, which = 0.0, None
            if len(steps7):
                j = int(np.argmax(np.abs(steps7)))
                worst, which = float(steps7[j]), dates[j + 1]
            # Gradual drift is not a problem: the multiple already uses the
            # share count of the day, so a decade of buybacks compares fine.
            # What breaks comparability is a STEP -- the company became a
            # different company. Measuring cumulative change instead flagged 90
            # of 459 rows for ordinary buybacks and buried the real ones.
            if abs(over_year) > 0.25 and which is not None:
                issues.append(
                    f"the share count is {abs(over_year):.0%} "
                    f"{'lower' if over_year < 0 else 'higher'} than a year ago, "
                    f"on {which.date()} -- the multiples before that point are "
                    f"built on a different share base, so the median is not a "
                    f"like-for-like comparison with today")
            # 22% caught 80 of 459 -- quarterly cover-page counts are lumpy
            # and a fifth is within normal range for an acquisitive large cap.
            # A third is the point where the company genuinely changed.
            # (worst is a LOG step, so compare it as a percentage.)
            elif abs(np.expm1(worst)) > 0.30 and which is not None:
                issues.append(
                    f"the share count moved {np.expm1(worst):+.0%} in one step on "
                    f"{which.date()}, which no split explains -- the multiples "
                    f"either side of it are measuring different companies")

    return issues


# Findings that mean the row cannot be trusted at all, as opposed to ones that
# merely qualify it. A row that survives with a fabricated percentile is worse
# than a missing row: it competes for attention against real candidates.
FATAL_AUDIT_MARKS = (
    "does not compute at all",
    "far below anything an index constituent",
    "too small for an index constituent",
    "broken denominator",
)


def audit_is_fatal(issues) -> bool:
    return any(any(m in i for m in FATAL_AUDIT_MARKS) for i in issues or [])



# ---------------------------------------------------------------------------
# Research module -- the second pass, run only on names the screen surfaced
# ---------------------------------------------------------------------------

def _latest_instant(facts: dict, tags: list[str], taxonomy: str = "us-gaap"
                    ) -> tuple[date, float] | None:
    """Most recent balance-sheet value, or None."""
    pts = collect_instants(facts, taxonomy, tags)
    return pts[-1] if pts else None


def _instant_at(facts: dict, tags: list[str], years_ago: float) -> float | None:
    """The balance-sheet value as of roughly N years back, for a trend."""
    pts = collect_instants(facts, "us-gaap", tags)
    if not pts:
        return None
    target = date.today() - timedelta(days=int(365.25 * years_ago))
    older = [(d0, v) for d0, v in pts if d0 <= target]
    return older[-1][1] if older else None


def _ttm(facts: dict, tags: list[str]) -> pd.DataFrame:
    """Trailing-twelve series for a flow item, reconciled against annuals."""
    # screen_annuals=False as well as smooth=False. Discarding a year whose
    # profit falls far below the median is defensible for revenue and wrong for
    # earnings: impairments, restructurings and outright losses are ordinary.
    # General Mills' real -$88m fiscal 2026, caused by a $1.75bn goodwill
    # writedown, was being thrown out as implausible. Suppressing a company's
    # worst years is a direct corruption of a tool built on medians.
    periods = collect_periods(facts, "us-gaap", tags, screen_annuals="upper_only")
    if not periods:
        return pd.DataFrame()
    return trailing_twelve(derive_quarters(periods, smooth=False), periods)


def _cagr(series: pd.DataFrame, years: int) -> float | None:
    """Annualised growth of a trailing-twelve series over N years."""
    if series.empty or len(series) < 2:
        return None
    latest = series.iloc[-1]
    target = latest["period_end"] - pd.DateOffset(years=years)
    older = series[series["period_end"] <= target]
    if older.empty:
        return None
    start, end = float(older.iloc[-1]["ttm"]), float(latest["ttm"])
    span = (latest["period_end"] - older.iloc[-1]["period_end"]).days / 365.25
    # A sign change makes a growth rate meaningless -- a company going from a
    # loss to a profit has not "grown 250%", and reporting that would be worse
    # than reporting nothing.
    if span < 0.5 or start <= 0 or end <= 0:
        return None
    # Nor has one that started from almost nothing. Insulet came out at 252% a
    # year and Salesforce at 208% purely because their profit three years ago
    # rounded to nothing. Both are true and both are noise; the UI says
    # "from near zero" instead, which is the fact that actually matters.
    #
    # But "near zero" has to mean actually near zero, not merely small RELATIVE
    # to an enormous present. NVIDIA three years ago had ~$28.6bn of trailing
    # revenue and ~$4.8bn of profit -- 9% and 7% of today's, so the pure ratio
    # test suppressed both, yet neither is remotely near zero. The base being a
    # tiny FRACTION of today is the signature of explosive real growth, which is
    # exactly what this column exists to show. So the relative floor only
    # applies when the starting value is also small in absolute terms; a base
    # above the threshold is a real number and its CAGR is reported however
    # large. $0.5bn clears Insulet/Salesforce's rounding-to-nothing starts while
    # admitting NVIDIA's multi-billion base.
    NEAR_ZERO_ABS = 0.5e9
    if start < 0.10 * end and start < NEAR_ZERO_ABS:
        return None
    return float(((end / start) ** (1 / span) - 1) * 100)


def research(ticker: str, facts: dict, hist: pd.DataFrame,
             ttm_rev: pd.DataFrame, mktcap_b: float | None = None,
             sector: str = "") -> dict:
    """
    The second pass: balance-sheet strength, growth quality, and dilution.

    Deliberately separate from the screen. The screen answers "is this unusually
    cheap against its own past"; none of these figures belong in that judgement,
    and folding them in would invite exactly the composite score that was
    rejected earlier. This runs on names the screen has already surfaced, and it
    only describes -- every number here is a fact from a filing, not a verdict.
    """
    out: dict = {"ticker": ticker}
    # For a bank "revenue" is net interest income plus fees, filed under
    # concepts the standard merge does not pick cleanly -- Huntington's Revenues
    # tag reads $1.7B against a true ~$9B -- and for a landlord it is rent. So
    # any figure whose denominator is revenue (gross margin, net margin, revenue
    # growth, net-debt-to-sales) is unreliable for these two sectors and is
    # suppressed rather than shown wrong. Everything built on PROFIT, EQUITY or
    # the BALANCE SHEET -- P/E, ROE, net income, debt, dilution, free cash flow,
    # profit growth -- is perfectly valid for a bank or REIT and is kept. This
    # is what un-blanks the panel these sectors used to show empty, while not
    # publishing the revenue-based numbers that made the blanking seem sensible.
    revenue_unreliable = sector in EXCLUDED_SECTORS

    ca = _latest_instant(facts, CURRENT_ASSETS_TAGS)
    cl = _latest_instant(facts, CURRENT_LIAB_TAGS)
    if ca and cl and cl[1] > 0:
        out["current_ratio"] = round(ca[1] / cl[1], 2)
        old_ca = _instant_at(facts, CURRENT_ASSETS_TAGS, 3)
        old_cl = _instant_at(facts, CURRENT_LIAB_TAGS, 3)
        if old_ca and old_cl and old_cl > 0:
            out["current_ratio_3y"] = round(old_ca / old_cl, 2)

    ld = _latest_instant(facts, LONG_DEBT_TAGS)
    sd = _latest_instant(facts, SHORT_DEBT_TAGS)
    debt = (ld[1] if ld else 0.0) + (sd[1] if sd else 0.0)
    cash = _latest_instant(facts, CASH_TAGS)
    inv = _latest_instant(facts, SHORT_INVEST_TAGS)
    liquid = (cash[1] if cash else 0.0) + (inv[1] if inv else 0.0)
    if ld or sd:
        out["debt_b"] = round(debt / 1e9, 2)
        out["net_debt_b"] = round((debt - liquid) / 1e9, 2)
        eq = _latest_instant(facts, EQUITY_TAGS)
        # Positive-but-tiny equity is as unusable as negative equity, and the
        # bare > 0 test let it straight through: Mettler-Toledo published a
        # return on equity of 7,096% and Masco 3,111%, both of which say only
        # that years of buybacks have left almost no book value to divide by.
        # The same near-zero guard already protects the growth rates and the
        # dilution figures; it was never carried across to here.
        thin = bool(eq and 0 < eq[1] < 0.05 * (debt + eq[1]))
        if thin:
            out["thin_equity"] = True
        if eq and eq[1] > 0 and not thin:
            out["debt_to_equity"] = round(debt / eq[1], 2)
        elif eq and eq[1] <= 0:
            # Roughly one company in ten has negative book equity, almost always
            # from years of buybacks. Leaving the field blank looked like missing
            # data; it is a fact about the balance sheet and worth saying.
            out["negative_equity"] = True
        if (not revenue_unreliable and not ttm_rev.empty
                and float(ttm_rev["ttm"].iloc[-1]) > 0):
            out["net_debt_to_sales"] = round((debt - liquid) / float(ttm_rev["ttm"].iloc[-1]), 2)

    gp = _ttm(facts, GROSS_PROFIT_TAGS) if not revenue_unreliable else pd.DataFrame()
    gp_val = None
    if revenue_unreliable:
        pass  # gross margin needs a cost of goods sold these sectors do not file
    elif gp.empty:
        cost = _ttm(facts, COST_TAGS)
        if not cost.empty and not ttm_rev.empty:
            gp_val = float(ttm_rev["ttm"].iloc[-1]) - float(cost["ttm"].iloc[-1])
        else:
            gp_val = None
    else:
        gp_val = float(gp["ttm"].iloc[-1])
    if gp_val is not None and not ttm_rev.empty and float(ttm_rev["ttm"].iloc[-1]) > 0:
        gm = gp_val / float(ttm_rev["ttm"].iloc[-1]) * 100
        # A gross margin at or near 100% is not a margin, it is a missing cost
        # of sales: CenterPoint came out at exactly 100.0 and Echo, a freight
        # broker that buys most of its revenue in, at 98.7. Utilities and
        # brokers often do not file the standard cost tags, and the subtraction
        # then returns the whole of revenue. Publishing it invites exactly the
        # wrong conclusion, since the drawer reads a high margin as strength.
        if -5 < gm < 96:
            out["gross_margin_pct"] = round(gm, 1)
        elif gm >= 96:
            out["no_cost_of_sales"] = True

    ni = _ttm(facts, NET_INCOME_TAGS)
    if not ni.empty:
        latest_ni = float(ni["ttm"].iloc[-1])
        rev_now = float(ttm_rev["ttm"].iloc[-1]) if not ttm_rev.empty else 0.0
        # A trailing-twelve total and the last reported full year overlap by
        # three quarters, so they cannot diverge by much -- one quarter rolls
        # off and one rolls on. Where they do, the quarters are wrong: Salesforce
        # came out at $2.6bn against a filed $7.4bn, about one quarter's profit
        # where there should be four. This is the general form of a fault that
        # would otherwise need chasing one company at a time.
        reported_years = [q for q in collect_periods(facts, "us-gaap", NET_INCOME_TAGS,
                                                     screen_annuals="upper_only")
                          if ANNUAL_DAYS[0] <= q.days <= ANNUAL_DAYS[1]]
        if reported_years:
            newest = max(reported_years, key=lambda q: q.end)
            gap_days = (ni["period_end"].iloc[-1].date() - newest.end).days
            if 0 <= gap_days <= 200 and abs(newest.val) > 1e8:
                drift = abs(latest_ni - newest.val) / abs(newest.val)
                if drift > 0.6:
                    out["net_income_unusable"] = (
                        f"trailing profit of ${latest_ni/1e9:,.2f}B against ${newest.val/1e9:,.2f}B "
                        f"in the year to {newest.end}, only {gap_days} days earlier")
                    # The filed year is audited and the quarters are not, so fall
                    # back to it rather than showing nothing. Alphabet's 2025
                    # quarters sum to its annual to the dollar while its 2026
                    # ones are plainly inflated; a slightly stale correct figure
                    # beats a blank, as long as its date is on the label.
                    out["net_income_b"] = round(newest.val / 1e9, 2)
                    out["net_income_asof"] = str(newest.end)
                    if rev_now > 0 and not revenue_unreliable:
                        out["net_margin"] = round(newest.val / rev_now * 100, 1)
                        out["net_margin_stale"] = True
        # A company cannot earn more than it sells. Outside banking and property
        # -- where "revenue" is a different animal and both sectors are excluded
        # by default anyway -- profit above 1.5x sales means the income series is
        # wrong, not that the business is extraordinary. Nvidia came out at a
        # 356% margin from a single bad fact. Publishing that and flagging it
        # afterwards is worse than publishing nothing.
        # Only a POSITIVE figure can be impossible here. You cannot earn more
        # than you sell; you can very easily lose more than you sell, which is
        # what Moderna did when its revenue collapsed and its spending did not.
        # Testing magnitude and ignoring sign withheld a real loss.
        if rev_now > 0 and not revenue_unreliable and latest_ni > 1.5 * rev_now:
            out["net_income_unusable"] = (
                f"reported profit of ${latest_ni/1e9:,.1f}B against ${rev_now/1e9:,.1f}B "
                f"of sales, which cannot be right")
        if out.get("net_income_unusable"):
            pass
        else:
            out["net_income_b"] = round(latest_ni / 1e9, 2)
            if rev_now > 0 and not revenue_unreliable:
                out["net_margin"] = round(latest_ni / rev_now * 100, 1)
        out["income_cagr_3y"] = _cagr(ni, 3)
        out["income_cagr_5y"] = _cagr(ni, 5)
        out["income_from_near_zero"] = bool(
            out["income_cagr_3y"] is None and len(ni) > 12
            and float(ni["ttm"].iloc[-1]) > 0)

    if out.get("net_income_b") is not None and out["net_income_b"] > 0 and mktcap_b:
        out["pe"] = round(mktcap_b / out["net_income_b"], 1)
    eq_now = _latest_instant(facts, EQUITY_TAGS)
    if (out.get("net_income_b") is not None and eq_now and eq_now[1] > 0
            and not out.get("thin_equity")):
        out["roe"] = round(out["net_income_b"] * 1e9 / eq_now[1] * 100, 1)

    if not revenue_unreliable:
        out["revenue_cagr_3y"] = _cagr(ttm_rev, 3)
        out["revenue_cagr_5y"] = _cagr(ttm_rev, 5)

    ocf = _ttm(facts, OPER_CASH_TAGS)
    capex = _ttm(facts, CAPEX_TAGS)
    if not ocf.empty:
        fcf = float(ocf["ttm"].iloc[-1]) - (float(capex["ttm"].iloc[-1]) if not capex.empty else 0.0)
        out["fcf_b"] = round(fcf / 1e9, 2)
        # Has to sit here, after fcf exists. Placing it earlier read a key that
        # had not been written yet -- the same ordering mistake that left every
        # research panel blank a few runs ago.
        ni_now = out.get("net_income_b")
        if ni_now is not None and ni_now < 0 and out["fcf_b"] > abs(ni_now):
            out["noncash_loss"] = True
        # Profit above gross profit means something other than trading produced
        # it -- an asset sale, a stake revaluation, a tax release. Real, and it
        # will not repeat, so it belongs in the panel as a caveat on the P/E.
        if (ni_now is not None and ni_now > 0 and gp_val is not None
                and gp_val > 0 and ni_now * 1e9 > gp_val):
            out["nonoperating_gain"] = True

    # Dilution, from the split-adjusted series the screen already built, so a
    # split can never be mistaken for issuance.
    if hist is not None and not hist.empty:
        h = hist.sort_values("date")
        h = h.assign(shares=shares_in_todays_units(h))
        now = float(h["shares"].iloc[-1])
        for yrs in (1, 3, 5):
            past = h[h["date"] <= h["date"].iloc[-1] - pd.DateOffset(years=yrs)]
            if len(past) and float(past["shares"].iloc[-1]) > 0:
                base = float(past["shares"].iloc[-1])
                # A company that listed or was formed inside the window has a
                # base near zero, and the percentage is then arithmetic rather
                # than information -- Viatris came out at 1,148,590,764%. Same
                # near-zero problem already handled for growth rates, which I
                # failed to carry across to here.
                if base < 0.05 * now:
                    out[f"share_base_near_zero_{yrs}y"] = True
                    continue
                out[f"dilution_{yrs}y"] = round((now / base - 1) * 100, 1)
    return out


def audit_research(row: dict, ps_row: dict | None = None) -> list[str]:
    """
    Plausibility checks on the research figures.

    The screen's numbers have been audited from the start; these have not, which
    is why General Mills reported a -0.5% net margin for four consecutive runs
    while every check in the tool passed it. A figure nobody validates is a
    figure nobody notices is wrong.
    """
    out = []
    if row.get("net_income_unusable"):
        # Only a fault when there is nothing to fall back on. Where the last
        # filed year is available the row is fine -- slightly dated, clearly
        # labelled -- and calling it a fault sends the reader looking for a
        # problem that has already been handled.
        if not row.get("net_income_asof"):
            out.append(row["net_income_unusable"] + "; profit figures are not shown for this row")
    # Asymmetric, for the same reason the annual screen is: a company cannot
    # earn far more than it sells, but it can certainly lose far more. Moderna
    # at -1,280% is a real collapse in revenue against continued spending, and
    # flagging it as broken data was wrong. The impossible side is already
    # caught upstream, where the figure is withheld rather than published.
    # A high net margin is not evidence of bad data. Net income includes gains
    # that never touched the operating business: Western Digital earned a real
    # 73% margin in FY2026 because $4.45bn of it was gains on its retained
    # SanDisk stake. Testing the number against the rest of the company is what
    # separates the two -- profit above GROSS profit can only come from
    # somewhere other than selling things, and that is worth saying rather than
    # flagging. Only a margin no accounting can produce still counts as a fault.
    nm = row.get("net_margin")
    if nm is not None and nm > 150:
        out.append(f"net margin of {nm:.0f}% cannot be produced by any accounting, "
                   f"so the net income series is wrong")
    cr = row.get("current_ratio")
    if cr is not None and not (0.05 <= cr <= 20):
        out.append(f"current ratio of {cr:.2f} is implausible")
    # High gearing is what sustained buybacks produce -- Mettler-Toledo and
    # Masco are correct, not broken -- so it is reported, not flagged.
    for k, label in [("dilution_1y", "one year"), ("dilution_5y", "five years")]:
        v = row.get(k)
        if v is not None and abs(v) > 400:
            out.append(f"share count moved {v:.0f}% over {label}, which needs explaining")
    fcf, ni = row.get("fcf_b"), row.get("net_income_b")
    if fcf is not None and ni is not None and abs(ni) > 1 and abs(fcf) > 25 * abs(ni):
        out.append("free cash flow is wildly out of proportion to profit")
    # Profit and cash flow diverge for real reasons, but a company generating
    # billions in cash while reporting a loss has a broken income series, not an
    # interesting accounting story. General Mills sat at a -0.5% margin against
    # $1.6bn of free cash flow for five runs, inside every plausibility band
    # because the number itself was unremarkable -- only its relationship to the
    # rest of the company gave it away.
    # A loss beside strong cash flow is NOT a broken series -- it is the
    # signature of a non-cash charge, and General Mills is the proof: a real
    # $88m loss on a $1.75bn goodwill writedown, with $1.63bn of cash still
    # coming in. I flagged that as suspicious and spent six attempts trying to
    # "fix" a figure that matched the filing to the thousandth. It belongs in
    # the panel as a fact about the company, not in the audit as a fault.
    return out


def revenue_concept_table(facts: dict, periods: list) -> list[str]:
    """Year-by-year annual value for every revenue-like concept the filer has.

    Restricted to the tags in REVENUE_TAGS, this table could not answer the
    question it was built for. ADM's trailing revenue drops from $85.2bn to
    $27.6bn at FY2022 and every listed concept agreed with the small figure, so
    the real total had to be under a name the list never tried -- and it was,
    with Revenues plus RevenueNotFromContractWithCustomer summing exactly to it.
    Anything whose name mentions revenue or sales is shown, marked or not.
    """
    node = facts.get("facts", {}).get("us-gaap", {})

    def annual_by_year(rows):
        out: dict[int, list] = {}
        for r in rows or []:
            if r.get("val") is None or "start" not in r or "end" not in r:
                continue
            try:
                s, e = _d(r["start"]), _d(r["end"])
            except (ValueError, TypeError):
                continue
            if ANNUAL_DAYS[0] <= (e - s).days <= ANNUAL_DAYS[1]:
                out.setdefault(e.year, []).append(float(r["val"]))
        return {y: max(v) for y, v in out.items()}

    interesting = {}
    for tag, entry in node.items():
        low = tag.lower()
        if "revenue" not in low and "sales" not in low:
            continue
        rows = entry.get("units", {}).get("USD")
        if not rows:
            continue
        ann = annual_by_year(rows)
        if ann:
            interesting[tag] = (ann, len(rows))

    L = ["", "REVENUE-LIKE CONCEPTS, ANNUAL VALUE BY FISCAL YEAR ($bn)"]
    years = sorted({y for ann, _ in interesting.values() for y in ann})[-11:]
    if years:
        L.append("  " + " " * 52 + " ".join(f"{y % 100:>6d}" for y in years) + "   facts")
        for tag, (ann, nrows) in sorted(interesting.items(),
                                        key=lambda kv: -max(kv[1][0].values()))[:12]:
            mark = "*" if tag in REVENUE_TAGS else " "
            cells = " ".join(f"{ann[y] / 1e9:>6.1f}" if y in ann else "     -"
                             for y in years)
            L.append(f" {mark}{tag[:51]:<52}{cells}  {nrows:>5}")
        L.append("  (* = already in REVENUE_TAGS; anything large and unmarked is a "
                 "concept the tag list is missing)")
    chosen = {p.tag for p in periods}
    L.append(f"  CHOSEN: {', '.join(sorted(chosen)) if chosen else '(none)'}")
    L += [f"  {line}" for line in CONCEPT_TRACE.get("revenue", [])]
    return L


def diagnose(ticker: str, facts: dict, periods: list, quarters: list,
             ttm: pd.DataFrame, hist: pd.DataFrame, shares: list,
             split_info: dict, actions: list, audit: list, row: dict | None) -> str:
    """
    Everything needed to debug one company, written at the moment it is flagged.

    Every fault in this pipeline so far has taken a separate diagnostic run to
    find, because the summary output shows conclusions and the bugs live in the
    intermediate steps. Dumping those steps for flagged companies as they are
    computed removes the round trip entirely: one run produces both the results
    and the evidence.
    """
    L = [f"{'=' * 78}", ticker, "=" * 78]
    if audit:
        L.append("\nFLAGGED FOR:")
        L += [f"  - {a}" for a in audit]
    if row:
        L.append(f"\nP/S now {row.get('ps_now', 0):.2f} | 5y med {row.get('ps_med_5y') or 0:.2f} "
                 f"| 10y med {row.get('ps_med_10y') or 0:.2f} | Z {row.get('zscore')} "
                 f"| years {row.get('years_covered')}")
        L.append(f"cross-check: {row.get('xc_verdict')} "
                 f"(revenue {row.get('xc_revenue_diff')}, mktcap {row.get('xc_mktcap_diff')})")

    L += revenue_concept_table(facts, periods)

    L.append("\nANNUAL FIGURES KEPT vs SUM OF DERIVED QUARTERS")
    ann_by_end = {p.end: p.val for p in periods
                  if ANNUAL_DAYS[0] <= p.days <= ANNUAL_DAYS[1]}
    for end, val in sorted(ann_by_end.items())[-8:]:
        window = [q for q in quarters if end - timedelta(days=370) < q.end <= end]
        qsum = sum(q.val for q in window[-4:])
        mark = "  <-- MISMATCH" if qsum and abs(qsum / val - 1) > 0.02 else ""
        L.append(f"  {end}  reported ${val/1e9:>10,.3f}B   4 quarters sum ${qsum/1e9:>10,.3f}B{mark}")

    L.append("\nDERIVED QUARTERS (last 10)")
    for q in quarters[-10:]:
        L.append(f"  {q.start} -> {q.end}  ({q.days:>3}d)  ${q.val/1e9:>10,.3f}B  [{q.tag[:34]}]")

    L.append("\nNET INCOME CONCEPTS (research module)")
    ni_node = facts.get("facts", {}).get("us-gaap", {})
    for tag in NET_INCOME_TAGS:
        rws = ni_node.get(tag, {}).get("units", {}).get("USD")
        if not rws:
            continue
        ends = sorted(r["end"] for r in rws if "end" in r)
        ann = sorted(float(r["val"]) for r in rws
                     if r.get("val") is not None and "start" in r and "end" in r
                     and ANNUAL_DAYS[0] <= (_d(r["end"]) - _d(r["start"])).days <= ANNUAL_DAYS[1])
        L.append(f"  {tag[:50]:<52} {len(rws):>4} facts  {ends[0]} .. {ends[-1]}"
                 + (f"  annual median ${ann[len(ann)//2]/1e9:,.2f}B" if ann else "  (no annual periods)"))
    # The annuals and quarters, not just the concepts. Six attempts at General
    # Mills failed because I could see WHAT the trailing total was and never
    # WHICH facts produced it.
    ni_periods = collect_periods(facts, "us-gaap", NET_INCOME_TAGS)
    ni_years = sorted([q for q in ni_periods
                       if ANNUAL_DAYS[0] <= q.days <= ANNUAL_DAYS[1]],
                      key=lambda x: x.end)
    L.append(f"  annual net income periods KEPT after screening ({len(ni_years)}), last 8:")
    for q in ni_years[-8:]:
        L.append(f"    {q.start} -> {q.end}  ({q.days:>3}d)  ${q.val/1e9:>9,.3f}B  [{q.tag[:30]}]")

    # And the raw facts for the most recent year, before any of my logic runs.
    node2 = facts.get("facts", {}).get("us-gaap", {})
    L.append("  RAW annual-length net income facts for the newest year:")
    raw = []
    for tag in NET_INCOME_TAGS:
        for r in node2.get(tag, {}).get("units", {}).get("USD", []) or []:
            if "start" not in r or "end" not in r or r.get("val") is None:
                continue
            if not (ANNUAL_DAYS[0] <= (_d(r["end"]) - _d(r["start"])).days <= ANNUAL_DAYS[1]):
                continue
            raw.append((r["end"], r["start"], float(r["val"]), r.get("form"), r.get("filed"), tag))
    for e, st, v, form, filed, tag in sorted(raw)[-10:]:
        L.append(f"    {st} -> {e}  ${v/1e9:>9,.3f}B  {form}  filed {filed}  [{tag[:26]}]")

    ni_q = derive_quarters(ni_periods, smooth=False)
    L.append(f"  derived net income QUARTERS, last 6:")
    for q in ni_q[-6:]:
        L.append(f"    {q.start} -> {q.end}  ({q.days:>3}d)  ${q.val/1e9:>9,.3f}B")

    ni_ttm = _ttm(facts, NET_INCOME_TAGS)
    if not ni_ttm.empty:
        L.append(f"  derived TTM net income, last 4:")
        for _, r in ni_ttm.tail(4).iterrows():
            L.append(f"    {r.period_end.date()}  ${r.ttm/1e9:>9,.3f}B  [{r.get('tag','?')}]")
    else:
        L.append("  no usable net income series")

    L.append("\nTTM SERIES (last 10)")
    if len(ttm):
        for _, r in ttm.tail(10).iterrows():
            L.append(f"  period {r.period_end.date()}  available {r.available.date()}  "
                     f"${r.ttm/1e9:>10,.3f}B  [{r.get('tag', '?')}]")
        L.append(f"  annual reconciliation: {ttm.attrs.get('annual_mismatches', 0)} mismatches "
                 f"of {ttm.attrs.get('annual_checks', 0)} year-ends checked")

    src = getattr(sys.modules[__name__], "CURRENT_SHARE_SRC", None)
    L.append(f"\nSHARE COUNTS AS COLLECTED (last 8)  [concept: {src or '?'}]")
    for when, val in shares[-8:]:
        L.append(f"  {when}  {val:>18,.0f}")
    L.append(f"SPLITS: { 
        {k: (round(v[0], 4), 'rescaled' if v[1] else 'NOT rescaled') for k, v in split_info.items()} }")
    L.append(f"CORPORATE ACTIONS: {[(str(t.date()), round(c, 3)) for t, c in (actions or [])]}")

    if hist is not None and not hist.empty:
        L.append("\nP/S SERIES, YEARLY SAMPLE")
        yearly = hist.set_index("date").resample("YE").last().dropna()
        for d0, r in yearly.iterrows():
            L.append(f"  {d0.date()}  price {r.price:>9.2f}  shares {r.shares/1e6:>10,.0f}M  "
                     f"cap ${r.mktcap/1e9:>9,.1f}B  rev ${r.ttm/1e9:>8,.2f}B  P/S {r.ps:>8.2f}")
    return "\n".join(L) + "\n"


def summarize(ticker: str, name: str, sector: str, hist: pd.DataFrame,
              margin_now: float | None, margin_then: float | None,
              rev_growth: float | None, off_high: float | None = None,
              actions: list | None = None,
              audit: list | None = None) -> dict | None:
    span_months = (hist["date"].max() - hist["date"].min()).days / 30.44
    if span_months < MIN_MONTHS_FOR_STATS or len(hist) < 200:
        return None

    hist = hist.sort_values("date")
    current = float(hist["ps"].iloc[-1])

    # Days whose denominator was already out of date are dropped from the
    # statistics. This is the point of the exercise: a stretch valued on a
    # year-old annual figure is biased in ONE direction -- the revenue is too
    # small, so the multiple is too high -- which lifts the historical median
    # and makes today look cheaper than it is. That bias is exactly the thing
    # the old "hole" warning was trying to describe, and describing it was never
    # as good as not carrying it. Nothing is fabricated to fill the space; the
    # median is simply taken over the days that were measured properly.
    #
    # Keep them only if dropping them would leave too little to measure, in
    # which case the audit says so rather than the statistics quietly changing
    # meaning.
    used_fresh = bool(hist.attrs.get("used_fresh", False))
    stats_src = hist[~hist["rev_stale"]] if used_fresh else hist

    win5 = stats_src[stats_src["date"] >= hist["date"].iloc[-1] - pd.DateOffset(years=5)]
    win10 = stats_src[stats_src["date"] >= hist["date"].iloc[-1] - pd.DateOffset(years=10)]
    if len(win10) < 200:
        # Every fresh day fell outside the ten-year window. Losing the row
        # outright would be a worse answer than the one the old code gave, so
        # fall back to the whole series and let the audit say what it rests on.
        used_fresh = False
        win5 = hist[hist["date"] >= hist["date"].iloc[-1] - pd.DateOffset(years=5)]
        win10 = hist[hist["date"] >= hist["date"].iloc[-1] - pd.DateOffset(years=10)]
    last5, last10 = win5["ps"], win10["ps"]

    have5 = len(last5) >= 400          # roughly two years of trading days
    med5 = float(last5.median()) if have5 else None
    med10 = float(last10.median())
    # Kept so the medians can be checked against sites that publish averages.
    # Multiples are right-skewed, so the mean always sits above the median; if a
    # published average comes in BELOW our median, the gap is not the statistic.
    mean5 = float(last5.mean()) if have5 else None
    mean10 = float(last10.mean())
    # How much history the "10 year" figure actually rests on -- a company that
    # listed six years ago has a six-year median wearing a ten-year label.
    years10 = ((win10["date"].iloc[-1] - win10["date"].iloc[0]).days / 365.25
               if len(win10) else 0)

    # Multiples are roughly lognormal, so the z-score belongs in log space --
    # otherwise a single bubble spike drags the mean and flattens the scale.
    logs = np.log(last10.values)
    spread = float(logs.std(ddof=1)) if len(logs) > 1 else 0.0
    # A multiple with no variance tells you nothing about whether today is
    # unusual. Reporting 0.0 would claim it is exactly normal, which is a
    # different and much stronger statement than "no information".
    z = float((math.log(current) - logs.mean()) / spread) if spread > 1e-9 else None
    pct_rank = float((last10 < current).mean() * 100)

    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "price": float(hist["price"].iloc[-1]),
        "mktcap_b": float(hist["mktcap"].iloc[-1]) / 1e9,
        "ps_now": current,
        "ps_med_5y": med5,
        "ps_med_10y": med10,
        "ps_mean_5y": mean5,
        "ps_mean_10y": mean10,
        "years_covered": round(years10, 1),
        "vs_10y_pct": (current / med10 - 1) * 100,
        "vs_5y_pct": (current / med5 - 1) * 100 if med5 else None,
        "zscore": z,
        "percentile": pct_rank,
        "ps_min": float(last10.min()),
        "ps_max": float(last10.max()),
        "months": int(round(span_months)),
        "stale_months_dropped": int(hist.attrs.get("stale_months_dropped", 0)),
        "stale_months_kept": int(hist.attrs.get("stale_months_kept", 0)),
        "restructure_date": (str(actions[-1][0].date()) if actions else None),
        "restructure_pct": (actions[-1][1] * 100 if actions else None),
        "months_since_restructure": (
            round((hist["date"].iloc[-1] - actions[-1][0]).days / 30.44)
            if actions else None),
        "audit": "; ".join(audit) if audit else None,
        "off_52w_high": off_high,
        "rev_growth_yoy": rev_growth,
        "gross_margin": margin_now,
        "gross_margin_delta": (margin_now - margin_then)
        if (margin_now is not None and margin_then is not None) else None,
    }


def revenue_growth(ttm: pd.DataFrame) -> float | None:
    """Trailing-twelve-month revenue against the same figure a year earlier."""
    if len(ttm) >= 5 and ttm["ttm"].iloc[-5] > 0:
        return float((ttm["ttm"].iloc[-1] / ttm["ttm"].iloc[-5] - 1) * 100)
    return None


def margins_and_growth(facts: dict, ttm: pd.DataFrame) -> tuple:
    """Gross margin now vs ~3y ago, and TTM revenue growth. Sanity-check columns."""
    rev_q = derive_quarters(collect_periods(facts, "us-gaap", REVENUE_TAGS))
    gp_q = derive_quarters(collect_periods(facts, "us-gaap", GROSS_PROFIT_TAGS))
    if not gp_q:
        cost_q = derive_quarters(collect_periods(facts, "us-gaap", COST_TAGS))
        if cost_q and rev_q:
            costs = {c.end: c.val for c in cost_q}
            gp_q = [Period(r.start, r.end, r.val - costs[r.end], r.filed)
                    for r in rev_q if r.end in costs]

    gp_ttm = trailing_twelve(gp_q) if gp_q else pd.DataFrame()

    def margin_at(offset_years: int):
        if gp_ttm.empty or ttm.empty:
            return None
        target = pd.Timestamp(date.today()) - pd.DateOffset(years=offset_years)
        g = gp_ttm[gp_ttm["available"] <= target] if offset_years else gp_ttm
        r = ttm[ttm["available"] <= target] if offset_years else ttm
        if g.empty or r.empty or r["ttm"].iloc[-1] <= 0:
            return None
        return float(g["ttm"].iloc[-1] / r["ttm"].iloc[-1] * 100)

    growth = None
    if len(ttm) >= 5 and ttm["ttm"].iloc[-5] > 0:
        growth = float((ttm["ttm"].iloc[-1] / ttm["ttm"].iloc[-5] - 1) * 100)

    return margin_at(0), margin_at(3), growth


# ---------------------------------------------------------------------------
# Constituents
# ---------------------------------------------------------------------------

def sp500_constituents() -> pd.DataFrame:
    """
    The constituent list. Wikipedia rejects requests that don't look like a
    browser, and pandas' built-in reader sends a bare Python user-agent, so the
    page has to be fetched separately and handed over as text.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def tidy(df, cols):
        df = df.rename(columns=cols)
        df["ticker"] = (df["ticker"].astype(str).str.strip().str.upper()
                        .str.replace(".", "-", regex=False))
        df = df[df["ticker"].str.match(r"^[A-Z\-]{1,6}$")]
        return df[["ticker", "name", "sector"]].drop_duplicates("ticker").reset_index(drop=True)

    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers, timeout=30,
        )
        r.raise_for_status()
        for table in pd.read_html(StringIO(r.text)):
            if {"Symbol", "GICS Sector"}.issubset(table.columns):
                return tidy(table, {"Symbol": "ticker", "Security": "name",
                                    "GICS Sector": "sector"})
        print("  Wikipedia page loaded but the table layout changed; using backup")
    except Exception as e:
        print(f"  Wikipedia unavailable ({type(e).__name__}); using backup source")

    r = requests.get(
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
        "main/data/constituents.csv",
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    return tidy(pd.read_csv(StringIO(r.text)),
                {"Symbol": "ticker", "Name": "name", "Sector": "sector"})


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_sqlite(summary: pd.DataFrame, series: dict[str, pd.DataFrame], path: Path):
    con = sqlite3.connect(path)
    summary.to_sql("screen", con, if_exists="replace", index=False)
    frames = []
    for t, df in series.items():
        d = df.set_index("date").resample("ME").last().dropna().reset_index()
        d = d[["date", "ps", "price", "ttm", "mktcap"]].copy()
        d.insert(0, "ticker", t)
        frames.append(d)
    if frames:
        pd.concat(frames).to_sql("ps_history", con, if_exists="replace", index=False)
    con.execute("CREATE INDEX IF NOT EXISTS ix_hist ON ps_history(ticker, date)")
    con.commit()
    con.close()


RESEARCH_FIELDS = ["current_ratio", "net_debt_b", "revenue_cagr_3y",
                   "net_margin", "dilution_1y"]


# Metric, direction, and the absolute line below/above which it is worth saying
# something regardless of sector. Direction is "up" where more is better.
RATED_METRICS = {
    "current_ratio":     ("up",   1.0,  "short-term assets over short-term liabilities"),
    "net_debt_to_sales": ("down", 3.0,  "borrowings net of cash, against a year of sales"),
    "debt_to_equity":    ("down", 2.5,  "borrowings against book equity"),
    "net_margin":        ("up",   0.0,  "profit as a share of sales"),
    "gross_margin_pct":  ("up",   None, "sales left after the direct cost of what was sold"),
    "roe":               ("up",   0.0,  "profit against the equity that produced it"),
    "pe":                ("down", None, "price against a year of profit"),
    "fcf_b":             ("up",   0.0,  "cash left after running and maintaining the business"),
}


def rate_against_sector(summary: pd.DataFrame) -> pd.DataFrame:
    """
    Grade every metric against the company's OWN sector, not a fixed table.

    Hand-written thresholds would be guesses, and wrong ones: a 0.75 current
    ratio is ordinary for a software company collecting subscriptions in advance
    and alarming for a manufacturer holding inventory. The S&P 500 already
    contains the comparison, so the quartiles of each sector do the work and
    stay current on their own.

    Absolute lines still apply where the number means something on its own --
    a current ratio below 1.0 is worth a word whatever the sector does.
    """
    for col, (direction, floor, _) in RATED_METRICS.items():
        if col not in summary.columns:
            continue
        vals = pd.to_numeric(summary[col], errors="coerce")
        med, rating, lo, hi = [], [], [], []
        for sector, idx in summary.groupby("sector").groups.items():
            peer = vals.loc[idx].dropna()
            if len(peer) < 6:
                continue
            p10, q1, q2, q3, p90 = peer.quantile([0.10, 0.25, 0.5, 0.75, 0.90])
            for i in idx:
                v = vals.get(i)
                if pd.isna(v):
                    continue
                lo.append((i, float(p10))); hi.append((i, float(p90)))
                if direction == "up":
                    grade = "good" if v >= q2 else ("warn" if v >= q1 else "bad")
                else:
                    grade = "good" if v <= q2 else ("warn" if v <= q3 else "bad")
                if floor is not None:
                    below = (v < floor) if direction == "up" else (v > floor)
                    if below and grade == "good":
                        grade = "warn"
                med.append((i, float(q2))); rating.append((i, grade))
        summary[f"{col}__sector_median"] = pd.Series(dict(med))
        summary[f"{col}__rating"] = pd.Series(dict(rating))
        # The 10th and 90th give the bar something to span. Quartiles alone
        # would clip most companies to the ends and show nothing useful.
        summary[f"{col}__sector_lo"] = pd.Series(dict(lo))
        summary[f"{col}__sector_hi"] = pd.Series(dict(hi))
    return summary


def build_stamp() -> str:
    """When the code last changed, and when this data was gathered.

    One date could not answer the question it was being asked. The screen
    rebuilds every weeknight on unchanged code, so "Built <date>" moved nightly
    and said nothing about which version produced it; and once prices refresh
    during the day, the run that wrote the page is not the run that fetched the
    prices either. Three separate facts, so three separate stamps.

    The code date comes from git, through the workflow, rather than from
    anything written by hand -- a hand-maintained version string is wrong the
    first time someone forgets it. If it is absent, say so rather than
    substituting today, which would silently claim the code had just changed.
    """
    parts = []
    code = os.environ.get("CODE_DATE", "").strip()
    if code:
        parts.append(f"Code {code}")
    parts.append(f"Data {_now_et()}")
    px = os.environ.get("PRICE_REFRESH_AT", "").strip()
    if px:
        parts.append(f"Prices {px}")
    return " &middot; ".join(parts)


def _now_et() -> str:
    """Now, in New York, so the stamp reads the way the market does."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        return now.strftime("%-d %b %Y %H:%M ET")
    except Exception:
        return datetime.utcnow().strftime("%-d %b %Y %H:%M UTC")


def write_html(summary: pd.DataFrame, path: Path):
    # The research panel reads straight from this payload, so if the fields are
    # not on the frame at this moment every panel renders blank -- which is
    # exactly what happened when the merge ran after this call. Say so loudly
    # rather than shipping a page that looks fine and is empty on click.
    absent = [c for c in RESEARCH_FIELDS if c not in summary.columns]
    if absent:
        print(f"  WARNING: research fields missing from the page ({', '.join(absent)}) "
              f"— every research panel will be empty")

    payload = json.dumps(
        summary.replace({np.nan: None}).to_dict(orient="records"), default=str
    )
    stamp = build_stamp()
    path.write_text(HTML_TEMPLATE.replace("__DATA__", payload).replace("__DATE__", stamp))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P/S vs. History &middot; S&amp;P 500</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0E1116; --panel:#151922; --rule:#242A35; --raised:#1C222C;
    --text:#C9D2DE; --dim:#6E7A8A; --bright:#EEF3F9;
    --cheap:#3FB68B; --rich:#C46A8D; --mid:#8A93A3; --warn:#D8A657;
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
    --sans:"IBM Plex Sans Condensed",system-ui,-apple-system,sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ink);color:var(--text);
       font-family:var(--mono);font-size:13px;line-height:1.45;
       -webkit-font-smoothing:antialiased}
  header{padding:26px 24px 16px}
  h1{font-family:var(--sans);font-size:26px;font-weight:700;margin:0;color:var(--bright)}
  .sub{color:var(--dim);font-size:12px;margin-top:6px;max-width:66ch}
  .controls{display:flex;flex-wrap:wrap;gap:10px;padding:14px 24px;
            border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);
            background:var(--panel)}
  .controls input,.controls select{background:var(--raised);color:var(--text);
      border:1px solid var(--rule);border-radius:3px;padding:7px 9px;
      font-family:var(--mono);font-size:12px}
  .controls input:focus,.controls select:focus,.guide:focus{outline:2px solid var(--cheap);
      outline-offset:1px}
  .chk{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px;
       white-space:nowrap;cursor:pointer}
  .chk:hover{color:var(--text)}
  .guide{background:var(--raised);border:1px solid var(--rule);border-radius:3px;
         padding:7px 11px;color:var(--cheap);text-decoration:none;font-size:12px;
         white-space:nowrap}
  .guide:hover{color:var(--bright);border-color:var(--cheap)}
  .count{margin-left:auto;color:var(--dim);font-size:12px;align-self:center;
         white-space:nowrap}
  /* The table scrolls inside its own pane, so the headings pin to the top of
     THAT pane. No measuring, no offsets, nothing to get out of sync. */
  .pane{overflow:auto;max-height:74vh;border-bottom:1px solid var(--rule)}
  table{border-collapse:separate;border-spacing:0;width:100%;min-width:1200px}
  thead th{position:sticky;top:0;background:var(--panel);z-index:3;
    font-family:var(--sans);font-weight:600;font-size:11px;letter-spacing:.09em;
    text-transform:uppercase;color:var(--dim);text-align:right;
    padding:11px 12px;cursor:pointer;white-space:nowrap;user-select:none;
    box-shadow:inset 0 -1px 0 var(--rule)}
  thead th:first-child,thead th.txt{text-align:left}
  thead th:hover{color:var(--bright)}
  thead th[aria-sort]{color:var(--bright)}
  thead th[aria-sort]::after{content:" \2193";opacity:.7}
  thead th[aria-sort="ascending"]::after{content:" \2191"}
  tbody td{padding:9px 12px;text-align:right;border-bottom:1px solid #1B212A;
    font-variant-numeric:tabular-nums}
  tbody td.txt{text-align:left}
  tbody tr:hover td{background:#181D26}
  tbody tr.flagged td{background:#1A1712}
  tbody tr.flagged:hover td{background:#221D15}
  tbody tr.flagged td:first-child{box-shadow:inset 2px 0 0 var(--warn)}
  .tk{color:var(--bright);font-weight:600}
  .nm{color:var(--dim);font-family:var(--sans);font-size:12px;
      max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cheap{color:var(--cheap)} .rich{color:var(--rich)} .flat{color:var(--mid)}
  .imp{color:var(--mid);font-size:.86em;white-space:nowrap}
  .what{margin:0 0 14px;padding:11px 13px;border-radius:7px;
        background:rgba(255,255,255,.03);border:1px solid var(--line);
        font-size:.93em;line-height:1.55;color:var(--text)}
  .what .ind{margin-top:6px;font-size:.85em;color:var(--mid)}
  .flag{color:var(--warn);cursor:help;margin-left:6px}
  .range{position:relative;width:104px;height:16px;display:inline-block;
         vertical-align:middle}
  .range i{position:absolute;left:0;right:0;top:7px;height:2px;background:#2B323D}
  .range b{position:absolute;top:3px;width:1px;height:10px;background:var(--dim)}
  .range s{position:absolute;top:1px;width:3px;height:14px;border-radius:1px}
  /* Research drawer: the second pass lives here rather than in a separate
     file, so a name can be interrogated without losing your place in the list. */
  .scrim{position:fixed;inset:0;background:rgba(6,8,12,.66);opacity:0;
         pointer-events:none;transition:opacity .16s;z-index:20}
  .scrim.on{opacity:1;pointer-events:auto}
  .drawer{position:fixed;top:0;right:0;bottom:0;width:min(492px,95vw);
          background:var(--ink);border-left:1px solid var(--rule);z-index:21;
          transform:translateX(100%);transition:transform .18s ease;overflow-y:auto}
  .drawer.on{transform:none}
  .dhead{padding:26px 26px 20px;border-bottom:1px solid var(--rule);
         background:var(--panel);position:sticky;top:0;z-index:2}
  .drawer h2{font-family:var(--sans);font-size:23px;font-weight:700;margin:0;
             color:var(--bright);letter-spacing:.01em}
  .drawer .co{color:var(--dim);font-family:var(--sans);font-size:12.5px;margin-top:3px}
  .headline{margin-top:15px;display:flex;gap:18px;align-items:baseline}
  .headline .big{font-size:27px;font-weight:600;color:var(--cheap);
                 font-variant-numeric:tabular-nums;line-height:1}
  .headline .big.rich{color:var(--rich)}
  .headline .sub2{color:var(--dim);font-size:11.5px;line-height:1.4}
  .close{position:absolute;top:20px;right:20px;background:none;border:none;
         color:var(--dim);font-size:24px;cursor:pointer;line-height:1;padding:4px}
  .close:hover{color:var(--bright)}
  .dbody{padding:0 26px 40px}
  .sect{margin-top:26px}
  .sect > h3{font-family:var(--sans);font-size:10.5px;letter-spacing:.13em;
             text-transform:uppercase;color:var(--dim);margin:0 0 2px;
             padding-bottom:7px;border-bottom:1px solid var(--rule)}
  .row{display:flex;align-items:center;gap:10px;padding:8px 0;
       border-bottom:1px solid #171C24}
  .row .lab{color:var(--dim);font-size:12.5px;flex:1;min-width:0}
  .row .val{font-variant-numeric:tabular-nums;font-size:13.5px;font-weight:500;
            text-align:right;white-space:nowrap}
  .note{display:flex;gap:9px;padding:2px 0 11px 0;color:var(--dim);font-size:11.5px;
        line-height:1.5;border-bottom:1px solid #171C24}
  .note::before{content:"";flex:0 0 2px;background:var(--rule);border-radius:1px;
                margin:2px 0}
  .note.warn::before{background:var(--warn)} .note.bad::before{background:var(--bad)}
  .pos{position:relative;width:74px;height:13px;flex:0 0 74px}
  .pos i{position:absolute;left:0;right:0;top:6px;height:2px;background:#252C36;
         border-radius:1px}
  .pos b{position:absolute;top:2px;width:1px;height:9px;background:#414B59}
  .pos s{position:absolute;top:0;width:3px;height:13px;border-radius:1px}
  .pos.empty{visibility:hidden}
  .g-good{color:var(--cheap)} .g-warn{color:var(--warn)} .g-bad{color:var(--bad)}
  .none{color:#4A5462;font-weight:400}
  .verdict{margin-top:26px;padding:16px 17px;background:var(--panel);
           border:1px solid var(--rule);border-radius:4px;color:var(--text);
           font-size:12.5px;line-height:1.62}
  .verdict h3{font-family:var(--sans);font-size:10.5px;letter-spacing:.13em;
              text-transform:uppercase;color:var(--dim);margin:0 0 4px;
              padding-bottom:9px;border-bottom:1px solid var(--rule)}
  .vpt{padding:11px 0;border-bottom:1px solid #1B212A}
  .vpt:last-child{border-bottom:none;padding-bottom:2px}
  .vlab{font-family:var(--sans);font-size:10px;letter-spacing:.12em;
        text-transform:uppercase;color:var(--dim);margin-bottom:3px}
  .vtxt{color:var(--text);font-size:12.5px;line-height:1.6}
  .vpt.final .vlab{color:var(--cheap)}
  .vpt.final .vtxt{color:var(--bright)}
  .caution{margin-top:12px;padding:12px 14px;border-left:2px solid var(--warn);
           background:#171B23;color:var(--dim);font-size:11.5px;line-height:1.55}
  .tk{cursor:pointer;text-decoration:underline;text-decoration-color:#38414F;
      text-underline-offset:3px}
  .tk:hover{color:var(--cheap);text-decoration-color:var(--cheap)}
  .key{padding:34px 24px 48px}
  .key h2{font-family:var(--sans);font-size:15px;letter-spacing:.09em;
          text-transform:uppercase;color:var(--bright);margin:0 0 4px}
  .key .lede{color:var(--dim);font-size:12px;margin-bottom:22px;max-width:72ch}
  .key dl{display:grid;grid-template-columns:132px minmax(0,74ch);margin:0;
          align-items:baseline}
  .key dt{font-family:var(--sans);font-weight:600;font-size:12px;
          letter-spacing:.07em;text-transform:uppercase;color:var(--bright);
          padding:13px 16px 13px 0;border-top:1px solid #1B212A}
  .key dd{margin:0;padding:13px 0;color:var(--dim);font-size:12.5px;
          border-top:1px solid #1B212A}
  .caveat{margin-top:26px;padding:16px 18px;border-left:2px solid var(--warn);
          background:#171B23;color:var(--dim);font-size:12.5px;max-width:80ch}
  .caveat strong{color:var(--text)}
  .caveat + .caveat{margin-top:14px}
  @media (max-width:700px){
    header{padding:18px 14px 12px} .controls{padding:12px 14px} .key{padding:26px 14px 40px}
    .pane{max-height:68vh}
    .key dl{grid-template-columns:1fr}
    .key dt{padding:14px 0 3px}
    .key dd{padding:0 0 14px;border-top:none}
  }
</style>
</head>
<body>
<header>
  <h1>Price / Sales vs. its own history</h1>
  <div class="sub">Multiples built from as-filed SEC revenue and split-adjusted share
  counts, sampled monthly.<br>__DATE__</div>
</header>

<div class="controls">
  <input id="q" type="search" placeholder="Ticker or name" size="16">
  <select id="sector"><option value="">All sectors</option></select>
  <select id="subsector"><option value="">All industries</option></select>
  <input id="mincap" type="number" placeholder="Min cap ($B)" size="10">
  <label class="chk"><input type="checkbox" id="growing"> Sales still growing</label>
  <label class="chk"><input type="checkbox" id="hideRestruct" checked> Hide restructured</label>
  <label class="chk"><input type="checkbox" id="hideShort" checked> Hide short history</label>
  <label class="chk"><input type="checkbox" id="hideFlagged"> Hide flagged</label>
  <label class="chk"><input type="checkbox" id="incFin"> Include banks &amp; property</label>
  <a class="guide" href="#key">What do the columns mean?</a>
  <span class="count" id="count"></span>
</div>

<div class="pane">
<table>
  <thead><tr>
    <th class="txt" data-k="ticker" title="Stock symbol">Ticker</th>
    <th class="txt" data-k="name" title="Company name">Company</th>
    <th class="txt" data-k="sector" title="Industry group">Sector</th>
    <th class="txt" data-k="industry" title="The narrower industry inside that sector. Filter by it to see whether a whole peer group has been marked down together">Industry</th>
    <th data-k="mktcap_b" title="Total market value, in billions of dollars">Cap $B</th>
    <th data-k="price" title="Current share price">Price</th>
    <th data-k="ps_now" title="Price-to-sales today: market value divided by the last 12 months of revenue">P/S</th>
    <th data-k="zscore" title="The discount scaled to how much this stock's multiple normally swings. Sort by this">Z</th>
    <th data-k="ps_med_5y" title="The P/S this stock has typically traded at over the last 5 years, and the share price that multiple implies on today's revenue">5y med</th>
    <th data-k="vs_5y_pct" title="How far today's P/S is from its 5-year normal">vs 5y</th>
    <th data-k="ps_med_10y" title="The P/S this stock has typically traded at over the last 10 years, and the share price that multiple implies on today's revenue">10y med</th>
    <th data-k="vs_10y_pct" title="How far today's P/S is from its 10-year normal">vs 10y</th>
    <th data-k="percentile" title="Share of the last ten years it spent cheaper than today">Percentile</th>
    <th class="txt" title="Where today sits between its historical low and high">Range</th>
    <th data-k="off_52w_high" title="How far the share price sits below its own 52-week peak">Off high</th>
    <th data-k="xc_revenue_diff" title="Agreement with Yahoo's independently reported revenue">Check</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
</div>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" role="dialog" aria-label="Company research">
  <button class="close" id="closeDrawer" aria-label="Close">&times;</button>
  <div id="drawerBody"></div>
</aside>

<section class="key" id="key">
  <h2>What the columns mean</h2>
  <div class="lede">On a computer you can also hover over any column heading for a
  one-line version.</div>
  <dl>
    <dt>Cap $B</dt><dd>What the whole company is worth at today's share price.</dd>
    <dt>P/S</dt><dd>What you pay per dollar of annual sales. A P/S of 3 means the company is priced at three times its yearly revenue. Lower is cheaper, but only meaningful compared to something.</dd>
    <dt>5y med</dt><dd>The middle value of its P/S over that period. Median rather than average, so one bubble year doesn't drag the number.</dd>
    <dt>10y med</dt><dd>Same idea over a longer window. Harder to distort, but can include a business that no longer resembles today's.</dd>
    <dt>vs 5y</dt><dd>Minus 40% means the stock is priced 40% cheaper per dollar of sales than it usually is.</dd>
    <dt>vs 10y</dt><dd>When this and the 5-year figure disagree, the multiple drifted over recent years rather than the stock suddenly getting cheap. When they agree, the discount is more believable.</dd>
    <dt>Z</dt><dd>The same discount, adjusted for volatility. A stock whose multiple always swings wildly needs a bigger drop to count as unusual. Below minus 2 is rare; minus 0.5 is noise. This is the most reliable column to sort by.</dd>
    <dt>Percentile</dt><dd>Where today's multiple sits among the last <b>ten years</b>
    of its own readings — 5 means it has almost never been this cheap, 95 almost never
    this expensive. It ranks the price-to-sales multiple, not the share price: a stock
    can be dearer than it has ever been and still near the bottom of this scale, if
    sales grew faster than the price did. Rows with less than ten years of filings are
    ranked against whatever history exists, and say so when you open them.</dd>
    <dt>Range</dt><dd>The bar spans its cheapest to priciest over the period. The coloured mark is today; the grey tick is the median.</dd>
    <dt>Check</dt><dd>Two independent checks, run on every row. First, Yahoo derives revenue and market cap separately from the SEC, so agreement is real evidence today's figure is right. Second, a structural audit of the whole history: market cap must not step without a split to explain it, revenue must not jump between filings or stop updating. That second check is what validates the medians — nobody publishes historical multiples through an API, but every median error found so far showed up as a break in the underlying series.</dd>
    <dt>Off high</dt><dd>Price only, nothing to do with valuation. A stock deep below its 52-week high alongside a low Z has fallen recently rather than drifted cheap over years, which usually means there is a specific piece of news to go and find.</dd>
    <dt>5y med / 10y med</dt><dd>The multiple this stock has typically traded at, with the same thing expressed as a share price in brackets &mdash; what it would cost per share at that multiple, on today's revenue. It sits beside the median rather than beside the discount on purpose: a &minus;50% reading next to a price twice the current one reads as a contradiction for a second, when in fact both say the same thing. The bracketed price holds revenue fixed and moves only the multiple, so it is a historical reference rather than a valuation. It says nothing about whether the old multiple was deserved; if the business has changed permanently, that multiple is the wrong yardstick and the price inherits the error.</dd>
    <dt>Sales growth</dt><dd>Moved into the research panel, under "Is the business still growing", alongside the three- and five-year rates and the profit growth to compare them against. A cheap stock with shrinking sales is often cheap for a reason, so it is worth reading before getting interested — but one year of revenue change was taking a column in the table to say less than the panel says in four lines.</dd>
  </dl>

  <div class="caveat">
    <strong>The amber &#9888; marks a row you shouldn't trust.</strong> Hover it, or tap it on a
    phone, to see why. Two things trigger it: not enough history for the averages to mean
    anything, or a price-to-sales figure so far outside any plausible range that the revenue
    almost certainly failed to read correctly from the filings. Flagged rows stay visible so you
    can see what got caught, tinted amber down the left edge. Use the checkbox to hide them.
  </div>

  <div class="caveat">
    <strong>Companies with under eight years of history are hidden by default</strong>, and this
    is the subtlest trap in the whole tool. A short window drops the older, usually cheaper years,
    so the median rises and the stock looks further below its own norm than it really is. The
    effect is not small: across this table, companies with a full history sit below minus two on Z
    about 3% of the time, while those under eight years do so 14% of the time &mdash; five times
    as often, for no reason connected to value. Sometimes the window is short because the company
    listed recently, and sometimes because its older filings used a revenue concept that could not
    be safely joined to the current one. Either way the comparison is weaker than the column
    heading implies. Untick to see them.
  </div>

  <div class="caveat">
    <strong>Companies that recently restructured are hidden by default.</strong> When a business
    spins off or breaks up a division, its market cap drops the moment the deal completes, but
    trailing revenue keeps counting the departed business for up to four more quarters. The
    price-to-sales that falls out is far too low, has nothing to do with value, and sorts
    straight to the top of the list. Worse, the years of history above it describe a company
    that no longer exists. Detected from share counts moving sharply with no split to explain
    it. Untick the box to see them.
  </div>

  <div class="caveat">
    <strong>Banks, insurers and property companies are excluded by default.</strong> Revenue
    barely means the same thing for a bank as it does for a retailer, so price-to-sales is a poor
    measure for them regardless of the data &mdash; and their filings tag revenue inconsistently,
    so a meaningful share of those rows come out wrong in ways that still look plausible. Tick the
    box to include them, but verify anything you find there against the company's own filings.
  </div>

  <div class="caveat">
    <strong>How to use this.</strong> Sort by Z, smallest first. That surfaces the names trading
    furthest below their own norm. Then open the row: the panel leads with what the company
    actually does and whether sales are growing. A discount alongside shrinking revenue usually
    means the market has repriced a worse business, and it is unlikely to close. A discount
    alongside growth is worth real research. This narrows five hundred names to a handful of
    questions. It does not answer them.
  </div>
</section>

<script id="data" type="application/json">__DATA__</script>
<script>
const rows = JSON.parse(document.getElementById('data').textContent);
const tbody = document.getElementById('rows');
const qEl = document.getElementById('q');
const secEl = document.getElementById('sector');
const subEl = document.getElementById('subsector');
const capEl = document.getElementById('mincap');
const growEl = document.getElementById('growing');
const hideEl = document.getElementById('hideFlagged');
const restructEl = document.getElementById('hideRestruct');
const shortEl = document.getElementById('hideShort');
const finEl = document.getElementById('incFin');
const countEl = document.getElementById('count');

const WEAK_SECTORS = ['Financials', 'Real Estate'];

/* A P/S far outside any plausible range means the revenue didn't parse, not that
   the stock is expensive. Thin history makes the averages meaningless. */
function problems(r){
  const p = [];
  if (r.ps_now > 30 || r.ps_med_10y > 30)
    p.push('Price-to-sales is far outside any plausible range, which almost always means the revenue did not read correctly from the filings. Verify against the company before believing this row.');
  if (r.years_covered != null && r.years_covered < 8 && r.months >= 60)
    p.push('The "10y" figures here rest on ' + r.years_covered +
      ' years — that is all the history there is. Older filings used a revenue ' +
      'concept that did not match the current one, so they were excluded rather than spliced. ' +
      'The medians are real but rest on a shorter window than the column headings suggest.');
  if (r.months < 60)
    p.push('Only ' + (Math.round(r.months / 12 * 10) / 10) + ' years of history, so the 5- and 10-year averages are not really that. Treat the Z score as unreliable.');
  if (r.ps_med_10y <= 0) p.push('No usable historical median.');
  if (r.xc_verdict && !['agrees','unchecked'].includes(r.xc_verdict))
    p.push('Yahoo disagrees with the SEC data: ' + r.xc_verdict + '.');
  if (r.audit) p.push('Structural check failed — ' + r.audit + '.');
  return p;
}
rows.forEach(r => {
  r._restructured = r.months_since_restructure != null && r.months_since_restructure <= 24;
  // Eight years hid anything that listed recently, and a five-year median is
  // still a median -- it just needs saying that that is what you are reading.
  r._short = r.years_covered != null && r.years_covered < 5;
  r._problems = problems(r);
  if (r._restructured)
    r._problems.push('Share count moved ' + Math.round(r.restructure_pct) + '% in ' +
      r.restructure_date + ' with no split to explain it — a spin-off, breakup or stock ' +
      'acquisition. Market cap already reflects the new company while trailing revenue still ' +
      'counts the old one for up to four quarters, so this P/S is not comparable to its history.');
  r._flagged = r._problems.length > 0;
});

[...new Set(rows.map(r => r.sector).filter(Boolean))].sort().forEach(s => {
  const o = document.createElement('option'); o.value = o.textContent = s;
  secEl.appendChild(o);
});

/* Industry is the level the market actually repriced. A sector selloff is
   rarely a sector: in 2025 it was application software specifically, while
   semiconductors in the same sector went the other way. Comparing a name to
   its own history tells you it is cheap; filtering to its industry tells you
   whether every peer is equally cheap, which is the difference between one
   company's problem and a whole group being marked down together.
   The list is rebuilt from whatever sector is selected, because 150 industries
   in one dropdown is not usable. */
function fillIndustries() {
  const sec = secEl.value;
  const cur = subEl.value;
  const pool = rows.filter(r => !sec || r.sector === sec);
  const list = [...new Set(pool.map(r => r.industry).filter(Boolean))].sort();
  subEl.innerHTML = '<option value="">All industries</option>';
  list.forEach(s => {
    const o = document.createElement('option'); o.value = o.textContent = s;
    subEl.appendChild(o);
  });
  subEl.value = list.includes(cur) ? cur : '';
}
fillIndustries();
secEl.addEventListener('change', () => { subEl.value = ''; fillIndustries(); });

let sortKey = 'zscore', sortAsc = true;

/* Colour has to follow MEANING, not sign. Below its own norm is good news, so
   negative is green there. Shrinking revenue is bad news, so negative must be
   red there — the one column meant to stop you cannot be reassuring you.
   polarity: 'discount' = low is good, 'growth' = high is good, 'none' = neutral. */
function impliedPrice(r, med) {
  // The share price today's revenue would support if the multiple went back to
  // its own historical normal. Scale the current price by how far the multiple
  // has to travel: price x (median P/S / current P/S). Revenue and share count
  // both cancel, so this needs no figure the row does not already carry, and it
  // cannot drift out of step with the P/S columns beside it.
  //
  // This is a HISTORICAL reference, not a valuation. It says where the price
  // sits relative to this company's own past multiple, holding today's revenue
  // fixed. It assumes nothing about whether that multiple was deserved then or
  // is deserved now -- if the business has permanently changed, the old
  // multiple is the wrong yardstick and this number inherits that error.
  if (r.price == null || med == null || r.ps_now == null || r.ps_now <= 0) return null;
  return r.price * (med / r.ps_now);
}

function impliedTo(r, med) {
  // The implied price plus the move it would take to get there, coloured the
  // same way as the analyst target so the two read as the same kind of
  // statement: here is a reference price, and here is the distance to it.
  //
  // The percentage is NOT the vs-column negated. vs 10y compares multiples: a
  // stock 20% below its median multiple has to rise 25% to reach it, because
  // the two are measured against different bases. Deriving it from the prices
  // themselves avoids publishing a number that contradicts the one beside it.
  const p = impliedPrice(r, med);
  if (p == null || r.price == null || r.price <= 0)
    return '<span class="none">&mdash;</span>';
  const move = (p / r.price - 1) * 100;
  return '$' + p.toFixed(2) +
    ` <span class="${move > 0 ? 'g-good' : 'g-bad'}">${move > 0 ? '+' : ''}${move.toFixed(0)}%</span>`;
}

function implied(r, med) {
  const p = impliedPrice(r, med);
  if (p == null) return '';
  return ` <span class="imp" title="Share price if the multiple returned to this median, on today's revenue">(${
    p >= 1000 ? p.toFixed(0) : p.toFixed(2)})</span>`;
}

function signed(v, d = 0, polarity = 'discount') {
  if (v === null || v === undefined || Number.isNaN(v)) return '<span class="flat">&mdash;</span>';
  const txt = `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}%`;
  let cls = 'flat';
  if (polarity === 'discount') cls = v < -5 ? 'cheap' : v > 5 ? 'rich' : 'flat';
  else if (polarity === 'growth') cls = v > 2 ? 'cheap' : v < 0 ? 'rich' : 'flat';
  return `<span class="${cls}">${txt}</span>`;
}

function checkMark(r) {
  if (r.audit)
    return `<span class="rich" title="${r.audit}. This is a fault found inside our own series, not a disagreement with anyone else.">&#10007;</span>`;
  const v = r.xc_verdict;
  if (!v || v === 'unchecked') return '<span class="flat" title="Not cross-checked. Only the most extreme rows are verified, since each costs a request.">&ndash;</span>';
  if (v === 'agrees') return `<span class="cheap" title="Yahoo's independently reported revenue and share count both agree with the SEC figures to within tolerance.">&#10003;</span>`;
  return `<span class="rich" title="${v}. Today's price-to-sales is probably wrong on this row.">&#10007;</span>`;
}

function medianNote(r) {
  const parts = [];
  if (r.ps_mean_10y != null)
    parts.push('Mean over the same window: ' + r.ps_mean_10y.toFixed(2) +
      '. Multiples are right-skewed, so the mean normally sits ABOVE the median — ' +
      'a published average that falls below this one is a real disagreement, not a ' +
      'difference of statistic.');
  if (r.years_covered != null) {
    parts.push('Based on ' + r.years_covered + ' years of data.');
    if (r.years_covered < 9.5)
      parts.push('That is short of ten, so this is really a ' + r.years_covered +
        '-year median. Companies that listed recently are missing their early, ' +
        'usually cheaper, years, which biases this figure upward.');
  }
  return parts.join(' ');
}

function rangeBar(r) {
  if (r.ps_min == null || r.ps_max == null || r.ps_max <= r.ps_min) return '';
  const lo = Math.log(r.ps_min), hi = Math.log(r.ps_max);
  const pos = p => Math.max(0, Math.min(100, (Math.log(p) - lo) / (hi - lo) * 100));
  const col = r.zscore < -0.5 ? 'var(--cheap)' : r.zscore > 0.5 ? 'var(--rich)' : 'var(--mid)';
  return `<span class="range" title="Ranged ${r.ps_min.toFixed(1)} to ${r.ps_max.toFixed(1)}; today ${r.ps_now.toFixed(1)}">
    <i></i><b style="left:${pos(r.ps_med_10y)}%"></b>
    <s style="left:${pos(r.ps_now)}%;background:${col}"></s></span>`;
}

function visible() {
  const q = qEl.value.trim().toLowerCase();
  const sec = secEl.value;
  const sub = subEl.value;
  const cap = parseFloat(capEl.value);
  return rows.filter(r => {
    if (!finEl.checked && !sec && WEAK_SECTORS.includes(r.sector)) return false;
    if (restructEl.checked && r._restructured) return false;
    if (shortEl.checked && r._short) return false;
    if (hideEl.checked && r._flagged) return false;
    if (q && !(r.ticker.toLowerCase().includes(q) ||
               (r.name || '').toLowerCase().includes(q))) return false;
    if (sec && r.sector !== sec) return false;
    if (sub && r.industry !== sub) return false;
    if (!Number.isNaN(cap) && (r.mktcap_b ?? 0) < cap) return false;
    if (growEl.checked && !((r.rev_growth_yoy ?? -999) > 0)) return false;
    return true;
  });
}

function render() {
  const list = visible().sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === 'string') return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
    return sortAsc ? x - y : y - x;
  });
  const flagged = list.filter(r => r._flagged).length;
  countEl.textContent = `${list.length} shown` + (flagged ? ` \u00b7 ${flagged} flagged \u26a0` : '');
  tbody.innerHTML = list.map(r => `<tr class="${r._flagged ? 'flagged' : ''}">
    <td class="txt tk" data-t="${r.ticker}" title="Open the research panel">${r.ticker}${r._flagged
      ? `<span class="flag" title="${r._problems.join(' ')}">&#9888;</span>` : ''}</td>
    <td class="txt nm" title="${r.name || ''}">${r.name || ''}</td>
    <td class="txt nm">${r.sector || ''}</td>
    <td class="txt nm" title="${r.industry || ''}">${r.industry || ''}</td>
    <td>${num(r.mktcap_b, 1)}</td>
    <td>${num(r.price, 2)}</td>
    <td>${num(r.ps_now)}</td>
    <td class="${r.zscore == null ? 'flat' : r.zscore < -0.5 ? 'cheap' : r.zscore > 0.5 ? 'rich' : 'flat'}"
        title="${r.zscore == null ? 'This multiple has barely moved, so there is no basis for calling today unusual either way.' : ''}">${num(r.zscore)}</td>
    <td>${num(r.ps_med_5y)}${implied(r, r.ps_med_5y)}</td>
    <td>${signed(r.vs_5y_pct)}</td>
    <td title="${medianNote(r)}">${num(r.ps_med_10y)}${implied(r, r.ps_med_10y)}</td>
    <td>${signed(r.vs_10y_pct)}</td>
    <td>${num(r.percentile, 0)}</td>
    <td class="txt">${rangeBar(r)}</td>
    <td>${signed(r.off_52w_high, 0, 'none')}</td>
    <td>${checkMark(r)}</td>
  </tr>`).join('');
}

document.querySelectorAll('th[data-k]').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.k;
    sortAsc = (k === sortKey) ? !sortAsc : (k === 'ticker' || k === 'name' || k === 'sector');
    sortKey = k;
    document.querySelectorAll('th').forEach(o => o.removeAttribute('aria-sort'));
    th.setAttribute('aria-sort', sortAsc ? 'ascending' : 'descending');
    render();
  });
});

/* ---- research drawer ---------------------------------------------------- */
const drawer = document.getElementById('drawer');
const scrim  = document.getElementById('scrim');
const body   = document.getElementById('drawerBody');

const num = (v, d = 2, suf = '') => (v === null || v === undefined || Number.isNaN(v))
  ? '<span class="none">not reported</span>' : Number(v).toFixed(d) + suf;

function grow(v, good = 'up', note) {
  if (v === null || v === undefined || Number.isNaN(v))
    return `<span class="none">${note || '&mdash;'}</span>`;
  const up = Number(v) > 0;
  const cls = (good === 'up') ? (up ? 'cheap' : Number(v) < 0 ? 'rich' : 'flat')
                              : (up ? 'rich' : Number(v) < 0 ? 'cheap' : 'flat');
  return `<span class="${cls}">${up ? '+' : ''}${Number(v).toFixed(1)}%</span>`;
}

function money(v) {
  if (v === null || v === undefined || Number.isNaN(v))
    return '<span class="none">not reported</span>';
  const cls = v < 0 ? 'cheap' : 'flat';     // negative net debt is a good thing
  return `<span class="${cls}">$${Number(v).toFixed(1)}B</span>`;
}


/* Colour follows the company's own sector, not a fixed table: a 0.75 current
   ratio is ordinary for software billing in advance and alarming for a
   manufacturer holding inventory. Growth is deliberately excluded — seeing
   positive growth painted red because peers grew faster would mislead. */
const EXPLAIN = {
  current_ratio: v => v < 1
    ? 'Short-term bills exceed short-term assets. Routine for a business paid up front by subscribers; a genuine warning for one carrying inventory or receivables.'
    : 'Short-term assets cover the next year of bills, but with less room than most of the sector.',
  net_debt_to_sales: v => v > 2.5
    ? `Debt net of cash is about ${v.toFixed(1)} times a full year of sales. Serviceable while cash flow holds up, punishing if a couple of bad years arrive together.`
    : 'Carries more debt against sales than most of the sector, though not at a level that constrains it.',
  debt_to_equity: v => v > 2.5
    ? 'Borrowings are several times book equity. Usually the arithmetic of years of buybacks rather than distress, but it leaves a thinner cushion if earnings drop.'
    : 'More borrowed against book value than most of the sector.',
  net_margin: v => v < 0
    ? 'Losing money on sales. Check whether a one-off charge is doing it before treating it as the run rate.'
    : `Keeps about ${v.toFixed(0)} cents of every sales dollar as profit, less than most of the sector.`,
  gross_margin_pct: v => `Only ${v.toFixed(0)}% of sales is left after the direct cost of what was sold, below most of the sector. That is the ceiling on everything beneath it, so there is less room to absorb rising costs.`,
  roe: v => v < 0
    ? 'Losing money against the equity shareholders have in the business.'
    : 'Generates less profit per dollar of book equity than most of the sector.',
  pe: v => `The market is paying about ${v.toFixed(0)} times a year of current profit, more than it pays for most of this sector. That premium has to be earned by growth.`,
  fcf_b: v => v < 0
    ? 'Spending more cash than the business generates, once capital spending is counted. Reasonable for a company investing hard, a problem for one that is not.'
    : 'Generates less free cash than most of the sector.',
};

function sectorBar(r, key, dp, suffix, prefix) {
  const v = r[key], med = r[key + '__sector_median'];
  const lo = r[key + '__sector_lo'], hi = r[key + '__sector_hi'];
  if (med == null || lo == null || hi == null || hi <= lo || v == null)
    return '<span class="pos empty"></span>';
  const at = x => Math.max(0, Math.min(100, (x - lo) / (hi - lo) * 100));
  const grade = r[key + '__rating'];
  const col = grade === 'good' ? 'var(--cheap)' : grade === 'bad' ? 'var(--bad)' : 'var(--warn)';
  const fmt = x => (prefix || '') + Number(x).toFixed(dp) + (suffix || '');
  return `<span class="pos" title="Sector spans ${fmt(lo)} to ${fmt(hi)}; median ${fmt(med)}. This company ${fmt(v)}.">
    <i></i><b style="left:${at(med)}%"></b><s style="left:${at(v)}%;background:${col}"></s></span>`;
}

/* A row, and beneath it a note ONLY where the figure needs context. Explaining
   every line would bury the two that matter; explaining none leaves a wall of
   numbers. The note carries the grade colour down its left edge so it reads as
   attached to the row above rather than dropped in loose. */
function row(r, key, label, dp, suffix, prefix) {
  const v = r[key];
  if (v == null || Number.isNaN(v))
    return `<div class="row"><span class="lab">${label}</span>` +
           `<span class="val none">not reported</span><span class="pos empty"></span></div>`;
  const grade = r[key + '__rating'];
  const shown = (prefix || '') + Number(v).toFixed(dp) + (suffix || '');
  const note = (grade && grade !== 'good' && EXPLAIN[key])
    ? `<div class="note ${grade}"><span>${EXPLAIN[key](Number(v))}</span></div>` : '';
  return `<div class="row"><span class="lab">${label}</span>` +
         `<span class="val ${grade ? 'g-' + grade : ''}">${shown}</span>` +
         `${sectorBar(r, key, dp, suffix, prefix)}</div>${note}`;
}

function plain(label, value, cls) {
  return `<div class="row"><span class="lab">${label}</span>` +
         `<span class="val ${cls || ''}">${value}</span><span class="pos empty"></span></div>`;
}

function verdict(r) {
  const n = v => (v === null || v === undefined || Number.isNaN(v)) ? null : Number(v);
  const ps = n(r.ps_now), med = n(r.ps_med_10y), z = n(r.zscore);
  const s1 = n(r.rev_growth_yoy), s3 = n(r.revenue_cagr_3y), p3 = n(r.income_cagr_3y);
  const gm = n(r.gross_margin_pct), gmMed = n(r.gross_margin_pct__sector_median);
  const cr = n(r.current_ratio), crMed = n(r.current_ratio__sector_median);
  const nds = n(r.net_debt_to_sales), fcf = n(r.fcf_b), dil3 = n(r.dilution_3y);
  const off = n(r.off_52w_high), up = n(r.target_upside), pct = n(r.percentile);
  const out = [];   // [label, text] pairs

  // 1. The gap, stated concretely.
  if (ps && med) {
    let v = `Priced at ${ps.toFixed(2)}x sales against a ten-year median of ${med.toFixed(2)}x`;
    if (r.vs_10y_pct != null)
      v += `, ${Math.abs(r.vs_10y_pct).toFixed(0)}% ${r.vs_10y_pct < 0 ? 'below' : 'above'} its own norm`;
    if (z != null) v += ` (${z.toFixed(1)} standard deviations)`;
    let extra = '';
    if (pct != null && r.years_covered) {
      if (pct < 3) extra = ` That is the cheapest it has been in ${r.years_covered} years.`;
      else if (pct < 20) extra = ` It has been cheaper than this only ${pct.toFixed(0)}% of the ` +
        `past ${r.years_covered} years.`;
    }
    out.push(['Valuation', v + '.' + extra]);
  }

  // 2. Demand and profit TOGETHER -- this is the part that carries the meaning.
  const salesFalling = (s1 != null && s1 < -1) || (s3 != null && s3 < -1);
  const salesGrowing = (s1 != null && s1 > 2) || (s3 != null && s3 > 2);
  const profFalling = p3 != null && p3 < -3;
  const profGrowing = p3 != null && p3 > 3;
  const sTxt = s1 != null ? `${s1 > 0 ? '+' : ''}${s1.toFixed(1)}% last year` : 'flat';
  const s3Txt = s3 != null ? `${s3 > 0 ? '+' : ''}${s3.toFixed(1)}% a year over three` : null;

  if (salesGrowing && profFalling)
    out.push(['The business', `Sales are still growing (${sTxt}${s3Txt ? ', ' + s3Txt : ''}) while profit has fallen ` +
      `${Math.abs(p3).toFixed(0)}% a year over three. The pressure is on margins, not demand ` +
      `\u2014 something is costing more, or pricing has gone.`]);
  else if (salesFalling && profFalling) {
    // Report both horizons. Nike is +0.2% on the year and negative over three,
    // and calling that "falling together" beside a positive number reads as an
    // error even though the three-year figure is what triggered it.
    const both = s3Txt ? `${sTxt}, ${s3Txt}` : sTxt;
    const shape = (s1 != null && s1 >= -1)
      ? `Sales have stalled (${both}) and profit has fallen ${Math.abs(p3).toFixed(0)}% a year ` +
        `over three \u2014 the top line stopped growing before the bottom line gave way`
      : `Sales and profit are falling together (${both}; profit ${Math.abs(p3).toFixed(0)}% a year ` +
        `over three)`;
    out.push(['The business', shape + `, so the discount is tracking a real deterioration rather than neglect.`]);
  }
  else if (salesFalling && profGrowing)
    out.push(['The business', `Sales are shrinking (${sTxt}) while profit still grows ${p3.toFixed(0)}% a year ` +
      `\u2014 volume is being traded for margin, which works until it doesn't.`]);
  else if (salesGrowing && profGrowing)
    out.push(['The business', `Both sides are still growing: sales ${sTxt}${s3Txt ? ', ' + s3Txt : ''}, profit ` +
      `${p3.toFixed(0)}% a year. Nothing in the operating numbers explains the derating.`]);
  else if (salesGrowing)
    out.push(['The business', `Sales are growing ${sTxt}${s3Txt ? ', ' + s3Txt : ''}.`]);
  else if (salesFalling)
    out.push(['The business', `Sales are shrinking (${sTxt}), which is usually the reason a multiple contracts.`]);
  else
    out.push(['The business', `Sales are broadly flat (${sTxt}).`]);

  if (r.nonoperating_gain)
    out.push(['Profit', `Profit exceeds gross profit, so most of it came from something other ` +
      `than trading \u2014 an asset sale, a stake revaluation or a tax release. Real money, but ` +
      `it will not repeat, so the P/E and margin below understate how expensive the ongoing ` +
      `business is.`]);
  if (r.noncash_loss)
    out.push(['Profit', `The reported loss is non-cash \u2014 an impairment or writedown, not money going out ` +
      `the door, which is why cash flow stayed positive.`]);

  // 3. Margin position within the sector, where it adds something.
  if (gm != null && gmMed != null && Math.abs(gm - gmMed) > 4)
    out.push(['Margins', `Gross margin of ${gm.toFixed(0)}% is ${gm > gmMed ? 'above' : 'below'} the sector's ` +
      `${gmMed.toFixed(0)}%, so the underlying economics are ${gm > gmMed ? 'better' : 'worse'} ` +
      `than its peers' before any of this.`]);

  // 4. Balance sheet, only when it changes the picture.
  const stretched = (cr != null && cr < 1) || (nds != null && nds > 2.5)
    || r.negative_equity || r.thin_equity;
  if (stretched) {
    const bits = [];
    if (cr != null && cr < 1) bits.push(`current ratio ${cr.toFixed(2)}` +
      (crMed != null ? ` against a sector median of ${crMed.toFixed(2)}` : ''));
    if (nds != null && nds > 2.5) bits.push(`net debt at ${nds.toFixed(1)}x sales`);
    if (r.negative_equity) bits.push('negative book equity');
    if (r.thin_equity) bits.push('almost no book equity left after buybacks');
    out.push(['Balance sheet', `The balance sheet carries some of the risk here: ${bits.join(', ')}` +
      (fcf != null && fcf > 0 ? `, though it still generates $${fcf.toFixed(1)}B of free cash flow.`
                              : '.')]);
  } else if (fcf != null && fcf < 0) {
    out.push(['Balance sheet', `It is burning cash after capital spending ($${fcf.toFixed(1)}B), which limits how ` +
      `long a weak patch can run.`]);
  } else if (cr != null && fcf != null && fcf > 0) {
    out.push(['Balance sheet', `The balance sheet is not the issue \u2014 current ratio ${cr.toFixed(2)}` +
      (crMed != null ? ` versus a sector median of ${crMed.toFixed(2)}` : '') +
      `, and $${fcf.toFixed(1)}B of free cash flow.`]);
  }

  if (dil3 != null && dil3 > 4)
    out.push(['Shares', `Share count is up ${dil3.toFixed(0)}% over three years, so per-share results are ` +
      `weaker than the totals suggest.`]);
  else if (dil3 != null && dil3 < -8)
    out.push(['Shares', `Buybacks have cut the share count ${Math.abs(dil3).toFixed(0)}% in three years, ` +
      `which flatters per-share figures and is worth stripping out.`]);

  // 5. The question this leaves, which is the actual output.
  //
  // The last branch used to read "why the multiple has contracted when the
  // numbers have not" for every row that matched none of the others. It never
  // looked at the multiple. Micron's has EXPANDED -- it screens rich, not cheap
  // -- and the panel told you the opposite. A sentence that states a direction
  // has to read that direction off the data.
  const rich = r.zscore != null ? r.zscore > 0.5
             : (r.vs_10y_pct != null ? r.vs_10y_pct > 10 : null);
  const cheapM = r.zscore != null ? r.zscore < -0.5
             : (r.vs_10y_pct != null ? r.vs_10y_pct < -10 : null);
  let q;
  if (salesFalling && profFalling) q = 'whether the decline has a floor, and what stops it';
  else if (salesGrowing && profFalling) q = 'what is compressing margins, and whether it is temporary';
  else if (salesFalling) q = 'whether the revenue decline is cyclical or structural';
  else if (stretched) q = 'whether the balance sheet can carry it through a weak year';
  else if (rich && (salesGrowing || profGrowing))
    q = 'whether the growth now in the numbers justifies a multiple above its own history, ' +
        'or whether the market has already priced several good years';
  else if (rich)
    q = 'what the multiple is pricing in, because it sits above this company\'s own norm ' +
        'without the numbers yet explaining why';
  else if (salesGrowing && profGrowing) q = 'what the market is pricing that the filings do not show';
  else if (cheapM) q = 'why the multiple has contracted when the numbers have not';
  else q = 'what changes the multiple from here, because neither the numbers nor the ' +
           'rating are far from this company\'s own normal';
  let close = `The question to answer is ${q}.`;
  if (off != null && off < -35)
    close += ` The shares are ${Math.abs(off).toFixed(0)}% off their 52-week high, so there is ` +
      `likely a specific piece of news behind this rather than a slow drift.`;
  if (up != null && r.target_analysts)
    close += ` Analysts average $${Number(r.target_price).toFixed(0)}, ` +
      `${Math.abs(up).toFixed(0)}% ${up > 0 ? 'above' : 'below'} the current price ` +
      `(${r.target_analysts} of them).`;
  out.push(['The question', close]);
  return out;
}

function openDrawer(t) {
  const r = rows.find(x => x.ticker === t);
  if (!r) return;
  const zeroNote = r.income_from_near_zero ? 'from near zero' : null;
  const concerns = [
    r.rev_growth_yoy != null && r.rev_growth_yoy < 0 ? 'sales shrinking' : null,
    r.current_ratio != null && r.current_ratio < 1 ? 'current ratio below 1' : null,
    r.dilution_1y != null && r.dilution_1y > 3 ? 'issuing shares' : null,
    r.income_cagr_3y != null && r.income_cagr_3y < 0 ? 'profit falling' : null,
    r.fcf_b != null && r.fcf_b < 0 ? 'burning cash' : null,
    r._flagged ? 'data quality flag on this row' : null,
  ].filter(Boolean);

  body.innerHTML = `
    <div class="dhead">
      <h2>${r.ticker}</h2>
      <div class="co">${r.name || ''} &middot; ${r.sector || ''}</div>
      <div class="headline">
        <span class="big ${r.vs_10y_pct > 0 ? 'rich' : ''}">${r.vs_10y_pct == null ? '&mdash;'
          : (r.vs_10y_pct > 0 ? '+' : '') + Number(r.vs_10y_pct).toFixed(0) + '%'}</span>
        <span class="sub2">against its own ten-year norm<br>
          ${num(r.ps_now)}x sales today &middot; ${num(r.ps_med_10y)}x typically &middot;
          Z ${r.zscore == null ? '&mdash;' : Number(r.zscore).toFixed(2)}
          &middot; ${r.years_covered ?? '?'} yrs of data</span>
      </div>
    </div>
    <div class="dbody">
      ${r.description ? `<div class="what">${r.description}${
        r.industry ? `<div class="ind">${r.industry}</div>` : ''}</div>` : ''}

      ${(r.sector === 'Financials' || r.sector === 'Real Estate') ? `<div class="caution">
        Research figures are not computed for banks, insurers or property companies. Every measure
        here is built on revenue, which means something different for them.</div>` : ''}

      ${r.net_income_asof ? `<div class="caution">Profit figures below are from the year to
        ${r.net_income_asof}. The quarters reported since do not add up to it, so they are not
        used. Sales, cash flow and the valuation table are unaffected.</div>` : ''}
      ${(r.research_audit && !r.net_income_asof)
        ? `<div class="caution">${r.research_audit}.</div>` : ''}

      <div class="sect">
        <h3>Can it pay its bills</h3>
        ${row(r, 'current_ratio', 'Current ratio', 2)}
        ${row(r, 'net_debt_to_sales', 'Net debt / sales', 2)}
        ${r.negative_equity
          ? plain('Debt / equity', 'negative equity', 'g-bad')
          : r.thin_equity
            ? plain('Debt / equity', 'almost no book equity left', 'g-bad')
            : row(r, 'debt_to_equity', 'Debt / equity', 2)}
        ${plain('Net debt', money(r.net_debt_b))}
      </div>

      <div class="sect">
        <h3>How profitable is it</h3>
        ${row(r, 'net_margin', 'Net margin', 1, '%')}
        ${r.no_cost_of_sales
          ? plain('Gross margin', 'no cost of sales reported', 'none')
          : row(r, 'gross_margin_pct', 'Gross margin', 1, '%')}
        ${row(r, 'roe', 'Return on equity', 1, '%')}
        ${row(r, 'fcf_b', 'Free cash flow', 2, 'B', '$')}
      </div>

      <div class="sect">
        <h3>What you are paying</h3>
        ${plain('Share price', r.price == null ? '<span class="none">&mdash;</span>'
          : '$' + Number(r.price).toFixed(2))}
        ${plain('Analyst target', r.target_price == null ? '<span class="none">not covered</span>'
          : '$' + Number(r.target_price).toFixed(0) + (r.target_upside == null ? ''
            : ` <span class="${r.target_upside > 0 ? 'g-good' : 'g-bad'}">${r.target_upside > 0 ? '+' : ''}${Number(r.target_upside).toFixed(0)}%</span>`))}
        ${plain('At its 5-year multiple', impliedTo(r, r.ps_med_5y))}
        ${plain('At its 10-year multiple', impliedTo(r, r.ps_med_10y))}
        ${row(r, 'pe', 'Price / earnings', 1)}
        ${plain('Forward P/E', num(r.forward_pe, 1))}
        ${plain('Dividend yield', r.dividend_yield == null ? '<span class="none">none</span>'
          : Number(r.dividend_yield).toFixed(2) + '%')}
      </div>

      <div class="sect">
        <h3>Is the business still growing</h3>
        ${plain('Sales, past year', grow(r.rev_growth_yoy))}
        ${plain('Sales, 3-year annual', grow(r.revenue_cagr_3y))}
        ${plain('Sales, 5-year annual', grow(r.revenue_cagr_5y))}
        ${plain('Profit, 3-year annual', grow(r.income_cagr_3y, 'up', zeroNote))}
        ${plain('Profit, 5-year annual', grow(r.income_cagr_5y, 'up', zeroNote))}
      </div>

      <div class="sect">
        <h3>Is your slice shrinking</h3>
        ${plain('Share count, 1 year', grow(r.dilution_1y, 'down',
          r.share_base_near_zero_1y ? 'listed since' : null))}
        ${plain('Share count, 3 years', grow(r.dilution_3y, 'down',
          r.share_base_near_zero_3y ? 'listed since' : null))}
        ${plain('Share count, 5 years', grow(r.dilution_5y, 'down',
          r.share_base_near_zero_5y ? 'listed since' : null))}
      </div>

      <div class="verdict">
        <h3>What the numbers say</h3>
        ${verdict(r).map(([lab, txt], i, a) =>
          `<div class="vpt${i === a.length - 1 ? ' final' : ''}">
             <div class="vlab">${lab}</div><div class="vtxt">${txt}</div></div>`).join('')}
      </div>
    </div>`;
  drawer.classList.add('on'); scrim.classList.add('on');
}

function closeDrawer(){ drawer.classList.remove('on'); scrim.classList.remove('on'); }

tbody.addEventListener('click', e => {
  const cell = e.target.closest('td.tk');
  if (cell && cell.dataset.t) openDrawer(cell.dataset.t);
});
scrim.addEventListener('click', closeDrawer);
document.getElementById('closeDrawer').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

[qEl, secEl, subEl, capEl].forEach(el => el.addEventListener('input', render));
[growEl, hideEl, finEl, restructEl, shortEl].forEach(el => el.addEventListener('change', render));
render();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Intraday price refresh
#
#  The nightly run does the whole job -- SEC filings, revenue, share counts,
#  the ten-year P/S history -- and is the only thing that reads EDGAR. But a
#  large intraday move (CRM dropped 15% in a day once) changes today's price,
#  and with it market cap, P/S, the z-score and a couple of research figures,
#  while everything historical is unchanged. Re-running the full pipeline every
#  30 minutes to catch that would hammer EDGAR for no reason.
#
#  So the nightly run saves each company's finished daily P/S history and its
#  static fields to state.json in docs/. The light refresh loads that, fetches
#  ONLY today's price in one bulk call, and recomputes exactly the fields a
#  price moves: price, market cap, ps_now, vs-median, z-score, percentile, and
#  the extremes if today set one. The share basis was resolved last night, so
#  ps_now is just yesterday's ps scaled by the price change -- which sidesteps
#  the entire share-count question. ~90 seconds, no EDGAR, no recompute of
#  anything a price cannot touch.
# ---------------------------------------------------------------------------

def save_state(summary: pd.DataFrame, series: dict[str, pd.DataFrame], path: Path):
    """Everything the intraday refresh needs and nothing it does not.

    Per company: the daily P/S and price arrays that the z-score, percentile and
    medians are built from (so the refresh recomputes them identically, not off
    a lossy monthly resample), the last price the history was built at, and the
    static summary row. Written once by the nightly run.
    """
    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(years=10)
    state = {"built": str(date.today()), "companies": {}}
    rows = {r["ticker"]: r for r in summary.to_dict(orient="records")}
    for t, df in series.items():
        if t not in rows:
            continue
        d = df.sort_values("date")
        d10 = d[d["date"] >= cutoff]
        if len(d10) < 200:
            d10 = d
        state["companies"][t] = {
            "row": rows[t],
            "last_price": float(d["price"].iloc[-1]),
            "last_ps": float(d["ps"].iloc[-1]),
            # the ten-year daily P/S distribution the stats rest on, and the
            # matching dates so the five-year window can still be cut
            "ps_hist": [round(float(v), 6) for v in d10["ps"].values],
            "ps_dates": [str(pd.Timestamp(x).date()) for x in d10["date"].values],
        }
    path.write_text(json.dumps(state))
    print(f"  wrote {path.name} for {len(state['companies'])} companies "
          f"({path.stat().st_size/1e6:.1f} MB)")


def _latest_prices(tickers: list[str]) -> dict[str, float]:
    """Today's price per ticker, one bulk download, no cache.

    Uses the most recent close (or live intraday last) from a single yfinance
    call. Yahoo is ~15 minutes delayed, so "now" means a quarter-hour behind --
    which is stated on the page, not hidden.
    """
    import yfinance as yf
    out = {}
    CHUNK = 100
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        try:
            data = yf.download(batch, period="2d", interval="1d",
                               auto_adjust=False, progress=False, threads=True)
        except Exception as exc:
            print(f"  price batch {i//CHUNK} failed: {exc}")
            continue
        close = data["Close"] if "Close" in data else data
        if isinstance(close, pd.Series):          # single ticker returns a Series
            close = close.to_frame(batch[0])
        for t in batch:
            if t in close.columns:
                col = close[t].dropna()
                if len(col):
                    out[t] = float(col.iloc[-1])
    return out


def refresh_prices(tag: str = ""):
    """Load state, reprice on today's quotes, rewrite the outputs. No EDGAR."""
    state_path = OUT / f"docs/state{tag}.json"
    if not state_path.exists():
        state_path = OUT / f"state{tag}.json"
    if not state_path.exists():
        raise SystemExit("No state file. The nightly run writes docs/state.json; "
                         "it must run once before an intraday refresh can reprice.")
    state = json.loads(state_path.read_text())
    companies = state["companies"]
    tickers = sorted(companies)
    print(f"Repricing {len(tickers)} companies from {state_path.name} "
          f"(built {state.get('built')})...")

    prices = _latest_prices(tickers)
    print(f"  got {len(prices)} live prices")

    rows = []
    moved = 0
    for t in tickers:
        c = companies[t]
        row = dict(c["row"])
        new_px = prices.get(t)
        if new_px and c.get("last_price"):
            factor = new_px / c["last_price"]
            if abs(factor - 1) > 1e-4:
                moved += 1
            # scale the price-driven fields; the share basis is fixed from last
            # night, so market cap and P/S move exactly with the price
            row["price"] = new_px
            if row.get("mktcap_b") is not None:
                row["mktcap_b"] = float(row["mktcap_b"]) * factor
            new_ps = float(c["last_ps"]) * factor
            row["ps_now"] = new_ps
            # recompute everything that depends on where today sits in the
            # distribution, against the saved ten-year P/S array
            hist = np.array(c["ps_hist"], dtype=float)
            dates = pd.to_datetime(c["ps_dates"])
            if len(hist) >= 200:
                last10 = hist
                cut5 = dates.max() - pd.DateOffset(years=5)
                last5 = hist[dates >= cut5]
                med10 = float(np.median(last10))
                med5 = float(np.median(last5)) if len(last5) >= 400 else None
                row["ps_med_10y"] = med10
                row["ps_med_5y"] = med5
                row["vs_10y_pct"] = (new_ps / med10 - 1) * 100 if med10 else None
                row["vs_5y_pct"] = (new_ps / med5 - 1) * 100 if med5 else None
                logs = np.log(last10[last10 > 0])
                spread = float(logs.std(ddof=1)) if len(logs) > 1 else 0.0
                row["zscore"] = (float((math.log(new_ps) - logs.mean()) / spread)
                                 if spread > 1e-9 and new_ps > 0 else None)
                row["percentile"] = float((last10 < new_ps).mean() * 100)
                row["ps_min"] = float(min(last10.min(), new_ps))
                row["ps_max"] = float(max(last10.max(), new_ps))
            # Research figures that are themselves price-driven must move too, or
            # the panel shows a fresh price beside a stale P/E. P/E is
            # market-cap over profit and profit is fixed overnight, so it scales
            # with the price exactly like market cap does. The analyst-target
            # upside and the dividend yield are both measured against the price,
            # so they are recomputed from the new one. Everything else in the
            # row -- margins, growth, debt, revenue, the audit -- a price cannot
            # touch, and is carried through unchanged.
            if row.get("pe") is not None:
                row["pe"] = round(float(row["pe"]) * factor, 1)
            if row.get("target_price") not in (None, "", 0) and new_px:
                try:
                    row["target_upside"] = (float(row["target_price"]) / new_px - 1) * 100
                except (TypeError, ValueError):
                    pass
            if row.get("dividend_per_share") not in (None, "", 0) and new_px:
                try:
                    row["dividend_yield"] = float(row["dividend_per_share"]) / new_px * 100
                except (TypeError, ValueError):
                    pass
        rows.append(row)

    summary = pd.DataFrame(rows)
    print(f"  {moved} prices moved since the state was built")

    # stamp the refresh time so the page shows prices are live
    os.environ["PRICE_REFRESH_AT"] = _now_et()

    summary.to_csv(OUT / f"ps_screen{tag}.csv", index=False)
    write_html(summary, OUT / f"ps_screen{tag}.html")
    print(f"  repriced and rewrote ps_screen.html / ps_screen.csv")


def main():
    ap = argparse.ArgumentParser()
    # The scheduled run supplies this as the SEC_EMAIL environment variable, not
    # as a flag, and it has to keep working that way: anything on a command line
    # is echoed into the Actions log in clear text, while a value injected from
    # a repository secret through env is masked. Requiring the flag meant the
    # workflow died on argparse in under a second, every night, without ever
    # reaching the SEC. Accept either, and prefer the flag when both are given
    # so a local run can override.
    ap.add_argument("--email", default=os.environ.get("SEC_EMAIL", ""),
                    help="Contact email for the SEC User-Agent header (required by SEC). "
                         "Falls back to the SEC_EMAIL environment variable.")
    ap.add_argument("--limit", type=int, help="Only process the first N tickers (for testing).")
    ap.add_argument("--tag", default="",
                    help="Suffix for the output filenames. The quick run uses this so "
                         "chasing one ticker cannot overwrite the real screen.")
    ap.add_argument("--only", default="",
                    help="Comma-separated tickers to run and nothing else. Chasing one "
                         "misclassified split should not cost a full universe.")
    ap.add_argument("--refresh", action="store_true", help="Ignore the EDGAR cache.")
    ap.add_argument("--years", type=int, default=HISTORY_YEARS)
    ap.add_argument("--watch", default="",
                    help="Comma-separated tickers to diagnose even if they pass every check.")
    ap.add_argument("--refresh-prices", action="store_true",
                    help="Intraday mode: reprice from docs/state.json on today's "
                         "quotes and rewrite the outputs. No EDGAR, no recompute "
                         "of anything a price cannot move.")
    ap.add_argument("--verify", type=int, default=10_000,
                    help="Cross-check the N cheapest rows against Yahoo. Default: all. 0 to skip.")
    args = ap.parse_args()

    # Intraday refresh is a separate, lightweight path: it never touches EDGAR
    # and returns as soon as the outputs are rewritten.
    if getattr(args, "refresh_prices", False):
        refresh_prices(f"_{args.tag}" if args.tag else "")
        return

    if "@" not in (args.email or ""):
        ap.error(
            "no contact email. The SEC rejects requests without one.\n"
            "  In GitHub Actions: check that the repository secret SEC_EMAIL exists and is\n"
            "  passed to the run step as an environment variable.\n"
            "  Running it yourself: add --email you@example.com")

    edgar = Edgar(args.email)

    print("Fetching S&P 500 constituents...")
    const = sp500_constituents()
    if args.only:
        wanted = {w.strip().upper() for w in args.only.split(",") if w.strip()}
        const = const[const["ticker"].str.upper().isin(wanted)]
        missing = wanted - set(const["ticker"].str.upper())
        if missing:
            print(f"  not in the index: {', '.join(sorted(missing))}")
    elif args.limit:
        const = const.head(args.limit)
    print(f"  {len(const)} tickers")

    print("Mapping tickers to CIKs...")
    cik_map = edgar.ticker_to_cik()

    tickers = [t for t in const["ticker"] if t in cik_map]
    missing = sorted(set(const["ticker"]) - set(tickers))

    print("Downloading prices...")
    closes, splits = fetch_prices(
        tickers, date.today() - timedelta(days=365 * args.years + 400),
        refresh=args.refresh)

    print("Pulling EDGAR fundamentals and building P/S history...")
    meta = const.set_index("ticker")
    results, series, diagnostics, research_rows = [], {}, [], {}
    _no_cik = list(missing)
    suppressed: list[tuple[str, str]] = []
    # A constituent used to be able to leave the screen without a trace. Five of
    # the six exits below were bare `continue`s, so 69 companies -- Alphabet,
    # Meta, Exxon, PepsiCo and Costco among them -- were simply absent from a
    # page that looked complete. Nothing recorded that they had ever been
    # considered, so the only way to notice was to go looking for a name you
    # expected to see. Every exit now writes down which company, which stage,
    # and why.
    dropped: list[tuple[str, str, str]] = [
        (t, "no CIK", "the ticker is not in the SEC's company_tickers.json map, "
                      "usually a ticker change the index list has not caught up with")
        for t in _no_cik]
    # The Yahoo check cannot run until every ticker is priced, so a row that
    # only fails THERE gets no diagnostics block -- 13 of the last 20
    # disagreements, including Marriott at +264%, were invisible for that
    # reason alone. Keep each concept table so those blocks can be written
    # once the verdicts exist.
    concept_tables: dict[str, str] = {}
    watch = {w.strip().upper() for w in (args.watch or "").split(",") if w.strip()}

    for n, t in enumerate(tickers, 1):
        if n % 25 == 0:
            print(f"  {n}/{len(tickers)}")
        if t not in closes:
            dropped.append((t, "no price history",
                            "the price download returned nothing for this ticker"))
            continue
        facts = edgar.company_facts(cik_map[t], refresh=args.refresh)
        if not facts:
            dropped.append((t, "no EDGAR filings",
                            f"companyfacts for CIK {cik_map[t]} came back empty"))
            continue

        periods = collect_periods(facts, "us-gaap", REVENUE_TAGS)
        quarters = derive_quarters(periods)
        ttm = trailing_twelve(quarters, periods)
        if ttm.empty:
            # Zero periods is a different situation from "revenue exists but did
            # not form a series". A constituent that reports NO revenue of any
            # kind is usually a when-issued ticker Wikipedia lists during a
            # corporate action before the entity files -- FedEx's freight
            # spin-off (FDXF) and Honeywell's break-up (HONA) appeared this way,
            # with real filings still under the parents FDX and HON. Saying so
            # keeps a routine index artefact from reading like a data fault.
            # Zero PERIODS -- not zero concepts -- is the phantom signature. A
            # when-issued ticker can carry an empty revenue concept shell in its
            # filing skeleton yet report no actual period, so keying on periods
            # (FedEx freight FDXF, Honeywell break-up HONA) is what separates a
            # not-yet-trading entity from a real collection fault.
            if not periods:
                dropped.append((t, "not yet filing",
                                "reports no revenue period of any kind -- typically a "
                                "when-issued or spin-off ticker the index list added "
                                "before the entity began filing with the SEC"))
            else:
                dropped.append((t, "no revenue series",
                                f"{len(periods)} period(s) and {len(quarters)} quarter(s) "
                                f"survived collection from {len(revenue_concepts)} revenue "
                                f"concept(s), not enough to form a trailing twelve"))
            continue

        # Pick ONE concept whole, in priority order, and never blend two.
        # Blending was the old 10x/20x error: dei is dated by filing and never
        # restated for splits, us-gaap is dated by period end and IS restated,
        # so mixing them and then applying a split factor double-counts it --
        # that is what made Supermicro, NetApp and HP appear to jump ~900% in a
        # day. So each concept is collected on its own, and the first one that
        # is present and current wins outright.
        #
        # Diluted leads because it is the consolidated count (see
        # SHARE_MARKETCAP_TAGS). dei is tried LAST, not first as before: for a
        # multi-class filer the cover page lists shares per class, and for Nike
        # it stopped in 2015, so preferring it was choosing the worst option in
        # exactly the cases that matter.
        shares, share_src = None, None
        for concept in SHARE_MARKETCAP_TAGS:
            got = collect_instants(facts, "us-gaap", [concept])
            if got and (date.today() - got[-1][0]).days <= SHARE_STALE_DAYS:
                shares, share_src = got, concept
                break
        if shares is not None:
            # Repair a power-of-ten units slip confirmed by an independent
            # concept (Waters' 1000x post-merger count) before it is trusted.
            shares = _correct_units_error(shares, facts)
            # Diluted is often only tagged from ~2022; extend it backward from a
            # longer concept so a decade-old filer is not dropped for want of
            # five years of share history (the Alphabet case).
            shares = _splice_share_history(shares, share_src, facts)
        if shares is None:
            dei_got = collect_instants(facts, "dei", SHARE_TAGS_DEI)
            if dei_got and (date.today() - dei_got[-1][0]).days <= SHARE_STALE_DAYS:
                shares, share_src = dei_got, "dei:EntityCommonStockSharesOutstanding"
        if shares is None:
            # Distinguish "nothing usable" from "only a stale figure", because
            # they need different fixes -- the first is a missing concept, the
            # second (Berkshire) needs a non-EDGAR source.
            any_share = collect_instants(facts, "us-gaap", SHARE_MARKETCAP_TAGS) or \
                        collect_instants(facts, "dei", SHARE_TAGS_DEI)
            if any_share:
                age = (date.today() - any_share[-1][0]).days
                dropped.append((t, "share count stale",
                                f"the newest consolidated share count is {age} days old "
                                f"({any_share[-1][0]}); a current one is only filed per "
                                f"share class, which companyfacts omits"))
            else:
                dropped.append((t, "no share count",
                                "no consolidated share concept is reported; the count "
                                "exists only as per-class dimensioned facts"))
            continue

        ps_module = sys.modules[__name__]
        ps_module.CURRENT_TICKER = t
        ps_module.CURRENT_SHARE_SRC = share_src
        hist = monthly_ps(closes[t], ttm, shares, splits.get(t), args.years)
        if hist.empty:
            dropped.append((t, "no overlapping history",
                            "prices and revenue exist but never line up over the window, "
                            "or too few months survived the staleness screen"))
            continue

        # Gross-margin columns were dropped from the table; only growth is used.
        m_now, m_then, growth = None, None, revenue_growth(ttm)
        actions = detect_corporate_actions(hist, hist.attrs.get("splits", {}))
        audit = audit_series(hist, ttm, hist.attrs.get("splits", {}), actions)
        concept_tables[t] = "\n".join(revenue_concept_table(facts, periods))

        # A row whose market cap or revenue is broken outright cannot be shown
        # with a Z-score and a percentile beside honest ones -- Erie ranked as
        # the 78th cheapest name of 462 on a market cap of $0.001bn. Drop it
        # from the table but always dump the working, so the reason is visible.
        if audit_is_fatal(audit):
            suppressed.append((t, "; ".join(audit)))
            diagnostics.append(diagnose(t, facts, periods, quarters, ttm, hist,
                                        shares, hist.attrs.get("splits", {}),
                                        actions, audit, None))
            continue

        row = summarize(t, meta.loc[t, "name"], meta.loc[t, "sector"],
                        hist, m_now, m_then, growth, pct_off_52w_high(closes[t]),
                        actions, audit)
        # Financials and Real Estate stay out of the default P/S view -- a
        # bank's price-to-sales is not comparable to an industrial's -- but the
        # research panel is not blanked any more. research() is run with the
        # sector so it shows the figures that ARE valid for a bank or REIT
        # (P/E, ROE, net income, debt, dilution, free cash flow, profit growth)
        # and suppresses only the revenue-based ones. This is the same code path
        # as every other sector below, just with the sector passed through, so
        # the previously empty panels now populate.
        try:
            rsch = research(t, facts, hist, ttm,
                            row.get('mktcap_b') if row else None,
                            sector=meta.loc[t, "sector"])
            rsch["research_audit"] = "; ".join(audit_research(rsch)) or None
            research_rows[t] = rsch
        except Exception as e:
            research_rows[t] = {"ticker": t, "research_audit": f"failed: {str(e)[:70]}"}
        if audit or t in watch or research_rows[t].get("research_audit"):
            diagnostics.append(diagnose(t, facts, periods, quarters, ttm, hist,
                                        shares, hist.attrs.get("splits", {}),
                                        actions, audit, row))
        if row:
            results.append(row)
            series[t] = hist
        else:
            dropped.append((t, "no summary row",
                            "summarize() returned nothing -- usually fewer than "
                            f"{MIN_MONTHS_FOR_STATS} usable months in the window"))

    if not results:
        raise SystemExit("No rows built. Try --limit 5 to debug a small batch.")

    save_facts_cache()   # one write, after the loop has finished with them
    summary = pd.DataFrame(results).sort_values("zscore").reset_index(drop=True)

    if args.verify:
        # The cheapest rows are the ones acted on, and a corrupted historical
        # scale always sorts to the top -- so verify from that end first.
        targets = summary.nsmallest(args.verify, "zscore")["ticker"].tolist()
        print(f"\nCross-checking {len(targets)} rows against Yahoo...")
        summary = apply_cross_check(summary, series,
                                    cross_check(targets, refresh=args.refresh))
        bad = (summary["xc_verdict"].notna()
               & ~summary["xc_verdict"].isin(["agrees", "unchecked"])).sum()
        ok = (summary.xc_verdict == "agrees").sum()
        miss = (summary.xc_verdict == "unchecked").sum()
        # Report the rate over rows actually CHECKED. Dividing by the total made
        # a failed lookup look like a disagreement and sent me chasing a
        # regression that was never there.
        denom = ok + bad
        print(f"  {ok} agree, {bad} disagree, {miss} could not be checked"
              + (f"  ({ok / denom * 100:.1f}% agreement among those checked)" if denom else ""))
    else:
        summary["xc_revenue_diff"] = None
        summary["xc_mktcap_diff"] = None
        summary["xc_verdict"] = "unchecked"

    # Attach the research figures BEFORE writing anything. They used to be
    # merged after write_html, so the page was rendered from a frame that did
    # not have them yet -- the CSV looked right and every research panel in the
    # HTML was empty.
    if research_rows:
        summary = summary.merge(pd.DataFrame(research_rows.values()),
                                on="ticker", how="left")
        summary = rate_against_sector(summary)

    tag = f"_{args.tag}" if args.tag else ""
    summary.to_csv(OUT / f"ps_screen{tag}.csv", index=False)
    write_sqlite(summary, series, OUT / f"ps_screen{tag}.db")
    write_html(summary, OUT / f"ps_screen{tag}.html")
    # Save the state the intraday refresh reprices from. Written into docs/ so
    # the publish step commits it alongside the page.
    (OUT / "docs").mkdir(exist_ok=True)
    save_state(summary, series, OUT / f"docs/state{tag}.json")

    audited = summary["audit"].notna().sum()
    xc_bad = (~summary["xc_verdict"].isin(["agrees", "unchecked"])).sum()
    print("\n" + "=" * 62)
    print("DATA QUALITY")
    print("=" * 62)
    print(f"  {len(summary) - audited:>4} of {len(summary)} pass every structural check")
    print(f"  {audited:>4} have a structural problem in their history")
    print(f"  {(summary.xc_verdict == 'agrees').sum():>4} independently confirmed against Yahoo")
    print(f"  {xc_bad:>4} disagree with Yahoo on today's figures")
    if REJECTED_RATIOS:
        # This belongs in the file, not only on screen. Printing it to the
        # notebook meant the one question the run existed to answer -- did the
        # new ratio test throw away a real split? -- was not in anything I
        # could look at afterwards.
        block = ["", "=" * 78,
                 "PRICE FACTORS NOT APPLIED TO ANY SHARE COUNT",
                 "=" * 78, "",
                 "Yahoo files spin-offs and special distributions in the same column as",
                 "splits, and sometimes omits a real split entirely. Every disagreement",
                 "between Yahoo and the company's own cover-page share count is listed",
                 "here with the evidence.",
                 "",
                 "  SPLIT ADDED  = EDGAR reports an exchange ratio Yahoo does not have.",
                 "  everything else = a factor Yahoo filed that the share count refuses.",
                 "",
                 f"{'tkr':<7}{'date':<13}{'ratio':>9}   why"]
        for tk, when, ratio, why in REJECTED_RATIOS:
            block.append(f"{tk:<7}{when:<13}{ratio:>9.4f}   {why}")
        diagnostics.append("\n".join(block))
        print(f"\n  {len(REJECTED_RATIOS)} price factor(s) treated as corporate actions "
              f"rather than splits (listed in ps_diagnostics.txt)")

    # Every constituent is accounted for: published, suppressed as unusable, or
    # dropped with a reason. If these three do not sum to the index, the screen
    # is incomplete in a way nobody asked about.
    print(f"\n  {len(const)} constituents -> {len(summary)} published, "
          f"{len(suppressed)} suppressed as unusable, {len(dropped)} dropped")
    if dropped:
        stages: dict[str, list[str]] = {}
        for tk, stage, _why in dropped:
            stages.setdefault(stage, []).append(tk)
        for stage, tks in sorted(stages.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(tks):>3}  {stage}: {', '.join(sorted(tks))}")
        block = ["=" * 78,
                 "CONSTITUENTS THAT NEVER REACHED THE SCREEN",
                 "=" * 78, ""]
        for tk, stage, why in sorted(dropped):
            block.append(f"{tk:<8}{stage:<24}{why}")
        diagnostics.insert(0, "\n".join(block))

    # A standing reliability report, built from the cross-check that already
    # runs: every published row whose market cap -- computed here as SEC shares
    # x Yahoo price -- disagrees with Yahoo's own reported market cap by more
    # than the tolerance. Two independent derivations of the same number, so a
    # gap is real evidence something is off (a stale or wrong share count, a
    # multi-class miscount), not noise. Collected in one place and sorted by
    # size so the figure can be watched run to run instead of hunted for a row
    # at a time -- this is the free "double check" a paid second market-cap feed
    # would have provided, using data already fetched.
    if "xc_mktcap_diff" in summary and "xc_revenue_diff" in summary:
        MKTCAP_TOL, REV_TOL = 12.0, 15.0
        cap_off = summary[summary["xc_mktcap_diff"].abs() > MKTCAP_TOL].copy()
        rev_off = summary[summary["xc_revenue_diff"].abs() > REV_TOL].copy()
        checked = int((summary["xc_verdict"] != "unchecked").sum())
        lines = ["=" * 78,
                 "RELIABILITY: WHERE OUR NUMBERS DISAGREE WITH YAHOO",
                 "=" * 78,
                 f"{checked} rows cross-checked against Yahoo's independently "
                 f"reported figures.",
                 f"{len(cap_off)} disagree on market cap by more than {MKTCAP_TOL:.0f}%, "
                 f"{len(rev_off)} on revenue by more than {REV_TOL:.0f}%.",
                 "A market-cap gap points at the share count; a revenue gap at the "
                 "concept merge.", ""]
        if len(cap_off):
            lines.append("MARKET CAP (ours = SEC shares x Yahoo price, vs Yahoo's own):")
            cap_off = cap_off.reindex(cap_off["xc_mktcap_diff"].abs()
                                      .sort_values(ascending=False).index)
            for _, r in cap_off.iterrows():
                lines.append(f"  {r['ticker']:<7}{r['xc_mktcap_diff']:+6.0f}%   "
                             f"ours ${r.get('mktcap_b', float('nan')):,.1f}B")
            lines.append("")
        if len(rev_off):
            lines.append("REVENUE (ours = trailing twelve from filings, vs Yahoo's):")
            rev_off = rev_off.reindex(rev_off["xc_revenue_diff"].abs()
                                      .sort_values(ascending=False).index)
            for _, r in rev_off.iterrows():
                lines.append(f"  {r['ticker']:<7}{r['xc_revenue_diff']:+6.0f}%")
            lines.append("")
        # Print a one-line summary to the run log too, so the number is visible
        # without opening the file.
        print(f"\n  reliability: {len(cap_off)} market-cap and {len(rev_off)} "
              f"revenue disagreements with Yahoo beyond tolerance "
              f"(of {checked} checked)")
        diagnostics.insert(0, "\n".join(lines))

    if suppressed:
        print(f"\n  {len(suppressed)} row(s) dropped as unusable rather than shown "
              f"with a fabricated rank:")
        for t, why in suppressed:
            print(f"    {t:<6} {why[:88]}")
    if audited:
        print("\n  Worst offenders by size:")
        bad = summary[summary["audit"].notna()].nlargest(8, "mktcap_b")
        for _, r in bad.iterrows():
            print(f"    {r.ticker:<6} {r.audit[:88]}")

    if research_rows:
        have = summary["current_ratio"].notna().sum() if "current_ratio" in summary else 0
        rbad = summary["research_audit"].notna().sum() if "research_audit" in summary else 0
        print(f"  Research figures on {have} of {len(summary)} rows — click any "
              f"ticker in the table to open them")
        if rbad:
            print(f"  {rbad} rows have implausible research figures (working in "
                  f"ps_diagnostics.txt)")

    if diagnostics:
        dis = summary[~summary["xc_verdict"].isin(["agrees", "unchecked"])]
        already = {d.split("\n")[1] for d in diagnostics if d.count("\n") > 1}
        for _, r in dis.sort_values("mktcap_b", ascending=False).iterrows():
            if r.ticker in already or r.ticker not in concept_tables:
                continue
            hist = series.get(r.ticker)
            myrev = float(hist["ttm"].iloc[-1]) if hist is not None and len(hist) else 0.0
            rd, sd = r.get("xc_revenue_diff"), r.get("xc_mktcap_diff")
            yrev = myrev / (1 + rd / 100) if rd is not None and rd == rd else None
            ycap = r.mktcap_b * 1e9 / (1 + sd / 100) if sd is not None and sd == sd else None
            diagnostics.append("\n".join([
                "=" * 78, r.ticker, "=" * 78, "",
                "FLAGGED FOR:",
                f"  - {r.xc_verdict}",
                "",
                f"P/S now {r.ps_now:.2f} | 5y med {r.ps_med_5y or 0:.2f} "
                f"| 10y med {r.ps_med_10y or 0:.2f} | Z {r.zscore}",
                f"  my revenue  ${myrev/1e9:>10,.2f}B" +
                (f"   Yahoo ${yrev/1e9:>10,.2f}B" if yrev else "   Yahoo (none)"),
                f"  my mktcap   ${r.mktcap_b:>10,.2f}B" +
                (f"   Yahoo ${ycap/1e9:>10,.2f}B" if ycap else "   Yahoo (none)"),
                "  (structurally clean -- this block exists only because the two "
                "sources disagree)",
                concept_tables[r.ticker], ""]))

        # diagnose() runs inside the per-ticker loop, before the Yahoo check has
        # been made, which is why every block says "cross-check: None". Append
        # the answers at the end rather than reorder the pipeline.
        tail = ["", "=" * 78, "CROSS-CHECK AGAINST YAHOO — every row that disagrees",
                "=" * 78, "",
                f"{'tkr':<7}{'sector':<24}{'my revenue':>13}{'my mktcap':>12}"
                f"{'rev diff':>10}{'cap diff':>10}"]
        for _, r in dis.sort_values("mktcap_b", ascending=False).iterrows():
            hist = series.get(r.ticker)
            myrev = float(hist["ttm"].iloc[-1]) / 1e9 if hist is not None and len(hist) else float("nan")
            rd = r.get("xc_revenue_diff"); sd = r.get("xc_mktcap_diff")
            tail.append(f"{r.ticker:<7}{str(r.sector)[:23]:<24}{myrev:>12,.1f}B"
                        f"{r.mktcap_b:>11,.1f}B"
                        f"{(f'{rd:+.0f}%' if rd is not None and rd == rd else '-'):>10}"
                        f"{(f'{sd:+.0f}%' if sd is not None and sd == sd else '-'):>10}")
        diagnostics.append("\n".join(tail))
        (OUT / f"ps_diagnostics{tag}.txt").write_text("\n".join(diagnostics))
        print(f"\n  Wrote ps_diagnostics.txt — full working for {len(diagnostics)} companies")

    print(f"\n{len(summary)} stocks screened. Cheapest vs. their own history:\n")
    cols = ["ticker", "sector", "ps_now", "ps_med_10y", "vs_10y_pct", "zscore", "rev_growth_yoy"]
    print(summary[cols].head(15).to_string(index=False))
    print("\nWrote ps_screen.csv, ps_screen.db, ps_screen.html"
          + (", ps_diagnostics.txt" if diagnostics else ""))


if __name__ == "__main__":
    main()
