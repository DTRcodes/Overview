# NIFTY 50 Sector/Stock Allocation Dashboard (web)

A web port of the "NIFTY50 Sector/Stock Allocation DashBoard Pro" Pine indicator. The
weights and the constituent list are downloaded from official sources on every refresh,
so a rebalance needs **no edit anywhere**.

```
site/index.html        the dashboard (one file, no libraries)
site/data/nifty50.json live snapshot + weights + bases   (written by the feed)
site/data/intraday.json 1-minute opens for 3 PM / Custom (written by the feed)
feed/nifty_feed.py     the data feed (Python 3.10+, needs requests; pypdf optional)
feed/nse_mcp.py        NSE's official MCP server: price fallback + its own intraday series
feed/reference_model.py independent check of the page's maths
.github/workflows/nifty-feed.yml  GitHub Actions schedule (static hosting)
```

## Where the weights come from

| What | Source | How often |
|---|---|---|
| Weight of each stock | NSE live index feed: free-float market cap (`ffmc`) per stock. weight = ffmc / Σffmc | every refresh |
| Constituent list + sector | niftyindices.com `ind_nifty50list.csv` | every 6 h, and at once if NSE shows an unknown symbol |
| Official month-end weights (check) | niftyindices.com monthly report `indices_data<Mon><YYYY>.zip` → `NIFTY_50_*.pdf` | daily check for a new month |
| Official weights + beta (check) | niftyindices.com `mcwb_<mon><yy>.zip` → `nifty50_mcwb.csv` | daily check for a new month |
| Previous week / month close | NSE bhavcopy + `ind_close_all`, split/bonus-adjusted via NSE corporate actions | cached per date |

Verified 4 Oct 2026: weights computed from NSE's ffmc at the 30-Sep close match the official
30-Sep-2026 PDF to within **0.005 %** on all 50 stocks, and the 50 stocks' points add up to
NIFTY's actual move (−198.52 vs −198.50 on 1 Oct).

NIFTY 50 changes members **twice a year** (March and September); free-float factors are
updated **quarterly**; the weights themselves move **every tick** with prices. Live ffmc
captures all three.

## Run it on your PC

```bash
pip install requests pypdf
python feed/nifty_feed.py serve --port 8050
```
Open http://localhost:8050/. The feed refreshes from NSE at most every 20 s while the market
is open (15 min otherwise), only when someone is looking.

## Put it on a website

**A. Static hosting (GitHub Pages / Cloudflare Pages / Netlify).** Push this folder to a
GitHub repo, publish `site/`, and the workflow in `.github/workflows/` refreshes the data
every ~5 min in market hours. GitHub's scheduler is best-effort (runs can be 5–15 min late).

**B. VPS / any server that runs Python.** `python feed/nifty_feed.py serve --port 8050`
behind your web server (or `once --intraday --out /var/www/nifty/data` from cron every minute).
This is the only way to get a true ~30 s live board.

**C. Embedding into an existing site.** Copy `site/index.html` in and point it at your data:
```html
<script>window.NIFTY_DASH_CONFIG = { dataUrl: "https://example.com/nifty/data/nifty50.json",
                                     intradayUrl: "https://example.com/nifty/data/intraday.json" };</script>
```
(The data host must send `Access-Control-Allow-Origin` if it is a different domain; serve mode does.)

A browser cannot call NSE directly (CORS + bot protection), so something server-side must
run the feed; that is all options A–C differ in.

## Rebalances inside a Weekly / Monthly period

If NIFTY changed members inside the period, the feed splits the period at the effective
date R (`rebalance` in nifty50.json):

- **before R:** the OLD members, weighted by the official month-end PDF's index market cap,
  rolled forward with daily closes (split/bonus-adjusted);
- **from R:** today's members, weighted by live ffmc rolled back to the close before R.

R is found from the data. It is the split day that lets the old list explain NIFTY's daily
returns before it, and the new list after it, with the least error. For the 30-Sep-2026
rebalance: R = 30-Sep, fit error 0.0003% vs 0.049% for the next-best day. Week to 1-Oct-2026:
stock points sum to **−718.55 = NIFTY −718.55** (−707.78 without the split).

On the page, an exited stock (WIPRO) shows as a dashed orange "(exited)" cell with its points
for its time in the index. A new stock (BSE) shows as a dashed blue "(new)" cell. Its % is the
full-period price move, its points count only from R, and the tooltip gives its in-index move.
Under a split, sector % = sector points ÷ sector weight, so Σ weight × sector % = NIFTY %.

## Fallback: NSE's official MCP server

`feed/nse_mcp.py` wraps NSE's MCP server (`https://mcp.nseindia.in/cmmkt/mcp`). The feed uses it
automatically when NSE's website API fails - the risk listed under "Known limits".

What that server does and does not give (probed 4 Oct 2026):

| | |
|---|---|
| Gives | last, open, high, low, prevClose, volume per symbol |
| Does NOT give | free-float market cap (`ffmc`), and **any** history - every tool returns the latest crawl |
| Freshness | crawls NSE every 5 min, caches 15 min (the primary feed refreshes every 20 s) |
| Bulk reads | `cm_get_equity_stocks(limit<=500, symbolFilter=<one prefix>)`; 50 symbols take ~19 calls, ~3 s |

Because it has no ffmc, the fallback carries free-float SHARE COUNTS from the last good primary
payload (cached at `feed/cache/last_live.json`): `shares = prev_ffmc / prev_last`, then
`ffmc = shares * live_last`, and the index is rebuilt through the same divisor. Share counts only
change when NSE revises free-float factors (quarterly), so they hold for the session.

It refuses rather than publish something misleading when:
- symbols carrying more than **0.5 %** of index weight are missing (a frozen heavyweight behind a
  live-looking index is worse than an honest stale badge), or
- a constituent's exchange previous close has moved more than 10 % away from the cached price,
  which is what a split, bonus or rights issue looks like from here - the carried share counts
  would be wrong.
Symbols missing below that threshold are held at their last price, kept on the board, and flagged
`stale` so the rows still sum to the index.

### Intraday series from the MCP

The MCP has no history, so `feed/cache/mcp_intraday/today.json` accumulates what we see: one entry
per symbol per crawl, appended on every refresh, deduplicated on the server's own crawl stamp.
At a date change the file is archived to `mcp_intraday/archive/<date>.json` and a new day starts.
A copy is published as `site/data/intraday_mcp.json`.

This is a **redundant, coarser** series: the page's own intraday still comes from NSE's 1-minute
chart endpoint, which is finer and covers the whole day retroactively. The MCP series only knows
what it was running for - it cannot back-fill a morning it did not watch - so treat it as a
backstop for the days the chart endpoint fails, not as the primary.

## Known limits

- One split per period. Two membership changes inside one month would need a second split
  point (very rare for NIFTY 50). Free-float-only changes with no member change are not split
  (effect is tiny).
- Intraday 3 PM / Custom use NSE's 1-minute chart points; "open of the HH:MM candle" is the last
  print before HH:MM:00, which can differ from TradingView's candle open by a tick.
- The feed scrapes NSE's website APIs. They are free but unofficial and change without notice
  (the old `equity-stockIndices` endpoint now returns 404). If a refresh fails, the page keeps
  the last good data and shows a "stale" badge.
