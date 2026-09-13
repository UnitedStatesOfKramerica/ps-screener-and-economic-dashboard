"""
One-off diagnostic -- NOT part of the permanent codebase. Run once, paste
the full output back.

Inspects the Federal Reserve's own Enhanced Financial Accounts monthly
IPO/SEO issuance CSV before writing any parsing code against it: real
header row, date range, and format, rather than guessing.
"""
import requests

URL = "https://www.federalreserve.gov/releases/efa/equity-issuance-retirement-monthly-historical.csv"

r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
print(f"HTTP {r.status_code}, {len(r.content)} bytes, content-type={r.headers.get('content-type')}")

lines = r.text.strip().splitlines()
print(f"\n{len(lines)} lines total")
print("first 5 lines:")
for line in lines[:5]:
    print(" ", repr(line))
print("last 5 lines:")
for line in lines[-5:]:
    print(" ", repr(line))
