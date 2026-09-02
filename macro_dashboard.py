#!/usr/bin/env python3
"""
Macro dashboard — recession-risk focused, built from FRED (free, public), published
as docs/dashboard.html. Standalone: shares no code with the screener.

Purpose: recognise DETERIORATION early. Every indicator shows its level, its recent
direction, and whether that direction is the worrying one. The page leads with two
published recession-probability models plus a transparent signal scorecard, then
four themed sections (Growth, Inflation, Financial Conditions, Labor).

Two recession models, layered:
  * NY Fed / Estrella-Mishkin (LEADING, ~12 months ahead) computed here from the
    10Y-3M spread with the published probit formula -- shown, not black-boxed.
  * Chauvet-Piger smoothed (COINCIDENT, "are we in one now") -- FRED RECPROUSM156N.
Leading is the headline because the goal is early warning; coincident confirms.

Every series names its exact FRED id; a failed series is listed on the page rather
than charting nothing. Requires a free FRED_API_KEY.
"""
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

OUT = Path(".")
FRED = "https://api.stlouisfed.org/fred/series/observations"
KEY = os.environ.get("FRED_API_KEY", "")

THEMES = {
    "Financial conditions": [
        {"id": "T10Y3M", "label": "Yield curve (10Y-3M)", "kind": "level",
         "units": "%", "worry": "down", "start": "1985-01-01",
         "caution": 0.0, "alert": -0.5,
         "note": "The NY Fed's recession model runs off this spread. Below zero "
                 "is inverted; every recession since 1970 followed an inversion."},
        {"id": "T10Y2Y", "label": "Yield curve (10Y-2Y)", "kind": "level",
         "units": "%", "worry": "down", "start": "1985-01-01",
         "caution": 0.0, "alert": -0.25,
         "note": "The most-watched curve. Un-inverting after an inversion has "
                 "historically been the final warning before recession."},
        {"id": "BAMLH0A0HYM2", "label": "High-yield credit spread", "kind": "level",
         "units": "%", "worry": "up", "start": "1997-01-01",
         "caution": 5.0, "alert": 7.0,
         "note": "Widening means credit markets are pricing rising default risk. "
                 "Spikes lead or coincide with downturns."},
        {"id": "NFCI", "label": "Financial conditions index", "kind": "level",
         "units": "", "worry": "up", "start": "1985-01-01",
         "caution": 0.0, "alert": 0.5,
         "note": "Chicago Fed index of overall financial stress. Above zero is "
                 "tighter than average; positive and rising is deterioration."},
        {"id": "MORTGAGE30US", "label": "30-year mortgage rate", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "High mortgage rates throttle housing, an early-cycle channel."},
    ],
    "Labor market": [
        {"id": "SAHMREALTIME", "label": "Sahm rule", "kind": "level",
         "units": "pp", "worry": "up", "start": "1990-01-01",
         "caution": 0.3, "alert": 0.5,
         "note": "Triggers a recession signal at 0.50. Fast, but fires at or just "
                 "after onset -- a confirmation, not a lead."},
        {"id": "UNRATE", "label": "Unemployment rate", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "The level matters less than the TURN: unemployment rising off "
                 "its lows is the classic early recession tell."},
        {"id": "ICSA", "label": "Initial jobless claims (4wk)", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01",
         "caution": 300000, "alert": 375000,
         "note": "A four-week-average breakout above ~310k has led recessions by "
                 "2-4 months. The earliest hard-data labor signal."},
        {"id": "PAYEMS", "label": "Nonfarm payrolls (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "caution": 1.0, "alert": 0.0,
         "note": "Year-over-year job growth. Slowing toward zero, then negative, "
                 "tracks the cycle turning down."},
    ],
    "Growth": [
        {"id": "GDPC1", "label": "Real GDP (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "caution": 1.0, "alert": 0.0,
         "note": "Output growth. Two negative quarters is the informal recession "
                 "definition; slowing toward zero is the warning."},
        {"id": "INDPRO", "label": "Industrial production (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "caution": 0.0, "alert": -2.0,
         "note": "Factory output. Cyclical and timely; turns down early because "
                 "manufacturing leads the broader economy."},
        {"id": "HOUST", "label": "Housing starts", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "Homebuilding is rate-sensitive and turns before the cycle; "
                 "falling starts is an early-warning channel."},
        {"id": "UMCSENT", "label": "Consumer sentiment", "kind": "level",
         "units": "", "worry": "down", "start": "1990-01-01",
         "caution": 70, "alert": 60,
         "note": "University of Michigan survey. Weak and falling sentiment "
                 "precedes pullbacks in consumer spending."},
    ],
    "Inflation & policy": [
        {"id": "CPIAUCSL", "label": "CPI inflation (YoY)", "kind": "yoy",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "caution": 3.0, "alert": 4.5,
         "note": "Headline inflation. High and sticky keeps the Fed tight, which "
                 "raises recession risk."},
        {"id": "PCEPILFE", "label": "Core PCE (YoY)", "kind": "yoy",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "caution": 2.5, "alert": 3.5,
         "note": "The Fed's preferred gauge. Above target constrains rate cuts "
                 "even as growth slows."},
        {"id": "FEDFUNDS", "label": "Fed funds rate", "kind": "level",
         "units": "%", "worry": None, "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "Policy rate. Restrictive policy held too long is the classic "
                 "cause of a policy-induced recession."},
        {"id": "M2SL", "label": "M2 money supply (YoY)", "kind": "yoy",
         "units": "%", "worry": None, "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "Money-supply growth. Sharp contraction is unusual and has "
                 "accompanied tightening cycles."},
    ],
}
COINCIDENT_ID = "RECPROUSM156N"


def fetch(series_id, start):
    if not KEY:
        raise SystemExit("FRED_API_KEY is not set. Register free at "
                         "fredaccount.stlouisfed.org and add the GitHub secret "
                         "FRED_API_KEY.")
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


def yoy(series):
    if not series:
        return []
    by = {datetime.strptime(d, "%Y-%m-%d").date(): v for d, v in series}
    dates = sorted(by)
    out = []
    for d in dates:
        target = d - timedelta(days=365)
        near = [pd for pd in dates if abs((pd - target).days) <= 45]
        if near:
            base = by[min(near, key=lambda x: abs((x - target).days))]
            if base != 0:
                out.append((d.isoformat(), (by[d] / base - 1) * 100))
    return out


def trend(series, lookback_days=180):
    if len(series) < 3:
        return None
    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in series]
    vals = [v for _, v in series]
    latest = vals[-1]
    target = dates[-1] - timedelta(days=lookback_days)
    pi = min(range(len(dates)), key=lambda i: abs((dates[i] - target).days))
    delta = latest - vals[pi]
    lo, hi = min(vals), max(vals)
    pct = (latest - lo) / (hi - lo) * 100 if hi > lo else 50.0
    return {"delta": round(delta, 3), "pct_of_range": round(pct, 1),
            "prior": round(vals[pi], 3)}


def state_of(worry, latest, caution, alert):
    if caution is None or alert is None or worry is None:
        return "neutral"
    if worry == "up":
        return "alert" if latest >= alert else "caution" if latest >= caution else "calm"
    return "alert" if latest <= alert else "caution" if latest <= caution else "calm"


def build():
    print("Building recession-risk dashboard from FRED...")
    failed = []

    spread = fetch("T10Y3M", "1985-01-01")
    ny_series = []
    if spread:
        for d, s in spread:
            z = -0.5333 * s - 0.5091
            ny_series.append((d, round(0.5 * (1 + math.erf(z / math.sqrt(2))) * 100, 1)))
    else:
        failed.append(("T10Y3M", "NY Fed recession model input"))
    ny_latest = ny_series[-1] if ny_series else None
    ny_trend = trend(ny_series, 365) if ny_series else None

    coin = fetch(COINCIDENT_ID, "1990-01-01")
    if not coin:
        failed.append((COINCIDENT_ID, "coincident recession model"))

    themes_out, scorecard = {}, []
    for theme, inds in THEMES.items():
        panels = []
        for ind in inds:
            raw = fetch(ind["id"], ind["start"])
            if not raw:
                failed.append((ind["id"], ind["label"])); continue
            series = yoy(raw) if ind["kind"] == "yoy" else raw
            if not series:
                failed.append((ind["id"], ind["label"] + " (empty after transform)")); continue
            latest = series[-1][1]
            tr = trend(series)
            st = state_of(ind["worry"], latest, ind.get("caution"), ind.get("alert"))
            deteriorating = bool(tr and ind["worry"] and (
                (ind["worry"] == "up" and tr["delta"] > 0) or
                (ind["worry"] == "down" and tr["delta"] < 0)))
            panels.append({
                "label": ind["label"], "series_id": ind["id"], "units": ind["units"],
                "worry": ind["worry"], "note": ind["note"], "state": st,
                "caution": ind.get("caution"), "alert": ind.get("alert"),
                "latest": round(latest, 2), "latest_date": series[-1][0],
                "trend": tr, "deteriorating": deteriorating,
                "points": [[d, round(v, 3)] for d, v in series]})
            scorecard.append({"theme": theme, "label": ind["label"],
                              "state": st, "deteriorating": deteriorating})
            print(f"  {ind['id']:<14} {len(series):>5} pts  latest {latest:.2f}  "
                  f"state={st} {'worse' if deteriorating else 'ok'}")
        themes_out[theme] = panels

    order = {"alert": 3, "caution": 2, "calm": 1, "neutral": 0}
    theme_states = {}
    for theme, panels in themes_out.items():
        if panels:
            worst = max(panels, key=lambda p: order.get(p["state"], 0))["state"]
            theme_states[theme] = {"state": worst,
                                   "deteriorating": sum(1 for p in panels if p["deteriorating"]),
                                   "total": len(panels)}

    if failed:
        print(f"\n  {len(failed)} series failed (listed on the page):")
        for sid, lbl in failed:
            print(f"    {sid}: {lbl}")

    payload = {
        "ny": {"latest": ny_latest, "trend": ny_trend, "points": ny_series},
        "coincident": {"latest": (coin[-1] if coin else None),
                       "points": [[d, round(v, 1)] for d, v in coin]},
        "themes": themes_out, "theme_states": theme_states, "scorecard": scorecard}
    html = PAGE.replace("__DATA__", json.dumps(payload)) \
               .replace("__FAILED__", json.dumps(failed)) \
               .replace("__STAMP__", _now_et_local())
    (OUT / "docs").mkdir(exist_ok=True)
    (OUT / "docs/dashboard.html").write_text(html)
    n = sum(len(v) for v in themes_out.values())
    print(f"\nwrote docs/dashboard.html: {n} indicators across {len(themes_out)} themes")


def _now_et_local():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%-d %b %Y %H:%M ET")
    except Exception:
        return datetime.utcnow().strftime("%-d %b %Y %H:%M UTC")


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recession-risk dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root { --bg:#0d1017; --card:#161a22; --ink:#e8eaed; --dim:#8b929e;
          --line:#242a35; --calm:#3fb950; --caution:#d29922; --alert:#f85149;
          --neutral:#58a6ff; --grid:#1e232c; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:22px 26px 18px; border-bottom:1px solid var(--line); }
  h1 { margin:0; font-size:21px; letter-spacing:-0.01em; }
  .stamp { color:var(--dim); font-size:13px; margin-top:4px; }
  .nav a { color:var(--neutral); text-decoration:none; }
  .wrap { max-width:1200px; margin:0 auto; padding:0 26px 40px; }
  .gauges { display:grid; grid-template-columns:1.4fr 1fr; gap:18px; margin:22px 0 8px; }
  @media(max-width:760px){ .gauges{ grid-template-columns:1fr; } }
  .gauge { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px 22px; }
  .gauge .k { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:0.05em; }
  .gauge .big { font-size:52px; font-weight:800; line-height:1.05; margin:6px 0 2px; }
  .gauge .sub { color:var(--dim); font-size:13px; }
  .track { height:10px; background:#0b0e14; border-radius:6px; margin:14px 0 6px;
           position:relative; overflow:hidden; border:1px solid var(--line); }
  .fill { height:100%; border-radius:6px; }
  .thresh { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--dim); }
  .delta { font-size:13px; font-weight:600; margin-top:8px; }
  .score { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:16px 20px; margin:8px 0 24px; }
  .score h3 { margin:0 0 12px; font-size:13px; text-transform:uppercase; letter-spacing:0.05em; color:var(--dim); }
  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { font-size:12px; padding:5px 10px; border-radius:20px; border:1px solid var(--line);
          display:flex; align-items:center; gap:6px; }
  .dot { width:8px; height:8px; border-radius:50%; }
  .arrow { font-size:11px; opacity:0.85; }
  .theme { margin:30px 0 0; }
  .theme-head { display:flex; align-items:baseline; gap:12px; margin-bottom:2px;
                border-bottom:1px solid var(--line); padding-bottom:8px; }
  .theme-head h2 { margin:0; font-size:17px; }
  .theme-state { font-size:12px; padding:3px 9px; border-radius:14px; font-weight:600; }
  .theme-note { color:var(--dim); font-size:12px; margin-left:auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px; margin-top:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:11px; padding:15px 17px; }
  .card .top { display:flex; justify-content:space-between; align-items:flex-start; }
  .card h4 { margin:0; font-size:14px; font-weight:600; }
  .card .sid { color:var(--dim); font-size:10.5px; font-family:ui-monospace,monospace; }
  .badge { font-size:10.5px; padding:2px 7px; border-radius:10px; font-weight:700;
           text-transform:uppercase; letter-spacing:0.03em; }
  .row { display:flex; align-items:baseline; gap:10px; margin:8px 0 2px; }
  .val { font-size:24px; font-weight:700; }
  .val small { font-size:13px; color:var(--dim); font-weight:500; }
  .move { font-size:12px; font-weight:600; }
  .asof { color:var(--dim); font-size:11px; }
  .cbox { height:130px; margin-top:10px; }
  .note { color:var(--dim); font-size:11.5px; margin-top:9px; line-height:1.45; }
  .calm{color:var(--calm);} .caution{color:var(--caution);} .alert{color:var(--alert);} .neutral{color:var(--neutral);} .dim{color:var(--dim);}
  .bg-calm{background:rgba(63,185,80,.15);color:var(--calm);}
  .bg-caution{background:rgba(210,153,34,.15);color:var(--caution);}
  .bg-alert{background:rgba(248,81,73,.15);color:var(--alert);}
  .bg-neutral{background:rgba(88,166,255,.13);color:var(--neutral);}
  .fail { color:var(--alert); font-size:13px; margin:14px 0; }
</style></head>
<body>
<header>
  <h1>Recession-risk dashboard</h1>
  <div class="stamp">FRED data &middot; __STAMP__ &middot;
    <span class="nav"><a href="index.html">&larr; back to the screener</a></span></div>
</header>
<div class="wrap">
<div id="fail" class="fail"></div>
<div class="gauges" id="gauges"></div>
<div class="score" id="score"></div>
<div id="themes"></div>
</div>
<script>
const D = __DATA__;
const FAILED = __FAILED__;
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
if (FAILED.length) {
  document.getElementById('fail').textContent =
    FAILED.length + ' series could not be loaded from FRED: ' + FAILED.map(f=>f[1]).join(', ');
}
function riskColour(p){ return p>=50?'alert':p>=30?'caution':'calm'; }

const g = document.getElementById('gauges');
const ny = D.ny;
if (ny && ny.latest){
  const p = ny.latest[1], c = riskColour(p);
  const dtxt = ny.trend ? (ny.trend.delta>=0?'+':'')+ny.trend.delta.toFixed(0)+' pts vs a year ago' : '';
  const dcls = ny.trend ? (ny.trend.delta>0?'alert':ny.trend.delta<0?'calm':'dim') : 'dim';
  g.innerHTML += `<div class="gauge">
    <div class="k">Recession probability &middot; 12 months ahead</div>
    <div class="big ${c}">${p.toFixed(0)}%</div>
    <div class="sub">NY Fed / Estrella-Mishkin model from the 10Y-3M curve. 30% has preceded every recession since 1969.</div>
    <div class="track"><div class="fill" style="width:${Math.min(100,p)}%;background:${css('--'+c)}"></div>
      <div class="thresh" style="left:30%"></div></div>
    <div class="delta ${dcls}">${dtxt}${ny.trend&&ny.trend.delta>0?' &middot; rising':ny.trend&&ny.trend.delta<0?' &middot; falling':''}</div>
  </div>`;
}
const co = D.coincident;
if (co && co.latest){
  const p = co.latest[1], c = riskColour(p);
  g.innerHTML += `<div class="gauge">
    <div class="k">In a recession now? &middot; coincident</div>
    <div class="big ${c}">${p.toFixed(0)}%</div>
    <div class="sub">Chauvet-Piger smoothed model (RECPROUSM156N) from four coincident indicators. Confirms rather than leads.</div>
    <div class="track"><div class="fill" style="width:${Math.min(100,p)}%;background:${css('--'+c)}"></div></div>
    <div class="delta dim">as of ${co.latest[0]}</div>
  </div>`;
}

const sc = D.scorecard || [];
const nAlert = sc.filter(x=>x.state==='alert').length;
const nCaution = sc.filter(x=>x.state==='caution').length;
const nDet = sc.filter(x=>x.deteriorating).length;
let scHTML = `<h3>Signal scorecard &middot; ${nAlert} alert, ${nCaution} caution, ${nDet} deteriorating of ${sc.length}</h3><div class="chips">`;
sc.forEach(x=>{ scHTML += `<span class="chip"><span class="dot" style="background:${css('--'+x.state)}"></span>${x.label}${x.deteriorating?' <span class="arrow">&#9650;</span>':''}</span>`; });
scHTML += `</div>`;
document.getElementById('score').innerHTML = scHTML;

const tRoot = document.getElementById('themes');
Object.keys(D.themes).forEach(theme=>{
  const panels = D.themes[theme]; if(!panels.length) return;
  const ts = D.theme_states[theme] || {state:'neutral',deteriorating:0,total:panels.length};
  const sec = document.createElement('div'); sec.className='theme';
  const detTxt = ts.deteriorating>0 ? `${ts.deteriorating} of ${ts.total} deteriorating` : 'stable';
  sec.innerHTML = `<div class="theme-head"><h2>${theme}</h2>
    <span class="theme-state bg-${ts.state}">${ts.state}</span>
    <span class="theme-note">${detTxt}</span></div><div class="cards"></div>`;
  tRoot.appendChild(sec);
  const cardsEl = sec.querySelector('.cards');
  panels.forEach((p,idx)=>{
    const cid = theme.replace(/[^a-z]/gi,'')+idx;
    const mv = p.trend;
    let moveTxt='', moveCls='dim';
    if(mv){ const d=mv.delta, sign=d>0?'+':'';
      moveTxt = `${sign}${Math.abs(d)<10?d.toFixed(2):d.toFixed(0)} ${p.units} over 6mo`;
      if(p.worry==='up') moveCls=d>0?'alert':'calm';
      else if(p.worry==='down') moveCls=d<0?'alert':'calm'; }
    const pctTxt = mv ? ` &middot; ${mv.pct_of_range.toFixed(0)}th pctile of its range` : '';
    const card = document.createElement('div'); card.className='card';
    card.innerHTML = `<div class="top"><div><h4>${p.label}</h4><div class="sid">${p.series_id}</div></div>
      <span class="badge bg-${p.state}">${p.state}</span></div>
      <div class="row"><span class="val ${p.state}">${p.latest}<small> ${p.units}</small></span>
      ${mv?`<span class="move ${moveCls}">${p.deteriorating?'&#9650; ':''}${moveTxt}</span>`:''}</div>
      <div class="asof">as of ${p.latest_date}${pctTxt}</div>
      <div class="cbox"><canvas id="cv-${cid}"></canvas></div>
      <div class="note">${p.note}</div>`;
    cardsEl.appendChild(card);
    const pts = p.points, step = Math.max(1,Math.floor(pts.length/500));
    const thin = pts.filter((_,k)=>k%step===0);
    const ds = [{ data: thin.map(x=>({x:x[0],y:x[1]})), borderColor:css('--neutral'),
                  borderWidth:1.5, pointRadius:0, tension:0.08, fill:false }];
    function refLine(val,cv){ if(val===null||val===undefined) return;
      ds.push({ data:[{x:thin[0][0],y:val},{x:thin[thin.length-1][0],y:val}],
        borderColor:css(cv), borderWidth:1, borderDash:[4,4], pointRadius:0, fill:false }); }
    refLine(p.caution,'--caution'); refLine(p.alert,'--alert');
    new Chart(document.getElementById('cv-'+cid),{ type:'line', data:{datasets:ds},
      options:{ responsive:true, maintainAspectRatio:false, animation:false,
        plugins:{legend:{display:false}, tooltip:{intersect:false,mode:'index',
          callbacks:{title:i=>i[0].raw.x, label:i=>i.raw.y+' '+p.units}}},
        scales:{ x:{type:'time',time:{unit:'year'},ticks:{color:css('--dim'),font:{size:9},maxTicksLimit:6},grid:{color:css('--grid')}},
                 y:{ticks:{color:css('--dim'),font:{size:10},maxTicksLimit:4},grid:{color:css('--grid')}} } } });
  });
});
</script>
</body></html>"""

if __name__ == "__main__":
    build()
