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


@source("nse_ipo")
def fetch_ipo():
    """Current/upcoming IPOs plus the past-issues archive. Cloud IPs may block.

    Listing-day gain is not in this payload; it needs a join to the listing-date
    close from the bhavcopy. Phase 2.
    """
    current = get("https://www.nseindia.com/api/all-upcoming-issues"
                  "?category=ipo").json()
    past = get("https://www.nseindia.com/api/public-past-issues").json()

    def clean_current(r):
        return {"company": r.get("companyName"), "symbol": r.get("symbol"),
                "price": r.get("issuePrice"), "opens": r.get("issueStartDate"),
                "closes": r.get("issueEndDate"), "status": r.get("status")}

    def clean_past(r):
        return {"company": r.get("company"), "symbol": r.get("symbol"),
                "price_range": r.get("priceRange"),
                "issue_price": r.get("issuePrice"),
                "listing_date": r.get("listingDate")}

    return {"current": [clean_current(r) for r in current][:15],
            "recent_past": [clean_past(r) for r in past][:40],
            "past_total": len(past), "source": "nseindia.com/api"}


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
    if args.nifty_history:
        return nifty_pe_history()
    if args.backfill:
        return backfill(args.backfill)

    print("Fetching @ " + now_iso())
    data = run(only=set(args.only) if args.only else None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")

    hist = merge_rows(load_history(), [row_from_sections(data["sections"])])
    save_history(hist)

    print("\nWrote %s  (%d bytes)" % (OUT, OUT.stat().st_size))
    print("History %d rows -> %s" % (len(hist["rows"]), HISTORY))
    if data["failures"]:
        print("%d source(s) failed - dashboard shows last good values, "
              "flagged stale." % len(data["failures"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
