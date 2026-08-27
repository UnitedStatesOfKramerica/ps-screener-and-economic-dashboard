# P/S Screener

S&P 500 companies priced against their own ten-year price-to-sales history,
built from as-filed SEC data and split-adjusted share counts.

The screen is published at `https://USERNAME.github.io/ps-screener/` and
refreshes on weekday evenings.

- `ps_screener.py` — the whole thing
- `test_current_build.py` — 36 checks on invented data
- `replay.py` — 17 checks against real filings, from `fixture.json.gz`
- `docs/index.html` — the latest run

The SEC requires a contact address on every request. It is supplied through a
repository secret named `SEC_EMAIL` so it never appears in the code.
