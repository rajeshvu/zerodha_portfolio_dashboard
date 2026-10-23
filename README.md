# Zerodha portfolio dashboard

This local Streamlit app tracks Zerodha mutual-fund and equity holdings using
Kite Connect. Mutual funds are loaded from `/mf/holdings`; long-term equity
holdings are loaded from `/portfolio/holdings`. Each portfolio has its own
current-value and estimated-invested-value chart, with daily snapshots stored
in SQLite. Dashboard figures use Indian digit grouping (for example,
`₹12,34,567.89`). Headline totals and the summary table use approximate lakh
and crore abbreviations for larger amounts; holding details and chart values
remain in rupees.

## Setup

1. In your Kite developer app settings, set the redirect URL to exactly:
   `http://127.0.0.1:8765/callback`
2. Copy `.env.example` to `.env` and fill in your API key and API secret.
   Keep `.env` private; do not share or commit it.
3. In PowerShell, from this directory:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python auth.py
   ```

   Complete the Zerodha login in the browser. The helper stores the resulting
   access token locally in `access_token.json`, which is excluded from Git.
4. Start the dashboard:

   ```powershell
   streamlit run dashboard.py
   ```

Open the local URL printed by Streamlit. Use the **Mutual funds** and **Stocks**
tabs to view each portfolio, and **Summary** to see combined invested amount,
current value, unrealized profit/loss, and a combined history chart. Use
**Custom investments** to add or edit assets such as FD, RD, EPF, and PPF; click
**Save custom investments** to store those values locally. Enter amounts as
whole rupees using Indian comma grouping (for example, `1,23,456`). Custom
investments are included in Summary totals, have a per-investment invested vs
current value chart, and are included in the combined history from their first
saved snapshot. The Custom investments tab has its own daily history chart; it
records the saved values when you open the dashboard and when you save edits,
with at most one snapshot per day. Refresh both Kite portfolio tabs on the same
day to add a combined history point. Refresh each Kite portfolio tab separately
to fetch the latest holdings and save its daily snapshot. The stock tab covers
long-term equity holdings, includes T+1 unsettled shares, and does not include
open intraday or futures and options positions.

## Desktop shortcut

Double-click **Zerodha MF Dashboard** on the desktop to run `auth.py` and,
after successful login, start the dashboard. Keep the console window open while
using the dashboard. It contains separate mutual fund and stock tabs. The
shortcut runs `launch_dashboard.bat` in this folder.

## Authentication and data freshness

Kite access tokens expire at 6 AM the next day. Log in again with `python auth.py`
when the session expires. Do not put your API secret or access token in source
code or publish them. Automatic unattended daily refresh is not possible with
the usual daily access token; Zerodha documents long-lived refresh tokens only
for certain approved platforms.

Kite returns the last available mutual-fund NAV or stock price, which may be
older than today on weekends, holidays, or before a fund publishes its NAV. The
dashboard shows NAV dates for mutual funds. Invested amount and profit/loss
figures are estimates from Kite's average price and units/quantity. Earlier
mutual-fund snapshots saved by a version that did not store invested value
cannot be reconstructed; newly refreshed snapshots include it.
