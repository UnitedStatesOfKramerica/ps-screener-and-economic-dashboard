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
        {"id": "IC4WSA", "label": "Initial jobless claims (4wk)", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01", "scale": 0.001,
         "fmt": "count", "caution": 300, "alert": 375,
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
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "caution": None, "alert": None,
         "note": "Homebuilding is rate-sensitive and turns before the cycle; "
                 "falling starts is an early-warning channel."},
        {"id": "UMCSENT", "label": "Consumer sentiment", "kind": "level",
         "units": "", "worry": "down", "start": "1990-01-01",
         "caution": 70, "alert": 60,
         "note": "University of Michigan survey. Weak and falling sentiment "
                 "precedes pullbacks in consumer spending."},
    ],
    "Consumer": [
        {"id": "RRSFS", "label": "Real retail sales (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1993-01-01",
         "caution": 1.0, "alert": 0.0,
         "note": "Inflation-adjusted spending. Growth slowing toward zero means the "
                 "consumer -- two-thirds of GDP -- is pulling back; a direct read on "
                 "consumer-discretionary exposure."},
        {"id": "PSAVERT", "label": "Personal saving rate", "kind": "level",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "caution": 4.0, "alert": 3.0,
         "note": "A very low saving rate means households are spending beyond their "
                 "cushion -- fine while jobs hold, fragile if they don't. Low and "
                 "falling is late-cycle behaviour."},
        {"id": "DRCCLACBS", "label": "Credit-card delinquency", "kind": "level",
         "units": "%", "worry": "up", "start": "1991-01-01",
         "caution": 3.5, "alert": 5.0,
         "note": "The first crack in consumer credit -- cards go bad before autos "
                 "and mortgages. Rising delinquency is early evidence the low-end "
                 "consumer is stretched."},
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
    "Valuation": [
        {"id": "Shiller CAPE", "label": "Shiller CAPE (10-yr P/E)", "compute": "cape",
         "kind": "level", "units": "x", "worry": "up", "start": "1990-01-01",
         "note": "Price divided by ten years of average real earnings -- the most-"
                 "cited long-run valuation gauge, smoothing through the profit cycle "
                 "that distorts a one-year P/E. Above ~30 has clustered around 1929, "
                 "2000 and 2021. It says little about the next year but a lot about "
                 "the next decade's returns."},
        {"id": "Market cap / GDP", "label": "Buffett indicator (market cap / GDP)",
         "compute": "ratio", "nums": ["NCBEILQ027S", "FBCELLQ027S"], "den": "GDP",
         "ratio_scale": 0.1, "kind": "level", "units": "%", "worry": "up",
         "start": "1990-01-01",
         "note": "Total US equity market value -- non-financial plus financial "
                 "corporate equities -- against the size of the economy. Buffett's "
                 "'best single measure' of what you are paying for American business. "
                 "It does not time tops, but extreme readings pull future returns "
                 "forward; read it against its own history."},
        {"id": "CP / GDP", "label": "Corporate profit share of GDP",
         "compute": "ratio", "num": "CP", "den": "GDP", "ratio_scale": 100,
         "kind": "level", "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "After-tax corporate profits as a share of GDP. Margins mean-revert "
                 "-- high profits draw competition, labour and regulation -- so a "
                 "historically elevated share flatters earnings the market may be "
                 "extrapolating. The valuation risk a simple P/E hides."},
    ],
}

# Drill-down sub-indicators: the signals that move BEFORE the headline in each
# theme. Same schema as THEMES. State for these is derived from where the latest
# reading sits in its OWN historical range (see substate_of) rather than invented
# absolute thresholds, so caution/alert are intentionally omitted. Every entry has
# a `worry` direction, which drives both the coloured 6-month move and the
# for/against split rendered under the theme. Only "Labor market" is populated for
# now; the same pattern extends to the other three themes.
DRILLDOWNS = {
    "Labor market": [
        {"id": "TEMPHELPS", "label": "Temporary-help employment", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "note": "Staffing firms shed temps before cutting permanent staff, so this "
                 "turns down first. A sustained decline is an early cyclical-risk "
                 "flag -- lighten high-beta, economically-sensitive exposure before "
                 "the headline confirms."},
        {"id": "AWHAETP", "label": "Average weekly hours", "kind": "level",
         "units": "hrs", "worry": "down", "start": "2006-03-01",
         "note": "Employers trim hours before headcount. Falling hours mean firms "
                 "are quietly cutting labour input -- a lead on hiring, then payrolls, "
                 "weakening next."},
        {"id": "CCSA", "label": "Continued jobless claims", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01", "scale": 0.001, "fmt": "count",
         "note": "Rising continued claims mean the newly unemployed take longer to "
                 "find work -- a hardening market even while layoffs stay low. Weekly, "
                 "so the timeliest hard-data labour signal on the page."},
        {"id": "JTSQUR", "label": "Quits rate", "kind": "level",
         "units": "%", "worry": "down", "start": "2000-12-01",
         "note": "Workers quit when confident of something better; the rate falls "
                 "when they turn cautious. A falling quits rate leads wage growth "
                 "down -- supports easing off wage-inflation-sensitive positioning."},
        {"id": "LNS13026638", "label": "Permanent job losers", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01", "fmt": "count",
         "note": "The structural, slow-to-reverse kind of job loss (vs temporary "
                 "layoff). Rising permanent losers is a more serious deterioration "
                 "signal than a temp-layoff blip -- watch this one against the next."},
        {"id": "LNS13023653", "label": "Temporary layoffs", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01", "fmt": "count",
         "note": "Job losers on temporary layoff -- the reversible kind, and often "
                 "noisy (one-off shutdowns). The question is whether a rise here is "
                 "truly temporary or feeds through into permanent losers, which is worse."},
        {"id": "LNS12032194", "label": "Part-time for economic reasons", "kind": "level",
         "units": "K", "worry": "up", "start": "1990-01-01", "fmt": "count",
         "note": "People who want full-time work but are stuck part-time because "
                 "business is slow. Rising involuntary part-time is hidden slack the "
                 "headline unemployment rate misses -- an early read on softening "
                 "labour demand."},
        {"id": "CIVPART", "label": "Labor-force participation", "kind": "level",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "note": "The share of working-age people in the labour force. A falling "
                 "participation rate can flatter the unemployment rate (people "
                 "leaving the workforce), so read the two together."},
        {"id": "U6RATE", "label": "Underemployment (U-6)", "kind": "level",
         "units": "%", "worry": "up", "start": "1994-01-01",
         "note": "The broad rate -- adds discouraged workers and involuntary "
                 "part-timers to the headline U-3. U-6 rising while U-3 is flat is "
                 "hidden softening the headline misses."},
    ],
    "Inflation & policy": [
        {"id": "T5YIE", "label": "5-year breakeven inflation", "kind": "level",
         "units": "%", "worry": "up", "start": "2003-01-01",
         "note": "What the bond market prices for average inflation over the next "
                 "five years, in real time. Rising breakevens are the earliest sign "
                 "expectations are drifting up -- the trigger to tilt toward "
                 "energy/value and TIPS, away from long-duration bonds and growth."},
        {"id": "T5YIFR", "label": "5y5y forward inflation", "kind": "level",
         "units": "%", "worry": "up", "start": "2003-01-01",
         "note": "The Fed's preferred long-run gauge -- expected inflation in years "
                 "six to ten, stripped of near-term shocks. Drift up here means the "
                 "market is doubting the 2% anchor itself: a more durable signal for "
                 "the value/energy tilt than spot CPI."},
        {"id": "CORESTICKM159SFRBATL", "label": "Sticky-price CPI (core)", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "The slow-to-reprice part of the basket -- rent, insurance, "
                 "services. It is the persistent core of inflation; while it stays "
                 "high the Fed stays higher-for-longer, keeping pressure on "
                 "long-duration assets regardless of headline CPI."},
        {"id": "FRBATLWGT3MMAWMHWGO", "label": "Wage growth tracker", "kind": "level",
         "units": "%", "worry": "up", "start": "1997-01-01",
         "note": "Median wage growth feeds services inflation, the stickiest "
                 "component. Re-accelerating wages make the last mile of disinflation "
                 "hard and keep the Fed cautious -- reinforces staying underweight "
                 "long-duration."},
        {"id": "PPIFIS", "label": "Producer prices (final demand)", "kind": "yoy",
         "units": "%", "worry": "up", "start": "2009-11-01",
         "note": "Producer prices sit upstream of consumer prices, so pressure here "
                 "shows up in CPI months later. An early read on whether goods "
                 "disinflation is stalling or reversing."},
        {"id": "IR", "label": "Import prices", "kind": "yoy",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Import prices capture globally-sourced and FX-driven cost pressure "
                 "before it reaches shelves. A weaker dollar or global cost-push "
                 "lands here first -- a channel headline CPI picks up only later."},
        {"id": "DCOILWTICO", "label": "WTI crude oil", "kind": "level",
         "units": "$", "worry": "up", "start": "1990-01-01",
         "note": "Real-time energy cost -- feeds headline inflation within weeks and "
                 "is the clearest 'add to energy' trigger. Rising crude is "
                 "inflationary and a tailwind for energy equities; it doubles as a "
                 "growth/demand signal, so read a spike alongside the Growth theme."},
    ],
    "Financial conditions": [
        {"id": "STLFSI4", "label": "Financial stress index", "kind": "level",
         "units": "", "worry": "up", "start": "1994-01-01",
         "note": "The St. Louis Fed's 18-input stress gauge, centred at zero. Above "
                 "zero and rising means market stress is building -- an early, broad "
                 "risk-off trigger before spreads blow out."},
        {"id": "NFCIRISK", "label": "NFCI risk subindex", "kind": "level",
         "units": "", "worry": "up", "start": "1990-01-01",
         "note": "The volatility and funding-risk piece of financial conditions. "
                 "Turns up first when markets get jumpy -- the leading limb of the "
                 "headline NFCI."},
        {"id": "NFCICREDIT", "label": "NFCI credit subindex", "kind": "level",
         "units": "", "worry": "up", "start": "1990-01-01",
         "note": "The credit-conditions piece -- lending standards and spreads. "
                 "Tightening here is the channel that chokes off cyclical and "
                 "small-cap financing."},
        {"id": "NFCILEVERAGE", "label": "NFCI leverage subindex", "kind": "level",
         "units": "", "worry": "up", "start": "1990-01-01",
         "note": "Debt and equity leverage in the system. Elevated leverage is dry "
                 "tinder -- it amplifies any shock, so a high reading raises the "
                 "stakes of everything else."},
        {"id": "VIXCLS", "label": "Volatility (VIX)", "kind": "level",
         "units": "", "worry": "up", "start": "1990-01-01",
         "note": "Equity-market fear. Spikes are contrarian short-term, but a "
                 "sustained rise off lows is a genuine risk-off signal -- a timing "
                 "input more than a trend-setter."},
        {"id": "DFII10", "label": "10-year real yield", "kind": "level",
         "units": "%", "worry": "up", "start": "2003-01-01",
         "note": "The 10-year TIPS yield -- interest rates after inflation, and the "
                 "discount rate for every long-duration asset. Rising real yields "
                 "compress valuations and are the true headwind for gold and long "
                 "bonds; falling real yields are the tailwind."},
    ],
    "Growth": [
        {"id": "NEWORDER", "label": "Core capital-goods orders (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1993-01-01",
         "note": "Non-defence capital goods ex-aircraft -- what businesses order "
                 "when confident. It leads capex and manufacturing; rolling over is "
                 "an early cyclical-downturn tell."},
        {"id": "PERMIT", "label": "Building permits", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "note": "Permits lead housing starts, which lead the cycle -- the earliest "
                 "point in the most rate-sensitive part of the economy. Watch it "
                 "ahead of the starts headline you already track."},
        {"id": "HTRUCKSSAAR", "label": "Heavy truck sales", "kind": "level",
         "units": "M", "worry": "down", "start": "1990-01-01",
         "note": "A classic recession lead: fleet buyers cut heavy-truck orders "
                 "before the downturn shows up elsewhere. A sustained drop off the "
                 "highs is a reliable late-cycle warning."},
        {"id": "CFNAI", "label": "National activity index", "kind": "level",
         "units": "", "worry": "down", "start": "1990-01-01",
         "note": "An 85-indicator composite of US activity, centred at zero -- zero "
                 "is trend growth, negative is below-trend. A broad confirmation "
                 "that ties the single-series growth signals together."},
    ],
    "Consumer": [
        {"id": "DSPIC96", "label": "Real disposable income (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "note": "Inflation-adjusted take-home pay -- the fuel for spending. When "
                 "real income growth stalls, retail sales follow, and any spending "
                 "above it is coming out of savings or credit."},
        {"id": "TDSP", "label": "Household debt-service ratio", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Required debt payments as a share of disposable income. Near the "
                 "top of its own range is where debt burdens start to crowd out "
                 "spending -- the 2007 peak was the warning."},
        {"id": "REVOLSL", "label": "Revolving credit (YoY)", "kind": "yoy",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Credit-card balances growing fast, especially while the saving "
                 "rate falls, means households are leaning on cards to keep "
                 "spending. Acceleration off a low base is the tell."},
        {"id": "DRSFRMACBS", "label": "Mortgage delinquency", "kind": "level",
         "units": "%", "worry": "up", "start": "1991-01-01",
         "note": "Single-family mortgage delinquency -- slower-moving but higher "
                 "stakes than cards. A sustained rise off historic lows is a "
                 "housing-stress and financial-stability signal."},
    ],
    "Valuation": [
        {"id": "BOGZ1FL153064486Q", "label": "Household equity allocation", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Share of household financial assets held in stocks. It peaks when "
                 "the public is all-in -- it topped near the 2000 and 2021 highs -- "
                 "and troughs at bottoms. A contrarian gauge: an extreme reading means "
                 "little marginal buying power is left."},
        {"id": "NCBCEPNW", "label": "Equities vs net worth (Tobin's Q)", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Corporate equity value against companies' net worth -- a Tobin's-Q "
                 "proxy. Well above 100% the market prices firms far over the "
                 "replacement cost of their assets; it mean-reverts over long horizons."},
    ],
}

# ---- Allocation layer ---------------------------------------------------------
# Buckets the dashboard forms a lean on, in display order.
ALLOC_BUCKETS = [
    "Long-duration Treasuries", "Overall equity exposure", "Value over Growth",
    "Energy", "Real assets & commodities", "Defensive equities",
    "Cyclicals & small caps", "High-yield credit", "Gold",
]
# What each signal argues for when it is ACTIVE (moving its worrying way over six
# months, or sitting at a caution/alert level). OW/UW = over/underweight. Curated
# to a balanced, high-signal subset so no one theme dominates by sheer count.
BUCKET_DEF = {
    "Long-duration Treasuries": "Long-dated US government bonds (10y+). Overweight adds duration -- it gains when growth and inflation fall and the Fed cuts, and loses when inflation runs hot.",
    "Overall equity exposure": "How much to hold in stocks at all, versus cash and bonds. Overweight leans risk-on; underweight de-risks toward cash and quality as conditions tighten.",
    "Value over Growth": "Cheap, low-multiple stocks (energy, financials, industrials) versus expensive long-duration growth (tech). Tilts to value when inflation and rates rise.",
    "Energy": "Oil, gas and energy equities. Overweight when inflation and crude are rising -- a direct inflation hedge that also tracks demand.",
    "Defensive equities": "Stable-demand sectors -- staples, utilities, healthcare -- that hold up in downturns regardless of price. This is about earnings stability, not cheapness (that is Value).",
    "Cyclicals & small caps": "Economically-sensitive stocks -- industrials, materials, discretionary, small caps -- that need growth and easy credit. Underweight when the cycle turns down.",
    "High-yield credit": "Below-investment-grade corporate bonds. Underweight when spreads widen or credit conditions tighten, because default risk and drawdowns rise together.",
    "Real assets & commodities": "Commodities, TIPS, real estate and infrastructure -- assets whose real value holds through inflation. Overweight when realised and expected inflation are rising.",
    "Gold": "A monetary and tail hedge that tracks real interest rates and stress, not inflation itself. It rises when real yields fall or fear spikes, and stalls when real yields climb -- which is why it went nowhere in 2022 despite high inflation.",
}

ALLOC = {
    # Inflation complex running hot -> away from duration, toward energy/value
    "T5YIE": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW"), ("Energy", "OW"), ("Real assets & commodities", "OW")],
    "T5YIFR": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW"), ("Energy", "OW"), ("Real assets & commodities", "OW")],
    "CORESTICKM159SFRBATL": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW")],
    "FRBATLWGT3MMAWMHWGO": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW")],
    "PPIFIS": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW"), ("Energy", "OW"), ("Real assets & commodities", "OW")],
    "IR": [("Value over Growth", "OW"), ("Energy", "OW"), ("Real assets & commodities", "OW")],
    "CPIAUCSL": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW"), ("Energy", "OW"), ("Real assets & commodities", "OW")],
    "PCEPILFE": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW")],
    "DCOILWTICO": [("Energy", "OW"), ("Value over Growth", "OW"), ("Long-duration Treasuries", "UW"), ("Real assets & commodities", "OW")],
    # Real yield: the discount rate. Rising real yields hurt gold and long bonds, favour value.
    "DFII10": [("Gold", "UW"), ("Long-duration Treasuries", "UW"), ("Value over Growth", "OW")],
    # Financial stress / tightening -> risk-off, safe havens
    "BAMLH0A0HYM2": [("Overall equity exposure", "UW"), ("High-yield credit", "UW"),
                     ("Defensive equities", "OW"), ("Long-duration Treasuries", "OW")],
    "NFCI": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "NFCICREDIT": [("High-yield credit", "UW"), ("Cyclicals & small caps", "UW"), ("Overall equity exposure", "UW")],
    "STLFSI4": [("Overall equity exposure", "UW"), ("High-yield credit", "UW"), ("Defensive equities", "OW"),
                ("Long-duration Treasuries", "OW"), ("Gold", "OW")],
    "VIXCLS": [("Overall equity exposure", "UW"), ("Defensive equities", "OW"), ("Gold", "OW")],
    "T10Y3M": [("Overall equity exposure", "UW"), ("Long-duration Treasuries", "OW"),
               ("Defensive equities", "OW"), ("Cyclicals & small caps", "UW")],
    # Labour / growth / consumer weakening -> defensive, duration
    "SAHMREALTIME": [("Overall equity exposure", "UW"), ("Defensive equities", "OW"),
                     ("Cyclicals & small caps", "UW"), ("Long-duration Treasuries", "OW")],
    "IC4WSA": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "TEMPHELPS": [("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "CFNAI": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "NEWORDER": [("Cyclicals & small caps", "UW"), ("Overall equity exposure", "UW")],
    "DRCCLACBS": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "RRSFS": [("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
}

# Per-signal weight in the allocation tally -- marquee signals count more than
# minor ones. Default 1.0 for anything unlisted. Conviction is the weighted margin.
SIGNAL_WEIGHT = {
    "T10Y3M": 2.0, "SAHMREALTIME": 2.0,
    "BAMLH0A0HYM2": 1.5, "DFII10": 1.5,
    "T5YIE": 1.25, "T5YIFR": 1.25, "STLFSI4": 1.25, "NFCI": 1.25,
    "CPIAUCSL": 1.25, "PCEPILFE": 1.25, "IC4WSA": 1.25, "CFNAI": 1.25,
    "CORESTICKM159SFRBATL": 1.0, "VIXCLS": 1.0, "NFCICREDIT": 1.0,
    "DCOILWTICO": 1.0, "TEMPHELPS": 1.0, "NEWORDER": 1.0, "DRCCLACBS": 1.0, "RRSFS": 1.0,
    "FRBATLWGT3MMAWMHWGO": 0.75, "PPIFIS": 0.75, "IR": 0.5,
}

# ---- Regime classifier (growth x inflation) ----------------------------------
# Signals whose 6-month direction defines momentum. Growth signals deteriorating
# => growth decelerating; inflation signals deteriorating (rising) => accelerating.
GROWTH_MOM = ["PAYEMS", "INDPRO", "GDPC1", "CFNAI", "NEWORDER", "RRSFS",
              "UNRATE", "IC4WSA", "SAHMREALTIME"]
INFLATION_MOM = ["CPIAUCSL", "PCEPILFE", "T5YIE", "T5YIFR",
                 "CORESTICKM159SFRBATL", "PPIFIS"]
REGIMES = {
    ("accelerating", "accelerating"): ("Reflation",
        "Growth and inflation both rising -- early-cycle. Historically favours "
        "cyclicals, energy, value, commodities and small caps; underweight "
        "long-duration bonds."),
    ("accelerating", "decelerating"): ("Goldilocks",
        "Growth rising while inflation cools -- the friendliest mix for markets. "
        "Favours equities broadly, growth/tech and credit; the case for heavy "
        "defensives and gold is weak."),
    ("decelerating", "accelerating"): ("Stagflation",
        "Growth slowing while inflation runs hot -- the hardest mix. Favours energy, "
        "real assets, gold and TIPS alongside defensive equities; underweight "
        "long-duration bonds, cyclicals and long-duration growth."),
    ("decelerating", "decelerating"): ("Slowdown / Disinflation",
        "Growth and inflation both falling -- late-cycle into contraction. Favours "
        "long-duration Treasuries and quality/defensive equities; underweight "
        "cyclicals, energy and commodities."),
}

# CES supersectors (thousands of persons, SA) for the jobs-by-sector breakdown.
SECTOR_JOBS = [
    ("USMINE", "Mining & logging"), ("USCONS", "Construction"),
    ("MANEMP", "Manufacturing"), ("USTPU", "Trade, transport & utilities"),
    ("USINFO", "Information"), ("USFIRE", "Financial activities"),
    ("USPBS", "Professional & business svcs"), ("USEHS", "Education & health"),
    ("USLAH", "Leisure & hospitality"), ("USSERV", "Other services"),
    ("USGOVT", "Government"),
]


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


def substate_of(worry, pct):
    """State for drill-down sub-indicators, from position in the series' OWN range.

    Avoids inventing absolute thresholds for levels (temp-help, hours, claims,
    job-loser counts) that scale with the labour force. worry='up': near the top
    of its own history is the worrying end; worry='down': near the bottom is.
    """
    if worry is None or pct is None:
        return "neutral"
    if worry == "up":
        return "alert" if pct >= 85 else "caution" if pct >= 65 else "calm"
    return "alert" if pct <= 15 else "caution" if pct <= 35 else "calm"


SHILLER_CAPE_URLS = [
    "http://www.econ.yale.edu/~shiller/data/ie_data.xls",
]


def fetch_cape(start):
    """Shiller CAPE (10-year cyclically-adjusted P/E) from his ie_data.xls.
    Defensive: locates the 'CAPE' column by header text, parses the fractional
    Shiller date (2026.1 == Oct 2026), and fails safe -- any error returns []
    so the dashboard still builds without CAPE."""
    try:
        import xlrd
    except Exception:
        print("  [cape] xlrd not installed -- skipping CAPE"); return []
    book = None
    for url in SHILLER_CAPE_URLS:
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            book = xlrd.open_workbook(file_contents=r.content)
            break
        except Exception as exc:
            print(f"  [cape] {url} failed ({exc})")
    if book is None:
        return []
    try:
        sheet = None
        for nm in book.sheet_names():
            if nm.strip().lower() == "data":
                sheet = book.sheet_by_name(nm); break
        if sheet is None:
            sheet = book.sheet_by_index(0)
        cape_col = header_row = None
        for want_exact in (True, False):     # prefer exact "CAPE" over "TR CAPE"
            for ri in range(min(15, sheet.nrows)):
                for ci in range(sheet.ncols):
                    v = sheet.cell_value(ri, ci)
                    if not isinstance(v, str):
                        continue
                    t = v.strip().upper()
                    hit = (t == "CAPE") if want_exact else ("CAPE" in t and "TR" not in t)
                    if hit:
                        cape_col, header_row = ci, ri; break
                if cape_col is not None:
                    break
            if cape_col is not None:
                break
        if cape_col is None:
            print("  [cape] CAPE column not found"); return []
        start_year = int(start[:4])
        out = []
        for ri in range(header_row + 1, sheet.nrows):
            dv = sheet.cell_value(ri, 0)
            cv = sheet.cell_value(ri, cape_col)
            if not isinstance(dv, (int, float)) or dv <= 0:
                continue
            if not isinstance(cv, (int, float)) or cv <= 0:
                continue
            year = int(dv)
            month = int(round((dv - year) * 100))
            if month < 1 or month > 12 or year < start_year:
                continue
            out.append((f"{year:04d}-{month:02d}-01", float(cv)))
        if out:
            print(f"  [cape] loaded {len(out)} points, latest {out[-1][1]:.1f}x")
        else:
            print("  [cape] no rows parsed")
        return out
    except Exception as exc:
        print(f"  [cape] parse error: {exc}"); return []


def fetch_sum(ids, start):
    """Sum several FRED series on their common dates (e.g. non-financial +
    financial corporate equities). Returns [] if any input is missing."""
    series = [fetch(i, start) for i in ids]
    if any(not s for s in series):
        return []
    maps = [dict(s) for s in series]
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    return sorted((d, sum(m[d] for m in maps)) for d in common)


def ratio_align(num, den, scale=1.0):
    """Numerator series over a (possibly lower-frequency) denominator, aligning
    each numerator date to the most recent denominator value on or before it."""
    if not num or not den:
        return []
    dd = sorted((datetime.strptime(d, "%Y-%m-%d").date(), v) for d, v in den)
    out = []
    for d, nv in num:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        dv = None
        for dday, dval in dd:
            if dday <= dt:
                dv = dval
            else:
                break
        if dv:
            out.append((d, nv / dv * scale))
    return out


def fetch_ratio(num_id, den_id, start, scale=1.0):
    return ratio_align(fetch(num_id, start), fetch(den_id, start), scale)


def panel_for(ind, percentile_state=False):
    """Fetch one indicator and build its panel dict. Returns (panel, None) on
    success or (None, fail_tuple) on failure. Shared by the main themes and the
    drill-down sub-indicators so both get identical treatment."""
    if ind.get("compute") == "ratio":
        if ind.get("nums"):
            num_series = fetch_sum(ind["nums"], ind["start"])
        else:
            num_series = fetch(ind["num"], ind["start"])
        raw = ratio_align(num_series, fetch(ind["den"], ind["start"]),
                          ind.get("ratio_scale", 1.0))
    elif ind.get("compute") == "cape":
        raw = fetch_cape(ind["start"])
    else:
        raw = fetch(ind["id"], ind["start"])
    if not raw:
        return None, (ind["id"], ind["label"])
    scale = ind.get("scale")
    if scale:
        raw = [(d, v * scale) for d, v in raw]
    series = yoy(raw) if ind["kind"] == "yoy" else raw
    if not series:
        return None, (ind["id"], ind["label"] + " (empty after transform)")
    latest = series[-1][1]
    tr = trend(series)
    if percentile_state:
        st = substate_of(ind["worry"], tr["pct_of_range"] if tr else None)
    else:
        st = state_of(ind["worry"], latest, ind.get("caution"), ind.get("alert"))
    deteriorating = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    panel = {
        "label": ind["label"], "series_id": ind["id"], "units": ind["units"],
        "worry": ind["worry"], "note": ind["note"], "state": st,
        "fmt": ind.get("fmt"),
        "caution": ind.get("caution"), "alert": ind.get("alert"),
        "latest": round(latest, 2), "latest_date": series[-1][0],
        "trend": tr, "deteriorating": deteriorating,
        "points": [[d, round(v, 3)] for d, v in series]}
    return panel, None


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
        pctile = theme == "Valuation"   # value cards read vs their own history
        for ind in inds:
            panel, fail = panel_for(ind, percentile_state=pctile)
            if panel is None:
                failed.append(fail); continue
            panels.append(panel)
            scorecard.append({"theme": theme, "label": ind["label"],
                              "state": panel["state"],
                              "deteriorating": panel["deteriorating"]})
            print(f"  {ind['id']:<14} {len(panel['points']):>5} pts  "
                  f"latest {panel['latest']:.2f}  state={panel['state']} "
                  f"{'worse' if panel['deteriorating'] else 'ok'}")
        themes_out[theme] = panels

    drill_out = {}
    for theme, inds in DRILLDOWNS.items():
        subs = []
        for ind in inds:
            panel, fail = panel_for(ind, percentile_state=True)
            if panel is None:
                failed.append(fail); continue
            subs.append(panel)
            print(f"  [drill] {ind['id']:<12} {len(panel['points']):>5} pts  "
                  f"latest {panel['latest']:.2f}  state={panel['state']} "
                  f"{'worse' if panel['deteriorating'] else 'ok'}")
        if subs:
            drill_out[theme] = subs

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

    # ---- Capital allocation: fold active signals into bucket leans ----
    all_panels = []
    for pls in themes_out.values():
        all_panels += pls
    for pls in drill_out.values():
        all_panels += pls
    by_id = {p["series_id"]: p for p in all_panels}
    bucket_signals = {b: [] for b in ALLOC_BUCKETS}
    for sid, imps in ALLOC.items():
        for bucket, lean in imps:
            bucket_signals[bucket].append((sid, lean))

    def _active(p):
        return bool(p["deteriorating"] or p["state"] in ("caution", "alert"))

    allocation = []
    for b in ALLOC_BUCKETS:
        ow = uw = 0.0
        n_ow = n_uw = 0
        drivers = []
        for sid, lean in bucket_signals[b]:
            p = by_id.get(sid)
            if not p:
                continue
            w = SIGNAL_WEIGHT.get(sid, 1.0)
            act = _active(p)
            if act:
                if lean == "OW":
                    ow += w; n_ow += 1
                else:
                    uw += w; n_uw += 1
            drivers.append({"label": p["label"], "lean": lean, "active": act,
                            "state": p["state"], "weight": w})
        drivers.sort(key=lambda d: (not d["active"], -d["weight"], d["lean"]))
        net = ow - uw
        active_total = ow + uw
        if net > 1e-9:
            lean = "Overweight"
        elif net < -1e-9:
            lean = "Underweight"
        elif active_total > 0:
            lean = "Balanced"
        else:
            lean = "No signal"
        mag = abs(net)
        conviction = ("strong" if mag >= 3.0 else "moderate" if mag >= 1.5
                      else "slight" if mag > 0 else "none")
        allocation.append({
            "bucket": b, "definition": BUCKET_DEF.get(b, ""),
            "lean": lean, "conviction": conviction, "net": round(net, 2),
            "ow": [d["label"] for d in drivers if d["active"] and d["lean"] == "OW"],
            "uw": [d["label"] for d in drivers if d["active"] and d["lean"] == "UW"],
            "drivers": drivers})
    n_active = sum(1 for p in all_panels if _active(p))
    print(f"  [alloc] {n_active} active signals -> "
          f"{sum(1 for a in allocation if a['lean'] in ('Overweight', 'Underweight'))} directional tilts")

    # ---- Regime: growth x inflation, plus a valuation condition ----
    def _decel(ids):
        ps = [by_id[i] for i in ids if i in by_id]
        det = sum(1 for p in ps if p["deteriorating"])
        return det, len(ps)
    g_det, g_tot = _decel(GROWTH_MOM)
    i_det, i_tot = _decel(INFLATION_MOM)
    growth = "decelerating" if (g_tot and g_det > g_tot / 2) else "accelerating"
    inflation = "accelerating" if (i_tot and i_det > i_tot / 2) else "decelerating"
    rname, rplay = REGIMES[(growth, inflation)]
    val_panels = themes_out.get("Valuation", []) + drill_out.get("Valuation", [])
    v_alert = sum(1 for p in val_panels if p["state"] == "alert")
    v_tot = len(val_panels)
    if v_tot and v_alert >= max(2, v_tot - 1):
        valcond = "extreme"
    elif v_alert >= 1:
        valcond = "elevated"
    else:
        valcond = "normal"
    cape_p = next((p for p in val_panels if p["label"].startswith("Shiller CAPE")), None)
    valnote = (f"Shiller CAPE {cape_p['latest']:.0f}x" if cape_p
               else "market cap/GDP and household equity allocation near records")
    regime = {"name": rname, "growth": growth, "inflation": inflation,
              "playbook": rplay, "valuation": valcond, "valnote": valnote}
    print(f"  [regime] {rname} (growth {growth}, inflation {inflation}) "
          f"| valuations {valcond} [{g_det}/{g_tot} growth decel, "
          f"{i_det}/{i_tot} infl accel]")

    # ---- Jobs by sector: payroll change (thousands) over 12 and 3 months ----
    def _change_over(series, days):
        if len(series) < 2:
            return None
        dts = [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in series]
        target = dts[-1] - timedelta(days=days)
        near = min(range(len(dts)), key=lambda i: abs((dts[i] - target).days))
        if abs((dts[near] - target).days) > 45:
            return None
        return series[-1][1] - series[near][1]
    sectors, jobs_asof = [], None
    for sid, lbl in SECTOR_JOBS:
        s = fetch(sid, "2015-01-01")
        if not s:
            failed.append((sid, lbl)); continue
        jobs_asof = s[-1][0]
        c12, c3 = _change_over(s, 365), _change_over(s, 92)
        if c12 is None:
            continue
        sectors.append({"label": lbl, "chg12": round(c12, 1),
                        "chg3": round(c3, 1) if c3 is not None else None,
                        "latest": round(s[-1][1], 1)})
        print(f"  [jobs] {sid:<8} 12m {c12:+6.0f}k  "
              f"3m {('%+.0f' % c3) if c3 is not None else '   na'}k")
    sectors.sort(key=lambda x: x["chg12"], reverse=True)
    jobs = {"asof": jobs_asof, "sectors": sectors}

    payload = {
        "ny": {"latest": ny_latest, "trend": ny_trend, "points": ny_series},
        "coincident": {"latest": (coin[-1] if coin else None),
                       "points": [[d, round(v, 1)] for d, v in coin]},
        "themes": themes_out, "theme_states": theme_states, "scorecard": scorecard,
        "drilldowns": drill_out, "allocation": allocation, "jobs": jobs,
        "regime": regime}
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
  .cbox { height:130px; margin-top:10px; position:relative; }
  .note { color:var(--dim); font-size:11.5px; margin-top:9px; line-height:1.45; }
  .calm{color:var(--calm);} .caution{color:var(--caution);} .alert{color:var(--alert);} .neutral{color:var(--neutral);} .dim{color:var(--dim);}
  .bg-calm{background:rgba(63,185,80,.15);color:var(--calm);}
  .bg-caution{background:rgba(210,153,34,.15);color:var(--caution);}
  .bg-alert{background:rgba(248,81,73,.15);color:var(--alert);}
  .bg-neutral{background:rgba(88,166,255,.13);color:var(--neutral);}
  .regime-banner { background:var(--card); border:1px solid var(--line); border-radius:13px; padding:16px 18px; margin-bottom:18px; }
  .regime-top { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .regime-tag { font-size:10px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); border:1px solid var(--line); border-radius:20px; padding:2px 9px; }
  .regime-top h2 { margin:0; font-size:22px; }
  .regime-sub { color:var(--dim); font-size:12.5px; }
  .regime-play { color:var(--ink); font-size:13px; line-height:1.55; margin:9px 0 10px; max-width:900px; }
  .regime-val { font-size:12px; color:var(--dim); display:flex; align-items:center; gap:8px; }
  .regime-valnote { color:var(--dim); }
  .alloc-h { font-size:18px; margin:6px 0 4px; }
  .alloc-sub { color:var(--dim); font-size:12px; margin:0 0 15px; line-height:1.5; max-width:860px; }
  .alloc-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:13px; margin-bottom:30px; }
  .alloc-card { background:var(--card); border:1px solid var(--line); border-radius:11px; padding:14px 16px; }
  .alloc-top { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:9px; }
  .alloc-top h4 { margin:0; font-size:14px; }
  .lean { font-size:10.5px; font-weight:700; padding:4px 10px; border-radius:20px; text-transform:uppercase; letter-spacing:.04em; white-space:nowrap; }
  .alloc-why { font-size:12px; color:var(--dim); line-height:1.5; }
  .alloc-why .side { margin:4px 0; }
  .s-ow { color:var(--calm); font-weight:600; }
  .s-uw { color:var(--alert); font-weight:600; }
  .alloc-badge { display:flex; align-items:center; gap:7px; }
  .conv { display:inline-flex; gap:3px; }
  .conv .seg { width:6px; height:12px; border-radius:2px; background:var(--line); }
  .conv .seg.on.calm { background:var(--calm); }
  .conv .seg.on.alert { background:var(--alert); }
  .conv .seg.on.neutral { background:var(--neutral); }
  .alloc-tally { font-size:10.5px; color:var(--dim); text-transform:uppercase; letter-spacing:.03em; margin:-3px 0 8px; }
  .sc-legend { display:flex; flex-wrap:wrap; gap:14px; margin:6px 0 12px; font-size:12px; color:var(--dim); }
  .sc-key { display:inline-flex; align-items:center; gap:6px; }
  .sc-key .dot { width:9px; height:9px; border-radius:50%; }
  .sc-key .arrow { color:var(--neutral); font-size:10px; }
  .sc-total { opacity:.75; }
  .alloc-card.expandable { cursor:pointer; }
  .alloc-hl { display:flex; align-items:center; gap:6px; }
  .alloc-hl .chev { margin:0; }
  .alloc-detail { display:none; margin-top:11px; padding-top:11px; border-top:1px solid var(--line); }
  .alloc-card.open .alloc-detail { display:block; }
  .alloc-def { font-size:12px; color:var(--dim); line-height:1.5; margin:0 0 11px; }
  .asig-h { font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim); margin-bottom:7px; }
  .drow { display:flex; align-items:center; gap:8px; font-size:12px; margin:5px 0; }
  .drow.off { opacity:.4; }
  .drow .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
  .drow-l { flex:1; color:var(--ink); }
  .drow-t { font-size:10px; text-transform:uppercase; letter-spacing:.03em; }
  .drow-s { font-size:10px; color:var(--dim); width:56px; text-align:right; text-transform:capitalize; }
  .wchip { font-size:8.5px; text-transform:uppercase; letter-spacing:.04em; padding:1px 5px; border-radius:8px; margin-left:6px; vertical-align:middle; }
  .wchip.key { background:rgba(88,166,255,.14); color:var(--neutral); }
  .wchip.minor { background:var(--line); color:var(--dim); }
  .jobs-h { font-size:18px; margin:6px 0 4px; }
  .jobs-sub { color:var(--dim); font-size:12px; margin:0 0 15px; max-width:860px; line-height:1.5; }
  .jobs-list { margin-bottom:30px; }
  .jobs-row { display:flex; align-items:center; gap:10px; margin:5px 0; }
  .jl { width:170px; font-size:12px; color:var(--ink); flex-shrink:0; text-align:right; }
  .jtrack { position:relative; flex:1; height:20px; background:var(--card); border-radius:4px; overflow:hidden; }
  .jcenter { position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--dim); opacity:.5; }
  .jb { position:absolute; top:3px; bottom:3px; border-radius:3px; }
  .jb.pos { background:var(--calm); }
  .jb.neg { background:var(--alert); }
  .jv { width:60px; font-size:12px; font-variant-numeric:tabular-nums; flex-shrink:0; }
  .jv.pos { color:var(--calm); }
  .jv.neg { color:var(--alert); }
  .theme-head.expandable { cursor:pointer; user-select:none; }
  .theme-head.expandable:hover h2 { color:var(--neutral); }
  .chev { display:inline-block; transition:transform .15s; color:var(--dim); font-size:12px; margin-right:2px; }
  .chev.open { transform:rotate(90deg); }
  .lead { color:var(--dim); }
  .drilldown { display:none; margin-top:16px; padding:18px; border:1px solid var(--line);
               border-radius:11px; background:rgba(88,166,255,.03); }
  .drilldown.open { display:block; }
  .dd-intro { color:var(--dim); font-size:12px; margin:0 0 15px; line-height:1.45; }
  .dd-cols { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  @media(max-width:820px){ .dd-cols{ grid-template-columns:1fr; } }
  .dd-col h5 { margin:0 0 12px; font-size:12px; text-transform:uppercase; letter-spacing:.04em;
               padding-left:10px; }
  .dd-col.for h5 { border-left:3px solid var(--alert); color:var(--alert); }
  .dd-col.against h5 { border-left:3px solid var(--calm); color:var(--calm); }
  .dd-col .card { margin-bottom:14px; }
  .dd-col .card:last-child { margin-bottom:0; }
  .dd-empty { color:var(--dim); font-size:12px; font-style:italic; padding-left:10px; }
  .expand { position:absolute; top:6px; right:6px; width:24px; height:24px; border:1px solid var(--line);
            background:rgba(13,16,23,.7); color:var(--dim); border-radius:6px; cursor:pointer;
            font-size:12px; line-height:1; display:flex; align-items:center; justify-content:center;
            opacity:0; transition:opacity .12s; }
  .card:hover .expand { opacity:1; }
  .expand:hover { color:var(--ink); border-color:var(--neutral); }
  @media(hover:none){ .expand{ opacity:.65; } }
  .modal { position:fixed; inset:0; background:rgba(3,5,10,.72); display:none; z-index:50;
           align-items:center; justify-content:center; padding:24px; }
  .modal.open { display:flex; }
  .modal-inner { background:var(--card); border:1px solid var(--line); border-radius:14px;
                 width:min(1000px,96vw); max-height:92vh; overflow:auto; padding:20px 22px; }
  .modal-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .modal-head h3 { margin:0; font-size:18px; }
  #modal-close { background:none; border:none; color:var(--dim); font-size:20px; cursor:pointer; line-height:1; padding:0 4px; }
  #modal-close:hover { color:var(--ink); }
  .modal-meta { margin:10px 0 2px; font-size:13px; color:var(--dim); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .modal-meta .m-val { font-size:22px; font-weight:700; }
  .modal-cbox { height:60vh; min-height:320px; margin:12px 0 12px; }
  .modal-note { color:var(--dim); font-size:13px; line-height:1.5; }
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
<div id="regime"></div>
<div class="gauges" id="gauges"></div>
<div class="score" id="score"></div>
<div id="alloc"></div>
<div id="themes"></div>
</div>
<div id="modal" class="modal">
  <div class="modal-inner">
    <div class="modal-head">
      <div><h3 id="modal-title"></h3><div class="sid" id="modal-sid"></div></div>
      <button id="modal-close" aria-label="Close">&#10005;</button>
    </div>
    <div class="modal-meta" id="modal-meta"></div>
    <div class="modal-cbox"><canvas id="modal-cv"></canvas></div>
    <div class="modal-note" id="modal-note"></div>
  </div>
</div>
<script>
const D = __DATA__;
const FAILED = __FAILED__;
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const stText = s => s==='alert' ? 'danger' : s;

const R = D.regime;
if (R){
  const vcls = R.valuation==='extreme'?'alert':R.valuation==='elevated'?'caution':'calm';
  document.getElementById('regime').innerHTML =
    `<div class="regime-banner">
       <div class="regime-top"><span class="regime-tag">Regime</span>`
       + `<h2>${R.name}</h2>`
       + `<span class="regime-sub">growth ${R.growth} &middot; inflation ${R.inflation}</span></div>`
     + `<p class="regime-play">${R.playbook}</p>`
     + `<div class="regime-val">Valuations <span class="badge bg-${vcls}">${R.valuation}</span>`
       + `<span class="regime-valnote">${R.valnote}</span></div>`
     + `</div>`;
}
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
const nCalm = sc.filter(x=>x.state==='calm').length;
const nNeutral = sc.filter(x=>x.state==='neutral').length;
const nDet = sc.filter(x=>x.deteriorating).length;
const scKey = (v,label,color) => `<span class="sc-key"><span class="dot" style="background:${color}"></span>${v} ${label}</span>`;
let scHTML = `<h3>Signal scorecard</h3><div class="sc-legend">`
  + scKey(nAlert,'danger',css('--alert'))
  + scKey(nCaution,'caution',css('--caution'))
  + scKey(nCalm,'calm',css('--calm'))
  + scKey(nNeutral,'neutral',css('--neutral'))
  + `<span class="sc-key"><span class="arrow">&#9650;</span>${nDet} deteriorating</span>`
  + `<span class="sc-key sc-total">${sc.length} signals</span></div><div class="chips">`;
sc.forEach(x=>{ scHTML += `<span class="chip"><span class="dot" style="background:${css('--'+x.state)}"></span>${x.label}${x.deteriorating?' <span class="arrow">&#9650;</span>':''}</span>`; });
scHTML += `</div>`;
document.getElementById('score').innerHTML = scHTML;

// ---- Capital Allocation ----
const allocEl = document.getElementById('alloc');
if (D.allocation && D.allocation.length){
  const acls = l => l==='Overweight'?'calm':l==='Underweight'?'alert':l==='Balanced'?'caution':'neutral';
  let ah = `<h2 class="alloc-h">Capital Allocation</h2>
    <p class="alloc-sub">A rules-based read of what the currently-active macro signals lean toward &mdash; not advice, and every driver is shown so you can judge for yourself. A signal counts as &ldquo;active&rdquo; when it is moving its worrying way or sitting at a caution/danger level; the meter shows conviction &mdash; the <b>weighted</b> margin, so heavier signals (the yield curve, Sahm rule, credit spreads) move it more than minor ones. <b>Balanced</b> means active signals pull both ways; <b>No signal</b> means nothing mapped here is firing. Tap a card for what the bucket means and every signal feeding it &mdash; dimmed rows are mapped but not currently active.</p>
    <div class="alloc-grid">`;
  D.allocation.forEach((a,ai)=>{
    const lc = acls(a.lean);
    const directional = (a.lean==='Overweight'||a.lean==='Underweight');
    const fill = {strong:3, moderate:2, slight:1, none:0}[a.conviction];
    let meter = '<span class="conv">';
    for (let i=0;i<3;i++) meter += `<span class="seg ${i<fill?('on '+lc):''}"></span>`;
    meter += '</span>';
    const tally = a.lean==='No signal'
      ? `<div class="alloc-tally">no active signals</div>`
      : a.lean==='Balanced'
      ? `<div class="alloc-tally">signals conflict &middot; ${a.ow.length} for, ${a.uw.length} against</div>`
      : `<div class="alloc-tally">${a.conviction} conviction &middot; ${a.ow.length} for, ${a.uw.length} against</div>`;
    let rows = '';
    a.drivers.forEach(d=>{
      const wt = d.weight>=1.5 ? '<span class="wchip key">key</span>'
               : d.weight<=0.75 ? '<span class="wchip minor">minor</span>' : '';
      rows += `<div class="drow ${d.active?'':'off'}"><span class="dot" style="background:${css('--'+d.state)}"></span>`
           + `<span class="drow-l">${d.label}${wt}</span>`
           + `<span class="drow-t ${d.lean==='OW'?'s-ow':'s-uw'}">${d.lean==='OW'?'overweight':'underweight'}</span>`
           + `<span class="drow-s">${d.active?stText(d.state):'inactive'}</span></div>`;
    });
    if (!a.drivers.length) rows = `<div class="drow off">No signals mapped to this bucket yet.</div>`;
    const detail = `<div class="alloc-detail"><p class="alloc-def">${a.definition||''}</p>`
                 + `<div class="asig-h">Signals feeding this bucket</div>${rows}</div>`;
    ah += `<div class="alloc-card expandable"><div class="alloc-top">`
        + `<span class="alloc-hl"><span class="chev" id="achev-${ai}">&#9656;</span><h4>${a.bucket}</h4></span>`
        + `<span class="alloc-badge"><span class="lean bg-${lc}">${a.lean}</span>${directional?meter:''}</span></div>`
        + tally + detail + `</div>`;
  });
  ah += `</div>`;
  allocEl.innerHTML = ah;
  allocEl.querySelectorAll('.alloc-card.expandable').forEach(card=>{
    card.addEventListener('click', ()=>{
      card.classList.toggle('open');
      const ch = card.querySelector('.chev'); if(ch) ch.classList.toggle('open');
    });
  });
}

// ---- Where the jobs are (sector payroll change) -- rendered under Labor ----
function buildJobsHTML(){
  if (!(D.jobs && D.jobs.sectors && D.jobs.sectors.length)) return '';
  const mx = Math.max(...D.jobs.sectors.map(s=>Math.abs(s.chg12))) || 1;
  const fmtk = v => (v>=0?'+':'') + Math.round(v).toLocaleString() + 'k';
  let jh = `<h2 class="jobs-h">Where the jobs are</h2>
    <p class="jobs-sub">Change in payrolls by sector over the last 12 months, largest gains to largest losses. As of ${D.jobs.asof||''}. Hover a bar for the 3-month change.</p>
    <div class="jobs-list">`;
  D.jobs.sectors.forEach(s=>{
    const pos = s.chg12 >= 0;
    const w = Math.abs(s.chg12)/mx*50;
    const bar = pos
      ? `<div class="jb pos" style="left:50%;width:${w}%"></div>`
      : `<div class="jb neg" style="left:${50-w}%;width:${w}%"></div>`;
    const t3 = (s.chg3===null||s.chg3===undefined) ? 'n/a' : fmtk(s.chg3);
    jh += `<div class="jobs-row" title="3-month change: ${t3}">`
        + `<div class="jl">${s.label}</div>`
        + `<div class="jtrack"><div class="jcenter"></div>${bar}</div>`
        + `<div class="jv ${pos?'pos':'neg'}">${fmtk(s.chg12)}</div></div>`;
  });
  jh += `</div>`;
  return jh;
}

const tRoot = document.getElementById('themes');
const drillReg = {};
const PANELS = {};
let modalChart = null;

// Adaptive number formatting. Person-counts (fmt='count') live in thousands in
// the data; show them in millions once they cross 1,000 (2,519.5 -> 2.52M) but
// keep small 6-month moves in K so early changes keep their resolution.
function dispVU(v, p){
  if(p && p.fmt==='count'){
    const a = Math.abs(v);
    if(a >= 1000) return {t:(v/1000).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}), u:'M'};
    return {t:v.toLocaleString('en-US',{maximumFractionDigits:1}), u:'K'};
  }
  const u = p ? p.units : '';
  const dec = Math.abs(v) < 10 ? 2 : 1;
  return {t:v.toLocaleString('en-US',{maximumFractionDigits:dec}), u:u};
}
function axisTick(val, p){
  if(p && p.fmt==='count'){
    const a = Math.abs(val);
    if(a >= 1000) return (val/1000).toLocaleString('en-US',{maximumFractionDigits:1})+'M';
    return val.toLocaleString('en-US',{maximumFractionDigits:0})+'K';
  }
  return val.toLocaleString('en-US',{maximumFractionDigits:1});
}

// Dashed vertical crosshair at the hovered point (FRED-style).
const crosshair = {
  id:'crosshair',
  afterDraw(chart){
    const act = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
    if(!act.length) return;
    const x = act[0].element.x, {top,bottom} = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,bottom);
    ctx.lineWidth = 1; ctx.setLineDash([3,3]); ctx.strokeStyle = css('--dim'); ctx.stroke(); ctx.restore();
  }
};

function moveInfo(p){
  const mv = p.trend; let moveTxt='', moveCls='dim';
  if(mv){ const d=mv.delta, sign = d>0?'+':d<0?'-':'';
    const ad = dispVU(Math.abs(d), p);
    moveTxt = `${sign}${ad.t} ${ad.u} over 6mo`;
    if(p.worry==='up') moveCls=d>0?'alert':'calm';
    else if(p.worry==='down') moveCls=d<0?'alert':'calm'; }
  return {mv,moveTxt,moveCls};
}
function makeCard(p,cid){
  PANELS[cid] = p;
  const {mv,moveTxt,moveCls} = moveInfo(p);
  const pctTxt = mv ? ` &middot; ${mv.pct_of_range.toFixed(0)}th pctile of its range` : '';
  const dv = dispVU(p.latest, p);
  const card = document.createElement('div'); card.className='card';
  card.innerHTML = `<div class="top"><div><h4>${p.label}</h4><div class="sid">${p.series_id}</div></div>
    <span class="badge bg-${p.state}">${stText(p.state)}</span></div>
    <div class="row"><span class="val ${p.state}">${dv.t}<small> ${dv.u}</small></span>
    ${mv?`<span class="move ${moveCls}">${p.deteriorating?'&#9650; ':''}${moveTxt}</span>`:''}</div>
    <div class="asof">as of ${p.latest_date}${pctTxt}</div>
    <div class="cbox"><canvas id="cv-${cid}"></canvas><button class="expand" data-cid="${cid}" title="Expand chart" aria-label="Expand chart">&#10530;</button></div>
    <div class="note">${p.note}</div>`;
  return card;
}
function drawSeries(canvasId, p, opts){
  opts = opts || {}; const big = !!opts.big;
  const cap = opts.maxPts || (big?4000:500);
  const pts = p.points, step = Math.max(1,Math.floor(pts.length/cap));
  const thin = pts.filter((_,k)=>k%step===0);
  const ds = [{ data: thin.map(x=>({x:x[0],y:x[1]})), borderColor:css('--neutral'),
                borderWidth: big?2:1.5, pointRadius:0, pointHoverRadius: big?5:4,
                pointHoverBackgroundColor:css('--neutral'), pointHoverBorderColor:css('--bg'),
                pointHoverBorderWidth:2, tension:0.08, fill:false }];
  function refLine(val,cv){ if(val===null||val===undefined) return;
    ds.push({ data:[{x:thin[0][0],y:val},{x:thin[thin.length-1][0],y:val}],
      borderColor:css(cv), borderWidth:1, borderDash:[4,4], pointRadius:0, pointHoverRadius:0, fill:false }); }
  refLine(p.caution,'--caution'); refLine(p.alert,'--alert');
  return new Chart(document.getElementById(canvasId),{ type:'line', data:{datasets:ds}, plugins:[crosshair],
    options:{ responsive:true, maintainAspectRatio:false, animation:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false}, tooltip:{position:'nearest',intersect:false,mode:'index',displayColors:false,
        padding: big?10:8, titleFont:{size: big?13:11}, bodyFont:{size: big?14:12},
        callbacks:{title:i=>i[0].raw.x, label:i=>{const d=dispVU(i.raw.y,p); return d.t+' '+d.u;}}}},
      scales:{ x:{type:'time',time:{unit:'year'},ticks:{color:css('--dim'),font:{size:big?11:9},maxTicksLimit:big?12:6},grid:{color:css('--grid')}},
               y:{ticks:{color:css('--dim'),font:{size:big?12:10},maxTicksLimit:big?6:4,callback:(val)=>axisTick(val,p)},grid:{color:css('--grid')}} } } });
}
function paintChart(p,cid){ drawSeries('cv-'+cid, p); }

function openModal(cid){
  const p = PANELS[cid]; if(!p) return;
  document.getElementById('modal-title').textContent = p.label;
  document.getElementById('modal-sid').textContent = p.series_id;
  const mv = p.trend, dv = dispVU(p.latest, p);
  let meta = `<span class="m-val ${p.state}">${dv.t} ${dv.u}</span><span class="badge bg-${p.state}">${stText(p.state)}</span><span>as of ${p.latest_date}`;
  if(mv) meta += ` &middot; ${mv.pct_of_range.toFixed(0)}th pctile of its range`;
  meta += `</span>`;
  document.getElementById('modal-meta').innerHTML = meta;
  document.getElementById('modal-note').textContent = p.note;
  document.getElementById('modal').classList.add('open');
  if(modalChart){ modalChart.destroy(); modalChart = null; }
  requestAnimationFrame(()=>{ modalChart = drawSeries('modal-cv', p, {big:true}); });
}
function closeModal(){
  document.getElementById('modal').classList.remove('open');
  if(modalChart){ modalChart.destroy(); modalChart = null; }
}
function toggleDrill(key){
  const dd=document.getElementById('dd-'+key), chev=document.getElementById('chev-'+key);
  const open=dd.classList.toggle('open'); if(chev) chev.classList.toggle('open',open);
  const reg=drillReg[key];
  if(open && reg && !reg.drawn){ reg.paints.forEach(a=>paintChart(a[0],a[1])); reg.drawn=true; }
}

Object.keys(D.themes).forEach(theme=>{
  const panels = D.themes[theme]; if(!panels.length) return;
  const ts = D.theme_states[theme] || {state:'neutral',deteriorating:0,total:panels.length};
  const key = theme.replace(/[^a-z]/gi,'');
  const subs = (D.drilldowns && D.drilldowns[theme]) || [];
  const forSubs = subs.filter(s=>s.deteriorating);
  const againstSubs = subs.filter(s=>!s.deteriorating);
  const sec = document.createElement('div'); sec.className='theme';
  const detTxt = ts.deteriorating>0 ? `${ts.deteriorating} of ${ts.total} deteriorating` : 'stable';
  const lead = subs.length ? ` &middot; <span class="lead">${forSubs.length}/${subs.length} leading signals worsening</span>` : '';
  const chev = subs.length ? `<span class="chev" id="chev-${key}">&#9656;</span>` : '';
  sec.innerHTML = `<div class="theme-head${subs.length?' expandable':''}">${chev}<h2>${theme}</h2>
    <span class="theme-state bg-${ts.state}">${stText(ts.state)}</span>
    <span class="theme-note">${detTxt}${lead}</span></div><div class="cards"></div>`;
  tRoot.appendChild(sec);
  const cardsEl = sec.querySelector('.cards');
  panels.forEach((p,idx)=>{ const cid=key+idx; cardsEl.appendChild(makeCard(p,cid)); paintChart(p,cid); });

  if(subs.length){
    const dd = document.createElement('div'); dd.className='drilldown'; dd.id='dd-'+key;
    dd.innerHTML = `<p class="dd-intro">Leading signals that move before the headline, split by what they're arguing right now. Left is the case for deterioration; right is genuine contrary evidence, so the panel isn't a one-way read.</p>
      <div class="dd-cols">
        <div class="dd-col for"><h5>Arguing for deterioration &middot; ${forSubs.length}</h5><div class="dd-for"></div></div>
        <div class="dd-col against"><h5>Arguing against &middot; ${againstSubs.length}</h5><div class="dd-against"></div></div>
      </div>`;
    sec.appendChild(dd);
    const forEl = dd.querySelector('.dd-for'), againstEl = dd.querySelector('.dd-against');
    const paints = [];
    if(!forSubs.length) forEl.innerHTML = '<div class="dd-empty">Nothing currently deteriorating.</div>';
    if(!againstSubs.length) againstEl.innerHTML = '<div class="dd-empty">Nothing currently stable or improving.</div>';
    forSubs.forEach((s,i)=>{ const cid=key+'-df-'+i; forEl.appendChild(makeCard(s,cid)); paints.push([s,cid]); });
    againstSubs.forEach((s,i)=>{ const cid=key+'-da-'+i; againstEl.appendChild(makeCard(s,cid)); paints.push([s,cid]); });
    drillReg[key] = {paints, drawn:false};
    sec.querySelector('.theme-head').addEventListener('click', ()=>toggleDrill(key));
  }

  if (theme === 'Labor market'){
    const jh = buildJobsHTML();
    if (jh){ const jsec = document.createElement('div'); jsec.className='theme jobs-sec';
      jsec.innerHTML = jh; tRoot.appendChild(jsec); }
  }
});

// Expand-to-fullscreen: click a chart (or its corner button) to open it large.
document.addEventListener('click',(e)=>{
  const btn = e.target.closest('.expand');
  if(btn){ e.stopPropagation(); openModal(btn.dataset.cid); return; }
  const box = e.target.closest('.cbox');
  if(box){ const b = box.querySelector('.expand'); if(b) openModal(b.dataset.cid); }
});
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal').addEventListener('click',(e)=>{ if(e.target.id==='modal') closeModal(); });
document.addEventListener('keydown',(e)=>{ if(e.key==='Escape') closeModal(); });
</script>
</body></html>"""

if __name__ == "__main__":
    build()
