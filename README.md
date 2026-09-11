# Markets Board — phase 1

A static dashboard of Indian and US macro data, readable on any device.
A Python fetcher writes JSON once a day; a static page reads it. No server,
nothing to keep running, nothing that can be rate-limited by visitors.

```
fetch.py  ──(daily 19:00 IST)──>  docs/data.json      current snapshot
                                  docs/history.json   one row per day
                                        │
                                  docs/index.html     reads both, draws charts
```

## Why static rather than Streamlit

Streamlit needs a live Python server, and it re-fetches on every page load and
every widget click. Point that at NSE and you get IP-banned. Here the sources
are hit exactly once a day by one process; visitors only ever touch a CDN.

## The sources

| Tile | Source | Notes |
|---|---|---|
| Policy rates, reference FX | `rbi.org.in` homepage | Repo, SDF, MSF, bank rate, CRR, SLR |
| Nifty P/E, P/B, div yield | `nsearchives.nseindia.com` | One CSV per trading day, all indices, no cookie |
| FII / DII cash flows | `nseindia.com/api/fiidiiTradeReact` | Plain UA header is enough |
| IPOs | `nseindia.com/api/…issues` | 1,400+ past issues |
| IPO listing gains | `sec_bhavdata_full` join | Computed: issue price vs listing-day open/close |
| World 5Y & 10Y | US Treasury, Bundesbank, BoE, Japan MOF, ChinaBond, FBIL | All daily, all key-less |

| US rates | FRED (with key) → Yahoo fallback | Works without a key |
| USD/INR spot | Yahoo `INR=X` | Deliberately separate from the RBI fixing |
| Earnings | Yahoo `quarterly_income_stmt` | Indian fundamentals are gappy |
| India G-Sec yields | FBIL par yield archive | Daily, 200 tenors, authoritative |

### The yield sources, and why not FRED

FRED's cross-country OECD family is **ten-year only and monthly**, which rules
it out for a 5Y comparison. Each country's own publisher has a daily, key-less
feed instead:

| Country | Source | Notes |
|---|---|---|
| US | home.treasury.gov | Daily par curve, one CSV per year |
| Germany | api.statistiken.bundesbank.de | Content-negotiates on Accept-Language — the parser takes both `3,45` and `3.45` |
| UK | bankofengland.co.uk IADB | `IUDSNPY` = 5Y, `IUDMNPY` = 10Y |
| Japan | mof.go.jp | The `all` file stops at last month-end; the current month layers on top |
| China | yield.chinabond.com.cn | Server-rendered HTML, takes a `workTime` date |
| India | fbil.org.in `/wasdm/gsec/download?date=` | Undocumented; found by watching what FBIL's own page calls |

FBIL is the RBI-recognised benchmark administrator, so it is the authoritative
Indian curve. Its workbook carries 200 tenors in two conventions; the
**semi-annual** one is stored, since that is how India's 10Y is quoted (6.98 vs
7.10 annualised on 04-Sep-2026, against 6.96 on investing.com).


## Running it

```bash
pip install -r requirements.txt
python fetch.py                    # write data.json + append a history row
python fetch.py --probe            # which hosts can this machine reach?
python fetch.py --max-age 86400    # replay from cache, hit nothing
python fetch.py --backfill 180     # rebuild history from NSE archives + Yahoo
python fetch.py --nifty-history    # weekly Nifty P/E back to 1999
python fetch.py --backfill-ipo 60  # price N past listing dates (one call each)
python seed_fii_history.py --check # validate the Mar-Aug FII seed, write nothing
python seed_fii_history.py         # ...then merge it
```

**Use `--max-age 86400` while editing the dashboard.** It replays responses
from `cache/` so repeated runs never re-hit NSE, AMFI or investing.com.
Hammering those is how you get banned. The nightly cron runs with the default
`0`, which always fetches fresh.

## How it fails

Every source fetches independently. If one is unreachable, the last good value
is carried forward with its **original** timestamp and flagged `stale`, and the
tile shows a "stale · 3 h ago" badge. The dashboard degrades honestly instead of
going blank or, worse, showing an old number as if it were live.

## History

`docs/history.json` holds one row per date, upserted — re-running corrects
rather than duplicates.

- **Nifty P/E: 1,540 points back to Jan 1999.** Weekly pre-2026 from
  nifty-pe-ratio.com, daily thereafter from the NSE archive.
- US 10Y / 5Y and USD/INR: ~200 days from Yahoo.
- FII/DII: accumulates forward only — NSE publishes no history endpoint.

`NiftyPE_History.txt` is the readable dump of the deep series with percentiles.

`docs/ipo_listings.json` accumulates listing-day gains. NSE gives the issue
price and listing date but not the listing print, so the gain is computed by
joining each listing date to that day's `sec_bhavdata_full` bhavcopy — one
request per date, covering every stock that listed that day. Both the open
(the flip) and the close (holding day one out) are kept; they diverge a lot.
Note the older `cmDDMMMYYYYbhav.csv.zip` path that most tutorials still use
now 404s.

> **Level break:** NSE switched Nifty P/E from standalone to consolidated
> earnings on 31-Mar-2021. Readings either side are not on the same basis.
> The third-party series was verified against NSE on all 26 overlapping dates —
> max absolute P/E difference 0.000.

## Deploying

The site is served straight from this repo by **GitHub Pages** — no other
account or service needed.

1. Push this folder to the repo.
2. Repo **Settings -> Pages**: Source = *Deploy from a branch*, branch `main`,
   folder **`/docs`**. Save. The URL appears within a minute or two.
3. Optional: add a `FRED_API_KEY` repo secret (free key from
   fredapi/stlouisfed) for authoritative US rates and the India-yield fallback.

Note that GitHub Pages sites are **public**. Everything here is public market
data, so that is fine; if you later want it private, put it behind Cloudflare
Pages + Cloudflare Access instead.

`.github/workflows/update.yml` runs at 13:30 UTC = **19:00 IST**, weekdays, and
commits the two JSON files if anything changed.

## Where the fetch runs — settled

Probed from a GitHub Actions runner on 2026-09-08: **NSE answers Azure runners
fine.** FII/DII, IPO and the index archive all returned 200. No self-hosted
runner is needed; phase 1 is the whole job.

(The first probe reported a block. It was wrong: it requested a bare directory
on `nsearchives`, which 404s from any connection because that host serves files
and not listings. It also read FRED's `400 api_key is not set` as a block rather
than a missing key. Both fixed.)

The one genuine block is **investing.com**, which 403s behind Cloudflare from
the runner. Set a `FRED_API_KEY` secret and the India-yield tile falls back to
FRED's monthly series; every other tile is unaffected.
