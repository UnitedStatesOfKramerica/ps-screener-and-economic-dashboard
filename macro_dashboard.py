#!/usr/bin/env python3
"""
Macro dashboard — a standalone economic page built from FRED (free, public),
published beside the screener as docs/dashboard.html. Deliberately separate from
ps_screener.py: it shares no code and cannot destabilise the screen, and it
refreshes on its own cadence (FRED updates weekly/monthly, not intraday).

Every indicator names its exact FRED series id. Ids are stable and do not change,
but a discontinued series would silently chart nothing, so each fetch is checked
and any series that fails to return data is listed on the page rather than left
as a blank panel. A single free API key (FRED_API_KEY) is required.

Run:  FRED_API_KEY=... python macro_dashboard.py
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

OUT = Path(".")
FRED = "https://api.stlouisfed.org/fred/series/observations"
KEY = os.environ.get("FRED_API_KEY", "")


# --- indicator registry ----------------------------------------------------
# Each entry: the FRED series id, a human label, how to read it, and the units.
# "invert_good" flags series where DOWN is the healthy direction (a widening
# credit spread is bad, a rising savings rate is broadly good, etc.) so the page
# can colour the latest print without editorialising in prose.
#
# level      : show the latest value and a long line chart
# yoy        : show year-over-year % change (for levels like M2, GDP)
# derived    : computed here from two series (Buffett indicator = market cap/GDP)
INDICATORS = [
    # recession / cycle
    {"id": "SAHMREALTIME", "label": "Sahm rule recession indicator",
     "kind": "level", "units": "pp", "good": "low", "start": "2000-01-01",
     "note": "Triggers a recession signal at 0.50 and above."},
    {"id": "T10Y2Y", "label": "Yield curve (10Y minus 2Y)",
     "kind": "level", "units": "%", "good": "high", "start": "1990-01-01",
     "note": "Below zero is an inversion; has preceded every modern recession."},
    {"id": "T10Y3M", "label": "Yield curve (10Y minus 3M)",
     "kind": "level", "units": "%", "good": "high", "start": "1990-01-01",
     "note": "The Fed's preferred curve measure; below zero is an inversion."},
    {"id": "BAMLH0A0HYM2", "label": "High-yield credit spread",
     "kind": "level", "units": "%", "good": "low", "start": "2000-01-01",
     "note": "ICE BofA option-adjusted spread; widens when credit stress rises."},

    # rates
    {"id": "MORTGAGE30US", "label": "30-year mortgage rate",
     "kind": "level", "units": "%", "good": "low", "start": "2000-01-01",
     "note": "Freddie Mac weekly average."},
    {"id": "DGS10", "label": "10-year Treasury yield",
     "kind": "level", "units": "%", "good": None, "start": "1990-01-01",
     "note": "Market yield, constant maturity."},
    {"id": "FEDFUNDS", "label": "Fed funds rate",
     "kind": "level", "units": "%", "good": None, "start": "1990-01-01",
     "note": "Effective federal funds rate, monthly."},

    # households / real economy
    {"id": "PSAVERT", "label": "Personal savings rate",
     "kind": "level", "units": "%", "good": "high", "start": "1990-01-01",
     "note": "Saving as a share of disposable income."},
    {"id": "HOUST", "label": "Housing starts",
     "kind": "level", "units": "K", "good": "high", "start": "1990-01-01",
     "note": "New privately-owned housing units started, annualised thousands."},
    {"id": "UNRATE", "label": "Unemployment rate",
     "kind": "level", "units": "%", "good": "low", "start": "1990-01-01",
     "note": "Civilian unemployment rate."},

    # commodities / money / growth
    {"id": "DCOILWTICO", "label": "WTI crude oil",
     "kind": "level", "units": "$", "good": None, "start": "2000-01-01",
     "note": "West Texas Intermediate spot price."},
    {"id": "M2SL", "label": "M2 money supply (YoY)",
     "kind": "yoy", "units": "%", "good": None, "start": "1990-01-01",
     "note": "Year-over-year growth in the M2 money stock."},
    {"id": "GDPC1", "label": "Real GDP (YoY)",
     "kind": "yoy", "units": "%", "good": "high", "start": "1990-01-01",
     "note": "Year-over-year growth in real GDP."},
    {"id": "CPIAUCSL", "label": "CPI inflation (YoY)",
     "kind": "yoy", "units": "%", "good": "low", "start": "1990-01-01",
     "note": "Year-over-year change in the consumer price index."},
]

# Shiller CAPE and the Buffett indicator are computed from series below.
# CAPE has a ready FRED-adjacent series via multpl; FRED carries the components
# for the Buffett indicator (corporate equities market value / GDP).
DERIVED = [
    {"label": "Buffett indicator (market cap / GDP)",
     "num": "NCBEILQ027S",   # nonfinancial corporate equities, market value (levels)
     "den": "GDP",           # nominal GDP
     "units": "%", "good": "low", "start": "1990-01-01",
     "note": "Total US equity market value against GDP; high readings are "
             "historically associated with low forward returns."},
]


def fetch(series_id: str, start: str) -> list[tuple[str, float]]:
    """Observations for one series, oldest first. Missing/failed -> empty."""
    if not KEY:
        raise SystemExit("FRED_API_KEY is not set. Register a free key at "
                         "fredaccount.stlouisfed.org and add it as the GitHub "
                         "secret FRED_API_KEY.")
    try:
        r = requests.get(FRED, params={
            "series_id": series_id, "api_key": KEY, "file_type": "json",
            "observation_start": start, "sort_order": "asc", "limit": 100000,
        }, timeout=30)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception as exc:
        print(f"  {series_id}: fetch failed ({exc})")
        return []
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    return out


def yoy(series: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Year-over-year percent change, matching each point to ~365 days earlier."""
    if not series:
        return []
    by_date = {datetime.strptime(d, "%Y-%m-%d").date(): v for d, v in series}
    dates = sorted(by_date)
    out = []
    for d in dates:
        target = d - timedelta(days=365)
        # nearest earlier observation within a 45-day window
        prior = [pd for pd in dates if abs((pd - target).days) <= 45]
        if prior and by_date[min(prior, key=lambda x: abs((x - target).days))] != 0:
            base = by_date[min(prior, key=lambda x: abs((x - target).days))]
            out.append((d.isoformat(), (by_date[d] / base - 1) * 100))
    return out


def build():
    print("Building macro dashboard from FRED...")
    panels = []
    failed = []

    for ind in INDICATORS:
        raw = fetch(ind["id"], ind["start"])
        if not raw:
            failed.append((ind["id"], ind["label"]))
            continue
        series = yoy(raw) if ind["kind"] == "yoy" else raw
        if not series:
            failed.append((ind["id"], ind["label"] + " (no data after transform)"))
            continue
        latest_date, latest_val = series[-1]
        panels.append({
            "label": ind["label"], "series_id": ind["id"], "units": ind["units"],
            "good": ind["good"], "note": ind["note"],
            "latest": round(latest_val, 2), "latest_date": latest_date,
            "points": [[d, round(v, 3)] for d, v in series],
        })
        print(f"  {ind['id']:<14} {len(series):>5} pts, "
              f"latest {latest_val:.2f} on {latest_date}")

    for dv in DERIVED:
        num = fetch(dv["num"], dv["start"])
        den = fetch(dv["den"], dv["start"])
        if not num or not den:
            failed.append((f"{dv['num']}/{dv['den']}", dv["label"]))
            continue
        dnum = {d: v for d, v in num}
        dden = {d: v for d, v in den}
        # GDP is quarterly; align the numerator to each GDP date
        pts = []
        den_dates = sorted(dden)
        for d in sorted(dnum):
            near = [x for x in den_dates if abs(
                (datetime.strptime(x, "%Y-%m-%d").date()
                 - datetime.strptime(d, "%Y-%m-%d").date()).days) <= 60]
            if near and dden[near[-1]] > 0:
                pts.append((d, dnum[d] / dden[near[-1]] * 100))
        if not pts:
            failed.append((f"{dv['num']}/{dv['den']}", dv["label"]))
            continue
        panels.append({
            "label": dv["label"], "series_id": f"{dv['num']} / {dv['den']}",
            "units": dv["units"], "good": dv["good"], "note": dv["note"],
            "latest": round(pts[-1][1], 1), "latest_date": pts[-1][0],
            "points": [[d, round(v, 2)] for d, v in pts],
        })
        print(f"  {dv['label']}: {len(pts)} pts, latest {pts[-1][1]:.1f}%")

    if failed:
        print(f"\n  {len(failed)} series failed and are listed on the page:")
        for sid, lbl in failed:
            print(f"    {sid}: {lbl}")

    stamp = _now_et_local()
    html = PAGE.replace("__DATA__", json.dumps(panels)) \
               .replace("__FAILED__", json.dumps(failed)) \
               .replace("__STAMP__", stamp)
    (OUT / "docs").mkdir(exist_ok=True)
    (OUT / "docs/dashboard.html").write_text(html)
    print(f"\nwrote docs/dashboard.html with {len(panels)} panels")


def _now_et_local() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime(
            "%-d %b %Y %H:%M ET")
    except Exception:
        return datetime.utcnow().strftime("%-d %b %Y %H:%M UTC")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Macro dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#0f1115; --card:#181b22; --ink:#e8eaed; --dim:#9aa0aa;
          --line:#2a2f3a; --good:#3fb950; --bad:#f85149; --neutral:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:20px 24px; border-bottom:1px solid var(--line); }
  h1 { margin:0; font-size:20px; }
  .stamp { color:var(--dim); font-size:13px; margin-top:4px; }
  .nav { margin-top:10px; }
  .nav a { color:var(--neutral); text-decoration:none; font-size:13px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
          gap:16px; padding:20px 24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; }
  .card h2 { margin:0 0 2px; font-size:14px; font-weight:600; }
  .sid { color:var(--dim); font-size:11px; font-family:ui-monospace,monospace; }
  .val { font-size:26px; font-weight:700; margin:8px 0 2px; }
  .val small { font-size:14px; font-weight:500; color:var(--dim); }
  .asof { color:var(--dim); font-size:11px; }
  .note { color:var(--dim); font-size:12px; margin-top:8px; }
  .chartbox { height:120px; margin-top:10px; }
  .good { color:var(--good); } .bad { color:var(--bad); } .neu { color:var(--neutral); }
  .fail { padding:12px 24px; color:var(--bad); font-size:13px; }
</style></head>
<body>
<header>
  <h1>Macro dashboard</h1>
  <div class="stamp">FRED data &middot; __STAMP__</div>
  <div class="nav"><a href="index.html">&larr; back to the screener</a></div>
</header>
<div id="fail" class="fail"></div>
<div id="grid" class="grid"></div>
<script>
const PANELS = __DATA__;
const FAILED = __FAILED__;

if (FAILED.length) {
  document.getElementById('fail').textContent =
    FAILED.length + ' series could not be loaded from FRED: ' +
    FAILED.map(f => f[1]).join(', ');
}

function colourClass(good, latest, points) {
  if (good === null) return 'neu';
  // "good: low" means a low latest value is healthy; compare to the series
  // median so the colour reflects where we sit historically, not an absolute.
  const vals = points.map(p => p[1]).slice().sort((a,b)=>a-b);
  const med = vals[Math.floor(vals.length/2)];
  const high = latest > med;
  if (good === 'high') return high ? 'good' : 'bad';
  if (good === 'low')  return high ? 'bad' : 'good';
  return 'neu';
}

const grid = document.getElementById('grid');
PANELS.forEach((p, i) => {
  const card = document.createElement('div');
  card.className = 'card';
  const cls = colourClass(p.good, p.latest, p.points);
  card.innerHTML =
    `<h2>${p.label}</h2><div class="sid">${p.series_id}</div>` +
    `<div class="val ${cls}">${p.latest}<small> ${p.units}</small></div>` +
    `<div class="asof">as of ${p.latest_date}</div>` +
    `<div class="chartbox"><canvas id="c${i}"></canvas></div>` +
    `<div class="note">${p.note}</div>`;
  grid.appendChild(card);

  // thin the series for the sparkline so 10k daily points don't choke the canvas
  const pts = p.points;
  const step = Math.max(1, Math.floor(pts.length / 400));
  const thin = pts.filter((_, k) => k % step === 0);
  new Chart(document.getElementById('c'+i), {
    type: 'line',
    data: { labels: thin.map(x => x[0]),
            datasets: [{ data: thin.map(x => x[1]), borderColor:
              getComputedStyle(document.documentElement).getPropertyValue('--neutral'),
              borderWidth:1.5, pointRadius:0, tension:0.1, fill:false }] },
    options: { responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false}, tooltip:{ intersect:false, mode:'index' } },
      scales:{ x:{ display:false }, y:{ ticks:{ color:'#9aa0aa', font:{size:10}, maxTicksLimit:4 },
               grid:{ color:'#2a2f3a' } } } }
  });
});
</script>
</body></html>"""


if __name__ == "__main__":
    build()
