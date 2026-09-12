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
from datetime import date, datetime, timedelta, timezone
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
                 "Spikes lead or coincide with downturns. History capped at 3 "
                 "years as of 2026 -- FRED restricted this and other ICE Data "
                 "index series to a rolling window; the chart won't extend "
                 "further back regardless of the range button chosen. Doesn't "
                 "affect the level/trend read above, which only uses recent "
                 "months."},
        {"id": "NFCI", "label": "Financial conditions index", "kind": "level",
         "units": "", "worry": "up", "start": "1985-01-01",
         "caution": 0.0, "alert": 0.5,
         "note": "Chicago Fed index of overall financial stress. Above zero is "
                 "tighter than average; positive and rising is deterioration."},
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
        {"id": "WEI", "label": "Weekly economic index", "kind": "level",
         "units": "%", "worry": "down", "start": "2008-01-01",
         "caution": 1.5, "alert": 0.0,
         "note": "The NY/Dallas Fed real-time growth gauge -- ten weekly series "
                 "(rail traffic, staffing, jobless claims, tax withholdings, steel, "
                 "fuel, electricity) scaled to four-quarter GDP growth. It moves "
                 "weeks ahead of the monthly data: the earliest read on whether "
                 "growth is accelerating or slowing."},
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
    "Liquidity": [
        {"id": "Net liquidity", "label": "Net liquidity", "compute": "combine",
         "parts": [["WALCL", 1.0], ["WTREGEN", -1.0], ["RRPONTSYD", -1000.0]],
         "ratio_scale": 1e-6, "kind": "level", "units": "T", "worry": "down",
         "start": "2003-01-01",
         "note": "The Fed balance sheet minus the Treasury's cash account minus "
                 "reverse repos -- the cash actually sloshing around markets. It "
                 "tracks risk assets more tightly than almost anything: when it "
                 "drains (QT, heavy Treasury issuance, RRP rising) risk assets lose "
                 "their tailwind."},
        {"id": "WALCL", "label": "Fed balance sheet (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "2003-01-01",
         "note": "Growth in the Fed's assets -- QE (expanding) adds liquidity, QT "
                 "(shrinking) drains it. The clearest single read on whether the Fed "
                 "is the wind at markets' back or in their face."},
    ],
    "Housing": [
        {"id": "HPIPONM226S", "label": "Home prices (FHFA, YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1991-01-01",
         "note": "FHFA purchase-only house prices (the public-domain alternative to "
                 "Case-Shiller). Housing is the most rate-sensitive, most-leading "
                 "part of the cycle; falling prices hit the wealth effect, "
                 "construction and mortgage credit together -- the channel that broke "
                 "in 2008."},
        {"id": "HSN1F", "label": "New home sales", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "note": "New single-family homes sold (Census). Recorded at contract "
                 "signing, so it leads existing-home sales by a month or two -- an "
                 "early read on housing demand and, through it, the rate-sensitive "
                 "economy."},
        {"id": "HOUST", "label": "Housing starts", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "note": "Homebuilding is the most rate-sensitive part of the economy and "
                 "turns before the broader cycle; falling starts is an early-warning "
                 "channel."},
        {"id": "MSACSR", "label": "Months' supply of homes", "kind": "level",
         "units": "mo", "worry": "up", "start": "1990-01-01",
         "note": "How many months to clear current inventory at the present sales "
                 "pace. Rising supply means demand is fading faster than builders "
                 "adjust -- above ~7 months has coincided with housing downturns."},
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
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "The policy rate -- read the DIRECTION, not the level: rising means "
                 "the Fed is tightening (removing accommodation), the classic pin "
                 "that pops expensive markets and precedes recessions; falling means "
                 "it is easing. Early-cycle hikes are healthy, so weigh this "
                 "alongside where the cycle and valuations sit."},
        {"id": "M2SL", "label": "M2 money supply (YoY)", "kind": "yoy",
         "units": "%", "worry": None, "start": "1990-01-01",
         "caution": None, "alert": None,
         "note": "Money-supply growth. Sharp contraction is unusual and has "
                 "accompanied tightening cycles."},
    ],
    "Valuation": [
        {"id": "Shiller CAPE", "label": "Shiller CAPE (10-yr P/E)", "compute": "cape",
         "kind": "level", "units": "x", "worry": "up", "start": "1990-01-01",
         "caution": 28.0, "alert": 33.0,
         "note": "Price divided by ten years of average real earnings -- the most-"
                 "cited long-run valuation gauge, smoothing through the profit cycle "
                 "that distorts a one-year P/E. Above ~30 clustered around 1929, 2000 "
                 "and 2021. Read it as the STAKES -- how poor the next decade's returns "
                 "are likely to be and how far a fall could go -- not a trigger: it "
                 "stayed extreme from 1998 through 2000, so act only when the Market "
                 "check confirms a turn, never on the level alone."},
        {"id": "Excess CAPE yield", "label": "Excess CAPE yield", "compute": "ecy",
         "kind": "level", "units": "%", "worry": "down", "start": "2003-01-01",
         "caution": 1.0, "alert": 0.0,
         "note": "Shiller's own fix for CAPE's blind spot to interest rates: the "
                 "market earnings yield (1/CAPE) minus the 10-year REAL yield -- what "
                 "stocks offer over safe inflation-protected bonds. In 2000 it was "
                 "NEGATIVE (stocks yielded less than bonds -- absurd); today it is thin "
                 "but positive, so 'expensive because rates are low', not a 2000-style "
                 "bubble. Below zero is the danger zone; still, this is stakes, not a "
                 "sell signal -- pair it with the Market check."},
        {"id": "Market cap / GDP", "label": "Buffett indicator (market cap / GDP)",
         "compute": "ratio", "nums": ["NCBEILQ027S", "FBCELLQ027S"], "den": "GDP",
         "ratio_scale": 0.1, "kind": "level", "units": "%", "worry": "up",
         "start": "1990-01-01", "pctile": True,
         "note": "Total US equity market value -- non-financial plus financial "
                 "corporate equities -- against the size of the economy. Buffett's "
                 "'best single measure' of what you are paying for American business. "
                 "It does not time tops, but extreme readings pull future returns "
                 "forward; read it against its own history."},
        {"id": "CP / GDP", "label": "Corporate profit share of GDP",
         "compute": "ratio", "num": "CP", "den": "GDP", "ratio_scale": 100,
         "kind": "level", "units": "%", "worry": "up", "start": "1990-01-01",
         "caution": 10.0, "alert": 12.0,
         "note": "After-tax corporate profits as a share of GDP. Margins mean-revert "
                 "-- high profits draw competition, labour and regulation -- so a "
                 "historically elevated share flatters earnings the market may be "
                 "extrapolating. The valuation risk a simple P/E hides."},
        {"id": "SPY/RSP", "label": "Market concentration (cap-wt over equal-wt)",
         "compute": "concentration", "kind": "level", "units": "", "worry": "up",
         "start": "2003-01-01", "pctile": True,
         "note": "The S&P 500 (SPY, cap-weighted -- mega-caps count more) against "
                 "the S&P 500 Equal Weight ETF (RSP, every company counted the "
                 "same), both set to 100 at RSP's April 2003 launch. Rising means "
                 "cap-weighted is pulling ahead -- a handful of mega-caps are "
                 "driving the market's gains while the average stock lags, the "
                 "same pattern that preceded 2000's unwind; falling means gains "
                 "are broadening to more of the market. For scale: published "
                 "research (RBC Wealth Management/FactSet) puts top-10 S&P 500 "
                 "weight at ~19% in 1990, ~23-27% at the 2000 peak, and a record "
                 "~40% by 2025 -- no true equal-weight product existed before "
                 "2003, so this specific ratio can't be computed for the dot-com "
                 "era itself, but that published figure is the closest honest "
                 "comparison available. Read today's level against its own "
                 "2003-present range."},
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
        {"id": "JTSJOL", "label": "Job openings (JOLTS)", "kind": "level",
         "units": "K", "worry": "down", "start": "2001-01-01", "fmt": "count",
         "note": "Unfilled jobs -- the cleanest read on labour DEMAND. Openings "
                 "roll over well before layoffs begin, so a sustained fall is an "
                 "early sign the jobs market is cooling from the demand side."},
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
        {"id": "PCETRIM12M159SFRBDAL", "label": "Trimmed-mean PCE", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "The Dallas Fed's trimmed-mean PCE -- it throws out the most "
                 "extreme price moves each month to reveal the underlying trend. A "
                 "cleaner core read than headline or ex-food-and-energy, and what "
                 "several FOMC members actually watch."},
    ],
    "Liquidity": [
        {"id": "RRPONTSYD", "label": "Reverse repo", "kind": "level",
         "units": "B", "worry": "up", "start": "2013-01-01",
         "note": "Cash parked overnight at the Fed instead of in markets. A high or "
                 "rising balance means liquidity is being absorbed; it draining back "
                 "out (2023-24) is a hidden tailwind. Read it inside net liquidity."},
        {"id": "WTREGEN", "label": "Treasury cash account", "kind": "level",
         "scale": 0.001, "units": "B", "worry": "up", "start": "2005-01-01",
         "note": "The Treasury's checking account at the Fed. When it builds (heavy "
                 "bill issuance) it pulls cash out of the banking system; when it is "
                 "spent down it adds liquidity back."},
    ],
    "Housing": [
        {"id": "PERMIT", "label": "Building permits", "kind": "level",
         "units": "K", "worry": "down", "start": "1990-01-01", "fmt": "count",
         "note": "Permits lead housing starts, which lead the cycle -- the earliest "
                 "point in the most rate-sensitive part of the economy."},
        {"id": "MORTGAGE30US", "label": "30-year mortgage rate", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "The price of housing credit. High or rising mortgage rates throttle "
                 "affordability and demand -- the transmission belt from Fed policy "
                 "to the housing cycle."},
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
        {"id": "DTWEXBGS", "label": "US dollar (broad)", "kind": "level",
         "units": "", "worry": "up", "start": "2006-01-01",
         "note": "The trade-weighted dollar. A rising dollar tightens global "
                 "conditions and is a direct headwind for commodities, energy, gold "
                 "and emerging markets -- the mirror image of the reflation trade. "
                 "Read it as the counterweight to the inflation complex, not a "
                 "recession signal on its own."},
        {"id": "DRTSCILM", "label": "Bank lending standards (C&I)", "kind": "level",
         "units": "%", "worry": "up", "start": "1990-01-01",
         "note": "Net share of banks tightening standards on business loans, from "
                 "the Fed's Senior Loan Officer survey. One of the best leading "
                 "recession signals there is: when banks pull back, credit-dependent "
                 "cyclicals and small caps feel it months before the hard data."},
    ],
    "Growth": [
        {"id": "NEWORDER", "label": "Core capital-goods orders (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1993-01-01",
         "note": "Non-defence capital goods ex-aircraft -- what businesses order "
                 "when confident. It leads capex and manufacturing; rolling over is "
                 "an early cyclical-downturn tell."},
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
        {"id": "SP500-PE", "label": "S&P 500 P/E (trailing)", "compute": "multpl",
         "url": "https://www.multpl.com/s-p-500-pe-ratio/table/by-month",
         "lo": 3.0, "hi": 150.0, "tag": "pe",
         "kind": "level", "units": "x", "worry": "up", "start": "1990-01-01",
         "note": "The plain trailing price-to-earnings of the S&P 500 -- what you "
                 "asked for. Simpler than CAPE but noisier: it looks cheap at profit "
                 "peaks and spikes when earnings collapse (2009), which is exactly "
                 "why CAPE exists. Read the two together."},
        {"id": "Margin debt (YoY)", "label": "Margin debt (YoY)", "compute": "margin",
         "kind": "yoy", "units": "%", "worry": "up", "start": "1997-01-01",
         "pctile": False, "caution": 20.0, "alert": 40.0,
         "note": "Customer margin debt (FINRA) -- money borrowed against portfolios to "
                 "buy more stock, the purest gauge of speculative leverage. Rapid "
                 "year-over-year growth marked the 2000, 2007 and 2021 tops; when it "
                 "rolls over, forced selling feeds on itself. Sourced from FINRA's own "
                 "file (no API), so it can lag a few weeks or drop out on delays."},
        {"id": "CP", "label": "Corporate profits (YoY)", "kind": "yoy",
         "units": "%", "worry": "down", "start": "1990-01-01",
         "note": "Growth in after-tax corporate profits -- the earnings that "
                 "valuations rest on. Rising profits can justify high multiples; "
                 "profits rolling over while prices stay high is the dangerous "
                 "combination. An earnings recession usually precedes a price one."},
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
               ("Defensive equities", "OW"), ("Cyclicals & small caps", "UW"),
               ("Energy", "UW"), ("Real assets & commodities", "UW"), ("Value over Growth", "UW")],
    # Labour / growth / consumer weakening -> defensive, duration
    "SAHMREALTIME": [("Overall equity exposure", "UW"), ("Defensive equities", "OW"),
                     ("Cyclicals & small caps", "UW"), ("Long-duration Treasuries", "OW"),
                     ("Energy", "UW"), ("Real assets & commodities", "UW"), ("Value over Growth", "UW")],
    "IC4WSA": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW"),
               ("Energy", "UW")],
    "TEMPHELPS": [("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "CFNAI": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW"),
              ("Energy", "UW"), ("Real assets & commodities", "UW")],
    "NEWORDER": [("Cyclicals & small caps", "UW"), ("Overall equity exposure", "UW")],
    "DRCCLACBS": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "WEI": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"),
            ("Defensive equities", "OW"), ("Long-duration Treasuries", "OW"),
            ("Energy", "UW"), ("Real assets & commodities", "UW"), ("Value over Growth", "UW")],
    "DRTSCILM": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"),
                 ("High-yield credit", "UW"), ("Defensive equities", "OW"),
                 ("Long-duration Treasuries", "OW"), ("Value over Growth", "UW")],
    "DTWEXBGS": [("Energy", "UW"), ("Real assets & commodities", "UW"), ("Gold", "UW")],
    "RRSFS": [("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "FEDFUNDS": [("Overall equity exposure", "UW")],
    "Net liquidity": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"),
                      ("High-yield credit", "UW")],
    "WALCL": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW")],
    "JTSJOL": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"),
               ("Defensive equities", "OW")],
    "HPIPONM226S": [("Overall equity exposure", "UW"), ("Cyclicals & small caps", "UW"),
                    ("Defensive equities", "OW"), ("Long-duration Treasuries", "OW")],
    "HSN1F": [("Cyclicals & small caps", "UW"), ("Defensive equities", "OW")],
    "PCETRIM12M159SFRBDAL": [("Long-duration Treasuries", "UW"), ("Value over Growth", "OW"),
                             ("Real assets & commodities", "OW")],
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
    "WEI": 1.25, "DRTSCILM": 1.5, "DTWEXBGS": 1.0,
    "Net liquidity": 1.5, "WALCL": 1.0, "JTSJOL": 1.0,
    "HPIPONM226S": 1.0, "HSN1F": 0.75, "PCETRIM12M159SFRBDAL": 1.25, "FEDFUNDS": 0.75,
}

# ---- Regime classifier (growth x inflation) ----------------------------------
# Signals whose 6-month direction defines momentum. Growth signals deteriorating
# => growth decelerating; inflation signals deteriorating (rising) => accelerating.
# A signal only counts as "worsening" or "improving" if its 6-month move exceeds
# this multiple of its own typical 6-month move -- filters noise so directions,
# the regime, and the allocation stop reacting to every wiggle. Small moves read
# as "steady". Tunable: raise it if things still look jumpy, lower if too quiet.
DEADBAND_K = 0.75

# The regime only calls growth "decelerating" or inflation "accelerating" when
# the worsening signals outnumber the improving ones by at least this margin --
# a bare edge (2 vs 1) is noise and was flipping the regime to false stagflation.
REGIME_MARGIN = 2

GROWTH_MOM = ["PAYEMS", "INDPRO", "GDPC1", "CFNAI", "NEWORDER", "RRSFS",
              "UNRATE", "IC4WSA", "SAHMREALTIME", "WEI", "DRTSCILM",
              "JTSJOL", "HPIPONM226S"]
INFLATION_MOM = ["CPIAUCSL", "PCEPILFE", "T5YIE", "T5YIFR",
                 "CORESTICKM159SFRBATL", "PPIFIS", "PCETRIM12M159SFRBDAL"]
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
    # Typical 6-month move (robust noise floor): median of |value now - value
    # ~lookback ago| across history, so "worsening/improving" can require a move
    # bigger than usual rather than any wiggle. Two-pointer keeps it O(n).
    moves, j = [], 0
    for i in range(len(dates)):
        tgt = dates[i] - timedelta(days=lookback_days)
        while j < i and dates[j] < tgt:
            j += 1
        cand = j - 1 if (j > 0 and abs((dates[j - 1] - tgt).days)
                         <= abs((dates[j] - tgt).days)) else j
        if cand < i:
            moves.append(abs(vals[i] - vals[cand]))
    typical = sorted(moves)[len(moves) // 2] if moves else 0.0
    return {"delta": round(delta, 3), "pct_of_range": round(pct, 1),
            "prior": round(vals[pi], 3), "typical": round(typical, 4)}


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
    "https://www.multpl.com/shiller-pe/table/by-month",
]
_CAPE_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fetch_multpl(url, start, lo=3.0, hi=80.0, tag="multpl"):
    """Parse a multpl.com by-month table (Shiller data, kept current). Returns
    monthly (date, value), keeping only values in [lo, hi]. Standard-library
    only, and fails safe -- any error returns [] so the dashboard still builds."""
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"}
    try:
        r = requests.get(url, timeout=60, headers=ua)
        r.raise_for_status()
        html = r.text
    except Exception as exc:
        print(f"  [{tag}] {url} failed ({exc})"); return []
    try:
        import re
        start_year = int(start[:4])
        out, seen = [], set()
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            dm = re.search(r"([A-Z][a-z]{2})\s+\d{1,2},\s+(\d{4})", row)
            vm = re.search(r"(\d{1,3}\.\d+)", row)
            if not dm or not vm:
                continue
            mon = _CAPE_MONTHS.get(dm.group(1))
            year = int(dm.group(2))
            val = float(vm.group(1))
            if not mon or year < start_year or not (lo <= val <= hi):
                continue
            key = f"{year:04d}-{mon:02d}-01"
            if key in seen:
                continue
            seen.add(key)
            out.append((key, val))
        out.sort()
        if out:
            print(f"  [{tag}] {url.split('/')[-3]} -> {len(out)} pts; last 3: {out[-3:]}")
        else:
            print(f"  [{tag}] no rows parsed from {url}")
        return out
    except Exception as exc:
        print(f"  [{tag}] parse error: {exc}"); return []


def fetch_cape(start):
    return fetch_multpl(SHILLER_CAPE_URLS[0], start, 3.0, 80.0, tag="cape")


def fetch_ecy(start):
    """Excess CAPE Yield (Shiller): the market earnings yield (100 / CAPE) minus the
    10-year REAL yield. Positive = stocks beat safe real bonds; negative = 2000-like.
    Rate-aware, so it distinguishes 'expensive because rates are low' from a bubble."""
    cape = fetch_cape(start)
    real = fetch("DFII10", start)
    if not cape or not real:
        return []
    real_by_month = {}
    for d, v in real:
        real_by_month[d[:7]] = v          # last real yield seen in each month
    out = []
    for d, c in cape:
        rm = real_by_month.get(d[:7])
        if c and rm is not None:
            out.append((d, round(100.0 / c - rm, 3)))
    return out


FINRA_MARGIN_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"


def _margin_date(cell):
    if hasattr(cell, "year") and hasattr(cell, "month"):
        return cell.year, cell.month
    if isinstance(cell, str):
        for fmt in ("%b-%y", "%b-%Y", "%B-%y", "%B-%Y", "%Y-%m", "%m/%Y", "%b %Y"):
            try:
                d = datetime.strptime(cell.strip(), fmt)
                return d.year, d.month
            except ValueError:
                pass
    return None


def fetch_finra_margin(start):
    """Customer margin debt (debit balances, $M) from FINRA's own Excel file -- the
    only source (no API/feed). Scans for the 'debit' column, parses either text or
    date cells, and fails safe: any error returns [] so the dashboard still builds."""
    try:
        import io
        import openpyxl
    except Exception:
        print("  [margin] openpyxl not installed -- skipping margin debt"); return []
    try:
        r = requests.get(FINRA_MARGIN_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    except Exception as exc:
        print(f"  [margin] download failed ({exc})"); return []
    try:
        rows = list(wb.active.iter_rows(values_only=True))
        debit_col = header_row = None
        for ri, row in enumerate(rows[:10]):
            for ci, cell in enumerate(row):
                if isinstance(cell, str) and "debit" in cell.lower():
                    debit_col, header_row = ci, ri; break
            if debit_col is not None:
                break
        if debit_col is None:
            print("  [margin] debit column not found"); return []
        start_year = int(start[:4])
        out = []
        for row in rows[header_row + 1:]:
            if not row or len(row) <= debit_col:
                continue
            dt = _margin_date(row[0])
            v = row[debit_col]
            if not isinstance(v, (int, float)):
                try:
                    v = float(str(v).replace(",", ""))
                except (ValueError, TypeError):
                    continue
            if dt is None or dt[0] < start_year or v <= 0:
                continue
            out.append((f"{dt[0]:04d}-{dt[1]:02d}-01", float(v)))
        out.sort()
        if out:
            print(f"  [margin] FINRA -> {len(out)} pts; latest {out[-1][1]:,.0f} $M")
        else:
            print("  [margin] no rows parsed")
        return out
    except Exception as exc:
        print(f"  [margin] parse error: {exc}"); return []


def fetch_concentration_ratio(start):
    """Equal-weight vs cap-weight breadth/concentration signal, proxied by SPY
    (SPDR S&P 500 ETF Trust) against RSP (Invesco S&P 500 Equal Weight ETF),
    both rebased to 100 at their first shared trading date. ETF share prices
    are ordinary quoted market data -- like any stock price -- not a
    licensed index product, so unlike SP500 itself there's no reproduction
    restriction on publishing this directly.

    Real data only goes back to RSP's April 2003 launch: no true
    equal-weight S&P 500 product existed before then (S&P's own Equal
    Weight Index launched Jan 2003 too), so this cannot be extended back
    to cover the 2000 dot-com peak -- a hard data-availability limit, not
    a choice. Rising means cap-weighted mega-caps are beating the average
    stock (gains narrowing to fewer names); falling means participation is
    broadening.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  [breadth] yfinance not installed -- skipping concentration ratio")
        return []
    try:
        rsp = yf.Ticker("RSP").history(start=start, auto_adjust=True)["Close"]
        spy = yf.Ticker("SPY").history(start=start, auto_adjust=True)["Close"]
    except Exception as exc:
        print(f"  [breadth] fetch failed ({exc})")
        return []
    if rsp.empty or spy.empty:
        print("  [breadth] empty result from yfinance")
        return []
    common = rsp.index.intersection(spy.index)
    if len(common) < 30:
        print("  [breadth] too few overlapping trading days")
        return []
    rsp, spy = rsp.loc[common].sort_index(), spy.loc[common].sort_index()
    ratio = (spy / spy.iloc[0]) / (rsp / rsp.iloc[0]) * 100.0
    out = [(d.strftime("%Y-%m-%d"), round(float(v), 3)) for d, v in ratio.items()]
    if out:
        print(f"  [breadth] SPY/RSP -> {len(out)} pts; last 3: {out[-3:]}")
    return out


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


def combine_series(parts, start, scale=1.0):
    """Weighted sum of several series on their common dates -- e.g. net liquidity
    = balance sheet - Treasury cash - reverse repo. parts: list of [id, coef]."""
    fetched = [(coef, fetch(sid, start)) for sid, coef in parts]
    if any(not s for _, s in fetched):
        return []
    maps = [(coef, dict(s)) for coef, s in fetched]
    common = set(maps[0][1])
    for _, m in maps[1:]:
        common &= set(m)
    return sorted((d, sum(coef * m[d] for coef, m in maps) * scale) for d in common)


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
    elif ind.get("compute") == "combine":
        raw = combine_series(ind["parts"], ind["start"], ind.get("ratio_scale", 1.0))
    elif ind.get("compute") == "margin":
        raw = fetch_finra_margin(ind["start"])
    elif ind.get("compute") == "ecy":
        raw = fetch_ecy(ind["start"])
    elif ind.get("compute") == "cape":
        raw = fetch_cape(ind["start"])
    elif ind.get("compute") == "concentration":
        raw = fetch_concentration_ratio(ind["start"])
    elif ind.get("compute") == "multpl":
        raw = fetch_multpl(ind["url"], ind["start"], ind.get("lo", 3.0),
                           ind.get("hi", 80.0), tag=ind.get("tag", "multpl"))
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
    sig = bool(tr and tr.get("typical", 0) > 0
               and abs(tr["delta"]) >= DEADBAND_K * tr["typical"])
    moved_bad = bool(tr and ind["worry"] and (
        (ind["worry"] == "up" and tr["delta"] > 0) or
        (ind["worry"] == "down" and tr["delta"] < 0)))
    deteriorating = bool(sig and moved_bad)
    improving = bool(sig and ind["worry"] and not moved_bad and tr["delta"] != 0)
    direction = "worsening" if deteriorating else "improving" if improving else "steady"
    u = ind["units"]
    usuf = u if u in ("%", "x") else (" " + u if u else "")
    w = ind["worry"]
    if percentile_state:
        if w == "up":
            crit = ("Judged against its own history: danger in the top 15% of past "
                    "readings, caution the top 35% (higher is worse).")
        elif w == "down":
            crit = ("Judged against its own history: danger in the bottom 15% of past "
                    "readings, caution the bottom 35% (lower is worse).")
        else:
            crit = "Shown for context; not scored against a fixed threshold."
    elif ind.get("caution") is not None and ind.get("alert") is not None and w:
        c, a = ind["caution"], ind["alert"]
        if w == "up":
            crit = f"Danger at/above {a}{usuf}, caution at/above {c}{usuf} (higher is worse)."
        else:
            crit = f"Danger at/below {a}{usuf}, caution at/below {c}{usuf} (lower is worse)."
    else:
        crit = "Shown for context; not scored against a fixed threshold."
    crit += (" Colour shows the level; the arrow shows 6-month direction "
             "(worsening or improving) -- a separate axis, so a calm signal can be "
             "worsening and a danger one improving.")
    panel = {
        "label": ind["label"], "series_id": ind["id"], "units": ind["units"],
        "worry": ind["worry"], "note": ind["note"], "state": st,
        "fmt": ind.get("fmt"), "criteria": crit, "direction": direction,
        "caution": ind.get("caution"), "alert": ind.get("alert"),
        "latest": round(latest, 2), "latest_date": series[-1][0],
        "trend": tr, "deteriorating": deteriorating, "improving": improving,
        "points": [[d, round(v, 3)] for d, v in series]}
    return panel, None


def _compute_changes(history, all_panels, today):
    """Deltas of the current snapshot vs history: scorecard counts ~a week ago,
    signals whose STATE changed recently, allocation lean shifts, regime change."""
    label_of = {p["series_id"]: p["label"] for p in all_panels}
    cur, prior = history[-1], history[:-1]
    td = datetime.strptime(today, "%Y-%m-%d")

    def days_ago(d):
        return (td - datetime.strptime(d, "%Y-%m-%d")).days

    out = {"vs": None, "recently_changed": [], "alloc_changes": [], "regime_change": None}
    for s in reversed(prior):                      # scorecard counts ~a week ago
        if days_ago(s["date"]) >= 6:
            out["vs"] = {"date": s["date"], "days": days_ago(s["date"]),
                         "counts": s["counts"]}
            break
    for sid, sig in cur["signals"].items():        # signals whose state changed
        for s in reversed(prior):
            ps = s["signals"].get(sid)
            if not ps:
                continue
            if ps[0] != sig[0]:
                d = days_ago(s["date"])
                if d <= 45:
                    out["recently_changed"].append(
                        {"label": label_of.get(sid, sid), "from": ps[0],
                         "to": sig[0], "days": d})
                break
    out["recently_changed"].sort(key=lambda x: x["days"])
    out["recently_changed"] = out["recently_changed"][:8]
    for bucket, av in cur["alloc"].items():        # allocation lean shifts
        for s in reversed(prior):
            pa = s["alloc"].get(bucket)
            if not pa:
                continue
            if pa[0] != av[0]:
                d = days_ago(s["date"])
                if d <= 45:
                    out["alloc_changes"].append(
                        {"bucket": bucket, "from": pa[0], "to": av[0], "days": d})
                break
    out["alloc_changes"].sort(key=lambda x: x["days"])
    for s in reversed(prior):                      # regime change
        if s.get("regime") != cur["regime"]:
            d = days_ago(s["date"])
            if d <= 90:
                out["regime_change"] = {"from": s["regime"], "to": cur["regime"], "days": d}
            break
    return out


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
        pctile = theme in ("Liquidity", "Housing")   # read vs own history
        for ind in inds:
            panel, fail = panel_for(ind, percentile_state=ind.get("pctile", pctile))
            if panel is None:
                failed.append(fail); continue
            panels.append(panel)
            scorecard.append({"theme": theme, "label": ind["label"],
                              "state": panel["state"], "criteria": panel["criteria"],
                              "direction": panel["direction"],
                              "deteriorating": panel["deteriorating"]})
            print(f"  {ind['id']:<14} {len(panel['points']):>5} pts  "
                  f"latest {panel['latest']:.2f}  state={panel['state']} "
                  f"{'worse' if panel['deteriorating'] else 'ok'}")
        themes_out[theme] = panels

    drill_out = {}
    for theme, inds in DRILLDOWNS.items():
        subs = []
        for ind in inds:
            panel, fail = panel_for(ind, percentile_state=ind.get("pctile", True))
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
    def _mom(ids):
        worse = better = 0
        for sid in ids:
            p = by_id.get(sid)
            if not p:
                continue
            worse += p["deteriorating"]
            better += p["improving"]
        return worse, better
    g_worse, g_better = _mom(GROWTH_MOM)
    i_worse, i_better = _mom(INFLATION_MOM)
    growth = "decelerating" if (g_worse - g_better) >= REGIME_MARGIN else "accelerating"
    inflation = "accelerating" if (i_worse - i_better) >= REGIME_MARGIN else "decelerating"
    rname, rplay = REGIMES[(growth, inflation)]
    val_panels = themes_out.get("Valuation", []) + drill_out.get("Valuation", [])
    v_alert = sum(1 for p in val_panels if p["state"] == "alert")
    v_hot = sum(1 for p in val_panels if p["state"] in ("alert", "caution"))
    if v_alert >= 3:
        valcond = "extreme"
    elif v_alert >= 1 or v_hot >= 3:
        valcond = "elevated"
    else:
        valcond = "normal"
    cape_p = next((p for p in val_panels if p["label"].startswith("Shiller CAPE")), None)
    valnote = (f"Shiller CAPE {cape_p['latest']:.0f}x" if cape_p
               else "market cap/GDP and household equity allocation near records")
    regime = {"name": rname, "growth": growth, "inflation": inflation,
              "playbook": rplay, "valuation": valcond, "valnote": valnote}
    print(f"  [regime] {rname} (growth {growth}, inflation {inflation}) "
          f"| valuations {valcond} [growth {g_worse}w/{g_better}b, "
          f"inflation {i_worse}w/{i_better}b]")

    # ---- Market confirmation: does the market's own risk pricing back the macro? ----
    def _mkt_status(sid, up_word):
        p = by_id.get(sid)
        if not p:
            return None, False
        if p["deteriorating"]:
            return up_word, True                     # moving the risk-off way
        if p["state"] in ("caution", "alert"):
            return "elevated", True                  # already at a risky level
        return "calm", False
    comps, n_hot = [], 0
    for sid, lbl, up in [("BAMLH0A0HYM2", "Credit spreads", "widening"),
                         ("VIXCLS", "Volatility", "rising"),
                         ("STLFSI4", "Financial stress", "rising")]:
        status, hot = _mkt_status(sid, up)
        if status is not None:
            comps.append({"label": lbl, "status": status, "hot": hot})
            n_hot += hot

    # Equity trend vs its 200-day average. S&P 500 index data is copyrighted
    # (reproduction prohibited), so the raw values are used only to derive this
    # verdict and are never written into the payload/page.
    def _equity_trend():
        try:
            sp = fetch("SP500", "2015-01-01")
            vals = [v for _, v in sp if isinstance(v, (int, float))]
            if len(vals) < 230:
                return None
            sma_now = sum(vals[-200:]) / 200.0
            sma_prev = sum(vals[-221:-21]) / 200.0     # ~1 trading month earlier
            above, rising = vals[-1] > sma_now, sma_now > sma_prev
            if above and rising:
                status = "above 200-day, rising"
            elif not above and not rising:
                status = "below 200-day, falling"
            elif above:
                status = "above 200-day, flattening"
            else:
                status = "below 200-day"
            return {"label": "Equity trend (200-day)", "status": status,
                    "hot": (not above)}
        except Exception as exc:
            print(f"  [confirm] equity trend skipped ({exc})"); return None
    et = _equity_trend()
    if et:
        comps.append(et); n_hot += et["hot"]

    market_riskoff = n_hot >= 2
    oee = next((a for a in allocation if a["bucket"] == "Overall equity exposure"), None)
    macro = ("risk-off" if oee and oee["lean"] == "Underweight"
             else "risk-on" if oee and oee["lean"] == "Overweight" else "neutral")
    if macro == "risk-off" and market_riskoff:
        verdict, tone = "Confirmed risk-off", "alert"
        msg = ("Macro and the market agree -- the equity-risk read is underweight and "
               "the market is pricing it too (see the gauges below). De-risking has "
               "confirmation, not just a forecast.")
    elif macro == "risk-off" and not market_riskoff:
        verdict, tone = "Macro early -- not yet confirmed", "caution"
        msg = ("The macro setup leans risk-off, but the market has not confirmed -- "
               "its risk gauges are mostly still calm (see below). The setup usually "
               "deteriorates ahead of price, so prepare and tighten stops, but the "
               "market is not validating an aggressive move yet. Being early here is "
               "the classic way to be wrong.")
    elif macro == "risk-on" and market_riskoff:
        verdict, tone = "Watch -- market pricing risk", "caution"
        msg = ("Macro is benign but the market is starting to price risk (see the "
               "gauges below). Either the macro catches down or this is a passing "
               "scare -- credit and price usually lead, so respect it.")
    elif macro == "risk-on":
        verdict, tone = "Confirmed risk-on", "calm"
        msg = ("Macro and the market agree -- conditions benign and the market calm. "
               "Risk-on tilts have confirmation.")
    else:
        verdict = "Market pricing risk" if market_riskoff else "Market calm"
        tone = "caution" if market_riskoff else "calm"
        msg = ("The macro equity-risk read is balanced; " +
               ("the market itself is starting to price stress (see below), which "
                "often moves first." if market_riskoff else
                "the market is calm, with risk gauges quiet."))
    confirmation = {"verdict": verdict, "tone": tone, "macro": macro,
                    "message": msg, "components": comps}
    print(f"  [confirm] macro {macro} | market {'risk-off' if market_riskoff else 'calm'} "
          f"-> {verdict}" + (f" | equity {et['status']}" if et else " | equity n/a"))

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

    # ---- History & change tracking (docs/history.json accumulates over time) ----
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts = {k: sum(1 for x in scorecard if x["state"] == k)
              for k in ("alert", "caution", "calm", "neutral")}
    counts["worsening"] = sum(1 for x in scorecard if x["direction"] == "worsening")
    counts["improving"] = sum(1 for x in scorecard if x["direction"] == "improving")
    snapshot = {
        "date": today,
        "signals": {p["series_id"]: [p["state"], p["direction"]] for p in all_panels},
        "counts": counts,
        "alloc": {a["bucket"]: [a["lean"], a["conviction"]] for a in allocation},
        "regime": regime["name"]}
    (OUT / "docs").mkdir(exist_ok=True)
    hist_path = OUT / "docs/history.json"
    try:
        history = json.loads(hist_path.read_text())
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history = [s for s in history if s.get("date") != today]   # replace same-day re-run
    history.append(snapshot)
    history.sort(key=lambda s: s["date"])
    if len(history) > 400:                                     # keep 400 daily, then monthly
        recent, older, seen, keep = history[-400:], history[:-400], set(), []
        for s in older:
            if s["date"][:7] not in seen:
                seen.add(s["date"][:7]); keep.append(s)
        history = keep + recent
    changes = _compute_changes(history, all_panels, today)
    try:
        hist_path.write_text(json.dumps(history))
    except Exception as exc:
        print(f"  [history] could not write history.json ({exc})")
    print(f"  [history] {len(history)} snapshots | {len(changes['recently_changed'])} "
          f"signal changes, {len(changes['alloc_changes'])} allocation shifts")

    payload = {
        "ny": {"latest": ny_latest, "trend": ny_trend, "points": ny_series},
        "coincident": {"latest": (coin[-1] if coin else None),
                       "points": [[d, round(v, 1)] for d, v in coin]},
        "themes": themes_out, "theme_states": theme_states, "scorecard": scorecard,
        "drilldowns": drill_out, "allocation": allocation, "jobs": jobs,
        "regime": regime, "confirmation": confirmation, "changes": changes}
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
  .confirm-banner { background:var(--card); border:1px solid var(--line); border-left-width:4px; border-radius:13px; padding:14px 18px; margin-bottom:18px; }
  .confirm-banner.bd-alert { border-left-color:var(--alert); }
  .confirm-banner.bd-caution { border-left-color:var(--caution); }
  .confirm-banner.bd-calm { border-left-color:var(--calm); }
  .cf-verdict { margin:0; font-size:16px; }
  .cf-verdict.alert { color:var(--alert); } .cf-verdict.caution { color:var(--caution); } .cf-verdict.calm { color:var(--calm); }
  .cf-msg { color:var(--ink); font-size:12.5px; line-height:1.55; margin:8px 0 10px; max-width:900px; }
  .cf-row { font-size:12px; color:var(--dim); display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
  .cf-lab { color:var(--dim); }
  .cf-sep { opacity:.5; }
  .cf-comp { border:1px solid var(--line); border-radius:20px; padding:2px 9px; }
  .cf-comp.hot { border-color:var(--alert); color:var(--ink); }
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
  .sc-key[title] { cursor:help; }
  .chip[title] { cursor:help; }
  .sc-key .dot { width:9px; height:9px; border-radius:50%; }
  .sc-key .arrow { font-size:10px; }
  .arrow.worse { color:var(--alert); }
  .arrow.better { color:var(--calm); }
  .sc-grouplabel { font-size:11px; color:var(--dim); font-weight:600; margin-right:2px; }
  .sc-div { display:inline-block; width:1px; height:15px; background:var(--line); margin:0 4px; vertical-align:middle; }
  .sc-total { opacity:.75; }
  .sc-since { font-size:11px; color:var(--dim); font-weight:400; }
  .cdelta { font-size:10px; font-weight:700; }
  .cdelta.good { color:var(--calm); }
  .cdelta.bad { color:var(--alert); }
  .rc-strip { display:flex; flex-wrap:wrap; align-items:center; gap:14px; margin:4px 0 14px; font-size:12px; }
  .rc-lab { font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--dim); }
  .rc-item { color:var(--dim); white-space:nowrap; }
  .rc-item b { color:var(--ink); font-weight:600; }
  .rc-from { text-transform:capitalize; }
  .rc-days { color:var(--dim); font-size:10.5px; }
  .alloc-chg { font-size:10.5px; color:var(--neutral); margin:-2px 0 8px; text-transform:capitalize; }
  .regime-chg { font-size:11.5px; color:var(--neutral); }
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
  #rangebar { display:flex; align-items:center; gap:6px; margin:2px 0 16px; flex-wrap:wrap; }
  .rangebar-lab { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; margin-right:4px; }
  .rangebtn, .mrangebtn { background:var(--card); border:1px solid var(--line); color:var(--dim);
     font-size:11px; font-weight:600; padding:4px 12px; border-radius:7px; cursor:pointer; }
  .rangebtn:hover, .mrangebtn:hover { color:var(--ink); border-color:var(--neutral); }
  .rangebtn.on, .mrangebtn.on { background:var(--neutral); color:var(--bg); border-color:var(--neutral); }
  #modal-range { display:flex; gap:6px; margin:4px 0 10px; }
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
<div id="confirm"></div>
<div class="gauges" id="gauges"></div>
<div class="score" id="score"></div>
<div id="alloc"></div>
<div id="rangebar">
  <span class="rangebar-lab">Chart window</span>
  <button class="rangebtn" data-rg="6m">6M</button>
  <button class="rangebtn" data-rg="1y">1Y</button>
  <button class="rangebtn" data-rg="5y">5Y</button>
  <button class="rangebtn on" data-rg="max">Max</button>
</div>
<div id="themes"></div>
</div>
<div id="modal" class="modal">
  <div class="modal-inner">
    <div class="modal-head">
      <div><h3 id="modal-title"></h3><div class="sid" id="modal-sid"></div></div>
      <button id="modal-close" aria-label="Close">&#10005;</button>
    </div>
    <div class="modal-meta" id="modal-meta"></div>
    <div id="modal-range">
      <button class="mrangebtn" data-rg="6m">6M</button>
      <button class="mrangebtn" data-rg="1y">1Y</button>
      <button class="mrangebtn" data-rg="5y">5Y</button>
      <button class="mrangebtn on" data-rg="max">Max</button>
    </div>
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
  const rchg = D.changes && D.changes.regime_change;
  const rchgTag = rchg ? `<span class="regime-chg">shifted from <b>${rchg.from}</b> ${rchg.days}d ago</span>` : '';
  document.getElementById('regime').innerHTML =
    `<div class="regime-banner">
       <div class="regime-top"><span class="regime-tag">Regime</span>`
       + `<h2>${R.name}</h2>`
       + `<span class="regime-sub">growth ${R.growth} &middot; inflation ${R.inflation}</span>${rchgTag}</div>`
     + `<p class="regime-play">${R.playbook}</p>`
     + `<div class="regime-val">Valuations <span class="badge bg-${vcls}">${R.valuation}</span>`
       + `<span class="regime-valnote">${R.valnote}</span></div>`
     + `</div>`;
}

const CF = D.confirmation;
if (CF){
  const comps = (CF.components||[]).map(c=>
    `<span class="cf-comp ${c.hot?'hot':''}">${c.label}: <b>${c.status}</b></span>`).join('');
  document.getElementById('confirm').innerHTML =
    `<div class="confirm-banner bd-${CF.tone}">
       <div class="regime-top"><span class="regime-tag">Market check</span>`
       + `<h3 class="cf-verdict ${CF.tone}">${CF.verdict}</h3></div>`
     + `<p class="cf-msg">${CF.message}</p>`
     + `<div class="cf-row"><span class="cf-lab">Macro read:</span> <b>${CF.macro}</b>`
       + `<span class="cf-sep">&middot;</span><span class="cf-lab">Market pricing:</span> ${comps}</div>`
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
const nWorse = sc.filter(x=>x.direction==='worsening').length;
const nBetter = sc.filter(x=>x.direction==='improving').length;
const CH = D.changes || {};
const scPrev = (CH.vs && CH.vs.counts) || null;
function cdelta(key, cur, goodUp){
  if(!scPrev || scPrev[key]===undefined) return '';
  const d = cur - scPrev[key]; if(d===0) return '';
  const good = (d>0)===goodUp;
  return ` <span class="cdelta ${good?'good':'bad'}">${d>0?'&#9650;':'&#9660;'}${d>0?'+':''}${d}</span>`;
}
const scKey = (v,label,color,tip,key,goodUp) => `<span class="sc-key" title="${tip||''}"><span class="dot" style="background:${color}"></span>${v} ${label}${key?cdelta(key,v,goodUp):''}</span>`;
let scHTML = `<h3>Signal scorecard${scPrev?` <span class="sc-since">change vs ${CH.vs.days}d ago</span>`:''}</h3><div class="sc-legend">`
  + `<span class="sc-grouplabel">Level now:</span>`
  + scKey(nAlert,'danger',css('--alert'),'Level is in the worst zone \u2014 past its danger threshold, or the extreme of its own history.','alert',false)
  + scKey(nCaution,'caution',css('--caution'),'Level is elevated \u2014 past the caution threshold but not yet danger.','caution',false)
  + scKey(nNeutral,'neutral',css('--neutral'),'Context only; not scored against a fixed threshold.','neutral',true)
  + scKey(nCalm,'calm',css('--calm'),'Level is in the healthy zone.','calm',true)
  + `<span class="sc-div"></span><span class="sc-grouplabel">6-mo trend:</span>`
  + `<span class="sc-key" title="The value has moved the worrying way over the last 6 months."><span class="arrow worse">&#9660;</span>${nWorse} worsening${cdelta('worsening',nWorse,false)}</span>`
  + `<span class="sc-key" title="The value has moved the reassuring way over the last 6 months."><span class="arrow better">&#9650;</span>${nBetter} improving${cdelta('improving',nBetter,true)}</span>`
  + `<span class="sc-key sc-total">${sc.length} signals</span></div>`;
if(CH.recently_changed && CH.recently_changed.length){
  scHTML += `<div class="rc-strip"><span class="rc-lab">Recently changed</span>`
    + CH.recently_changed.map(c=>`<span class="rc-item"><b>${c.label}</b> `
        + `<span class="rc-from">${stText(c.from)}</span>&#8202;&rarr;&#8202;`
        + `<span class="badge bg-${c.to}">${stText(c.to)}</span> `
        + `<span class="rc-days">${c.days}d ago</span></span>`).join('')
    + `</div>`;
}
scHTML += `<div class="chips">`;
sc.forEach(x=>{ const arw = x.direction==='worsening'?' <span class="arrow worse">&#9660;</span>':x.direction==='improving'?' <span class="arrow better">&#9650;</span>':'';
  scHTML += `<span class="chip" title="${(x.criteria||'').replace(/"/g,'&quot;')}"><span class="dot" style="background:${css('--'+x.state)}"></span>${x.label}${arw}</span>`; });
scHTML += `</div>`;
document.getElementById('score').innerHTML = scHTML;

// ---- Capital Allocation ----
const allocEl = document.getElementById('alloc');
if (D.allocation && D.allocation.length){
  const acls = l => l==='Overweight'?'calm':l==='Underweight'?'alert':l==='Balanced'?'caution':'neutral';
  let ah = `<h2 class="alloc-h">Capital Allocation</h2>
    <p class="alloc-sub">A rules-based read of what the currently-active macro signals lean toward &mdash; not advice, and every driver is shown so you can judge for yourself. A signal counts as &ldquo;active&rdquo; when it is moving its worrying way or sitting at a caution/danger level; the meter shows conviction &mdash; the <b>weighted</b> margin, so heavier signals (the yield curve, Sahm rule, credit spreads) move it more than minor ones. <b>Balanced</b> means active signals pull both ways; <b>No signal</b> means nothing mapped here is firing. Tap a card for what the bucket means and every signal feeding it &mdash; dimmed rows are mapped but not currently active.</p>
    <div class="alloc-grid">`;
  const allocCh = {}; (CH.alloc_changes||[]).forEach(c=>allocCh[c.bucket]=c);
  D.allocation.forEach((a,ai)=>{
    const lc = acls(a.lean);
    const directional = (a.lean==='Overweight'||a.lean==='Underweight');
    const fill = {strong:3, moderate:2, slight:1, none:0}[a.conviction];
    let meter = '<span class="conv">';
    for (let i=0;i<3;i++) meter += `<span class="seg ${i<fill?('on '+lc):''}"></span>`;
    meter += '</span>';
    const chg = allocCh[a.bucket];
    const chgTag = chg ? `<div class="alloc-chg">changed &middot; ${chg.from} &rarr; <b>${chg.to}</b> ${chg.days}d ago</div>` : '';
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
        + chgTag + tally + detail + `</div>`;
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
let modalRange = 'max';
let modalPanel = null;
let chartRange = 'max';          // universal window for the small card charts
const chartReg = {};             // canvasId -> {p} for redraw on range change
function slicePts(pts, range){
  if(range==='max' || !pts.length) return pts;
  const days = {'6m':183,'1y':366,'5y':1827}[range] || 0;
  if(!days) return pts;
  const cut = new Date(pts[pts.length-1][0]); cut.setDate(cut.getDate()-days);
  const kept = pts.filter(x => new Date(x[0]) >= cut);
  return kept.length >= 2 ? kept : pts;   // never leave a chart with <2 points
}
function setRange(rg){
  chartRange = rg;
  document.querySelectorAll('#rangebar .rangebtn').forEach(b =>
    b.classList.toggle('on', b.dataset.rg === rg));
  Object.keys(chartReg).forEach(cv => {
    const r = chartReg[cv];
    if(r.chart) r.chart.destroy();
    r.chart = drawSeries(cv, r.p);
  });
}

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
    let unitTxt;
    if(ad.u==='x' || ad.u==='') unitTxt='';       // multiples / unitless: change only
    else if(ad.u==='%') unitTxt='%';              // attached, no space
    else unitTxt=' '+ad.u;                         // "50 K", "5 M"
    moveTxt = `${sign}${ad.t}${unitTxt} over 6 mo`;
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
    ${mv?`<span class="move ${moveCls}">${p.direction==='worsening'?'&#9660; worsening':p.direction==='improving'?'&#9650; improving':'&#8213; steady'} &middot; ${moveTxt}</span>`:''}</div>
    <div class="asof">as of ${p.latest_date}${pctTxt}</div>
    <div class="cbox"><canvas id="cv-${cid}"></canvas><button class="expand" data-cid="${cid}" title="Expand chart" aria-label="Expand chart">&#10530;</button></div>
    <div class="note">${p.note}</div>`;
  return card;
}
function drawSeries(canvasId, p, opts){
  opts = opts || {}; const big = !!opts.big;
  const cap = opts.maxPts || (big?4000:500);
  const pts = slicePts(p.points, opts.range || (big ? 'max' : chartRange));
  const step = Math.max(1,Math.floor(pts.length/cap));
  const thin = pts.filter((_,k)=>k%step===0);
  const ds = [{ data: thin.map(x=>({x:x[0],y:x[1]})), borderColor:css('--neutral'),
                borderWidth: big?2:1.5, pointRadius:0, pointHoverRadius: big?5:4,
                pointHoverBackgroundColor:css('--neutral'), pointHoverBorderColor:css('--bg'),
                pointHoverBorderWidth:2, tension:0.08, fill:false }];
  function refLine(val,cv){ if(val===null||val===undefined) return;
    ds.push({ data:[{x:thin[0][0],y:val},{x:thin[thin.length-1][0],y:val}],
      borderColor:css(cv), borderWidth:1, borderDash:[4,4], pointRadius:0, pointHoverRadius:0, fill:false }); }
  refLine(p.caution,'--caution'); refLine(p.alert,'--alert');
  const rng = opts.range || (big ? 'max' : chartRange);
  const xunit = (rng === '6m' || rng === '1y') ? 'month' : 'year';
  return new Chart(document.getElementById(canvasId),{ type:'line', data:{datasets:ds}, plugins:[crosshair],
    options:{ responsive:true, maintainAspectRatio:false, animation:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false}, tooltip:{position:'nearest',intersect:false,mode:'index',displayColors:false,
        padding: big?10:8, titleFont:{size: big?13:11}, bodyFont:{size: big?14:12},
        callbacks:{title:i=>i[0].raw.x, label:i=>{const d=dispVU(i.raw.y,p); return d.t+' '+d.u;}}}},
      scales:{ x:{type:'time',time:{unit:xunit},ticks:{color:css('--dim'),font:{size:big?11:9},maxTicksLimit:big?12:6},grid:{color:css('--grid')}},
               y:{ticks:{color:css('--dim'),font:{size:big?12:10},maxTicksLimit:big?6:4,callback:(val)=>axisTick(val,p)},grid:{color:css('--grid')}} } } });
}
function paintChart(p,cid){
  const cv = 'cv-'+cid;
  if(chartReg[cv] && chartReg[cv].chart) chartReg[cv].chart.destroy();
  chartReg[cv] = {p, chart: drawSeries(cv, p)};
}

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
  modalPanel = p; modalRange = 'max';
  document.querySelectorAll('#modal-range .mrangebtn').forEach(b=>b.classList.toggle('on', b.dataset.rg==='max'));
  requestAnimationFrame(()=>{ modalChart = drawSeries('modal-cv', p, {big:true, range:modalRange}); });
}
function setModalRange(rg){
  modalRange = rg;
  document.querySelectorAll('#modal-range .mrangebtn').forEach(b=>b.classList.toggle('on', b.dataset.rg===rg));
  if(modalPanel){ if(modalChart) modalChart.destroy();
    modalChart = drawSeries('modal-cv', modalPanel, {big:true, range:rg}); }
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
  const flashSubs = subs.filter(s=>s.state==='alert'||s.state==='caution');
  const okSubs = subs.filter(s=>!(s.state==='alert'||s.state==='caution'));
  const sec = document.createElement('div'); sec.className='theme';
  const detTxt = ts.deteriorating>0 ? `${ts.deteriorating} of ${ts.total} deteriorating` : 'stable';
  const lead = subs.length ? ` &middot; <span class="lead">${flashSubs.length}/${subs.length} leading signals flashing</span>` : '';
  const chev = subs.length ? `<span class="chev" id="chev-${key}">&#9656;</span>` : '';
  sec.innerHTML = `<div class="theme-head${subs.length?' expandable':''}">${chev}<h2>${theme}</h2>
    <span class="theme-state bg-${ts.state}">${stText(ts.state)}</span>
    <span class="theme-note">${detTxt}${lead}</span></div><div class="cards"></div>`;
  tRoot.appendChild(sec);
  const cardsEl = sec.querySelector('.cards');
  panels.forEach((p,idx)=>{ const cid=key+idx; cardsEl.appendChild(makeCard(p,cid)); paintChart(p,cid); });

  if(subs.length){
    const dd = document.createElement('div'); dd.className='drilldown'; dd.id='dd-'+key;
    dd.innerHTML = `<p class="dd-intro">Leading signals that move before the headline. Grouped by where each sits <b>right now</b> &mdash; <b>flashing</b> (elevated or extreme) vs <b>healthy</b> &mdash; and colour is the level. The arrow on each is a separate thing: its <b>6-month direction</b>, so a healthy signal can be worsening and a flashing one improving.</p>
      <div class="dd-cols">
        <div class="dd-col for"><h5>Flashing now &middot; ${flashSubs.length}</h5><div class="dd-for"></div></div>
        <div class="dd-col against"><h5>Healthy now &middot; ${okSubs.length}</h5><div class="dd-against"></div></div>
      </div>`;
    sec.appendChild(dd);
    const forEl = dd.querySelector('.dd-for'), againstEl = dd.querySelector('.dd-against');
    const paints = [];
    if(!flashSubs.length) forEl.innerHTML = '<div class="dd-empty">Nothing elevated right now.</div>';
    if(!okSubs.length) againstEl.innerHTML = '<div class="dd-empty">Nothing in the healthy zone.</div>';
    flashSubs.forEach((s,i)=>{ const cid=key+'-df-'+i; forEl.appendChild(makeCard(s,cid)); paints.push([s,cid]); });
    okSubs.forEach((s,i)=>{ const cid=key+'-da-'+i; againstEl.appendChild(makeCard(s,cid)); paints.push([s,cid]); });
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
document.querySelectorAll('#rangebar .rangebtn').forEach(b=>
  b.addEventListener('click', ()=>setRange(b.dataset.rg)));
document.querySelectorAll('#modal-range .mrangebtn').forEach(b=>
  b.addEventListener('click', ()=>setModalRange(b.dataset.rg)));
</script>
</body></html>"""

if __name__ == "__main__":
    build()
