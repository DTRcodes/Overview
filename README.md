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
| Gilt NAVs | `portal.amfiindia.com/spages/NAVAll.txt` | Note the 302 from the old host |
| US rates | FRED (with key) → Yahoo fallback | Works without a key |
| USD/INR spot | Yahoo `INR=X` | Deliberately separate from the RBI fixing |
| Earnings | Yahoo `quarterly_income_stmt` | Indian fundamentals are gappy |
| India G-Sec yields | investing.com → FRED monthly | **The weak one — see below** |

### India yields is the weak tile

There is no reliable free live source. CCIL, FBIL and worldgovernmentbonds all
render their tables in JavaScript; CCIL forbids commercial reuse; Yahoo has no
India tenor at all. investing.com is scrapeable but sits behind Cloudflare and
starts returning 403 under any sustained polling — it did exactly that during
this build. The fallback is FRED's OECD series, which is authoritative but
**monthly and lagged**. For real coverage, point this at a broker feed.

## Running it

```bash
pip install -r requirements.txt
python fetch.py                    # write data.json + append a history row
python fetch.py --probe            # which hosts can this machine reach?
python fetch.py --max-age 86400    # replay from cache, hit nothing
python fetch.py --backfill 180     # rebuild history from NSE archives + Yahoo
python fetch.py --nifty-history    # weekly Nifty P/E back to 1999
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

## The one open question

NSE firewalls cloud-provider IP ranges, and GitHub Actions runs on Azure. Six of
the nine sources are unaffected, but FII/DII and IPOs may 403 from a runner.

Run the **Source reachability probe** workflow once (Actions tab → Run workflow)
and read the log:

- *all ok* → phase 1 is the whole job.
- *NSE blocked* → phase 2: register a self-hosted runner on a home machine or a
  Raspberry Pi. The workflow stays on GitHub; execution happens on a residential
  IP, where NSE answers. Everything else keeps running on GitHub's runners.

Until that is settled, those two tiles simply show their last good value with a
stale badge — which is why the carry-forward behaviour exists.
