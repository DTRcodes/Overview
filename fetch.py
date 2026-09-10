#!/usr/bin/env python3
"""
Financial dashboard data fetcher.

Pulls every source into a single docs/data.json. Each section fetches
independently and fails soft: if a source is unreachable (e.g. NSE blocking a
cloud IP), the previous good value is carried forward with its original
timestamp and flagged stale, so the dashboard degrades honestly instead of
going blank or silently lying.

Usage:
    python fetch.py                      # write docs/data.json
    python fetch.py --only rbi           # run a subset
    python fetch.py --probe              # connectivity check, writes nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "data.json"
HISTORY = ROOT / "docs" / "history.json"
CACHE = ROOT / "cache"

# Seconds a cached response stays usable. 0 = always hit the network, which is
# what the nightly cron wants. Set it high (--max-age 86400) while editing the
# dashboard so repeated runs replay from disk and never re-hit NSE, AMFI or
# investing.com. Hammering those is how you get IP-banned.
MAX_AGE = 0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 30

# ---------------------------------------------------------------- config ----

INDEXES = [
    "Nifty 50", "Nifty Next 50", "Nifty Bank", "Nifty 500",
    "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty IT", "Nifty Auto",
]

INDIA_TENORS = {"10Y": "india-10-year-bond-yield",
                "5Y": "india-5-year-bond-yield",
                "2Y": "india-2-year-bond-yield"}

US_YF = {"3M": "^IRX", "5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX"}

FRED_SERIES = {
    "fed_target_upper": "DFEDTARU",
    "fed_target_lower": "DFEDTARL",
    "fed_effective": "EFFR",
    "us_2y": "DGS2",
    # DGS5 must stay in this list: the yfinance fallback supplies us_5y, and
    # the dashboard plots us_10y against us_5y. Without it, switching to FRED
    # silently stops feeding that series and the chart line freezes.
    "us_5y": "DGS5",
    "us_10y": "DGS10",
    "us_30y": "DGS30",
    "us_10y_2y_spread": "T10Y2Y",
}

EARNINGS_TICKERS = ["RELIANCE.NS", "HDFCBANK.NS", "TCS.NS", "INFY.NS", "ICICIBANK.NS"]

GILT_MATCH = re.compile(r"gilt", re.I)
GILT_PREFER = re.compile(r"direct.*growth", re.I)
GILT_LIMIT = 12

REGISTRY = {}


def source(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def now_iso():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


class Cached:
    """Stand-in for a requests.Response, replayed from disk."""

    def __init__(self, text, from_cache=True):
        self.text = text
        self.content = text.encode("utf-8", "replace")
        self.status_code = 200
        self.from_cache = from_cache

    def json(self):
        return json.loads(self.text)


def get(url, **kw):
    """GET with an on-disk cache.

    Only successful responses are cached, so the 404 walk-back that finds the
    last NSE trading day still works normally.
    """
    kw.setdefault("headers", HEADERS)
    kw.setdefault("timeout", TIMEOUT)

    key = hashlib.sha1(
        (url + json.dumps(kw.get("params") or {}, sort_keys=True)).encode()
    ).hexdigest()[:16]
    path = CACHE / (key + ".cache")

    if MAX_AGE > 0 and path.exists():
        if time.time() - path.stat().st_mtime < MAX_AGE:
            return Cached(path.read_text(encoding="utf-8"))

    r = requests.get(url, **kw)
    r.raise_for_status()
    if MAX_AGE > 0:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text, encoding="utf-8")
    return r


def num(s):
    """Parse NSE/RBI numbers: '.1' -> 0.1, '-.23' -> -0.23, '1,234.5' -> 1234.5."""
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "--", "NA", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------- sources ----

@source("rbi")
def fetch_rbi():
    """RBI policy rates + reference FX, straight off the homepage."""
    html = get("https://www.rbi.org.in/").text
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", html))

    def grab(label):
        m = re.search(re.escape(label) + r"\s*\|[\s|]*:?\s*([0-9.]+)\s*%?", flat)
        return num(m.group(1)) if m else None

    rates = {
        "policy_repo_rate": grab("Policy Repo Rate"),
        "standing_deposit_facility": grab("Standing Deposit Facility Rate"),
        "marginal_standing_facility": grab("Marginal Standing Facility Rate"),
        "bank_rate": grab("Bank Rate"),
        "reverse_repo_rate": grab("Fixed Reverse Repo Rate"),
        "crr": grab("CRR"),
        "slr": grab("SLR"),
    }
    if rates["policy_repo_rate"] is None:
        raise RuntimeError("RBI homepage layout changed - repo rate not found")

    fx = {}
    for label, key in [("INR / 1 USD", "USD"), ("INR / 1 GBP", "GBP"),
                       ("INR / 1 EUR", "EUR"), ("INR / 100 JPY", "JPY_100")]:
        m = re.search(re.escape(label) + r"\s*\|[\s|]*:?\s*([0-9.,]+)", flat)
        if m:
            fx[key] = num(m.group(1))

    return {"rates": rates, "fx_reference": fx,
            "unit": "percent", "source": "rbi.org.in"}


@source("nse_fii_dii")
def fetch_fii_dii():
    """Daily FII/DII cash-market activity. Cloud IPs may get blocked here."""
    data = get("https://www.nseindia.com/api/fiidiiTradeReact").json()
    out = {}
    for row in data:
        key = "fii" if "FII" in row.get("category", "").upper() else "dii"
        out[key] = {"date": row.get("date"),
                    "buy": num(row.get("buyValue")),
                    "sell": num(row.get("sellValue")),
                    "net": num(row.get("netValue"))}
    if not out:
        raise RuntimeError("empty FII/DII payload")
    return {"flows": out, "unit": "INR crore", "source": "nseindia.com/api"}


def participant_oi(day):
    """FII/DII/Pro/Client open interest in equity derivatives for one day.

    This is the only FII/DII series NSE actually archives. The cash-market
    figure on the FII/DII tile (INR crore bought and sold) exists at
    /api/fiidiiTradeReact for the latest day ONLY - it ignores any date
    parameter, and no dated cash file exists under nsearchives (probed
    several path shapes, all 404). So cash can only accumulate forward,
    while this one backfills to 2015.

    Different metric, deliberately kept in different history keys: contracts
    of open interest, not rupees traded.
    """
    url = ("https://nsearchives.nseindia.com/content/nsccl/"
           "fao_participant_oi_" + day.strftime("%d%m%Y") + ".csv")
    lines = [l for l in get(url).text.splitlines() if l.strip()]
    hdr = None
    out = {}
    for ln in lines:
        cells = [c.strip().strip('"') for c in ln.split(",")]
        if cells[0].lower() == "client type":
            hdr = cells
            continue
        if hdr is None or cells[0] not in ("FII", "DII", "Pro", "Client"):
            continue
        row = dict(zip(hdr, cells))
        who = cells[0].lower()
        fl, fs = num(row.get("Future Index Long")), num(row.get("Future Index Short"))
        sl, ss = num(row.get("Future Stock Long")), num(row.get("Future Stock Short"))
        out[who] = {
            "index_fut_long": fl, "index_fut_short": fs,
            "index_fut_net": (fl - fs) if (fl is not None and fs is not None) else None,
            "stock_fut_net": (sl - ss) if (sl is not None and ss is not None) else None,
        }
    if "fii" not in out:
        raise RuntimeError("no FII row in participant OI for " + str(day))
    return out


@source("fii_derivatives")
def fetch_fii_derivatives():
    """Latest FII/DII positioning in equity derivatives (walk back to last file)."""
    today = dt.datetime.now(IST).date()
    for back in range(0, 8):
        day = today - dt.timedelta(days=back)
        try:
            data = participant_oi(day)
        except Exception:
            continue
        return {"date": day.isoformat(), "participants": data,
                "unit": "contracts of open interest",
                "caveat": ("Open interest in equity derivatives - NOT the cash "
                           "INR-crore flow shown on the FII/DII tile."),
                "source": "nsearchives.nseindia.com/content/nsccl"}
    raise RuntimeError("no participant OI file in the last 8 days")


def backfill_participants(days=400, pause=0.3):
    """Walk back N calendar days building FII/DII derivatives history."""
    today = dt.datetime.now(IST).date()
    rows, hits, miss = [], 0, 0
    for back in range(0, days + 1):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:               # skip weekends without a request
            continue
        try:
            d = participant_oi(day)
        except Exception:
            miss += 1
            continue
        hits += 1
        rows.append({"date": day.isoformat(),
                     "fii_idx_fut_net": d["fii"]["index_fut_net"],
                     "dii_idx_fut_net": d["dii"]["index_fut_net"],
                     "fii_stk_fut_net": d["fii"]["stock_fut_net"],
                     "dii_stk_fut_net": d["dii"]["stock_fut_net"]})
        time.sleep(pause)
    hist = merge_rows(load_history(), rows)
    save_history(hist)
    return hits, miss, len(hist["rows"] if isinstance(hist, dict) else hist)


@source("nse_valuation")
def fetch_index_valuation():
    """Index close + P/E + P/B + dividend yield from the NSE archives CSV.

    One file per trading day, all indices in it, no cookie needed. Walk back
    from today to skip weekends and holidays.
    """
    today = dt.datetime.now(IST).date()
    last_err = None
    for back in range(0, 11):
        day = today - dt.timedelta(days=back)
        stamp = day.strftime("%d%m%Y")
        url = ("https://nsearchives.nseindia.com/content/indices/"
               "ind_close_all_" + stamp + ".csv")
        try:
            r = get(url)
        except Exception as e:  # 404 on non-trading days is expected
            last_err = e
            continue

        rows = [ln.split(",") for ln in r.text.splitlines() if ln.strip()]
        header, body = rows[0], rows[1:]
        idx = {h.strip(): i for i, h in enumerate(header)}
        wanted = {n.lower() for n in INDEXES}
        out = {}
        for row in body:
            name = row[idx["Index Name"]].strip()
            if name.lower() not in wanted:
                continue
            out[name] = {
                "close": num(row[idx["Closing Index Value"]]),
                "change_pct": num(row[idx["Change(%)"]]),
                "pe": num(row[idx["P/E"]]),
                "pb": num(row[idx["P/B"]]),
                "div_yield": num(row[idx["Div Yield"]]),
            }
        if out:
            return {"as_of": day.isoformat(), "indices": out,
                    "note": ("NSE switched Nifty P/E from standalone to "
                             "consolidated earnings on 31-Mar-2021; the series "
                             "has a level break there."),
                    "source": "nsearchives.nseindia.com"}
    raise RuntimeError("no archive CSV in last 11 days (%s)" % last_err)


@source("india_yields")
def fetch_india_yields():
    """India G-Sec yields - the weakest tile on the board.

    Every free source is compromised: CCIL and FBIL render their tables in
    JavaScript, worldgovernmentbonds too, CCIL forbids commercial reuse, and
    Yahoo has no India tenor at all. investing.com is scrapeable but sits
    behind Cloudflare and starts 403-ing under any sustained polling.

    So: try investing.com opportunistically, fall back to FRED's OECD series,
    which is authoritative but MONTHLY and lagged. Either way we report which
    one answered and how old the number is, so the dashboard can say
    "as of Jun 2026, monthly" instead of implying it is live.
    """
    out, via, as_of = {}, None, None

    for tenor, slug in INDIA_TENORS.items():
        try:
            html = get("https://in.investing.com/rates-bonds/" + slug).text
            m = re.search(r'"last"\s*:\s*"?([0-9]+\.[0-9]+)', html)
            if m:
                out[tenor] = num(m.group(1))
                via = "investing.com (live)"
        except Exception:
            continue

    if not out:
        key = os.environ.get("FRED_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "investing.com blocked (Cloudflare) and no FRED_API_KEY set "
                "for the monthly fallback")
        r = get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": "INDIRLTLT01STM", "api_key": key,
                        "file_type": "json", "sort_order": "desc", "limit": 5})
        obs = [o for o in r.json().get("observations", [])
               if o.get("value") not in (".", "", None)]
        if not obs:
            raise RuntimeError("no India tenors from investing.com or FRED")
        out["10Y"] = num(obs[0]["value"])
        as_of = obs[0]["date"]
        via = "FRED INDIRLTLT01STM (monthly, lagged)"

    return {"yields": out, "unit": "percent", "via": via, "as_of": as_of,
            "fragile": True,
            "caveat": ("India yields have no reliable free live source. "
                       "Point this at a broker feed for real coverage."),
            "source": via}


@source("us_rates")
def fetch_us_rates():
    """US policy rate and Treasury curve. FRED when a key is present
    (authoritative), yfinance otherwise (no key, close enough)."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    out, via = {}, None

    if key:
        via = "fred"
        for label, sid in FRED_SERIES.items():
            try:
                r = get("https://api.stlouisfed.org/fred/series/observations",
                        params={"series_id": sid, "api_key": key,
                                "file_type": "json", "sort_order": "desc",
                                "limit": 5})
                obs = [o for o in r.json().get("observations", [])
                       if o.get("value") not in (".", "", None)]
                if obs:
                    out[label] = {"value": num(obs[0]["value"]),
                                  "date": obs[0]["date"]}
            except Exception:
                continue

    if not out:
        via = "yfinance"
        try:
            import yfinance as yf
            for tenor, tk in US_YF.items():
                h = yf.Ticker(tk).history(period="5d")
                if len(h):
                    out["us_" + tenor.lower()] = {
                        "value": round(float(h["Close"].iloc[-1]), 3),
                        "date": str(h.index[-1].date())}
        except Exception as e:
            raise RuntimeError("yfinance fallback failed: %s" % e)

    if not out:
        raise RuntimeError("no US rates retrieved")
    return {"rates": out, "unit": "percent", "via": via,
            "source": "fred.stlouisfed.org" if via == "fred" else "yahoo finance"}


# FRED renamed this OECD family part-way through its life, so both spellings
# are live depending on the country. India's working id uses the second form.
# Try each in turn rather than guessing - whichever answers, wins.
WORLD_10Y = {
    "Japan": ["IRLTLT01JPM156N", "JPNIRLTLT01STM"],
    "UK": ["IRLTLT01GBM156N", "GBRIRLTLT01STM"],
}
WORLD_HISTORY_KEY = {"Japan": "jp_10y", "UK": "uk_10y"}


@source("world_yields")
def fetch_world_yields():
    """Japan and UK 10-year government bond yields.

    Same problem as India: no free live source. Yahoo has no tenor for either,
    and the usual scrape targets render in JavaScript. FRED's OECD series are
    authoritative but **monthly and lagged**, so the card says so rather than
    implying a live quote.

    Returns the last decade of observations too, so the chart has real history
    on day one instead of accumulating a point a month.
    """
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY not set - required for JP/UK yields")

    latest, series, used = {}, {}, {}
    for country, candidates in WORLD_10Y.items():
        for sid in candidates:
            try:
                r = get("https://api.stlouisfed.org/fred/series/observations",
                        params={"series_id": sid, "api_key": key,
                                "file_type": "json", "sort_order": "desc",
                                "limit": 130})
                obs = [o for o in r.json().get("observations", [])
                       if o.get("value") not in (".", "", None)]
            except Exception:
                continue
            if not obs:
                continue
            latest[country] = {"value": num(obs[0]["value"]),
                               "date": obs[0]["date"]}
            series[country] = [[o["date"], num(o["value"])] for o in obs]
            used[country] = sid
            break

    if not latest:
        raise RuntimeError("no JP/UK series resolved from " +
                           str({k: v for k, v in WORLD_10Y.items()}))

    return {"latest": latest, "series": series, "series_ids": used,
            "unit": "percent", "frequency": "monthly (OECD via FRED)",
            "caveat": ("Monthly and lagged - these are not live quotes. "
                       "No free live source exists for either tenor."),
            "source": "fred.stlouisfed.org"}


@source("fx_spot")
def fetch_fx_spot():
    """USD/INR market spot.

    Kept separate from the RBI reference rate on purpose. RBI publishes a
    once-daily reference fixing; Yahoo carries the traded spot. They differ by
    a couple of paise, so splicing one onto the other inside a single series
    puts a fake step in the chart. The reference rate stays in the RBI table.
    """
    import yfinance as yf
    h = yf.Ticker("INR=X").history(period="5d")
    if not len(h):
        raise RuntimeError("no INR=X data")
    return {"usd_inr": round(float(h["Close"].iloc[-1]), 4),
            "as_of": str(h.index[-1].date()),
            "source": "yahoo finance (INR=X spot)"}


@source("gilt_funds")
def fetch_gilt_funds():
    """Gilt fund NAVs from AMFI. Note the 302 to portal.amfiindia.com."""
    txt = get("https://portal.amfiindia.com/spages/NAVAll.txt",
              allow_redirects=True).text
    lines = txt.splitlines()

    # Column layout is read from the header rather than hardcoded: AMFI split
    # Plan and Option into their own columns, so the file is 8 fields wide, not
    # the 6 that older scrapers assume.
    header = next((l for l in lines if l.startswith("Scheme Code;")), None)
    if not header:
        raise RuntimeError("NAVAll.txt header row not found")
    col = {h.strip().lower(): i for i, h in enumerate(header.split(";"))}
    try:
        c_code = col["scheme code"]
        c_name = col["scheme name"]
        c_nav = col["net asset value"]
        c_date = col["date"]
    except KeyError as e:
        raise RuntimeError("NAVAll.txt columns changed: %s" % e)
    c_plan = col.get("plan")
    c_opt = col.get("option")
    width = len(col)

    picks, fallback = [], []
    for line in lines:
        if ";" not in line or line.startswith("Scheme Code;"):
            continue
        parts = line.split(";")
        if len(parts) < width:
            continue
        name = parts[c_name].strip()
        if not GILT_MATCH.search(name):
            continue
        nav = num(parts[c_nav])
        if nav is None:
            continue
        plan = parts[c_plan].strip() if c_plan is not None else ""
        option = parts[c_opt].strip() if c_opt is not None else ""
        rec = {"code": parts[c_code].strip(), "name": name, "plan": plan,
               "option": option, "nav": nav, "date": parts[c_date].strip()}
        direct_growth = (GILT_PREFER.search(plan + " " + option)
                         or GILT_PREFER.search(name))
        (picks if direct_growth else fallback).append(rec)

    funds = (picks or fallback)[:GILT_LIMIT]
    if not funds:
        raise RuntimeError("no gilt schemes parsed from NAVAll.txt")
    return {"funds": funds, "count_matched": len(picks) + len(fallback),
            "source": "portal.amfiindia.com"}


def _d(s):
    """'08-SEP-2026' -> date, or None."""
    s = (s or "").strip()
    if s in ("", "-"):
        return None
    try:
        return dt.datetime.strptime(s.title(), "%d-%b-%Y").date()
    except ValueError:
        return None


@source("nse_ipo")
def fetch_ipo():
    """The IPO pipeline, split by where each issue actually is.

    NSE assigns the trading symbol when the issue opens, well before listing,
    so the TradingView ticker is knowable in advance - that is the whole point
    of the tv field below.

    NSE never publishes a *future* listing date (checked: 0 of 1431 rows), so
    "lists tomorrow" is not something any endpoint can tell you. What it does
    give is an issue whose subscription has closed and whose listingDate is
    still blank - that is an imminent listing, which is the useful signal.

    Three buckets:
      listing_today   - listingDate == today          (green on the board)
      awaiting        - closed, listingDate still '-'  (blue on the board)
      open_now        - inside the subscription window
    """
    today = dt.datetime.now(IST).date()
    current = get("https://www.nseindia.com/api/all-upcoming-issues"
                  "?category=ipo").json()
    past = get("https://www.nseindia.com/api/public-past-issues").json()

    def tv(sym):
        """TradingView needs the exchange prefix to resolve a fresh listing."""
        sym = (sym or "").strip()
        return "NSE:" + sym if sym else None

    # past-issues mixes equity with debt paper - 40 N0 rows, 22 DEBT, 13 Z9.
    # Tata Capital's 805TACA29 NCD listed today and would otherwise show up as
    # an "IPO listing today" and land in the TradingView watchlist.
    EQUITY = {"EQ", "SME", "BE"}

    # NSE's listingDate is NOT reliable: MOMSBELIEF listed 08-Sep-2026 (it is in
    # that day's bhavcopy at 239.00/228.34) and still reads '-' days later. So
    # listingDate is treated as a hint and the bhavcopy as the fact - if a
    # symbol trades, it has listed, whatever the field says.
    candidates = []
    for r in past:
        sym = (r.get("symbol") or "").strip()
        if not sym or (r.get("securityType") or "").strip().upper() not in EQUITY:
            continue
        ld, closed = _d(r.get("listingDate")), _d(r.get("ipoEndDate"))
        if ld is None and not (closed and 0 <= (today - closed).days <= 45):
            continue                       # old, or withdrawn and never listed
        candidates.append((r, sym, ld, closed))

    # One request settles "has it started trading?" for every candidate at once:
    # a stock that listed at any point is still in the newest bhavcopy.
    trading = set()
    for back in range(0, 8):
        try:
            trading = set(_bhavcopy(today - dt.timedelta(days=back)))
            break
        except Exception:
            continue

    listing_today, awaiting = [], []
    for r, sym, ld, closed in candidates:
        row = {"company": r.get("company"), "symbol": sym, "tv": tv(sym),
               "series": (r.get("securityType") or "").strip(),
               "price_range": r.get("priceRange"),
               "issue_price": r.get("issuePrice"),
               "listing_date": r.get("listingDate"),
               "ipo_closed": r.get("ipoEndDate")}

        if ld is None and sym in trading:
            # Trades but NSE never filled the field in: find the real first day.
            ld = first_traded_day(sym, closed)
            row["listing_date"] = ld.strftime("%d-%b-%Y").upper() if ld else "listed"
            row["listing_date_source"] = "bhavcopy (NSE field blank)"

        if ld == today:
            listing_today.append(row)
        elif ld is None and sym not in trading and closed:
            row["days_since_close"] = (today - closed).days
            awaiting.append(row)

    awaiting.sort(key=lambda r: r["days_since_close"])

    open_now = []
    for r in current:
        sym = (r.get("symbol") or "").strip()
        o, c = _d(r.get("issueStartDate")), _d(r.get("issueEndDate"))
        open_now.append({"company": r.get("companyName"), "symbol": sym,
                         "tv": tv(sym), "price": r.get("issuePrice"),
                         "opens": r.get("issueStartDate"),
                         "closes": r.get("issueEndDate"),
                         "series": r.get("series"), "status": r.get("status"),
                         "live": bool(o and c and o <= today <= c)})
    open_now.sort(key=lambda r: (not r["live"], r["closes"] or ""))

    def clean_past(r):
        return {"company": r.get("company"), "symbol": r.get("symbol"),
                "tv": tv(r.get("symbol")),
                "price_range": r.get("priceRange"),
                "issue_price": r.get("issuePrice"),
                "listing_date": r.get("listingDate")}

    # Everything with a ticker worth adding to a watchlist, newest state first.
    watchlist = [r["tv"] for r in listing_today + awaiting + open_now if r["tv"]]

    return {"listing_today": listing_today, "awaiting": awaiting[:15],
            "open_now": open_now[:15],
            "current": open_now[:15],          # kept: older card reads this
            "recent_past": [clean_past(r) for r in past][:40],
            "past_total": len(past),
            "tv_watchlist": watchlist,
            "tv_note": ("NSE assigns the symbol at issue open, so these "
                        "resolve on TradingView before the stock lists."),
            "as_of": today.isoformat(), "source": "nseindia.com/api"}


IPO_STORE = ROOT / "docs" / "ipo_listings.json"
IPO_PER_RUN = 12          # cap bhavcopy requests in a normal daily run


def _price_from(rec):
    """Issue price. Falls back to the top of the band, which is where Indian
    IPOs price in practice when the book is covered."""
    p = num(rec.get("issuePrice"))
    if p:
        return p
    band = re.findall(r"([0-9][0-9,.]*)", rec.get("priceRange") or "")
    return num(band[-1]) if band else None


_BHAV_MEMO = {}


def first_traded_day(symbol, closed, window=25):
    """First day `symbol` appears in a bhavcopy after its issue closed.

    NSE leaves listingDate blank on a fair number of issues even after they
    start trading, so this reconstructs it. Scans forward from the close;
    bhavcopies are memoised per run because candidates share dates.
    """
    if not closed:
        return None
    for step in range(1, window + 1):
        day = closed + dt.timedelta(days=step)
        if day > dt.datetime.now(IST).date():
            return None
        if day.weekday() >= 5:
            continue
        try:
            if symbol in _bhavcopy(day):
                return day
        except Exception:
            continue          # holiday / missing file
    return None


def _bhavcopy(day):
    """All symbols' OHLC for one trading day, keyed by symbol.

    One request covers every stock that listed that day, so resolving N IPOs
    costs one call per distinct listing date rather than one per company.
    (Note the old `cmDDMMMYYYYbhav.csv.zip` path most tutorials use now 404s;
    `sec_bhavdata_full` is the live one.)
    """
    if day in _BHAV_MEMO:
        return _BHAV_MEMO[day]
    url = ("https://nsearchives.nseindia.com/products/content/"
           "sec_bhavdata_full_" + day.strftime("%d%m%Y") + ".csv")
    lines = get(url).text.splitlines()
    if not lines:
        raise RuntimeError("empty bhavcopy")
    hdr = [h.strip() for h in lines[0].split(",")]
    out = {}
    for ln in lines[1:]:
        parts = [c.strip() for c in ln.split(",")]
        if len(parts) < len(hdr):
            continue
        row = dict(zip(hdr, parts))
        sym, series = row.get("SYMBOL"), row.get("SERIES")
        # EQ/SM are the tradable listing series; BE/GS etc. would shadow them
        if sym and (sym not in out or series in ("EQ", "SM")):
            out[sym] = row
    _BHAV_MEMO[day] = out
    return out


def _load_ipo_store():
    if IPO_STORE.exists():
        try:
            return json.loads(IPO_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"listings": {}, "unresolvable": []}


def resolve_ipo_gains(limit=IPO_PER_RUN, pause=0.35):
    """Fill in listing-day gains for IPOs we have not priced yet.

    NSE gives issue price and listing date but not the listing print, so the
    gain has to be computed: join each listing date to that day's bhavcopy.
    Two numbers come out of it - the open (the pop you'd get flipping at the
    bell) and the close (holding the day out). They differ a lot, so both are
    kept rather than picking one and calling it "the" listing gain.
    """
    store = _load_ipo_store()
    done, bad = store["listings"], set(store.get("unresolvable", []))

    past = get("https://www.nseindia.com/api/public-past-issues").json()
    todo = []
    for rec in past:
        sym = (rec.get("symbol") or "").strip()
        ld = (rec.get("listingDate") or "").strip()
        if not sym or sym in done or sym in bad:
            continue
        price = _price_from(rec)
        if not price:
            continue

        day = None
        if ld not in ("", "-"):
            try:
                day = dt.datetime.strptime(ld.title(), "%d-%b-%Y").date()
            except ValueError:
                day = None
        else:
            # NSE leaves listingDate blank on plenty of issues that HAVE listed
            # - MOMSBELIEF traded from 08-Sep-2026 while the field still read
            # '-'. Skipping those silently lost them from the gains table
            # entirely, so recover the date from the bhavcopy instead. Bounded
            # to recent closes; older blanks are genuinely withdrawn issues.
            closed = _d(rec.get("ipoEndDate"))
            if closed and 0 <= (dt.datetime.now(IST).date() - closed).days <= 60:
                day = first_traded_day(sym, closed)
        if day is None:
            continue
        todo.append((day, sym, rec.get("company"), price))

    todo.sort(key=lambda t: t[0], reverse=True)     # newest first
    by_day = {}
    for day, sym, company, price in todo:
        by_day.setdefault(day, []).append((sym, company, price))

    added, calls = 0, 0
    for day in sorted(by_day, reverse=True):
        if calls >= limit:
            break
        try:
            bhav = _bhavcopy(day)
            calls += 1
        except Exception:
            for sym, _, _ in by_day[day]:
                bad.add(sym)          # holiday or missing file: do not retry forever
            continue
        for sym, company, price in by_day[day]:
            row = bhav.get(sym)
            if not row:
                bad.add(sym)
                continue
            op, cl = num(row.get("OPEN_PRICE")), num(row.get("CLOSE_PRICE"))
            if not op or not cl:
                bad.add(sym)
                continue
            done[sym] = {
                "symbol": sym, "company": company,
                "issue_price": price, "listing_date": day.isoformat(),
                "open": op, "close": cl,
                "gain_open_pct": round((op - price) / price * 100, 2),
                "gain_close_pct": round((cl - price) / price * 100, 2),
            }
            added += 1
        time.sleep(pause)

    store["listings"], store["unresolvable"] = done, sorted(bad)
    IPO_STORE.parent.mkdir(parents=True, exist_ok=True)
    IPO_STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    return added, len(done), calls


@source("ipo_gains")
def fetch_ipo_gains():
    """Listing-day performance, newest first, plus hit-rate stats."""
    added, total, calls = resolve_ipo_gains()
    store = _load_ipo_store()
    rows = sorted(store["listings"].values(),
                  key=lambda r: r["listing_date"], reverse=True)
    if not rows:
        raise RuntimeError("no IPO listings resolved yet")

    def stats(sample):
        if not sample:
            return None
        opens = sorted(r["gain_open_pct"] for r in sample)
        closes = sorted(r["gain_close_pct"] for r in sample)
        mid = lambda xs: xs[len(xs) // 2]
        return {"count": len(sample),
                "median_open_pct": round(mid(opens), 2),
                "median_close_pct": round(mid(closes), 2),
                "positive_open_pct": round(
                    100 * sum(1 for v in opens if v > 0) / len(opens), 1),
                "best": max(sample, key=lambda r: r["gain_open_pct"])["symbol"],
                "worst": min(sample, key=lambda r: r["gain_open_pct"])["symbol"]}

    return {"listings": rows[:40], "resolved_total": total,
            "added_this_run": added, "bhavcopy_calls": calls,
            "stats_last_50": stats(rows[:50]), "stats_all": stats(rows),
            "note": ("Gain measured against the issue price. Open = the "
                     "listing pop; close = holding the first day out."),
            "source": "nseindia.com/api + sec_bhavdata_full"}


@source("earnings")
def fetch_earnings():
    """Quarterly revenue and net income. Yahoo's Indian fundamentals have
    gaps - quarters go missing - so treat these as indicative."""
    import yfinance as yf
    out = {}
    for tk in EARNINGS_TICKERS:
        try:
            df = yf.Ticker(tk).quarterly_income_stmt
            if df is None or df.empty:
                continue
            quarters = []
            for col in list(df.columns)[:4]:
                rec = {"quarter": str(col.date())}
                for label, key in [("Total Revenue", "revenue"),
                                   ("Net Income", "net_income")]:
                    val = df.loc[label, col] if label in df.index else None
                    rec[key] = float(val) if val is not None and val == val else None
                quarters.append(rec)
            if quarters:
                out[tk] = quarters
        except Exception:
            continue
    if not out:
        raise RuntimeError("no earnings retrieved")
    return {"companies": out, "unit": "INR",
            "caveat": ("Yahoo Indian fundamentals are gappy; quarters go "
                       "missing. Validate before relying."),
            "source": "yahoo finance"}


# ------------------------------------------------------------ orchestration --

HISTORY_FIELDS = ["nifty_close", "nifty_pe", "nifty_pb", "nifty_div_yield",
                  "fii_net", "dii_net", "repo", "us_10y", "us_2y", "us_5y",
                  "jp_10y", "uk_10y",
                  "fii_idx_fut_net", "dii_idx_fut_net",
                  "fii_stk_fut_net", "dii_stk_fut_net",
                  "india_10y", "usd_inr"]


def load_history():
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"rows": []}


def save_history(hist):
    hist["rows"].sort(key=lambda r: r["date"])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist, indent=1, ensure_ascii=False),
                       encoding="utf-8")


def merge_rows(hist, new_rows):
    """Upsert by date, so re-running on the same day corrects rather than
    duplicates, and a backfill never clobbers a field it does not carry."""
    by_date = {r["date"]: r for r in hist.get("rows", [])}
    for row in new_rows:
        cur = by_date.setdefault(row["date"], {"date": row["date"]})
        for k, v in row.items():
            if k != "date" and v is not None:
                cur[k] = v
    hist["rows"] = list(by_date.values())
    return hist


def row_from_sections(S):
    """Flatten today's snapshot into one history row."""
    def dig(*path, default=None):
        cur = S
        for p in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(p)
        return cur if cur is not None else default

    n50 = dig("nse_valuation", "indices", "Nifty 50", default={}) or {}
    date = dig("nse_valuation", "as_of") or dt.datetime.now(IST).date().isoformat()
    us = dig("us_rates", "rates", default={}) or {}

    def usv(k):
        v = us.get(k)
        return v.get("value") if isinstance(v, dict) else None

    return {
        "date": date,
        "nifty_close": n50.get("close"),
        "nifty_pe": n50.get("pe"),
        "nifty_pb": n50.get("pb"),
        "nifty_div_yield": n50.get("div_yield"),
        "fii_net": dig("nse_fii_dii", "flows", "fii", "net"),
        "dii_net": dig("nse_fii_dii", "flows", "dii", "net"),
        "repo": dig("rbi", "rates", "policy_repo_rate"),
        # market spot, not the RBI fixing - see fetch_fx_spot
        "usd_inr": dig("fx_spot", "usd_inr"),
        "us_10y": usv("us_10y"),
        "us_2y": usv("us_2y"),
        "us_5y": usv("us_5y"),
        "india_10y": dig("india_yields", "yields", "10Y"),
        "jp_10y": (dig("world_yields", "latest", "Japan") or {}).get("value"),
        "uk_10y": (dig("world_yields", "latest", "UK") or {}).get("value"),
    }


NIFTY_PE_JSON = ("https://nifty-pe-ratio.com/wp-content/uploads/nifty-data/"
                 "nifty-public.json")
NIFTY_PE_TXT = ROOT / "NiftyPE_History.txt"


def nifty_pe_history():
    """Deep Nifty P/E history - weekly, back to 1999.

    NSE's own archive only goes back as far as you are willing to walk it one
    CSV per day. nifty-pe-ratio.com publishes the same numbers as a single
    weekly JSON. Verified against NSE on every overlapping date in the local
    history: max absolute P/E difference 0.000, so it is a faithful mirror
    rather than someone's re-derivation.

    Writes NiftyPE_History.txt and folds the series into history.json.
    """
    d = get(NIFTY_PE_JSON, headers={**HEADERS,
                                    "Referer": "https://nifty-pe-ratio.com/"}).json()
    weekly = d.get("weekly") or []
    if not weekly:
        raise RuntimeError("no weekly series in the feed")

    meta, kpi = d.get("meta", {}), d.get("kpi", {})
    pes = sorted(r[1] for r in weekly if r[1])

    def pct(p):
        return pes[min(len(pes) - 1, int(len(pes) * p / 100))]

    # cross-check against whatever NSE-sourced rows we already hold
    local = {r["date"]: r.get("nifty_pe") for r in load_history().get("rows", [])
             if r.get("nifty_pe") is not None}
    overlap = [(r[0], r[1], local[r[0]]) for r in weekly if r[0] in local]
    worst = max((abs(a - b) for _, a, b in overlap), default=None)

    lines = []
    add = lines.append
    add("NIFTY 50 - HISTORICAL P/E, P/B AND DIVIDEND YIELD")
    add("=" * 66)
    add("Source       : nifty-pe-ratio.com (nifty-public.json)")
    add("               mirrors NSE-published index valuation figures")
    if overlap:
        add("Cross-check  : %d dates overlap the NSE archive "
            "(archives.nseindia.com)" % len(overlap))
        add("               max |P/E difference| = %.3f" % worst)
    add("Granularity  : %s" % meta.get("granularity", "weekly"))
    add("Range        : %s .. %s  (%d points)"
        % (weekly[0][0], weekly[-1][0], len(weekly)))
    add("Generated    : %s" % now_iso())
    add("")
    add("!! LEVEL BREAK - READ BEFORE COMPARING ACROSS DATES")
    add("NSE switched Nifty P/E from STANDALONE to CONSOLIDATED earnings on")
    add("31-Mar-2021. Values either side of that date are not on the same")
    add("basis. A pre-2021 reading is not directly comparable to a later one.")
    add("")
    add("CURRENT      : P/E %.2f   P/B %.2f   Div yield %.2f%%"
        % (kpi.get("pe", 0), kpi.get("pb", 0), kpi.get("dy", 0)))
    add("P/E RANGE    : min %.2f   p25 %.2f   median %.2f   p75 %.2f   max %.2f"
        % (pes[0], pct(25), pct(50), pct(75), pes[-1]))
    add("")
    add("%-12s %8s %8s %8s %12s" % ("DATE", "P/E", "P/B", "DIV%", "CLOSE"))
    add("-" * 66)
    for row in weekly:
        date, pe, pb, dy = row[0], row[1], row[2], row[3]
        close = row[4] if len(row) > 4 else None
        add("%-12s %8s %8s %8s %12s"
            % (date,
               "%.2f" % pe if pe else "-",
               "%.2f" % pb if pb else "-",
               "%.2f" % dy if dy else "-",
               "%.2f" % close if close else "-"))

    NIFTY_PE_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Wrote %s (%d points, %s..%s)"
          % (NIFTY_PE_TXT, len(weekly), weekly[0][0], weekly[-1][0]))
    if overlap:
        print("Cross-check vs NSE: %d dates, max |diff| %.3f" % (len(overlap), worst))

    rows = [{"date": r[0], "nifty_pe": r[1], "nifty_pb": r[2],
             "nifty_div_yield": r[3],
             "nifty_close": r[4] if len(r) > 4 else None} for r in weekly]
    hist = merge_rows(load_history(), rows)
    save_history(hist)
    print("History now %d rows" % len(hist["rows"]))
    return 0


def backfill(days=180, pause=0.4):
    """Rebuild history from sources that expose the past.

    NSE publishes one all-indices CSV per trading day, so the Nifty P/E series
    can be reconstructed rather than accumulated from today forward. Yahoo hands
    over the US curve in one call per tenor. FII/DII has no public history
    endpoint - that one only accumulates going forward.
    """
    hist = load_history()
    rows, hits = [], 0
    today = dt.datetime.now(IST).date()
    print("Backfilling %d days of NSE index valuation..." % days)

    for back in range(days):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:          # skip weekends, no file exists
            continue
        stamp = day.strftime("%d%m%Y")
        url = ("https://nsearchives.nseindia.com/content/indices/"
               "ind_close_all_" + stamp + ".csv")
        try:
            r = get(url)
        except Exception:
            continue                    # holiday
        for ln in r.text.splitlines()[1:]:
            p = ln.split(",")
            if len(p) > 12 and p[0].strip().lower() == "nifty 50":
                rows.append({"date": day.isoformat(),
                             "nifty_close": num(p[5]), "nifty_pe": num(p[10]),
                             "nifty_pb": num(p[11]), "nifty_div_yield": num(p[12])})
                hits += 1
                break
        time.sleep(pause)               # be a polite client
        if hits and hits % 20 == 0:
            print("  ...%d trading days" % hits)
    print("  got %d trading days" % hits)

    try:
        import yfinance as yf
        print("Backfilling US treasury curve...")
        for label, tk in [("us_10y", "^TNX"), ("us_5y", "^FVX")]:
            h = yf.Ticker(tk).history(period="%dd" % (days + 10))
            for idx, val in h["Close"].items():
                rows.append({"date": str(idx.date()), label: round(float(val), 3)})
        h = yf.Ticker("INR=X").history(period="%dd" % (days + 10))
        for idx, val in h["Close"].items():
            rows.append({"date": str(idx.date()), "usd_inr": round(float(val), 4)})
        print("  ok")
    except Exception as e:
        print("  skipped (%s)" % e)

    merge_rows(hist, rows)
    save_history(hist)
    print("History now %d rows -> %s" % (len(hist["rows"]), HISTORY))
    return 0


def load_previous():
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def run(only=None):
    prev = load_previous()
    prev_sections = prev.get("sections", {})
    sections, failures = {}, []

    for name, fn in REGISTRY.items():
        if only and name not in only:
            if name in prev_sections:
                sections[name] = prev_sections[name]
            continue
        try:
            payload = fn()
            payload["fetched_at"] = now_iso()
            payload["stale"] = False
            sections[name] = payload
            print("  [ok]    " + name)
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, e)
            failures.append({"source": name, "error": msg})
            old = prev_sections.get(name)
            if old:
                carried = dict(old)
                carried["stale"] = True
                carried["stale_reason"] = msg
                sections[name] = carried
                print("  [STALE] %s -> carried forward from %s (%s)"
                      % (name, old.get("fetched_at"), msg))
            else:
                sections[name] = {"stale": True, "stale_reason": msg,
                                  "fetched_at": None}
                print("  [FAIL]  %s -> no previous value (%s)" % (name, msg))

    return {"generated_at": now_iso(),
            "generated_at_utc": dt.datetime.now(dt.timezone.utc)
                                  .isoformat(timespec="seconds"),
            "failures": failures,
            "sections": sections}


def probe():
    """Connectivity check - the point is to find out which hosts a cloud
    runner can actually reach. Writes nothing."""
    # Probe a real dated CSV, not a bare directory: nsearchives serves files,
    # not listings, so the directory 404s even from an unblocked connection.
    # That false alarm made the first run report a block that was not there.
    day = dt.datetime.now(IST).date()
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    archive_url = ("https://nsearchives.nseindia.com/content/indices/"
                   "ind_close_all_" + day.strftime("%d%m%Y") + ".csv")

    targets = [
        ("RBI homepage", "https://www.rbi.org.in/", "rbi", None),
        ("NSE api (FII/DII)", "https://www.nseindia.com/api/fiidiiTradeReact",
         "nse", None),
        ("NSE api (IPO)",
         "https://www.nseindia.com/api/all-upcoming-issues?category=ipo",
         "nse", None),
        ("NSE archives", archive_url, "nse", None),
        ("AMFI portal", "https://portal.amfiindia.com/spages/NAVAll.txt",
         "amfi", None),
        ("investing.com",
         "https://in.investing.com/rates-bonds/india-10-year-bond-yield",
         "india_yields", "403 here is routine Cloudflare; FRED is the fallback"),
        # 400 "api_key is not set" proves reachability - it is an auth error,
        # not a network block.
        ("FRED api", "https://api.stlouisfed.org/fred/series?series_id=DGS10",
         "fred", "400 = reachable, key not set"),
        ("Yahoo Finance",
         "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
         "yahoo", None),
    ]

    print("Connectivity probe @ " + now_iso())
    print("-" * 72)
    failed = set()
    for label, url, group, hint in targets:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            code = r.status_code
            reachable = code < 400 or (group == "fred" and code == 400)
            if not reachable:
                failed.add(group)
            note = "ok" if reachable else "BLOCKED"
            if hint:
                note += "  (" + hint + ")"
            print("%-20s HTTP %-4s %9d b  %s" % (label, code, len(r.content), note))
        except Exception as e:
            failed.add(group)
            print("%-20s ERROR  %s: %s" % (label, type(e).__name__, e))

    print("-" * 72)
    if "nse" in failed:
        print("NSE unreachable -> phase 2: run the fetch on a self-hosted "
              "runner at home for the FII/DII, IPO and valuation tiles.")
    else:
        print("NSE reachable from this runner -> no self-hosted runner needed.")
    if "india_yields" in failed:
        print("investing.com blocked - expected. Set FRED_API_KEY for the "
              "monthly India-yield fallback; every other tile is unaffected.")
    core = failed - {"india_yields"}
    print("All core sources reachable." if not core
          else "Core sources blocked: " + ", ".join(sorted(core)))
    return 0


def main():
    global MAX_AGE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", nargs="*", help="subset of %s" % sorted(REGISTRY))
    ap.add_argument("--probe", action="store_true",
                    help="connectivity check only, writes nothing")
    ap.add_argument("--backfill", type=int, metavar="DAYS",
                    help="rebuild history from NSE archives + Yahoo, then exit")
    ap.add_argument("--backfill-participants", type=int, metavar="N", default=0,
                    help="walk back N calendar days building FII/DII "
                         "derivatives-OI history from the NSE archive")
    ap.add_argument("--backfill-ipo", type=int, metavar="N", default=0,
                    help="resolve listing-day gains for up to N past listing "
                         "dates (one bhavcopy request each); run once")
    ap.add_argument("--nifty-history", action="store_true",
                    help="fetch weekly Nifty P/E back to 1999, write "
                         "NiftyPE_History.txt and merge into history.json")
    ap.add_argument("--max-age", type=int, default=0, metavar="SEC",
                    help="reuse cached responses younger than SEC "
                         "(use 86400 while editing the dashboard)")
    args = ap.parse_args()

    MAX_AGE = args.max_age
    if MAX_AGE:
        print("cache: reusing responses younger than %ds" % MAX_AGE)

    if args.probe:
        return probe()
    if args.backfill_participants:
        hits, miss, total = backfill_participants(args.backfill_participants)
        print("Participant OI: %d days fetched, %d missing (holidays), "
              "history now %d rows" % (hits, miss, total))
        return 0
    if args.backfill_ipo:
        added, total, calls = resolve_ipo_gains(limit=args.backfill_ipo)
        print("Resolved %d new listings in %d bhavcopy calls; %d total -> %s"
              % (added, calls, total, IPO_STORE))
        return 0
    if args.nifty_history:
        return nifty_pe_history()
    if args.backfill:
        return backfill(args.backfill)

    print("Fetching @ " + now_iso())
    data = run(only=set(args.only) if args.only else None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")

    rows = [row_from_sections(data["sections"])]
    wy = data["sections"].get("world_yields") or {}
    for country, obs in (wy.get("series") or {}).items():
        key = WORLD_HISTORY_KEY.get(country)
        if key:
            rows += [{"date": d, key: v} for d, v in obs if v is not None]
    hist = merge_rows(load_history(), rows)
    save_history(hist)

    print("\nWrote %s  (%d bytes)" % (OUT, OUT.stat().st_size))
    print("History %d rows -> %s" % (len(hist["rows"]), HISTORY))
    if data["failures"]:
        print("%d source(s) failed - dashboard shows last good values, "
              "flagged stale." % len(data["failures"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
