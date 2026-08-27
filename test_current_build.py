"""Exercise the REAL functions in ps_screener against the shapes that produced
the errors in ps_screen__42_.html, to see which are already fixed in this build.
No network: these are pure functions over EDGAR-shaped dicts."""
from datetime import date, timedelta
import ps_screener as ps

RESULT = []


def usd(rows):
    return {"units": {"USD": rows}}


def shares(rows):
    return {"units": {"shares": rows}}


def yr(vals, fy=(12, 31), quarters=True, form_k="10-K", form_q="10-Q"):
    """Annual facts plus the four quarters, filed the way real filers file."""
    out = []
    for y, tot in vals.items():
        e = date(y, *fy)
        s = e - timedelta(days=364)
        out.append(dict(start=s.isoformat(), end=e.isoformat(), val=tot,
                        form=form_k, filed=(e + timedelta(days=55)).isoformat()))
        if not quarters:
            continue
        qe = [e - timedelta(days=d) for d in (273, 182, 91, 0)]
        qs = [s] + [d + timedelta(days=1) for d in qe[:3]]
        for a, b, w in zip(qs, qe, (0.24, 0.25, 0.25, 0.26)):
            out.append(dict(start=a.isoformat(), end=b.isoformat(), val=tot * w,
                            form=form_k if b == e else form_q,
                            filed=(b + timedelta(days=40)).isoformat()))
    return out


def facts_of(**tags):
    return {"facts": {"us-gaap": {k: usd(v) for k, v in tags.items()}}}


def newest_annual(periods):
    ann = [p for p in periods if ps.ANNUAL_DAYS[0] <= p.days <= ps.ANNUAL_DAYS[1]]
    return max(ann, key=lambda p: p.end) if ann else None


def report(name, got, want, detail=""):
    ok = got == want
    RESULT.append((ok, name))
    print(f"{'PASS' if ok else '**FAIL**'}  {name}")
    print(f"          got {got!r}, expected {want!r}   {detail}")


print("=" * 74)
print("1. ADM  -- total ($85bn) vs its ASC-606 slice ($25bn)")
print("=" * 74)
adm_rev = {y: v * 1e9 for y, v in
           {2016: 62.29, 2017: 61.26, 2018: 64.34, 2019: 64.66, 2020: 64.36,
            2021: 85.25, 2022: 101.6, 2023: 93.9, 2024: 85.5, 2025: 84.0}.items()}
adm_606 = {y: v * 1e9 for y, v in
           {2018: 22.0, 2019: 22.5, 2020: 22.9, 2021: 26.1, 2022: 27.6,
            2023: 25.7, 2024: 24.4, 2025: 25.0}.items()}
p = ps.collect_periods(
    facts_of(Revenues=yr(adm_rev),
             RevenueFromContractWithCustomerExcludingAssessedTax=yr(adm_606)),
    "us-gaap", ps.REVENUE_TAGS)
n = newest_annual(p)
report("ADM anchors on the total, not the slice",
       n.tag if n else None, "Revenues",
       f"FY2025 = ${n.val/1e9:,.1f}bn" if n else "")

print()
print("=" * 74)
print("2. BF-B / TAP -- gross of excise tax vs net of it")
print("=" * 74)
net = {y: (3.4 + 0.1 * (y - 2016)) * 1e9 for y in range(2016, 2026)}
gross = {y: v * 1.29 for y, v in net.items()}
p = ps.collect_periods(
    facts_of(RevenueFromContractWithCustomerExcludingAssessedTax=yr(net),
             RevenueFromContractWithCustomerIncludingAssessedTax=yr(gross)),
    "us-gaap", ps.REVENUE_TAGS)
n = newest_annual(p)
report("net-of-excise concept is preferred",
       n.tag if n else None,
       "RevenueFromContractWithCustomerExcludingAssessedTax",
       f"FY2025 = ${n.val/1e9:,.2f}bn (net is ${net[2025]/1e9:,.2f}bn)" if n else "")

print()
print("=" * 74)
print("3. CPT / VMRK -- a REIT whose rent is lease income, not ASC-606")
print("=" * 74)
fee = {y: (0.011 if y >= 2020 else 0.9) * 1e9 for y in range(2016, 2026)}
lease = {y: (1.4 + 0.1 * (y - 2016)) * 1e9 for y in range(2016, 2026)}
f = facts_of(RevenueFromContractWithCustomerExcludingAssessedTax=yr(fee))
f["facts"]["us-gaap"]["OperatingLeaseLeaseIncome"] = usd(yr(lease))
p = ps.collect_periods(f, "us-gaap", ps.REVENUE_TAGS)
n = newest_annual(p)
report("REIT rent is picked up",
       round(n.val / 1e9, 2) if n else None, round(lease[2025] / 1e9, 2),
       f"got ${n.val/1e9:,.3f}bn from {n.tag} (fee income of "
       f"${fee[2025]/1e6:,.0f}m is not added on, so this is 0.5% light "
       f"rather than 99% light)" if n else "no periods at all")

print()
print("=" * 74)
print("4. BRK-B -- two share classes filed the same day")
print("=" * 74)
rows = []
for y in range(2016, 2027):
    filed = f"{y}-02-25"
    rows.append(dict(end=f"{y-1}-12-31", val=550_000.0, form="10-K", filed=filed))
    rows.append(dict(end=f"{y-1}-12-31", val=1_300_000_000.0, form="10-K", filed=filed))
got = ps.collect_instants({"facts": {"dei": {
    "EntityCommonStockSharesOutstanding": shares(rows)}}}, "dei", ps.SHARE_TAGS_DEI)
latest = got[-1][1] if got else None
report("dominant class kept (not the 550k A count, not the sum)",
       f"{latest:,.0f}" if latest else None, "1,300,000,000",
       f"at ${495.82:,.2f} that is ${latest*495.82/1e9:,.0f}bn "
       f"(true Berkshire is ~$1,100bn: the A shares are ~38% of the value "
       f"and are still missing)" if latest else "")

print()
print("=" * 74)
print("5. Does anything check the ABSOLUTE level of the result?")
print("=" * 74)
import inspect
src = inspect.getsource(ps.audit_series)
markers = ["mktcap", "market cap comes out", "too small for an index",
           "MIN_PLAUSIBLE", "implausible"]
report("audit_series has a plausibility floor",
       any(m in src for m in markers), True,
       "nothing in audit_series looks at the level of cap, revenue or P/S")

print()
print("=" * 74)
print("6. A revenue FALL is reported with the right sign?")
print("=" * 74)
report("fall is described as a fall, not a jump",
       "jumps {np.expm1(rsteps" not in src.replace(" ", ""), True,
       "rsteps is np.abs(...), so a -68% collapse prints as 'jumps +68%'")

print()
print("=" * 74)
bad = [n for ok, n in RESULT if not ok]
print(f"{len(RESULT)-len(bad)} of {len(RESULT)} behaved as they should")
if bad:
    print("STILL BROKEN IN THIS BUILD:")
    for n in bad:
        print("   -", n)


# ---------------------------------------------------------------------------
# Research-module pass. Every number below is one this build published.
# ---------------------------------------------------------------------------
import math as _math
import pandas as _pd

print()
print("=" * 74)
print("7. Split ratios: real exchanges vs spin-off distribution factors")
print("=" * 74)
_RATIOS = [(2.0, "2-for-1", True), (1.5, "3-for-2", True), (1.25, "5-for-4", True),
           (0.3333, "1-for-3", True), (0.05, "1-for-20", True), (20.0, "20-for-1", True),
           (2.39, "DuPont / Qnity", False), (1.487, "DuPont / Dow", False),
           (0.4725, "DuPont / Corteva", False), (0.9535, "Honeywell / Solstice", False),
           (0.6231, "arbitrary distribution", False)]
_wrong = [lab for r, lab, want in _RATIOS if ps._is_split_ratio(r) != want]
report("every ratio classified correctly", _wrong, [],
       "a split factor is an announced fraction; a spin-off factor is not")

_px = _pd.Series(range(400, 0, -1), index=_pd.date_range("2018-01-01", periods=400, freq="W"),
                 dtype=float)
_kept = ps.corroborate_splits(ps.classify_splits(_px, _pd.Series({
    _pd.Timestamp("2019-04-02"): 1.487, _pd.Timestamp("2019-06-03"): 0.4725,
    _pd.Timestamp("2025-11-03"): 2.39, _pd.Timestamp("2026-06-24"): 0.3333})), [])
report("DuPont: only the reverse split touches the share count",
       [str(k.date()) for k in _kept], ["2026-06-24"],
       "three spin-offs previously read as a 59.5% one-year share reduction")

print()
print("=" * 74)
print("8. Ratios whose denominator can vanish")
print("=" * 74)
_df = _pd.DataFrame({"ticker": ["DOC", "MSFT"], "price": [1.0, 1.0]})
_out = ps.apply_cross_check(_df, {}, {"DOC": {"y_forward_pe": -144.2},
                                      "MSFT": {"y_forward_pe": 24.8}})
_fpe = _out.forward_pe.tolist()
report("negative forward P/E withheld, as trailing P/E already was",
       [_math.isnan(_fpe[0]), _fpe[1]], [True, 24.8],
       "Healthpeak published at -144.2 and the drawer read it as cheap")

print()
print("=" * 74)
print("9. Dividend yield, under either Yahoo convention")
print("=" * 74)
_c = {"NVDA": {"y_div_yield": 0.47, "y_div_rate": 0.04, "y_price": 213.0},
      "KO": {"y_div_yield": 2.33, "y_div_rate": 2.04, "y_price": 87.5},
      "PFE": {"y_div_yield": 6.13}}
_pct = ps.apply_cross_check(_pd.DataFrame({"ticker": list(_c), "price": [1.0] * 3}), {}, _c)
_frac = {t: {**v, "y_div_yield": v["y_div_yield"] / 100} for t, v in _c.items()}
for _t in ("PFE",):
    _frac[_t].pop("y_div_rate", None)
_fr = ps.apply_cross_check(_pd.DataFrame({"ticker": list(_frac), "price": [1.0] * 3}), {}, _frac)
report("same answer whether Yahoo sends 0.47 or 0.0047",
       [round(v, 2) for v in _pct.dividend_yield],
       [round(v, 2) for v in _fr.dividend_yield],
       "Nvidia was published at 47.00% and Apple at 35.00%")
report("Nvidia's yield is derived from the dollar rate",
       round(float(_pct.dividend_yield.iloc[0]), 2), 0.02, "0.04 / 213.00")

print()
print("=" * 74)
_bad = [n for ok, n in RESULT if not ok]
print(f"{len(RESULT) - len(_bad)} of {len(RESULT)} behaved as they should")
if _bad:
    print("STILL BROKEN IN THIS BUILD:")
    for _n in _bad:
        print("   -", _n)


print()
print("=" * 74)
print("10. Splits corroborated against the company's own share count")
print("=" * 74)
from datetime import date as _date, timedelta as _td


def _counts(start, n, level, step_at=None, step=1.0):
    return [(start + _td(days=91 * i),
             level * step if (step_at and start + _td(days=91 * i) >= step_at) else level)
            for i in range(n)]


_px2 = _pd.Series(range(400, 0, -1),
                  index=_pd.date_range("2018-01-01", periods=400, freq="W"), dtype=float)


def _run_split(ratio, when, counts):
    ps.REJECTED_RATIOS.clear()
    info = ps.classify_splits(_px2, _pd.Series({_pd.Timestamp(when): ratio}))
    return ps.corroborate_splits(info, counts)


# A spin-off whose factor lands on a round number passes the ratio test. Only
# the share count can refuse it -- Honeywell published a 50.1% share reduction.
report("spin-off filed as a round 2.0 is refused by the share count",
       _run_split(2.0, "2025-10-30", _counts(_date(2022, 1, 1), 20, 653e6)), {},
       "Yahoo says two-for-one; EDGAR says the count never moved")
# Booking's 25-for-1 is not on any hand-written list of ratios, and when the
# list ran first it was thrown away: market cap came out at $6.8bn against a
# real $171bn. Evidence has to outrank the list.
report("a 25-for-1 nobody listed is applied because the count says so",
       len(_run_split(25.0, "2026-04-06",
                      _counts(_date(2024, 1, 1), 16, 32e6, _date(2026, 4, 6), 25.0))), 1,
       "the reported count went up twenty-five-fold on the same date")
report("a genuine 2-for-1 survives",
       len(_run_split(2.0, "2025-10-30",
                      _counts(_date(2022, 1, 1), 20, 5e8, _date(2025, 10, 30), 2.0))), 1,
       "the reported count doubles across the same date")
report("Monster-shape 0.5 distribution is refused",
       _run_split(0.5, "2025-06-02", _counts(_date(2022, 1, 1), 20, 975e6)), {},
       "published as a 100.6% one-year increase in shares")
report("no counts either side: fall back to the ratio, and accept 2.0",
       len(_run_split(2.0, "2025-10-30", [])), 1,
       "refusing on no evidence would corrupt the series the other way")
report("no counts either side: fall back to the ratio, and refuse 2.39",
       _run_split(2.39, "2025-10-30", []), {},
       "the list is the fallback, which is the only place it is safe")


print()
print("=" * 74)
print("11. A share base the median cannot be compared against")
print("=" * 74)
import numpy as _np


def _sharecheck(shape):
    _n = len(shape)
    _d = _pd.date_range("2016-01-31", periods=_n, freq="ME")
    _df = _pd.DataFrame({"date": _d, "price": 50.0, "ttm": 40e9, "shares": shape})
    _df["mktcap"] = _df.price * _df.shares
    _df["ps"] = _df.mktcap / _df.ttm
    _pe = _pd.date_range("2016-03-31", periods=max(_n // 3, 9), freq="QE")
    _t = _pd.DataFrame({"period_end": _pe, "available": _pe + _pd.Timedelta(days=40),
                        "ttm": 40e9, "tag": "Revenues"})
    return [i for i in ps.audit_series(_df, _t, {}, []) if "share count" in i]


report("Honeywell-shape halving is reported",
       bool(_sharecheck(_np.r_[_np.full(106, 653e6), _np.full(14, 342e6)])), True,
       "it was the second cheapest name on the screen with an empty audit column")
report("Waters-shape +65% is reported",
       bool(_sharecheck(_np.r_[_np.full(112, 59e6), _np.full(8, 97e6)])), True,
       "a stock-funded acquisition still resets what the median is measuring")
report("Monster-shape doubling is reported",
       bool(_sharecheck(_np.r_[_np.full(112, 975e6), _np.full(8, 1960e6)])), True,
       "a split Yahoo had not published yet")
report("an ordinary buyback stays quiet",
       _sharecheck(_np.linspace(600e6, 480e6, 120)), [],
       "20% over ten years is a buyback, not a discontinuity")
report("a flat share count stays quiet", _sharecheck(_np.full(120, 5e8)), [])

# audit_series used to `return` when a company had too few clean revenue steps
# to judge a splice, which silently skipped every check after it -- the share
# base test among them. Honeywell and Waters came through with empty audits.
_short = _pd.DataFrame({"period_end": _pd.date_range("2016-03-31", periods=6, freq="QE")})
_short["available"] = _short.period_end + _pd.Timedelta(days=40)
_short["ttm"], _short["tag"] = 40e9, "Revenues"
_d = _pd.date_range("2016-01-31", periods=120, freq="ME")
_hd = _pd.DataFrame({"date": _d, "price": 50.0, "ttm": 40e9,
                     "shares": _np.r_[_np.full(106, 653e6), _np.full(14, 342e6)]})
_hd["mktcap"] = _hd.price * _hd.shares
_hd["ps"] = _hd.mktcap / _hd.ttm
report("too few revenue steps no longer skips the later checks",
       bool([i for i in ps.audit_series(_hd, _short, {}, []) if "share count" in i]),
       True, "one early return was hiding checks 3 through 7")

print()
print("=" * 74)
_bad2 = [n for ok, n in RESULT if not ok]
print(f"{len(RESULT) - len(_bad2)} of {len(RESULT)} behaved as they should")
if _bad2:
    print("STILL BROKEN IN THIS BUILD:")
    for _n in _bad2:
        print("   -", _n)


print()
print("=" * 74)
print("12. Splits Yahoo never published, read out of EDGAR's own counts")
print("=" * 74)
from datetime import date as _dt, timedelta as _tdd
import numpy as _np

def _flat_px(a, b, level, jump_at=None, factor=1.0):
    _i = _pd.date_range(a, b, freq="B").astype("datetime64[ns]")
    _v = _np.full(len(_i), float(level))
    if jump_at is not None:
        _v[_i >= _pd.Timestamp(jump_at)] *= factor
    return _pd.Series(_v, index=_i)


def _cnt(start, n, level, at=None, f=1.0):
    return [(start + _tdd(days=91 * i),
             level * f if (at and start + _tdd(days=91 * i) >= at) else level)
            for i in range(n)]


def _reveal(sh, px, known=None):
    ps.REJECTED_RATIOS.clear()
    return ps.splits_the_share_count_reveals(sh, known or {}, px)



# Honeywell's actual reported counts, from ps_diagnostics__33_.txt.
_HON = [(_dt(2025, 2, 14), 649_918_551), (_dt(2025, 4, 29), 642_682_909),
        (_dt(2025, 7, 24), 634_896_562), (_dt(2025, 10, 23), 634_887_208),
        (_dt(2026, 2, 17), 635_675_701), (_dt(2026, 4, 23), 633_653_119),
        (_dt(2026, 7, 23), 316_940_010), (_dt(2026, 7, 24), 316_940_010)]
ps.REJECTED_RATIOS.clear()
ps.CURRENT_TICKER = "HON"
_info = ps.classify_splits(_px2, _pd.Series({_pd.Timestamp("2026-06-29"): 0.9535}))
_hk = ps.corroborate_splits(_info, _HON)
report("the spin-off factor is refused",
       [round(v[0], 4) for v in _hk.values()], [],
       "0.9535 against a count that halved; the loose test let it through and "
       "Honeywell became the cheapest name on the screen")
# Yahoo has no record of this split, so Yahoo cannot have back-adjusted its
# prices for it either: the doubling has to be visible in the raw series.
_hpx = _flat_px("2024-06-01", "2026-08-20", 110, "2026-06-15", 2.0)
_hk.update(ps.splits_the_share_count_reveals(_HON, _hk, _hpx))
report("the real 1-for-2 reverse split is recovered",
       [round(v[0], 2) for v in _hk.values()], [0.5],
       "Yahoo has no record of it; the cover-page count does")




def _flat_px(a, b, level, jump_at=None, factor=1.0):
    _i = _pd.date_range(a, b, freq="B").astype("datetime64[ns]")
    _v = _np.full(len(_i), float(level))
    if jump_at is not None:
        _v[_i >= _pd.Timestamp(jump_at)] *= factor
    return _pd.Series(_v, index=_i)


def _reveal(sh, px, known=None):
    ps.REJECTED_RATIOS.clear()
    return ps.splits_the_share_count_reveals(sh, known or {}, px)


report("an ordinary buyback is not read as a split",
       _reveal([(_dt(2024, 1, 1), 1e9), (_dt(2024, 4, 1), .96e9),
                (_dt(2024, 7, 1), .92e9), (_dt(2024, 10, 1), .88e9)],
               _flat_px("2023-06-01", "2025-06-01", 90)), {},
       "a real change in the share base, not something to adjust history for")
report("a split Yahoo already reports is not added twice",
       _reveal(_cnt(_dt(2024, 1, 1), 12, 5e8, _dt(2026, 1, 1), 2.0),
               _flat_px("2023-06-01", "2026-09-01", 90, "2026-01-01", 0.5),
               {_pd.Timestamp("2026-01-05"): (2.0, "raw")}), {})
report("Monster's two-for-one is picked up from the count",
       bool(_reveal(_cnt(_dt(2024, 1, 1), 12, 975e6, _dt(2026, 8, 11), 2.0),
                    _flat_px("2023-06-01", "2026-09-01", 100, "2026-08-05", 0.5))),
       True, "it reached the cover pages before it reached Yahoo")

# One run read the count on its own and invented 113 splits. Every acquisition
# paid in stock lands on some ratio, and a short list of ratios always has one
# nearby. The price is what separates them: in a split it moves inversely on
# the day, in an issuance it does not.
for _lab, _f, _when, _lvl in [("AMD / Xilinx", 1.3512, _dt(2022, 5, 4), 1_199_303_422.0),
                              ("Bunge / Viterra", 1.4885, _dt(2025, 8, 5), 134_404_972.0),
                              ("Aon / Hewitt", 1.2297, _dt(2011, 2, 25), 270_867_656.0),
                              ("Omnicom / IPG", 1.47, _dt(2026, 2, 20), 196e6)]:
    report(f"{_lab} is issuance, not a split",
           _reveal(_cnt(_dt(_when.year - 2, 1, 1), 14, _lvl, _when, _f),
                   _flat_px(f"{_when.year - 2}-01-01", f"{_when.year + 1}-06-01", 90)), {},
           f"shares x{_f:.2f} with no matching move in the price")

print()
print("=" * 74)
_b3 = [n for ok, n in RESULT if not ok]
print(f"{len(RESULT) - len(_b3)} of {len(RESULT)} behaved as they should")
for _n in _b3:
    print("   -", _n)


print()
print("=" * 74)
print("13. The whole of monthly_ps, end to end")
print("=" * 74)
# Twenty-nine unit tests did not call monthly_ps, so a mismatch between the key
# types two of its helpers used walked past every one of them and only showed
# up as "Invalid comparison between dtype=datetime64[ns] and date" two minutes
# into a real run. Anything that changes split handling has to come through
# here as well as through the units.
import numpy as _np3

_HON_FULL = ([(_dt(y, 3, 1), 700_000_000.0 - (y - 2016) * 8e6) for y in range(2016, 2025)]
             + _HON)
_idx = _pd.date_range("2016-01-04", "2026-08-20", freq="W").astype("datetime64[ns]")
_px3 = _pd.Series(_np3.linspace(100, 215, len(_idx)), index=_idx)
_px3[_px3.index >= _pd.Timestamp("2026-06-15")] *= 2.0   # the unadjusted split move
_pe3 = _pd.date_range("2016-03-31", periods=42, freq="QE").astype("datetime64[ns]")
_ttm3 = _pd.DataFrame({"period_end": _pe3,
                       "available": (_pe3 + _pd.Timedelta(days=45)).astype("datetime64[ns]"),
                       "ttm": _np3.linspace(38e9, 40e9, 42), "tag": "Revenues"})

ps.REJECTED_RATIOS.clear()
ps.CURRENT_TICKER = "HON"
try:
    _hist3 = ps.monthly_ps(_px3, _ttm3, _HON_FULL,
                           _pd.Series({_pd.Timestamp("2026-06-29"): 0.9535}), 12)
    _ran, _why = len(_hist3) > 100, ""
except Exception as _e:
    _ran, _why = False, f"{type(_e).__name__}: {_e}"
report("monthly_ps survives a split recovered from EDGAR", _ran, True, _why)

if _ran:
    report("the recovered 1-for-2 is the split that gets applied",
           [round(v[0], 2) for v in _hist3.attrs["splits"].values()], [0.5],
           "not Yahoo's 0.9535 spin-off factor")
    _drop = _hist3["shares"].iloc[-1] / _hist3["shares"].iloc[0] - 1
    report("the share history reads as a buyback, not a collapse",
           abs(_drop) < 0.25, True,
           f"{_drop:+.0%} across ten years; it read -55% while the split was missing")
    _a3 = [i for i in ps.audit_series(_hist3, _ttm3, _hist3.attrs["splits"],
                                      ps.detect_corporate_actions(_hist3, _hist3.attrs["splits"]))
           if "share count" in i]
    report("and the share-base warning goes quiet", _a3, [],
           "there is no discontinuity left once the split is applied")

print()
print("=" * 74)
_b4 = [n for ok, n in RESULT if not ok]
print(f"{len(RESULT) - len(_b4)} of {len(RESULT)} behaved as they should")
for _n in _b4:
    print("   -", _n)
