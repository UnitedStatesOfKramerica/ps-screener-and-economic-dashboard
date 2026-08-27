"""Run the real pipeline against real filings from fixture.json.gz.

Every regression in this project came from testing a fix against invented data.
This runs the actual functions over the actual facts.
"""
import gzip, json, sys
import numpy as np, pandas as pd
import ps_screener as ps

FIX = "/mnt/user-data/uploads/fixture__2__json.gz"


def load(path=FIX):
    return json.load(gzip.open(path, "rt"))


def series(d, key):
    if not d[key]:
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in d[key].items()}).sort_index()
    s.index = s.index.astype("datetime64[ns]")
    return s


def run_one(t, d, years=12):
    ps.CURRENT_TICKER = t
    facts = d["facts"]
    px, sp = series(d, "prices"), series(d, "splits")
    periods = ps.collect_periods(facts, "us-gaap", ps.REVENUE_TAGS)
    quarters = ps.derive_quarters(periods)
    ttm = ps.trailing_twelve(quarters)
    shares = ps.collect_instants(facts, "dei", ps.SHARE_TAGS_DEI) or \
             ps.collect_instants(facts, "us-gaap", ps.SHARE_TAGS_GAAP)
    hist = ps.monthly_ps(px, ttm, shares, sp if len(sp) else None, years)
    return dict(ticker=t, px=px, splits=sp, periods=periods, ttm=ttm,
                shares=shares, hist=hist)


if __name__ == "__main__":
    bundle = load()
    only = sys.argv[1].split(",") if len(sys.argv) > 1 else list(bundle)
    ps.REJECTED_RATIOS.clear()
    print(f"{'tkr':6s}{'shares now':>15s}{'shares 10y ago':>16s}{'chg':>8s}"
          f"{'rev now':>11s}{'P/S':>8s}{'10y med':>9s}")
    for t in only:
        r = run_one(t, bundle[t])
        h = r["hist"]
        if h.empty:
            print(f"{t:6s}  (no history)")
            continue
        chg = h["shares"].iloc[-1] / h["shares"].iloc[0] - 1
        print(f"{t:6s}{h['shares'].iloc[-1]:>15,.0f}{h['shares'].iloc[0]:>16,.0f}"
              f"{chg:>7.0%}{r['ttm']['ttm'].iloc[-1]/1e9:>10,.1f}B"
              f"{h['ps'].iloc[-1]:>8.2f}{h['ps'].tail(120).median():>9.2f}")
    print()
    if ps.REJECTED_RATIOS:
        print("split factor decisions:")
        for tk, when, ratio, why in ps.REJECTED_RATIOS:
            print(f"  {tk:6s} {when}  {ratio:>9.4f}  {why[:88]}")


def assert_real_data():
    """What the real filings say, asserted. Anything touching split handling,
    share counts or revenue concepts has to come through here before it ships:
    every regression in this project passed invented data first."""
    bundle = load()
    expect = {   # ticker: (dilution_1y within, share-base audit expected)
        "HON":  (2.0,  False),   # 1-for-2 reverse split Yahoo never filed
        "MNST": (2.0,  False),   # two-for-one, three weeks old
        "AMD":  (2.0,  True),    # Xilinx: real issuance, worth flagging
        "BG":   (5.0,  True),    # Viterra: must NOT read as a split, but the
                                 # step itself is real and worth flagging
        "AON":  (3.0,  False),   # Hewitt: 1.23x, must not read as five-for-four
        "ADM":  (2.0,  False),
        "DD":   (5.0,  False),   # 1-for-3 filed by Yahoo as 0.4725
        "CPT":  (8.0,  False),
    }
    bad = []
    for t, (tol, want_flag) in expect.items():
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
        if abs(d1) > tol:
            bad.append(f"{t}: dilution_1y {d1:+.1f}%, expected within {tol}%")
        if flag != want_flag:
            bad.append(f"{t}: share-base flag {flag}, expected {want_flag}")
    adm = run_one("ADM", bundle["ADM"])
    rev = adm["ttm"]["ttm"].iloc[-1] / 1e9
    if not 70 < rev < 95:
        bad.append(f"ADM revenue ${rev:,.1f}B — the ASC-606 slice is back")
    for line in bad:
        print("  FAIL", line)
    print(f"\n{len(expect) * 2 + 1 - len(bad)} of {len(expect) * 2 + 1} "
          f"checks pass against real filings")
    return not bad
