"""NIFTY 50 sector/stock dashboard - data feed.

Builds the JSON the web dashboard (site/index.html) reads. Every number comes from an
official NSE / NSE Indices source; nothing is typed in by hand, so a rebalance needs
no edit here.

    python nifty_feed.py once  [--out ../site/data] [--intraday]
        one snapshot -> nifty50.json (+ intraday.json). For cron / GitHub Actions.

    python nifty_feed.py serve [--site ../site] [--port 8050]
        serves the page and refreshes the JSON on demand (20 s cache while the market
        is open). For a VPS or a home PC.

Sources (all probed 4 Oct 2026):
  live      NSE  /api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=NIFTY 50
            -> price, open, prev close and FREE-FLOAT MARKET CAP (ffmc) for all 50.
            Weight_i = ffmc_i / sum(ffmc). Checked against the official 30-Sep-2026
            month-end PDF: max error 0.005 % on all 50 stocks.
  members   niftyindices.com/IndexConstituent/ind_nifty50list.csv
            -> the official constituent list + NSE sector ("Industry").
  official  niftyindices.com monthly reports, two files:
            indices_data<Mon><YYYY>.zip -> NIFTY_50_<Mon><YYYY>.pdf  (month-end weights)
            mcwb_<mon><yy>.zip          -> nifty50_mcwb.csv          (weights + beta)
            Used as a cross-check and shown in tooltips; the live weights drive the maths.
  bases     NSE UDiFF bhavcopy + ind_close_all  -> previous week / month closes.
            Split / bonus between then and now: api/corporates-corporateActions.
  intraday  getSymbolgraphData / getIndexChart (flag=1D) -> 1-minute series for the
            3 PM and Custom HH:MM anchors.
"""
import argparse, csv, datetime as dt, io, json, os, re, sys, threading, time, zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

NSE = "https://www.nseindia.com"
ARCH = "https://nsearchives.nseindia.com"
NI = "https://www.niftyindices.com"
MW = NSE + "/api/NextApi/apiClient/marketWatchApi?functionName="
INDEX_NAME = "NIFTY 50"


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now_ist():
    return dt.datetime.now(IST)


def cpath(*p):
    path = os.path.join(CACHE, *p)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, separators=(",", ":"))
        else:
            json.dump(obj, f, indent=1)
    for i in range(6):        # atomic: a reader never sees half a file
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # Windows: a server thread has it open - retry briefly
            time.sleep(0.05 * 2 ** i)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------
class Http:
    """requests.Session with NSE cookie warm-up. NSE answers 401/403 both for a stale
    cookie and for rate limiting, so a failure re-warms and backs off before retrying."""

    def __init__(self):
        self.s = None
        self.warm_at = 0

    def _new(self):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                          "Accept": "application/json, text/plain, */*"})
        self.s = s
        self.warm_at = 0

    def _warm(self):
        if self.s is None:
            self._new()
        if time.time() - self.warm_at > 240:
            try:
                self.s.get(NSE + "/market-data/live-equity-market", timeout=20)
            except requests.RequestException as e:
                log("warm-up failed:", e)
            self.warm_at = time.time()

    def get(self, url, nse=False, tries=4, timeout=30, ok404=False):
        for i in range(tries):
            if nse:
                self._warm()
            elif self.s is None:
                self._new()
            try:
                hdr = {"Referer": NSE + "/market-data/live-equity-market"} if nse else {}
                r = self.s.get(url, timeout=timeout, headers=hdr)
                if r.status_code == 200:
                    return r
                if r.status_code == 404 and ok404:
                    return MISSING
                log(f"HTTP {r.status_code} {url[:110]}")
            except requests.RequestException as e:
                log("error", type(e).__name__, url[:110])
            self._new()
            time.sleep(2 + 3 * i)
        return None


H = Http()
MISSING = object()   # returned by H.get(ok404=True) for a genuine 404; None = failed / blocked


# ----------------------------------------------------------------------------------
# 1. Live snapshot (one call gives all 50 + the index)
# ----------------------------------------------------------------------------------
def _fetch_live_nse():
    r = H.get(MW + "getIndicesData&symbol=" + requests.utils.quote(INDEX_NAME), nse=True)
    if r is None:
        raise RuntimeError("NSE live index data unavailable")
    rows = r.json()["data"]["data"]
    idx = next((x for x in rows if x.get("priority") == 1), None)
    if idx is None:
        raise RuntimeError("index row missing from live payload")
    stocks = [x for x in rows if x.get("priority") == 0]
    if len(stocks) < 45 or any(not x.get("ffmc") for x in stocks):
        raise RuntimeError(f"live payload looks wrong: {len(stocks)} stocks")
    status = None
    m = H.get(NSE + "/api/marketStatus", nse=True, tries=2)
    if m is not None:
        for x in m.json().get("marketState", []):
            if x.get("market") == "Capital Market":
                status = {"status": x.get("marketStatus"), "tradeDate": x.get("tradeDate"),
                          "message": x.get("marketStatusMessage")}
    _cache_last_good(idx, stocks)
    return idx, stocks, status


LAST_GOOD = "last_live.json"


def _cache_last_good(idx, stocks):
    """Keep the newest good NSE payload so the MCP fallback has share counts.

    The MCP publishes prices but no free-float market cap, and ffmc is what the
    whole board is weighted by. Free-float factors change quarterly, so the
    share count implied by the last good payload (ffmc / price) stays valid for
    the rest of the day - which is exactly as long as the fallback needs it.
    """
    try:
        jsave(cpath(LAST_GOOD), {"idx": idx, "stocks": stocks,
                                 "saved": now_ist().isoformat(timespec="seconds")})
    except Exception as e:
        log(f"could not cache the live payload: {e}")


def fetch_live():
    """NSE's own endpoint, falling back to NSE's MCP server when it fails.

    The website API this feed reads is unofficial and has changed under us
    before (the old equity-stockIndices path now 404s). NSE also runs an
    official MCP server with live capital-market data; it is coarser - a
    5-minute crawl, no ffmc, no history - but it is a different system with a
    different failure mode, which is the point of a fallback.
    """
    try:
        return _fetch_live_nse()
    except Exception as e:
        log(f"NSE live feed failed ({e}); falling back to the NSE MCP")
        prev = jload(cpath(LAST_GOOD)) or {}
        snap = {"stocks": prev.get("stocks") or [], "index": prev.get("idx") or {},
                "market": None}
        if not snap["stocks"]:
            raise RuntimeError(f"NSE live failed ({e}) and no cached payload to fall back on")
        import nse_mcp
        idx, stocks, status = nse_mcp.live_fallback(snap)
        log(f"MCP fallback carried {len(stocks)} stocks; index {idx['lastPrice']}")
        return idx, stocks, status


# ----------------------------------------------------------------------------------
# 2. Constituents + sectors (official CSV). Refreshed every 6 h, or at once when the
#    live feed carries a symbol the cached list does not know (rebalance day).
# ----------------------------------------------------------------------------------
def fetch_constituents(live_syms, force=False):
    path = cpath("constituents.json")
    c = jload(path)
    stale = c is None or time.time() - c.get("ts", 0) > 6 * 3600
    unknown = c is not None and any(s not in c["map"] for s in live_syms)
    if force or stale or unknown:
        r = H.get(NI + "/IndexConstituent/ind_nifty50list.csv")
        if r is not None and "Symbol" in r.text[:200]:
            rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
            m = {row["Symbol"].strip(): {"name": row["Company Name"].strip(),
                                         "sector": row["Industry"].strip(),
                                         "isin": row["ISIN Code"].strip()} for row in rows}
            if len(m) >= 45:
                c = {"ts": time.time(), "fetched": now_ist().strftime("%Y-%m-%d %H:%M"), "map": m}
                jsave(path, c)
                _log_membership(sorted(m))
        elif c is None:
            raise RuntimeError("constituent list unavailable")
    return c


def _log_membership(symbols):
    """Append a dated entry whenever the official member list changes."""
    path = cpath("membership_log.json")
    lg = jload(path, [])
    if not lg or lg[-1]["symbols"] != symbols:
        prev = set(lg[-1]["symbols"]) if lg else None
        e = {"date": now_ist().strftime("%Y-%m-%d"), "symbols": symbols}
        if prev is not None:
            e["added"] = sorted(set(symbols) - prev)
            e["removed"] = sorted(prev - set(symbols))
        lg.append(e)
        jsave(path, lg)


# ----------------------------------------------------------------------------------
# 3. Official month-end weights (niftyindices monthly reports). Checked once a day for
#    a newer month; each month's parsed result is cached for good.
# ----------------------------------------------------------------------------------
def _month_back(d, k):
    y, m = d.year, d.month - k
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


def _zip_member(url, want):
    r = H.get(url, ok404=True, tries=2)
    if r is None or r is MISSING or r.content[:2] != b"PK":      # a missing month answers 200 + HTML
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for n in z.namelist():
        if want(n):
            return z.read(n)
    return None


def _parse_mcwb(raw, label):
    text = raw.decode("utf-8-sig", errors="replace").splitlines()
    hdr_i = next((i for i, l in enumerate(text) if "Security Symbol" in l and "Weightage" in l), None)
    if hdr_i is None:
        return None
    rd = csv.DictReader(text[hdr_i:])
    out = {}
    for row in rd:
        sym = (row.get("Security Symbol") or "").strip()
        try:
            w = float(row.get("Weightage (%)") or "")
        except ValueError:
            continue
        if sym:
            out[sym] = {"w": w, "beta": _f(row.get("Beta")), "industry": (row.get("Basic Industry") or "").strip()}
    tot = sum(v["w"] for v in out.values())
    if len(out) < 45 or not 99 <= tot <= 101:
        log(f"MCWB {label}: rejected ({len(out)} rows, sum {tot:.2f})")
        return None
    return out


SECTORS = ["Automobile and Auto Components", "Capital Goods", "Chemicals", "Construction Materials",
           "Construction", "Consumer Durables", "Consumer Services", "Diversified", "Fast Moving Consumer Goods",
           "Financial Services", "Forest Materials", "Healthcare", "Information Technology",
           "Media Entertainment & Publication", "Metals & Mining", "Oil Gas & Consumable Fuels", "Power",
           "Realty", "Services", "Telecommunication", "Textiles", "Utilities"]


def _sector_of(desc):
    """'Wipro Ltd. Information Technology' -> 'Information Technology' (CSV spelling, no commas)."""
    d = desc.replace(",", "")
    known = set(SECTORS) | {v["sector"].replace(",", "") for v in (jload(cpath("constituents.json"), {}) or {}).get("map", {}).values()}
    hits = [s for s in known if d.endswith(" " + s) or d == s]
    return max(hits, key=len) if hits else "Unclassified"


def _strip_sector(desc, sector):
    d = desc.replace(",", "")
    return desc[: len(desc) - len(sector)].strip() if sector and d.endswith(sector) else desc


def _parse_pdf(raw, label):
    try:
        import pypdf
    except ImportError:
        log("pypdf not installed - skipping the month-end PDF (pip install pypdf)")
        return None, None
    rd = pypdf.PdfReader(io.BytesIO(raw))
    flat = re.sub(r"\s+", " ", " ".join(p.extract_text() or "" for p in rd.pages))
    asof = None
    m = re.search(r"Constituents of NIFTY 50\s+([A-Z][a-z]+ \d{1,2}, \d{4})", flat)
    if m:
        asof = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    # Strip the repeating page headers, then read every row generically - NOT just today's
    # symbols, or a stock that has since left the index (WIPRO in Aug-2026) is silently lost.
    flat = re.sub(r"Symbol Security Name Industry Close Price Index Mcap \(Rs\. Crores\) Weightage \(%\)", " ", flat)
    flat = re.sub(r"Constituents of NIFTY 50 [A-Z][a-z]+ \d{1,2}, \d{4}", " ", flat)
    # pypdf glues the last two columns: "<close 2dp> <weight 2dp><mcap crores>"
    # e.g. "HDFCBANK HDFC Bank Ltd. Financial Services 708.70 10.381083321"
    out = {}
    for sym, desc, close, w, mcap in re.findall(
            r"(?:^|\s)([A-Z][A-Z0-9&\-]{1,19}) ([A-Z].*?) (\d+\.\d{2}) (\d+\.\d{2})(\d+)(?=\s|$)", flat):
        out[sym] = {"w": float(w), "close": float(close), "mcapCr": int(mcap),
                    "desc": desc, "sector": _sector_of(desc)}
    tot = sum(v["w"] for v in out.values())
    if not 48 <= len(out) <= 52 or not 99.5 <= tot <= 100.5:
        log(f"PDF {label}: rejected ({len(out)} rows, sum {tot:.2f})")
        return None, None
    return out, asof


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_official(symbols):
    """-> list of {label, asof, source, weights:{sym: w}} newest first (max one of each kind)."""
    state_p = cpath("official", "state.json")
    st = jload(state_p, {})
    today = now_ist().date()
    if time.time() - st.get("checkedTs", 0) > 6 * 3600:
        for k in range(0, 4):                    # newest month first
            mo = _month_back(today, k)
            key = mo.strftime("%Y-%m")
            pdf_p = cpath("official", f"pdf_{key}.json")
            if not os.path.exists(pdf_p):
                raw = _zip_member(f"{NI}/Indices_-_Market_Capitalisation_and_Weightage/indices_data{mo:%b%Y}.zip",
                                  lambda n: n.upper().startswith("NIFTY_50_"))
                if raw:
                    w, asof = _parse_pdf(raw, key)
                    if w:
                        jsave(pdf_p, {"asof": asof or key, "weights": w})
            mc_p = cpath("official", f"mcwb_{key}.json")
            if not os.path.exists(mc_p):
                raw = _zip_member(f"{NI}/Market_Capitalisation_Weightage_Beta_for_NIFTY_50_And_NIFTY_Next_50/"
                                  f"mcwb_{mo:%b%y}".lower() + ".zip",
                                  lambda n: n.lower().startswith("nifty50"))
                if raw:
                    w = _parse_mcwb(raw, key)
                    if w:
                        jsave(mc_p, {"asof": key, "weights": w})
        st["checkedTs"] = time.time()
        jsave(state_p, st)

    out = []
    files = sorted(os.listdir(os.path.dirname(state_p)), reverse=True)
    pdf = next((f for f in files if f.startswith("pdf_")), None)
    mc = next((f for f in files if f.startswith("mcwb_")), None)
    if pdf:
        d = jload(cpath("official", pdf))
        out.append({"kind": "monthEndPdf", "label": f"Month-end {d['asof']}", "asof": d["asof"],
                    "source": "niftyindices.com - Indices Market Capitalisation & Weightage (PDF)",
                    "weights": {k: v["w"] for k, v in d["weights"].items()}})
    if mc:
        d = jload(cpath("official", mc))
        out.append({"kind": "mcwb", "label": f"MCWB {d['asof']}", "asof": d["asof"],
                    "source": "niftyindices.com - Market Cap, Weightage & Beta (CSV)",
                    "weights": {k: v["w"] for k, v in d["weights"].items()},
                    "beta": {k: v["beta"] for k, v in d["weights"].items()}})
    return out


# ----------------------------------------------------------------------------------
# 4. Previous week / month closes
# ----------------------------------------------------------------------------------
def _closes_on(d):
    """{SYMBOL: close} for EQ/BE series + {'__INDEX__': NIFTY close}, or None if no
    trading that day. Cached per date - a past day never changes."""
    p = cpath("closes", f"{d:%Y%m%d}.json")
    c = jload(p)
    if c is not None:
        return c or None                          # {} = cached holiday
    r = H.get(f"{ARCH}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip", nse=True, ok404=True)
    if r is MISSING:
        if (now_ist().date() - d).days > 2:       # today's file appears ~18:00 IST
            jsave(p, {})                          # a confirmed 404 on an old date = holiday
        return None
    if r is None or r.content[:2] != b"PK":       # blocked / rate-limited: try again next run
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("SctySrs") in ("EQ", "BE", "BZ"):
            v = _f(row.get("ClsPric"))
            if v:
                out[row["TckrSymb"]] = v
    ri = H.get(f"{ARCH}/content/indices/ind_close_all_{d:%d%m%Y}.csv", nse=True, ok404=True)
    if ri is not None and ri is not MISSING:
        for row in csv.DictReader(io.StringIO(ri.text)):
            if (row.get("Index Name") or "").strip().upper() == INDEX_NAME:
                out["__INDEX__"] = _f(row.get("Closing Index Value"))
    if out.get("__INDEX__"):
        jsave(p, out, compact=True)               # cache only a complete day
    return out


def _last_trading_before(d):
    """Most recent trading day strictly before date d (weekends are tried too: NSE has
    held Saturday/Sunday sessions)."""
    for k in range(1, 15):
        x = d - dt.timedelta(days=k)
        c = _closes_on(x)
        if c:
            return x, c
    return None, None


RATIO_BONUS = re.compile(r"bonus\s*[-:]?\s*(\d+)\s*:\s*(\d+)", re.I)
RATIO_SPLIT = re.compile(r"(?:Rs|Re)\.?\s*([0-9]+(?:\.[0-9]+)?).*?\bto\b\s+(?:Rs|Re)\.?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def _price_ratio(subject):
    """Price multiplier for a split / bonus subject line, else None.
    Same rules as BhavCopy/_tools_build_db.py (bonus a:b -> b/(a+b); FV split old->new)."""
    low = " ".join((subject or "").split()).lower()
    mult = None
    if "bonus" in low and not any(k in low for k in ("ncrps", "ncd", "preference")):
        m = RATIO_BONUS.search(low)
        if m and int(m.group(2)) > 0:
            mult = int(m.group(2)) / (int(m.group(1)) + int(m.group(2)))
    for kw in ("split", "splt", "sub-division", "subdivision"):
        k = low.find(kw)
        if k >= 0:
            m = RATIO_SPLIT.search(subject[k:]) if subject else None
            if m:
                old, new = float(m.group(1)), float(m.group(2))
                if 0 < new < old:
                    mult = (mult or 1.0) * new / old
            break
    return mult


def _adjust_factors(frm, to, symbols):
    """{sym: [(exDate, mult)]} for splits/bonuses with exDate in (frm, to]."""
    p = cpath("corpact", f"{frm:%Y%m%d}_{to:%Y%m%d}.json")
    rows = jload(p)
    if rows is None:
        r = H.get(f"{NSE}/api/corporates-corporateActions?index=equities"
                  f"&from_date={frm + dt.timedelta(days=1):%d-%m-%Y}&to_date={to:%d-%m-%Y}", nse=True, tries=3)
        rows = r.json() if r is not None else []
        if r is not None:
            jsave(p, rows)
    out = {}
    for x in rows or []:
        s = x.get("symbol")
        if s not in symbols or x.get("series") not in ("EQ", None, ""):
            continue
        mult = _price_ratio(x.get("subject"))
        if not mult:
            continue
        try:
            ex = dt.datetime.strptime(x["exDate"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if frm < ex <= to:
            out.setdefault(s, []).append((ex.isoformat(), mult))
    return out


def fetch_bases(trade_date, symbols):
    """Previous-week and previous-month closes, split/bonus adjusted to today's terms."""
    week_start = trade_date - dt.timedelta(days=trade_date.weekday())
    month_start = trade_date.replace(day=1)
    res = {"index": {}, "stocks": {s: {} for s in symbols}, "dates": {}, "adjusted": []}
    for key, anchor in (("pwc", week_start), ("pmc", month_start)):
        d, closes = _last_trading_before(anchor)
        if not closes:
            log(f"{key}: no bhavcopy found before {anchor}")
            continue
        res["dates"][key] = d.isoformat()
        res["index"][key] = closes.get("__INDEX__")
        fac = _adjust_factors(d, trade_date, set(symbols))
        for s in symbols:
            v = closes.get(s)
            if v is None:
                continue
            for ex, mult in fac.get(s, []):
                v *= mult
                res["adjusted"].append({"sym": s, "basis": key, "exDate": ex, "mult": round(mult, 6)})
            res["stocks"][s][key] = round(v, 4)
    return res


def fetch_rebalance(key, base_date, trade_date, live, index):
    """Make Weekly/Monthly points add up to NIFTY's move across a membership change.

    Today's ffmc describes today's members only. If the index changed members between the
    base date and today, the period is split at the effective date R:
      before R - the OLD members, weighted by the official month-end PDF's index mcap
                 rolled forward with daily closes  -> pts1 per stock (fixed history)
      from R   - today's members, weighted by ffmc rolled back to the close before R
                 -> the page adds  I_pre * wPre_i * (last_i / closePre_i - 1)
    R is found from the data, not assumed: the split day that lets the old list explain
    NIFTY's daily returns before it, and the new list after it, with the least error.
    Returns None when no change falls inside the window."""
    cur = set(live)
    old = None
    off = cpath("official", "x")[:-2]
    os.makedirs(off, exist_ok=True)
    for f in sorted((f for f in os.listdir(off) if f.startswith("pdf_")), reverse=True):
        d = jload(cpath("official", f))
        if d and set(d["weights"]) != cur:
            old = d
            break
    if old is None:
        return None
    days = []
    d = base_date
    while d < trade_date:
        c = _closes_on(d)
        if c:
            days.append((d, c))
        d += dt.timedelta(days=1)
    if not days or days[0][0] != base_date:
        return None
    today = {s: x["lastPrice"] for s, x in live.items()}
    today["__INDEX__"] = index["lastPrice"]
    days.append((trade_date, today))
    if len(days) < 2:
        return None

    ow = old["weights"]

    # Splits / bonuses between the old list's date and today: put every close in today's share
    # terms, else a 1:2 split reads as a -50% day and the rolled mcap collapses (Opus review 2.1).
    pdf_date = dt.date.fromisoformat(old["asof"][:10]) if len(old["asof"]) >= 10 else base_date
    fac = _adjust_factors(min(pdf_date, base_date), trade_date, cur | set(ow))
    if fac:
        def adj(sym, d, px):
            for ex, mult in fac.get(sym, []):
                if dt.date.fromisoformat(ex) > d:
                    px *= mult
            return px
        days = [(d, {k: (adj(k, d, v) if k != "__INDEX__" else v) for k, v in c.items()}) for d, c in days[:-1]] + [days[-1]]
        ow = {k: dict(v, close=adj(k, pdf_date, v["close"])) for k, v in ow.items()}

    def ret(ff, cp, ct):
        ok = [s for s in ff if cp.get(s) and ct.get(s)]
        den = sum(ff[s] for s in ok)
        return sum(ff[s] * (ct[s] / cp[s] - 1) for s in ok) / den * 100 if den else 0.0

    errs = []                                     # per transition: (|old - idx|, |new - idx|)
    for (_, cp), (_, ct) in zip(days, days[1:]):
        ff_old = {s: v["mcapCr"] * cp.get(s, v["close"]) / v["close"] for s, v in ow.items()}
        ff_new = {s: live[s]["ffmc"] * cp[s] / live[s]["lastPrice"] for s in live if cp.get(s)}
        ir = (ct["__INDEX__"] / cp["__INDEX__"] - 1) * 100
        errs.append((abs(ret(ff_old, cp, ct) - ir), abs(ret(ff_new, cp, ct) - ir)))
    n = len(errs)
    # split s: transitions < s use the old list, >= s the new one. s = 0 -> change was before base.
    score = [sum(e[0] for e in errs[:s]) + sum(e[1] for e in errs[s:]) for s in range(n + 1)]
    s = min(range(n + 1), key=score.__getitem__)
    runner_up = min((score[k] for k in range(n + 1) if k != s), default=score[s])
    if s == 0 or s == n:                         # n -> old list still live: nothing to split
        return None

    (b_date, cb), (p_date, cpre) = days[0], days[s]
    eff = days[s + 1][0]
    B, I_pre = cb["__INDEX__"], cpre["__INDEX__"]
    ff_base = {x: v["mcapCr"] * cb[x] / v["close"] for x, v in ow.items() if cb.get(x) and cpre.get(x)}
    tot = sum(ff_base.values())
    pts1, pct1 = {}, {}
    for x, f in ff_base.items():
        r = cpre[x] / cb[x] - 1
        pts1[x] = round(B * f / tot * r, 4)
        pct1[x] = round(r * 100, 4)
    exited = sorted(set(ow) - cur)
    return {"effective": eff.isoformat(), "preDate": p_date.isoformat(), "indexPre": I_pre,
            "oldList": f"official month-end {old['asof']}",
            "pts1": pts1, "pct1": pct1,
            "closePre": {x: cpre[x] for x in live if cpre.get(x)},
            "exited": [{"sym": x, "name": _strip_sector(ow[x].get("desc", x), ow[x].get("sector")),
                        "sector": ow[x].get("sector", "Unclassified")}
                       for x in exited],
            "entered": sorted(cur - set(ow)),
            "fitError": {"gapToNextBestSplit": round(runner_up - score[s], 4),
                         "oldListBefore": round(sum(e[0] for e in errs[:s]), 4),
                         "newListAfter": round(sum(e[1] for e in errs[s:]), 4)}}


# ----------------------------------------------------------------------------------
# 5. Intraday 1-minute series -> "open of the HH:MM candle" for every minute 09:15..15:30
# ----------------------------------------------------------------------------------
FIRST_MIN, LAST_MIN = 9 * 60 + 15, 15 * 60 + 30


def _minute_opens(points, day_open):
    """NSE chart points are [ms, price, flag, ...], stamped IST-as-UTC at the END of each
    minute (hh:mm:59). The open of the HH:MM candle ~= the last print before HH:MM:00.
    Index 0 (09:15) is the day's official open. A minute not reached yet is null."""
    pts = sorted((int(p[0]) // 1000 % 86400, float(p[1])) for p in points if p and p[1] is not None)
    out, j, last = [], 0, day_open
    if not pts:
        return []
    latest = pts[-1][0]
    for m in range(FIRST_MIN, LAST_MIN + 1):
        t = m * 60
        while j < len(pts) and pts[j][0] < t:
            last = pts[j][1]
            j += 1
        if m == FIRST_MIN:
            out.append(day_open)
        elif t > latest:
            break                                   # that candle has not opened yet
        else:
            out.append(last)
    return out


def fetch_intraday(idx, stocks):
    series = {}
    r = H.get(MW + "getIndexChart&&identifier=" + requests.utils.quote(INDEX_NAME) + "&flag=1D", nse=True, tries=2)
    if r is not None:
        series["__INDEX__"] = _minute_opens(r.json().get("grapthData") or [], idx.get("open"))
    for x in stocks:
        r = H.get(MW + "getSymbolgraphData&&identifier=" + requests.utils.quote(x["identifier"]) + "&flag=1D",
                  nse=True, tries=2)
        if r is not None:
            series[x["symbol"]] = _minute_opens(r.json().get("grapthData") or [], x.get("open"))
        time.sleep(0.25)                            # 51 calls; stay well under the rate limit
    return {"schema": 1, "generated": now_ist().isoformat(timespec="seconds"),
            "tradeDate": (idx.get("lastUpdateTime") or "")[:10], "firstMinute": "09:15",
            "note": "series[sym][k] = open of the 1-minute candle at 09:15 + k minutes",
            "series": series}


# ----------------------------------------------------------------------------------
# Assemble
# ----------------------------------------------------------------------------------
def build_snapshot():
    idx, stocks, status = fetch_live()
    syms = [x["symbol"] for x in stocks]
    cons = fetch_constituents(syms)
    official = fetch_official(syms)
    trade_date = dt.date.fromisoformat((idx.get("lastUpdateTime") or now_ist().isoformat())[:10])
    bases = fetch_bases(trade_date, syms)
    live = {x["symbol"]: x for x in stocks}
    rebalance = {}
    for key in ("pwc", "pmc"):
        if key in bases["dates"]:
            try:
                rb = fetch_rebalance(key, dt.date.fromisoformat(bases["dates"][key]), trade_date, live, idx)
            except Exception as e:                  # never let this sink the live snapshot
                log(f"rebalance {key} failed: {e}")
                rb = None
            if rb:
                rebalance[key] = rb

    cmap = cons["map"]
    out_stocks = []
    for x in stocks:
        s = x["symbol"]
        meta = cmap.get(s, {})
        out_stocks.append({
            "sym": s, "name": meta.get("name") or x.get("companyName"),
            "sector": meta.get("sector") or "Unclassified",
            "last": x["lastPrice"], "open": x["open"], "prevClose": x["previousClose"],
            "high": x["dayHigh"], "low": x["dayLow"], "ffmc": x["ffmc"],
            "pwc": bases["stocks"][s].get("pwc"), "pmc": bases["stocks"][s].get("pmc"),
            "official": {o["kind"]: o["weights"].get(s) for o in official},
        })

    # Membership changes: logged diffs, plus the diff against the newest official
    # month file whose list differs (that file predates the rebalance).
    changes = [{"date": e["date"], "added": e["added"], "removed": e["removed"], "source": "detected by feed"}
               for e in jload(cpath("membership_log.json"), []) if e.get("added") or e.get("removed")]
    now_set = set(syms)
    for o in official:
        prev = set(o["weights"])
        if prev != now_set:
            changes.append({"date": o["asof"], "added": sorted(now_set - prev), "removed": sorted(prev - now_set),
                            "source": f"vs official {o['label']} list"})
            break

    tot = sum(x["ffmc"] for x in stocks)
    official_slim = [{k: v for k, v in o.items() if k not in ("weights", "beta")} for o in official]
    return {
        "schema": 1,
        "generated": now_ist().isoformat(timespec="seconds"),
        "lastUpdate": idx.get("lastUpdateTime"),
        "tradeDate": trade_date.isoformat(),
        "market": status,
        "index": {"symbol": INDEX_NAME, "last": idx["lastPrice"], "open": idx["open"],
                  "prevClose": idx["previousClose"], "high": idx["dayHigh"], "low": idx["dayLow"],
                  "pwc": bases["index"].get("pwc"), "pmc": bases["index"].get("pmc")},
        "baseDates": bases["dates"],
        "baseAdjustments": bases["adjusted"],
        "rebalance": rebalance,
        "weights": {"method": "Live free-float market cap from NSE (ffmc). weight_i = ffmc_i / sum(ffmc).",
                    "totalFfmcCr": round(tot / 1e7, 2)},
        "constituents": {"source": "niftyindices.com/IndexConstituent/ind_nifty50list.csv",
                         "fetched": cons["fetched"], "count": len(syms), "changes": changes},
        "official": official_slim,
        "stocks": out_stocks,
    }


def accumulate_mcp(out_dir, symbols):
    """Append one MCP snapshot to today's series, rotating at the date change.

    The MCP has no history at all - every tool returns the latest 5-minute
    crawl - so the only way to get an intraday series out of it is to keep the
    snapshots as they go past. Writes feed/cache/mcp_intraday/today.json and
    publishes a copy next to the other site data. Deduplicated on the server's
    own crawl stamp, so polling faster than the crawl adds nothing.
    """
    try:
        import nse_mcp
        store = cpath("mcp_intraday", "today.json")
        os.makedirs(os.path.dirname(store), exist_ok=True)
        mcp = nse_mcp.NseMcp()
        prices, updated = mcp.quotes(symbols)
        mcp.close()
        if not prices:
            return 0
        day = now_ist().date().isoformat()
        n = nse_mcp.append_intraday(store, prices, updated, day)
        if n and out_dir:
            jsave(os.path.join(out_dir, "intraday_mcp.json"), jload(store, {}), compact=True)
        return n
    except Exception as e:                      # never let the extra series break a run
        log(f"mcp intraday skipped: {e}")
        return 0


def write_once(out_dir, intraday):
    snap = build_snapshot()
    jsave(os.path.join(out_dir, "nifty50.json"), snap)
    accumulate_mcp(out_dir, [s["sym"] for s in snap["stocks"]])
    log(f"nifty50.json  {snap['lastUpdate']}  {len(snap['stocks'])} stocks")
    if intraday:
        idx, stocks, _ = fetch_live()
        jsave(os.path.join(out_dir, "intraday.json"), fetch_intraday(idx, stocks), compact=True)
        log("intraday.json written")


# ----------------------------------------------------------------------------------
# serve mode
# ----------------------------------------------------------------------------------
def market_hours(t=None):
    t = t or now_ist()
    return t.weekday() < 5 and dt.time(9, 0) <= t.time() <= dt.time(15, 50)


class Feed:
    def __init__(self, out_dir, live_ttl):
        self.out, self.live_ttl = out_dir, live_ttl
        self.locks = {"nifty50.json": threading.Lock(), "intraday.json": threading.Lock()}
        self.t_snap = self.t_intra = 0.0

    def ensure(self, name):
        ttl_open = {"nifty50.json": self.live_ttl, "intraday.json": 120}[name]
        ttl = ttl_open if market_hours() else 900
        attr = "t_snap" if name == "nifty50.json" else "t_intra"
        if time.time() - getattr(self, attr) < ttl:
            return
        with self.locks[name]:
            if time.time() - getattr(self, attr) < ttl:
                return
            try:
                if name == "nifty50.json":
                    snap = build_snapshot()
                    jsave(os.path.join(self.out, name), snap)
                    accumulate_mcp(self.out, [s["sym"] for s in snap["stocks"]])
                else:
                    idx, stocks, _ = fetch_live()
                    jsave(os.path.join(self.out, name), fetch_intraday(idx, stocks), compact=True)
                setattr(self, attr, time.time())
            except Exception as e:                  # keep serving the last good file
                log(f"refresh {name} failed: {e}")
                setattr(self, attr, time.time() - ttl + 30)


def serve(site, port, live_ttl):
    out = os.path.join(site, "data")
    feed = Feed(out, live_ttl)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=site, **k)

        def do_GET(self):
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            if self.path.startswith("/data/") and name in ("nifty50.json", "intraday.json"):
                feed.ensure(name)
            return super().do_GET()

        def end_headers(self):
            if self.path.startswith("/data/"):
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, *a):
            pass

    log(f"serving {site} on http://localhost:{port}/")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("once")
    a1.add_argument("--out", default=os.path.join(HERE, "..", "site", "data"))
    a1.add_argument("--intraday", action="store_true", help="also write intraday.json (51 extra NSE calls)")
    a2 = sub.add_parser("serve")
    a2.add_argument("--site", default=os.path.join(HERE, "..", "site"))
    a2.add_argument("--port", type=int, default=8050)
    a2.add_argument("--live-ttl", type=int, default=20, help="seconds between NSE refreshes while open")
    a = ap.parse_args()
    if a.cmd == "once":
        write_once(os.path.abspath(a.out), a.intraday)
    else:
        serve(os.path.abspath(a.site), a.port, a.live_ttl)


if __name__ == "__main__":
    main()
