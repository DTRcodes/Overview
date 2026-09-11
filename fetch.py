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


SENSIBULL_ID = ("https://oxide.sensibull.com/v1/pluto/auth/web/session/a/"
                "platform/identify")
SENSIBULL_FII = "https://oxide.sensibull.com/v1/compute/cache/fii_dii_daily"


def _sensibull_fii_dii():
    """Rolling window of daily FII/DII cash flows from Sensibull.

    NSE's own endpoint returns ONE day and ignores every date parameter, and
    no dated cash file exists under nsearchives - so cash flow was the one
    series here that could not be backfilled. This fixes that.

    Two steps: hit the anonymous session endpoint first (no login, no
    credentials), then the cache. Calling the cache cold returns 403.

    Cross-checked against NSE on overlapping dates: 07-Sep FII +280.13 /
    DII +566.76 and 08-Sep FII -123 / DII +1350 agree exactly.

    Returns roughly a month per call. Run daily, that overlap means a missed
    run costs nothing and any revision gets corrected on the next pass. The
    payload advertises seven months in `key_list`, but the month-paging
    parameter is built at runtime and 403s on every shape tried, so only the
    current window is taken.
    """
    sess = requests.Session()
    hdr = dict(HEADERS, Referer="https://web.sensibull.com/",
               Origin="https://web.sensibull.com")
    sess.get(SENSIBULL_ID, headers=hdr, timeout=TIMEOUT)
    r = sess.get(SENSIBULL_FII, headers=hdr, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()

    rows, latest = [], None
    for day, rec in sorted((payload.get("data") or {}).items()):
        cash = (rec or {}).get("cash") or {}
        fii, dii = cash.get("fii") or {}, cash.get("dii") or {}
        f_net, d_net = fii.get("buy_sell_difference"), dii.get("buy_sell_difference")
        if f_net is None and d_net is None:
            continue
        row = {"date": day}
        if f_net is not None:
            row["fii_net"] = round(f_net, 2)
        if d_net is not None:
            row["dii_net"] = round(d_net, 2)

        # ---- F&O. Column names follow Sensibull's own table so the numbers
        # can be checked against it directly.
        opt = ((rec.get("option") or {}).get("fii") or {})
        fut = ((rec.get("future") or {}).get("fii") or {})
        qty, amt = fut.get("quantity-wise") or {}, fut.get("amount-wise") or {}
        fo = {
            "fii_call_oi_chg": (opt.get("call") or {}).get("net_oi_change"),
            "fii_put_oi_chg": (opt.get("put") or {}).get("net_oi_change"),
            "fii_fut_amt": amt.get("net_oi"),        # INR crore, net buy/sell
            "fii_fut_oi_chg": qty.get("net_oi"),     # contracts, day change
            "fii_fut_oi": qty.get("outstanding_oi"), # contracts, outstanding
        }
        for k, v in fo.items():
            if v is not None:
                row[k] = round(v, 2)
        rows.append(row)

        latest = {
            "date": day,
            "fii": {"buy": fii.get("buy"), "sell": fii.get("sell"),
                    "net": f_net, "view": fii.get("net_view")},
            "dii": {"buy": dii.get("buy"), "sell": dii.get("sell"),
                    "net": d_net, "view": dii.get("net_view")},
            "fno": dict(fo,
                        fut_view=qty.get("net_view"),
                        call_view=(opt.get("call") or {}).get("net_oi_change_view"),
                        put_view=(opt.get("put") or {}).get("net_oi_change_view")),
        }
    return rows, latest, payload.get("key_list") or []


@source("nse_fii_dii")
def fetch_fii_dii():
    """Daily FII/DII cash-market activity, with history where it exists."""
    try:
        rows, latest, months = _sensibull_fii_dii()
    except Exception as e:
        rows, latest, months, err = [], None, [], repr(e)
    else:
        err = None

    if latest and rows:
        return {"flows": {"fii": latest["fii"], "dii": latest["dii"]},
                "fno": latest.get("fno"),
                "as_of": latest["date"], "series": rows,
                "window_days": len(rows), "months_advertised": months,
                "unit": "INR crore", "via": "sensibull (rolling window)",
                "source": "oxide.sensibull.com/v1/compute/cache/fii_dii_daily"}

    # Fall back to NSE's single-day figure.
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
    return {"flows": out, "unit": "INR crore",
            "via": "nseindia (single day; sensibull failed: %s)" % err,
            "source": "nseindia.com/api"}


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


def last_trading_day(today=None):
    """Most recent NSE trading day, today included."""
    d = today or dt.datetime.now(IST).date()
    hol = nse_holidays()
    while d.weekday() >= 5 or d in hol:
        d -= dt.timedelta(days=1)
    return d


def have_fii_for(day):
    """Is the cash FII/DII figure for `day` already in history?"""
    for row in load_history()["rows"]:
        if row.get("date") == day.isoformat() and row.get("fii_net") is not None:
            return True
    return False


def last_settled_trading_day():
    """The most recent trading day whose FII/DII figure should EXIST by now.

    NSE posts the cash number after close. Before ~18:00 IST on a trading day
    that day's figure is simply not out yet, and treating it as missing would
    make every pre-close run think it had work to do.
    """
    now = dt.datetime.now(IST)
    day = last_trading_day(now.date())
    if day == now.date() and now.hour < 18:
        day = last_trading_day(day - dt.timedelta(days=1))
    return day


def ensure_fii():
    """Fetch only if the latest trading day's FII/DII cash is still missing.

    NSE publishes the cash figure after close, and the exact minute moves - it
    is usually there by 19:00 IST but not always. Rather than hammering every
    source on a fixed retry schedule, this checks first and exits doing nothing
    when the number has already landed, so an hourly cron is nearly free on the
    days it is not needed.
    """
    day = last_settled_trading_day()
    if have_fii_for(day):
        print("FII/DII for %s already present - nothing to do." % day)
        return 0
    print("FII/DII for %s missing - fetching." % day)
    return None          # caller runs the full fetch


def audit_history(max_lag=5):
    """Report how current each history series is.

    A series can go stale silently: the card reads data.json and looks fine
    while the chart reads history.json and quietly stops advancing. That has
    happened twice here - once when FRED had no DGS5 to feed us_5y, and once
    when the participant-OI section fed the card but was never written to
    history. This makes the failure visible instead of cosmetic.
    """
    rows = load_history()["rows"]
    today = dt.datetime.now(IST).date()
    report = []
    for key in HISTORY_FIELDS:
        pts = [r for r in rows if r.get(key) is not None]
        if not pts:
            report.append((key, 0, None, None))
            continue
        last = dt.date.fromisoformat(pts[-1]["date"])
        report.append((key, len(pts), pts[-1]["date"], (today - last).days))

    print("History audit @ " + today.isoformat())
    print("-" * 58)
    lagging, empty = [], []
    for key, n, last, lag in report:
        if last is None:
            print("%-22s %6d  never written" % (key, n))
            empty.append(key)
            continue
        flag = ""
        if lag > max_lag:
            flag = "  <-- LAGGING"
            lagging.append(key)
        print("%-22s %6d  %s  %2dd%s" % (key, n, last, lag, flag))
    print("-" * 58)
    if empty:
        print("NEVER WRITTEN: " + ", ".join(empty))
        print("  -> in HISTORY_FIELDS but nothing feeds it. Either a source")
        print("     must write it into a row, or the field should be removed.")
    if lagging:
        print("LAGGING (>%dd): %s" % (max_lag, ", ".join(lagging)))
        print("  -> check whether the source still feeds history, or whether")
        print("     the publisher itself is behind (BoE runs ~2 days late).")
    if not empty and not lagging:
        print("All %d series current." % len(report))
    return 1 if (empty or lagging) else 0


def prune_ipo_store():
    """Drop stored listings whose gap fails LISTING_MAX_GAP_DAYS.

    Needed once because those rows were written before the guard existed.
    Idempotent, so it is safe to re-run.
    """
    store = _load_ipo_store()
    past = get("https://www.nseindia.com/api/public-past-issues").json()
    close = {(r.get("symbol") or "").strip(): r.get("ipoEndDate") for r in past}
    dropped = store.setdefault("excluded", {})
    keep = {}
    for sym, rec in store["listings"].items():
        cd = _d(close.get(sym) or rec.get("ipo_closed"))
        ld = None
        try:
            ld = dt.date.fromisoformat(rec["listing_date"])
        except Exception:
            pass
        if cd and ld:
            gap = (ld - cd).days
            if gap < 0 or gap > LISTING_MAX_GAP_DAYS:
                rec["excluded_reason"] = (
                    "listing %dd after issue close - migration or relisting, "
                    "not a listing-day gain" % gap)
                dropped[sym] = rec
                continue
        keep[sym] = rec
    removed = len(store["listings"]) - len(keep)
    store["listings"] = keep
    IPO_STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    return removed, len(keep)


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
    """India G-Sec curve from FBIL - the RBI-recognised benchmark administrator.

    This tile used to be the weakest on the board: investing.com behind
    Cloudflare, falling back to a FRED series that was monthly, lagged and
    10Y-only. FBIL publishes the authoritative curve daily as an archive
    workbook, and `/wasdm/gsec/download?date=` serves any date. 200 tenors from
    0.25 to 50 years; a useful spread is surfaced here.

    Semi-annual YTM, which is how India's 10Y is quoted (6.98 vs 7.10
    annualised on 04-Sep-2026, against 6.96 on investing.com).
    """
    import io
    import openpyxl

    today = dt.datetime.now(IST).date()
    for back in range(0, 10):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            r = get("https://www.fbil.org.in/wasdm/gsec/download?date="
                    + day.isoformat(),
                    headers=dict(HEADERS, Referer="https://www.fbil.org.in/"))
        except Exception:
            continue
        if r.content[:2] != b"PK":
            continue
        ws = openpyxl.load_workbook(io.BytesIO(r.content),
                                    data_only=True)["Par Yield"]
        ten = {}
        for row in ws.iter_rows(values_only=True):
            try:
                ten[float(row[0])] = num(row[1])
            except (TypeError, ValueError, IndexError):
                continue
        if not ten.get(10.0):
            continue
        want = [("1Y", 1), ("2Y", 2), ("3Y", 3), ("5Y", 5),
                ("7Y", 7), ("10Y", 10), ("30Y", 30)]
        out = {lbl: ten[t] for lbl, t in want if ten.get(float(t)) is not None}
        spread = (None if not (ten.get(10.0) and ten.get(2.0))
                  else round(ten[10.0] - ten[2.0], 3))
        return {"yields": out, "unit": "percent",
                "via": "FBIL par yield (semi-annual)", "as_of": day.isoformat(),
                "spread_10y_2y": spread, "tenors_available": len(ten),
                "source": "fbil.org.in/wasdm/gsec"}
    raise RuntimeError("no FBIL G-Sec archive file in the last 10 days")


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


# Major-economy government bond yields, 5Y and 10Y.
#
# FRED's cross-country OECD family (IRLTLT01...) is TEN-YEAR ONLY, so a 5Y
# comparison needs a national source per country. Each of these publishes a
# free, key-less daily curve:
#
#   US       home.treasury.gov      daily par yield curve, every tenor
#   Japan    mof.go.jp              daily JGB curve, 1Y..40Y
#   UK       bankofengland.co.uk    IUDSNPY = 5Y, IUDMNPY = 10Y nominal par
#   Germany  api.statistiken.bundesbank.de  BBSIS daily svensson curve
#
# India and China have no free 5Y at all, and only a monthly 10Y via FRED.
# They appear on the 10Y chart and are absent from the 5Y one rather than
# being faked from a nearby tenor.
WORLD_FRED_10Y = {
    # India only. China moved to ChinaBond, which is daily and has a real 5Y -
    # FRED's OECD series for China was monthly and 10Y-only.
    "India": ["INDIRLTLT01STM", "IRLTLT01INM156N"],
}
YKEY = {"US": "us", "Japan": "jp", "UK": "uk", "Germany": "de",
        "India": "in", "China": "cn"}
WORLD_ORDER = ["US", "Germany", "UK", "Japan", "India", "China"]


def _hkey(tenor, country):
    """History column: y10_us, y5_de, ..."""
    return "y%s_%s" % (tenor, YKEY[country])


def _us_curve():
    """US Treasury daily par yields. One CSV per calendar year."""
    out = {}
    year = dt.datetime.now(IST).year
    for y in (year, year - 1, year - 2):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/"
               "interest-rates/daily-treasury-rates.csv/%d/all"
               "?type=daily_treasury_yield_curve&field_tdr_date_value=%d"
               "&page&_format=csv" % (y, y))
        try:
            lines = get(url).text.splitlines()
        except Exception:
            continue
        hdr = [h.strip().strip('"') for h in lines[0].split(",")]
        try:
            i5, i10 = hdr.index("5 Yr"), hdr.index("10 Yr")
        except ValueError:
            continue
        for ln in lines[1:]:
            c = [x.strip().strip('"') for x in ln.split(",")]
            if len(c) <= i10:
                continue
            try:
                d = dt.datetime.strptime(c[0], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            out[d] = {"5": num(c[i5]), "10": num(c[i10])}
    return out


def _jp_curve():
    """JGB curve. The 'all' file stops at last month-end, so the current
    month's file is layered on top of it."""
    out = {}
    base = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/")
    for part in ("historical/jgbcme_all.csv", "jgbcme.csv"):
        try:
            lines = get(base + part).text.splitlines()
        except Exception:
            continue
        hdr = None
        for ln in lines:
            c = [x.strip() for x in ln.split(",")]
            if c[0] == "Date":
                hdr = c
                continue
            if not hdr or not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", c[0] or ""):
                continue
            row = dict(zip(hdr, c))
            try:
                d = dt.datetime.strptime(c[0], "%Y/%m/%d").date().isoformat()
            except ValueError:
                continue
            out[d] = {"5": num(row.get("5Y")), "10": num(row.get("10Y"))}
    return out


def _uk_curve():
    """Bank of England IADB. IUDSNPY = 5Y, IUDMNPY = 10Y nominal par yield."""
    url = ("https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
           "?csv.x=yes&Datefrom=01/Jan/2015&Dateto=now"
           "&SeriesCodes=IUDSNPY,IUDMNPY&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    lines = get(url).text.splitlines()
    hdr = [h.strip() for h in lines[0].split(",")]
    i5, i10 = hdr.index("IUDSNPY"), hdr.index("IUDMNPY")
    out = {}
    for ln in lines[1:]:
        c = [x.strip() for x in ln.split(",")]
        if len(c) <= i10:
            continue
        try:
            d = dt.datetime.strptime(c[0], "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        out[d] = {"5": num(c[i5]), "10": num(c[i10])}
    return out


def _de_curve():
    """Bundesbank BBSIS. Semicolon separated, comma decimal separator."""
    ids = {"5": "D.I.ZST.ZI.EUR.S1311.B.A604.R05XX.R.A.A._Z._Z.A",
           "10": "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"}
    out = {}
    for tenor, sid in ids.items():
        try:
            lines = get("https://api.statistiken.bundesbank.de/rest/data/"
                        "BBSIS/" + sid + "?format=csv").text.splitlines()
        except Exception:
            continue
        # Bundesbank content-negotiates on Accept-Language: German locale gives
        # "2026-09-09;3,45;" while en-GB (what HEADERS sends) gives
        # "2026-09-09,3.45,". Accept either rather than depending on a header.
        for ln in lines:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})[;,]\s*([-\d.,]+)", ln.strip())
            if not m:
                continue
            raw = m.group(2).rstrip(",;")
            if "," in raw and "." not in raw:      # German decimal comma
                raw = raw.replace(",", ".")
            v = num(raw)
            if v is not None:
                out.setdefault(m.group(1), {})[tenor] = v
    return out


INDIA_STORE = ROOT / "docs" / "india_curve.json"
CHINA_STORE = ROOT / "docs" / "china_yields.json"


def _in_day(day):
    """India par yield curve for one date, from FBIL's own archive file.

    FBIL is the RBI-recognised benchmark administrator, so this is the
    authoritative Indian curve - far better than the monthly OECD series FRED
    carries, and it has a real 5Y. The endpoint is undocumented; it was found
    by watching what fbil.org.in itself calls. `/wasdm/gsec/fetch` lists only
    recent archive dates, but `/wasdm/gsec/download?date=` serves any date.

    The workbook's "Par Yield" sheet holds 200 tenors from 0.25 to 50 years in
    two conventions. Semi-annual is the one India's 10Y is quoted in (6.98 vs
    7.10 annualised on 04-Sep-2026, against 6.96 on investing.com), so that is
    what is stored - the other would read as a different market.
    """
    import io
    import openpyxl

    url = ("https://www.fbil.org.in/wasdm/gsec/download?date=" + day.isoformat())
    r = get(url, headers=dict(HEADERS, Referer="https://www.fbil.org.in/"))
    if not r.content[:2] == b"PK":
        return None
    ws = openpyxl.load_workbook(io.BytesIO(r.content), data_only=True)["Par Yield"]
    ten = {}
    for row in ws.iter_rows(values_only=True):
        try:
            ten[float(row[0])] = num(row[1])        # semi-annual YTM
        except (TypeError, ValueError, IndexError):
            continue
    out = {"5": ten.get(5.0), "10": ten.get(10.0)}
    return out if out["10"] is not None else None


def _load_curve(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_curve(path, store):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=0, sort_keys=True), encoding="utf-8")


def _in_curve():
    """India curve: cached history plus the newest published date."""
    store = _load_curve(INDIA_STORE)
    today = dt.datetime.now(IST).date()
    for back in range(0, 8):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5 or day.isoformat() in store:
            continue
        try:
            v = _in_day(day)
        except Exception:
            continue
        if v:
            store[day.isoformat()] = v
            break
    if store:
        _save_curve(INDIA_STORE, store)
    return store


def backfill_india(days=400, pause=0.4):
    """One-off: walk FBIL's archive back N calendar days."""
    store = _load_curve(INDIA_STORE)
    today, added, miss = dt.datetime.now(IST).date(), 0, 0
    for back in range(0, days + 1):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5 or day.isoformat() in store:
            continue
        try:
            v = _in_day(day)
        except Exception:
            miss += 1
            continue
        if v:
            store[day.isoformat()] = v
            added += 1
        else:
            miss += 1          # market holiday: no file published
        time.sleep(pause)
    _save_curve(INDIA_STORE, store)
    return added, miss, len(store)


def _cn_day(day):
    """ChinaBond official government yield curve for one date.

    Server-rendered HTML (no JS, no key) and it accepts a workTime parameter,
    so unlike FRED's monthly OECD series this gives a real daily 5Y and 10Y
    and can be backfilled. Columns are [O/N, 3M, 6M, 1Y, 3Y, 5Y, 7Y, 10Y, 30Y].
    """
    url = ("https://yield.chinabond.com.cn/cbweb-cbrc-web/cbrc/queryGjqxInfo"
           "?workTime=" + day.isoformat() + "&locale=en_US")
    html = get(url).text
    ths = [re.sub(r"<!--.*?-->", "", t, flags=re.S).strip()
           for t in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)]
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = [re.sub(r"<[^>]+>", "", x).strip()
               for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if not tds or "Government Bond Yield Curve" not in tds[0]:
            continue
        # tds[0] is the row label, so values line up with ths[1:]
        vals = dict(zip(ths[1:], tds[1:]))
        out = {t: num(vals.get(lbl)) for t, lbl in (("5", "5Y"), ("10", "10Y"))}
        if out.get("10") is not None:
            return out
    return None


def _cn_curve():
    """China curve: cached history on disk plus today's print."""
    store = {}
    if CHINA_STORE.exists():
        try:
            store = json.loads(CHINA_STORE.read_text(encoding="utf-8"))
        except Exception:
            store = {}
    today = dt.datetime.now(IST).date()
    for back in range(0, 6):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5 or day.isoformat() in store:
            continue
        try:
            v = _cn_day(day)
        except Exception:
            continue
        if v:
            store[day.isoformat()] = v
            break
    if store:
        CHINA_STORE.parent.mkdir(parents=True, exist_ok=True)
        CHINA_STORE.write_text(json.dumps(store, indent=0, sort_keys=True),
                               encoding="utf-8")
    return store


def backfill_china(days=400, pause=0.4):
    """One-off: walk ChinaBond back N calendar days to seed the curve."""
    store = {}
    if CHINA_STORE.exists():
        store = json.loads(CHINA_STORE.read_text(encoding="utf-8"))
    today, added, miss = dt.datetime.now(IST).date(), 0, 0
    for back in range(0, days + 1):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5 or day.isoformat() in store:
            continue
        try:
            v = _cn_day(day)
        except Exception:
            miss += 1
            continue
        if v:
            store[day.isoformat()] = v
            added += 1
        else:
            miss += 1
        time.sleep(pause)
    CHINA_STORE.parent.mkdir(parents=True, exist_ok=True)
    CHINA_STORE.write_text(json.dumps(store, indent=0, sort_keys=True),
                           encoding="utf-8")
    return added, miss, len(store)


@source("world_yields")
def fetch_world_yields():
    """5Y and 10Y government bond yields for the major economies.

    Every national source here is daily and needs no key. India and China are
    monthly-only via FRED and have no free 5Y, which the payload states rather
    than papering over.
    """
    curves, errors = {}, {}
    for country, fn in (("US", _us_curve), ("Japan", _jp_curve),
                        ("UK", _uk_curve), ("Germany", _de_curve),
                        ("China", _cn_curve), ("India", _in_curve)):
        try:
            c = fn()
            if c:
                curves[country] = c
            else:
                errors[country] = "empty"
        except Exception as e:
            errors[country] = "%s: %s" % (type(e).__name__, e)

    # India / China: FRED monthly, 10Y only.
    key = os.environ.get("FRED_API_KEY", "").strip()
    series_ids = {}
    if "India" in curves:
        pass                       # FBIL answered; no need for the monthly proxy
    elif key:
        for country, cands in WORLD_FRED_10Y.items():
            for sid in cands:
                try:
                    r = get("https://api.stlouisfed.org/fred/series/observations",
                            params={"series_id": sid, "api_key": key,
                                    "file_type": "json", "sort_order": "desc",
                                    "limit": 200})
                    obs = [o for o in r.json().get("observations", [])
                           if o.get("value") not in (".", "", None)]
                except Exception:
                    continue
                if not obs:
                    continue
                curves[country] = {o["date"]: {"10": num(o["value"])} for o in obs}
                series_ids[country] = sid
                break
            else:
                errors[country] = "no FRED series resolved"
    else:
        errors["India"] = "FRED_API_KEY not set"

    if not curves:
        raise RuntimeError("no yield curve resolved: " + json.dumps(errors))

    latest, series = {"5": {}, "10": {}}, {}
    for country, curve in curves.items():
        for tenor in ("5", "10"):
            pts = sorted((d, v[tenor]) for d, v in curve.items()
                         if v.get(tenor) is not None)
            if not pts:
                continue
            series[_hkey(tenor, country)] = pts[-1500:]
            latest[tenor][country] = {"value": pts[-1][1], "date": pts[-1][0]}

    return {"latest": latest, "series": series, "fred_series_ids": series_ids,
            "order": WORLD_ORDER, "errors": errors, "unit": "percent",
            "daily": [c for c in WORLD_ORDER if c in curves],
            "monthly": [],
            "note": ("All six from daily national sources: US Treasury, "
                     "Bundesbank, Bank of England, Japan MOF, ChinaBond and "
                     "FBIL. India is FBIL's semi-annual par yield, the "
                     "convention its 10Y is quoted in."),
            "source": "treasury.gov, mof.go.jp, bankofengland.co.uk, "
                      "bundesbank.de, fred.stlouisfed.org"}



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




_HOLIDAYS = None


def nse_holidays():
    """NSE trading holidays, for business-day arithmetic. Empty set on failure -
    that only shifts an estimate by a day, never breaks the run."""
    global _HOLIDAYS
    if _HOLIDAYS is not None:
        return _HOLIDAYS
    out = set()
    try:
        j = get("https://www.nseindia.com/api/holiday-master?type=trading").json()
        for seg in j.values():
            for r in seg:
                d = _d(r.get("tradingDate"))
                if d:
                    out.add(d)
    except Exception:
        pass
    _HOLIDAYS = out
    return out


def add_business_days(start, n):
    """Advance `n` NSE trading days from `start`."""
    hol, d, left = nse_holidays(), start, n
    while left > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5 and d not in hol:
            left -= 1
    return d


def listing_label(expected, today):
    """How to describe an expected listing date to a human.

    "Lists tomorrow" must mean tomorrow on the CALENDAR. The expected date is
    the next trading day, which on a Friday is Monday - calling that "tomorrow"
    is simply wrong. So Friday says "Lists Monday", Sunday says "Lists
    tomorrow", and Monday says "Lists today", which is how anyone would say it.
    """
    if not expected:
        return "Awaiting listing"
    delta = (expected - today).days
    if delta < 0:
        return "Awaiting listing"          # overdue; NSE has not confirmed
    if delta == 0:
        return "Lists today"
    if delta == 1:
        return "Lists tomorrow"
    if delta <= 6:
        return "Lists " + expected.strftime("%A")
    return "Awaiting listing"


def expected_listing(closed):
    """SEBI's T+3 rule: listing within 3 working days of issue close.

    Measured against 151 resolved listings in this repo: 82.8% land exactly on
    T+3, and ~93% fall between T+2 and T+6. Neither NSE nor Chittorgarh
    publishes a forward listing date - Chittorgarh's Listing Date column is
    blank for every upcoming issue, same as NSE's - so computing it is the only
    way to know before the fact. It is labelled an estimate everywhere it
    surfaces, and is replaced by the real date as soon as the stock appears in
    a bhavcopy.
    """
    return add_business_days(closed, 3) if closed else None


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
            if not is_fresh_listing(r, ld):
                continue                  # migration, not a new listing
            listing_today.append(row)
        elif ld is None and sym not in trading and closed:
            row["days_since_close"] = (today - closed).days
            exp = expected_listing(closed)
            row["expected_listing"] = exp.isoformat() if exp else None
            row["expected_listing_dmy"] = (
                exp.strftime("%d-%b-%Y") if exp else None)
            row["expected_basis"] = "T+3 (SEBI rule; 83% exact over 151 past listings)"
            row["lists_today_expected"] = (exp == today)
            row["listing_label"] = listing_label(exp, today)
            row["days_to_listing"] = (exp - today).days if exp else None
            awaiting.append(row)

    awaiting.sort(key=lambda r: (r.get("expected_listing") or "9999",
                                 r["days_since_close"]))

    # Both NSE endpoints describe the same issue in the window between close
    # and listing: public-past-issues has it with a blank listingDate, and
    # all-upcoming-issues still lists it as "Closed". Shown raw, GLASSWALL,
    # KANOHAR and PRASOLCHEM each appeared twice - once with an expected date
    # and again below as "Closed". The awaiting row is the better one (it
    # carries the estimate), so it wins and the duplicate is dropped.
    already = {r["symbol"] for r in listing_today} | {r["symbol"] for r in awaiting}

    open_now, dropped = [], []
    for r in current:
        sym = (r.get("symbol") or "").strip()
        if sym in already:
            dropped.append(sym)
            continue
        o, c = _d(r.get("issueStartDate")), _d(r.get("issueEndDate"))
        row = {"company": r.get("companyName"), "symbol": sym,
               "tv": tv(sym), "price": r.get("issuePrice"),
               "opens": r.get("issueStartDate"),
               "closes": r.get("issueEndDate"),
               "series": r.get("series"), "status": r.get("status"),
               "live": bool(o and c and o <= today <= c)}

        # The T+3 clock starts at the issue close, so the estimate exists as
        # soon as that date is known - which is before the book even opens.
        # Worth showing while an issue is still live: anyone deciding whether
        # to apply wants to know roughly when it would list.
        if c:
            exp_any = expected_listing(c)
            if exp_any:
                row["expected_listing"] = exp_any.isoformat()
                row["expected_listing_dmy"] = exp_any.strftime("%d-%b-%Y")
                row["expected_basis"] = "T+3 trading days from issue close (SEBI)"
        # Only once the book has SHUT does the estimate become the row's
        # headline state; while open, the subscription window still leads.
        if c and c < today:
            row["listing_label"] = listing_label(exp_any, today)
            row["book_closed"] = True
        open_now.append(row)

    # Order: live issues by how soon they shut, then the rest by close date.
    open_now.sort(key=lambda r: (not r["live"], r["closes"] or ""))
    if dropped:
        ipo_dupes = sorted(set(dropped))
    else:
        ipo_dupes = []

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
            # Migrations are hidden everywhere, not just from the buckets
            # above: a 2022 SME issue carrying a 2026 listing date is not a
            # recent IPO and does not belong in a list of them.
            "recent_past": [clean_past(r) for r in past
                            if is_fresh_listing(r, _d(r.get("listingDate")))][:40],
            "past_total": len(past),
            "tv_watchlist": watchlist,
            "deduped_from_open": ipo_dupes,
            "tv_note": ("NSE assigns the symbol at issue open, so these "
                        "resolve on TradingView before the stock lists."),
            "as_of": today.isoformat(), "source": "nseindia.com/api"}


IPO_STORE = ROOT / "docs" / "ipo_listings.json"
IPO_PER_RUN = 12          # cap bhavcopy requests in a normal daily run
# Beyond this many days between issue close and listing it is not a listing:
# it is an SME-to-mainboard migration or a relisting recorded against the old
# issue row. T+3 is the rule and ~93% land by T+6, so 30 days is generous.
LISTING_MAX_GAP_DAYS = 30


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


def is_fresh_listing(rec, listing_date):
    """Is this a genuine IPO listing, or a migration wearing one's clothes?

    NSE records SME-to-mainboard migrations against the ORIGINAL issue row, so
    a 2022 IPO can carry a 2026 listing date. Dollex Agrotech is the case in
    point: IPO 15-20 Dec 2022, listingDate 11-Sep-2026, and it was already
    trading in the 10-Sep bhavcopy at 39.05. Treating that as a fresh listing
    produced a "-7.89% listing gain" against a four-year-old issue price.

    The gains resolver already rejected these. The intraday capture did not,
    which is why the test lives here now and both call it.
    """
    closed = _d(rec.get("ipoEndDate"))
    if not (closed and listing_date):
        return True                       # nothing to contradict it
    gap = (listing_date - closed).days
    return 0 <= gap <= LISTING_MAX_GAP_DAYS


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
        # A provisional row (captured intraday) must stay eligible so the
        # bhavcopy can supersede it; a confirmed one is skipped.
        existing = done.get(sym)
        if not sym or sym in bad or (existing and not existing.get("provisional")):
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

        # A listing-day gain only means anything when the listing follows the
        # issue. NSE's listingDate also records SME-to-mainboard migrations and
        # relistings against the ORIGINAL issue row: Swaraj Suiting shows a
        # 2022 IPO with a 13-AUG-2026 listing date, and ADANIENPP1 produced a
        # "+288.89% listing gain" across a 522-day gap. Those are multi-year
        # returns, not listing pops, and they were skewing the medians.
        closed_d = _d(rec.get("ipoEndDate"))
        if closed_d:
            gap = (day - closed_d).days
            if gap < 0 or gap > LISTING_MAX_GAP_DAYS:
                bad.add(sym)
                continue
        todo.append((day, sym, rec.get("company"), price))

    todo.sort(key=lambda t: t[0], reverse=True)     # newest first
    by_day = {}
    for day, sym, company, price in todo:
        by_day.setdefault(day, []).append((sym, company, price))

    rec_close = {(r.get("symbol") or "").strip(): (r.get("ipoEndDate") or "").strip()
                 for r in past if r.get("symbol")}
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
                "ipo_closed": (rec_close.get(sym) or None),
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


GMP_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"


def _strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def _rupee(s):
    """'₹1,973' -> 1973.0, '₹-' -> None."""
    if not s:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s.replace("₹", ""))
    return num(m.group(0)) if m else None


def _norm_name(s):
    """Company names for matching across sources: NSE says 'Hero Motors
    Limited', ipowatch says 'Hero Motors'."""
    s = (s or "").lower()
    s = re.sub(r"\b(limited|ltd|private|pvt|india|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


CIRCULAR_SUBJECT = re.compile(
    r"Listing of Equity Shares of (.+?)\s*\((SME\s+)?IPO\)", re.I)
CIRCULAR_EFFECT = re.compile(
    r"with effect from\s+([A-Z][a-z]+ \d{1,2},\s*\d{4})", re.I)
# The annexure is a label/value list, not a table: 'Symbol QUALIANCE' on its
# own line, with 'ISIN INE1XJ401012' separately. Matching a token before the
# ISIN grabbed the word ISIN itself, so key off the Symbol label instead.
CIRCULAR_SYMBOL = re.compile(
    r"Symbol[:\s]+([A-Z][A-Z0-9&]{2,14})\b"
    r"|\(Symbol:\s*([A-Z][A-Z0-9&]{2,14})\)")
CIRCULARS_PER_RUN = 12


def _circular_text(url):
    """Circular body text. Some are PDFs, some are zips containing one."""
    import io
    import zipfile
    from pypdf import PdfReader

    raw = get(url).content
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in z.namelist()
                 if n.lower().endswith(".pdf") and "SHP" not in n.upper()]
        if not names:
            return ""
        raw = z.read(names[0])
    reader = PdfReader(io.BytesIO(raw))
    return re.sub(r"[ \t]+", " ",
                  "\n".join((p.extract_text() or "") for p in reader.pages))


@source("ipo_circulars")
def fetch_ipo_circulars():
    """Confirmed listing dates from NSE's own listing circulars.

    This is the earliest AUTHORITATIVE answer to "when does it list". The T+3
    rule is a good estimate (83% exact) but it is still an estimate; the
    circular is NSE telling members the date.

    Two forms exist and only one carries a date:

      "...admitted to dealings ... with effect from September 11, 2026"
          -> confirmed. Qualiance's was published 10-Sep for an 11-Sep listing.

      "The date of listing of the security shall be informed through a
       separate circular."
          -> the listing is confirmed, the date is not. Pranav's read this way
             on 11-Sep. Worth surfacing anyway: it means NSE has admitted the
             security and a date circular follows within a day or two.
    """
    data = get("https://www.nseindia.com/api/circulars").json()
    rows = data.get("data") or []

    seen, out = set(), []
    for r in rows:
        if (r.get("circCategory") or "") != "Listing":
            continue
        subj = r.get("sub") or ""
        m = CIRCULAR_SUBJECT.search(subj)
        if not m:
            continue
        link = r.get("circFilelink")
        if not link or link in seen:
            continue
        seen.add(link)
        if len(out) >= CIRCULARS_PER_RUN:
            break

        company = m.group(1).strip()
        rec = {"company": company, "match": _norm_name(company),
               "board": "SME" if m.group(2) else "mainboard",
               "circular_date": r.get("cirDisplayDate"),
               "circular": link, "subject": subj,
               "confirmed_listing": None, "symbol": None}
        try:
            text = _circular_text(link)
        except Exception as e:
            rec["error"] = "%s: %s" % (type(e).__name__, e)
            out.append(rec)
            continue

        eff = CIRCULAR_EFFECT.search(text)
        if eff:
            try:
                d = dt.datetime.strptime(re.sub(r"\s+", " ", eff.group(1)),
                                         "%B %d, %Y").date()
                rec["confirmed_listing"] = d.isoformat()
                rec["confirmed_listing_dmy"] = d.strftime("%d-%b-%Y")
            except ValueError:
                pass
        # Two layouts. The confirmation circular labels it ("Symbol QUALIANCE");
        # the forthcoming one prints a header row instead ("Name of the company
        # Symbol ISIN") above the values, so a label match captures the word
        # ISIN. Gather every candidate and drop the header words.
        STOP = {"ISIN", "SYMBOL", "SERIES", "NAME", "THE", "AND", "NSE", "IPO"}
        cands = []
        for m in CIRCULAR_SYMBOL.finditer(text):
            cands.append(m.group(1) or m.group(2))
        for m in re.finditer(r"([A-Z][A-Z0-9&]{2,14})\s+INE[0-9A-Z]{9}", text):
            cands.append(m.group(1))
        for c in cands:
            if c and c.upper() not in STOP:
                rec["symbol"] = c
                break
        rec["date_pending"] = ("separate circular" in text.lower()
                               and not rec["confirmed_listing"])
        out.append(rec)

    confirmed = [r for r in out if r["confirmed_listing"]]
    return {"circulars": out, "confirmed": len(confirmed),
            "checked": len(out), "as_of": now_iso(),
            "note": ("NSE's own listing circular - the earliest authoritative "
                     "listing date. Supersedes the T+3 estimate."),
            "source": "nseindia.com/api/circulars"}


@source("ipo_listing_live")
def fetch_listing_live():
    """Intraday prices for IPOs that listed this morning.

    Listings open at 10:00 IST and the bhavcopy is not published until after
    close, so for most of the day the board would otherwise show nothing at
    all for a stock that has been trading for hours.

    NSE's quote API is behind the bot wall (403 even with a homepage cookie),
    so this uses Yahoo. Coverage is partial and honestly so: Yahoo carries
    DOLLEX within the hour but had no data for QUALIANCE on its listing day -
    fresh SME symbols can take a day to appear.

    Anything captured here is written PROVISIONAL. The bhavcopy is the
    authority, and verify_listings() overwrites these values once it exists.
    """
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    today = dt.datetime.now(IST).date()
    past = get("https://www.nseindia.com/api/public-past-issues").json()
    EQUITY = {"EQ", "SME", "BE"}

    todays, migrations = [], []
    for r in past:
        sym = (r.get("symbol") or "").strip()
        if not sym or (r.get("securityType") or "").strip().upper() not in EQUITY:
            continue
        if _d(r.get("listingDate")) != today:
            continue
        if not is_fresh_listing(r, today):
            migrations.append(sym)        # migration, not an IPO
            continue
        todays.append((sym, r.get("company"), _price_from(r)))

    store = _load_ipo_store()
    found, missing = [], []
    for sym, company, issue in todays:
        try:
            hist = yf.Ticker(sym + ".NS").history(period="1d", interval="5m")
        except Exception:
            hist = None
        if hist is None or not len(hist):
            missing.append(sym)
            continue
        op = float(hist["Open"].iloc[0])
        last = float(hist["Close"].iloc[-1])
        rec = {
            "symbol": sym, "company": company, "issue_price": issue,
            "listing_date": today.isoformat(), "open": round(op, 2),
            "last": round(last, 2), "close": round(last, 2),
            "provisional": True, "price_source": "yahoo intraday",
            "gain_open_pct": round((op - issue) / issue * 100, 2) if issue else None,
            "gain_close_pct": round((last - issue) / issue * 100, 2) if issue else None,
            "from_open_pct": round((last - op) / op * 100, 2) if op else None,
        }
        found.append(rec)
        # Provisional rows go into the store so the gains table is populated the
        # same day; verify_listings() replaces them from the bhavcopy tonight.
        store["listings"][sym] = rec

    if found:
        IPO_STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False),
                             encoding="utf-8")

    return {"date": today.isoformat(), "listed_today": [s for s, _, _ in todays],
            "live": found, "no_quote_yet": missing,
            "excluded_migrations": migrations,
            "note": ("Provisional. Yahoo intraday, because NSE's quote API is "
                     "bot-walled; the bhavcopy overwrites these after close."),
            "source": "yfinance"}


def verify_listings(limit=40):
    """Re-check recent stored listings against the bhavcopy and correct them.

    Two things get fixed here. Provisional rows captured intraday from Yahoo
    are replaced with the official open and close. And any row whose stored
    price disagrees with the bhavcopy - whatever wrote it - is corrected, with
    the old value kept so the change is auditable rather than silent.
    """
    store = _load_ipo_store()
    rows = sorted(store["listings"].values(),
                  key=lambda r: r.get("listing_date") or "", reverse=True)[:limit]
    corrected, confirmed, pending = [], 0, []

    for rec in rows:
        sym, day_s = rec.get("symbol"), rec.get("listing_date")
        if not sym or not day_s:
            continue
        try:
            day = dt.date.fromisoformat(day_s)
        except ValueError:
            continue
        try:
            bhav = _bhavcopy(day)
        except Exception:
            pending.append(sym)          # file not out yet
            continue
        row = bhav.get(sym)
        if not row:
            pending.append(sym)
            continue
        op, cl = num(row.get("OPEN_PRICE")), num(row.get("CLOSE_PRICE"))
        if op is None or cl is None:
            pending.append(sym)
            continue

        was_prov = rec.get("provisional")
        changed = (rec.get("open") != op) or (rec.get("close") != cl)
        if changed:
            corrected.append({"symbol": sym, "was_provisional": bool(was_prov),
                              "old_open": rec.get("open"), "new_open": op,
                              "old_close": rec.get("close"), "new_close": cl,
                              "old_source": rec.get("price_source", "bhavcopy")})
        issue = rec.get("issue_price")
        rec.update({"open": op, "close": cl, "price_source": "bhavcopy"})
        rec.pop("provisional", None)
        rec.pop("last", None)
        if issue:
            rec["gain_open_pct"] = round((op - issue) / issue * 100, 2)
            rec["gain_close_pct"] = round((cl - issue) / issue * 100, 2)
        store["listings"][sym] = rec
        confirmed += 1

    IPO_STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    return corrected, confirmed, pending


@source("ipo_gmp")
def fetch_ipo_gmp():
    """Grey market premium for open and upcoming IPOs.

    GMP is an UNOFFICIAL, unregulated over-the-counter indication - it is not
    exchange data, no regulator stands behind it, and it can move or vanish
    without trace. It is widely watched anyway, so it is carried here with a
    measured track record rather than presented as a forecast.

    ipowatch.in renders its tables server-side, which is why it is the source:
    investorgain, ipocentral and Chittorgarh all build theirs in JavaScript and
    would need a browser.

    Three tables: mainboard live, SME live, and a history of GMP against the
    actual listing price. The third is what makes the first two honest - it is
    scored below so the card can say how often this signal has been right.
    """
    html = get(GMP_URL).text
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S)
    if len(tables) < 2:
        raise RuntimeError("ipowatch layout changed - %d tables" % len(tables))

    def rows_of(tbl):
        out = []
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            cells = [_strip_tags(c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            if cells:
                out.append(cells)
        return out

    def live(tbl, board):
        out = []
        for c in rows_of(tbl)[1:]:
            if len(c) < 7 or c[0].lower().startswith("ipo name"):
                continue
            gmp = _rupee(c[1])
            band = _rupee(c[3])
            est = _rupee(c[4])
            pct = None
            m = re.search(r"\(([-\d.]+)%\)", c[4])
            if m:
                pct = num(m.group(1))
            out.append({"company": c[0], "board": board, "gmp": gmp,
                        "price_band_upper": band, "est_listing": est,
                        "est_gain_pct": pct,
                        "direction": ("premium" if (gmp or 0) > 0 else
                                      "discount" if (gmp or 0) < 0 else "flat"),
                        "window": c[5], "status": c[6],
                        "match": _norm_name(c[0])})
        return out

    current = live(tables[0], "mainboard") + live(tables[1], "sme")

    # ---- track record: GMP vs what the stock actually listed at
    hist, scored = [], []
    if len(tables) > 2:
        for c in rows_of(tables[2])[1:]:
            if len(c) < 4:
                continue
            issue, gmp, listed = _rupee(c[1]), _rupee(c[2]), _rupee(c[3])
            if not issue or gmp is None or not listed:
                continue
            implied = (gmp / issue) * 100
            actual = ((listed - issue) / issue) * 100
            hist.append({"company": c[0], "issue": issue, "gmp": gmp,
                         "listed": listed, "implied_pct": round(implied, 2),
                         "actual_pct": round(actual, 2),
                         "error_pct": round(actual - implied, 2)})
            scored.append((implied, actual))

    track = None
    if scored:
        errs = sorted(abs(a - i) for i, a in scored)
        same_way = sum(1 for i, a in scored
                       if (i > 0) == (a > 0) or (abs(i) < 1 and abs(a) < 1))
        over = sum(1 for i, a in scored if i > a)
        track = {
            "sample": len(scored),
            "direction_right_pct": round(100 * same_way / len(scored), 1),
            "median_abs_error_pct": round(errs[len(errs) // 2], 2),
            "overstated_pct": round(100 * over / len(scored), 1),
        }

    return {"current": current, "history": hist[:40], "track_record": track,
            "as_of": now_iso(),
            "caveat": ("Grey market premium is unofficial and unregulated. It "
                       "is an indication of sentiment, not a price you can "
                       "trade or a forecast anyone stands behind."),
            "source": "ipowatch.in"}


@source("ipo_gains")
def fetch_ipo_gains():
    """Listing-day performance, newest first, plus hit-rate stats."""
    # Correct before adding: replaces intraday-provisional prices with the
    # official bhavcopy figures and repairs anything that disagrees with it.
    corrected, confirmed, pending = verify_listings()
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
            "corrections": corrected, "verified_against_bhavcopy": confirmed,
            "awaiting_bhavcopy": pending,
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
                  "y10_us", "y10_de", "y10_uk", "y10_jp", "y10_in", "y10_cn",
                  "y5_us", "y5_de", "y5_uk", "y5_jp",
                  "fii_idx_fut_net", "dii_idx_fut_net",
                  "fii_call_oi_chg", "fii_put_oi_chg",
                  "fii_fut_amt", "fii_fut_oi_chg", "fii_fut_oi",
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
    ap.add_argument("--backfill-india", type=int, metavar="N", default=0,
                    help="seed the FBIL India par-yield curve back N days")
    ap.add_argument("--backfill-china", type=int, metavar="N", default=0,
                    help="seed the ChinaBond curve back N calendar days")
    ap.add_argument("--ensure-fii", action="store_true",
                    help="fetch only if the last trading day's FII/DII cash "
                         "is still missing; otherwise exit doing nothing")
    ap.add_argument("--audit", action="store_true",
                    help="report how current each history series is")
    ap.add_argument("--prune-ipo", action="store_true",
                    help="re-validate the IPO store and drop migrations/relistings")
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
    if args.backfill_india:
        a, m, tot = backfill_india(args.backfill_india)
        print("FBIL India: +%d days (%d holidays/misses), %d stored" % (a, m, tot))
        return 0
    if args.backfill_china:
        a, m, tot = backfill_china(args.backfill_china)
        print("ChinaBond: +%d days (%d misses), %d stored" % (a, m, tot))
        return 0
    if args.audit:
        return audit_history()
    if args.ensure_fii:
        done = ensure_fii()
        if done is not None:
            return done
        # fall through to the normal fetch
    if args.prune_ipo:
        removed, kept = prune_ipo_store()
        print("Pruned %d migration/relisting rows; %d genuine listings kept."
              % (removed, kept))
        return 0
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

    # ---- join GMP onto the IPO pipeline.
    # Cross-source, so it happens here rather than inside either fetcher: the
    # two name the same company differently ("Hero Motors Limited" vs "Hero
    # Motors"), and _norm_name is the single place that reconciles them.
    # ---- confirmed listing dates from NSE circulars override the T+3 estimate.
    circ = data["sections"].get("ipo_circulars") or {}
    by_circ = {}
    for c in circ.get("circulars") or []:
        if c.get("confirmed_listing"):
            by_circ[c["match"]] = c
            if c.get("symbol"):
                by_circ[c["symbol"]] = c
    ipo_s = data["sections"].get("nse_ipo") or {}
    confirmed_n = 0
    for bucket in ("listing_today", "awaiting", "open_now", "current"):
        for row in ipo_s.get(bucket) or []:
            c = by_circ.get(row.get("symbol")) or by_circ.get(_norm_name(row.get("company")))
            if not c:
                continue
            row["confirmed_listing"] = c["confirmed_listing"]
            row["confirmed_listing_dmy"] = c.get("confirmed_listing_dmy")
            row["confirmed_by"] = "NSE circular " + str(c.get("circular_date"))
            row["circular_url"] = c.get("circular")
            try:
                d = dt.date.fromisoformat(c["confirmed_listing"])
                row["listing_label"] = listing_label(d, dt.datetime.now(IST).date())
            except Exception:
                pass
            confirmed_n += 1
    if ipo_s:
        ipo_s["circular_confirmed"] = confirmed_n

    gmp_sec = data["sections"].get("ipo_gmp") or {}
    by_name = {g["match"]: g for g in (gmp_sec.get("current") or [])
               if g.get("match")}

    def find_gmp(company):
        """Exact normalised match, else containment. ipowatch truncates names
        ("Asset Reconstruction" for "Asset Reconstruction Company (India)
        Limited"), so one normalised name is often a prefix of the other. The
        8-char floor stops short names colliding."""
        n = _norm_name(company)
        if not n:
            return None
        if n in by_name:
            return by_name[n]
        cands = [g for k, g in by_name.items()
                 if len(k) >= 8 and len(n) >= 8 and (k in n or n in k)]
        return cands[0] if len(cands) == 1 else None
    ipo_sec = data["sections"].get("nse_ipo") or {}
    matched = 0
    for bucket in ("listing_today", "awaiting", "open_now", "current"):
        for row in ipo_sec.get(bucket) or []:
            g = find_gmp(row.get("company"))
            if not g:
                continue
            row["gmp"] = g.get("gmp")
            row["gmp_est_gain_pct"] = g.get("est_gain_pct")
            row["gmp_direction"] = g.get("direction")
            matched += 1
    if ipo_sec:
        ipo_sec["gmp_matched"] = matched

    rows = [row_from_sections(data["sections"])]
    # FII/DII now arrives as a rolling window, not a single day - fold every
    # date in so history deepens and past revisions get corrected.
    fd = data["sections"].get("nse_fii_dii") or {}
    rows += list(fd.get("series") or [])

    # Participant-wise OI carries its OWN date - the file for today may not be
    # published yet, so the source walks back to the last one. Emitting it as a
    # dated row rather than folding it into today's row keeps that honest.
    #
    # This was missing: the section fed the card but never the history, so the
    # positioning CHART silently froze at whatever --backfill-participants last
    # wrote while the card above it kept showing fresh numbers.
    fdv = data["sections"].get("fii_derivatives") or {}
    part = fdv.get("participants") or {}
    if fdv.get("date") and part:
        prow = {"date": fdv["date"]}
        for who, prefix in (("fii", "fii"), ("dii", "dii")):
            p = part.get(who) or {}
            if p.get("index_fut_net") is not None:
                prow[prefix + "_idx_fut_net"] = p["index_fut_net"]
            if p.get("stock_fut_net") is not None:
                prow[prefix + "_stk_fut_net"] = p["stock_fut_net"]
        if len(prow) > 1:
            rows.append(prow)
    wy = data["sections"].get("world_yields") or {}
    for key, obs in (wy.get("series") or {}).items():
        rows += [{"date": d, key: v} for d, v in obs if v is not None]
    hist = merge_rows(load_history(), rows)
    save_history(hist)

    # Those series are bulk history and are now in history.json. Leaving them
    # in data.json as well made it 798 KB - every visitor downloading a decade
    # of yields twice, on a page whose whole point is loading fast on mobile.
    if isinstance(fd, dict) and "series" in fd:
        fd["series_days"] = len(fd["series"])
        del fd["series"]
    if isinstance(wy, dict) and "series" in wy:
        wy["series_points"] = {k: len(v) for k, v in wy["series"].items()}
        del wy["series"]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")

    print("\nWrote %s  (%d bytes)" % (OUT, OUT.stat().st_size))
    print("History %d rows -> %s" % (len(hist["rows"]), HISTORY))
    if data["failures"]:
        print("%d source(s) failed - dashboard shows last good values, "
              "flagged stale." % len(data["failures"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
