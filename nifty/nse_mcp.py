"""Official NSE MCP server client and fallback feed for NIFTY 50.

The dashboard feed relies primarily on NSE's unofficial public JSON endpoints,
which are prone to sudden structure changes, IP throttling, and session drops.
NSE also hosts an official Model Context Protocol (MCP) streamable HTTP server
providing capital market data.

This module wraps the MCP server to provide:
  1. A resilient live price fallback when the unofficial feed fails.
  2. Same-day intraday snapshot accumulation (since MCP has no historical data).
  3. Index level synthesis derived from previous free-float market cap weights.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import sys
import time
import requests

ENDPOINT = "https://mcp.nseindia.in/cmmkt/mcp"
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_response(text):
    """Parse MCP HTTP response handling both raw JSON and Server-Sent Events."""
    body = text.strip()
    if body.startswith("event:") or body.startswith("data:"):
        payloads = []
        for line in body.splitlines():
            trimmed = line.strip()
            if trimmed.startswith("data:"):
                part = trimmed[5:]
                if part.startswith(" "):
                    part = part[1:]
                payloads.append(part)
        combined = "\n".join(payloads)
        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            # Fallback if multiple events exist: parse the last non-empty chunk
            for item in reversed(payloads):
                try:
                    return json.loads(item)
                except json.JSONDecodeError:
                    continue
            raise
    return json.loads(body)


def _iso_to_ist_dt(iso_str):
    """Parse a UTC ISO timestamp ending in Z and convert to an IST datetime."""
    if not iso_str:
        return datetime.now(IST)
    try:
        clean = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return datetime.now(IST)


class NseMcp:
    def __init__(self, endpoint=ENDPOINT, timeout=60, session_id=None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.session_id = session_id
        self.call_count = 0
        self._request_id = 0
        self._session = requests.Session()

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _post(self, payload, include_session_header=True):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
        if include_session_header and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        self.call_count += 1
        return self._session.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.timeout
        )

    def _handshake(self):
        """Perform MCP initialization handshake and store the returned session id."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "nifty-dash", "version": "1"}
            }
        }
        resp = self._post(payload, include_session_header=False)
        if resp.status_code >= 400:
            raise RuntimeError(f"initialize: HTTP {resp.status_code} - {resp.text}")

        session_id = None
        for k, v in resp.headers.items():
            if k.lower() == "mcp-session-id":
                session_id = v
                break
        self.session_id = session_id

        notify_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        try:
            self._post(notify_payload, include_session_header=True)
        except Exception:
            pass

    def _call_tool(self, name, arguments=None):
        args = arguments if arguments is not None else {}
        rehandshaked = False
        other_retries = 0
        max_other_retries = 2

        while True:
            if not self.session_id:
                self._handshake()

            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": args
                }
            }

            try:
                resp = self._post(payload, include_session_header=True)

                if resp.status_code in (400, 404):
                    if not rehandshaked:
                        rehandshaked = True
                        self.session_id = None
                        continue
                    raise RuntimeError(f"{name}: HTTP {resp.status_code} - {resp.text}")

                if resp.status_code >= 400:
                    raise RuntimeError(f"{name}: HTTP {resp.status_code} - {resp.text}")

                parsed = _parse_response(resp.text)
                if "error" in parsed:
                    err = parsed["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    if "session" in msg.lower() and not rehandshaked:
                        rehandshaked = True
                        self.session_id = None
                        continue
                    raise RuntimeError(f"{name}: {msg}")

                result = parsed.get("result", {})
                content = result.get("content", [])
                if not content or "text" not in content[0]:
                    raise RuntimeError(f"{name}: missing content text in tool response")

                raw_text = content[0]["text"]
                return json.loads(raw_text) if isinstance(raw_text, str) else raw_text

            except Exception as exc:
                if rehandshaked and isinstance(exc, RuntimeError) and (
                    "HTTP 400" in str(exc) or "HTTP 404" in str(exc) or "session" in str(exc).lower()
                ):
                    raise

                if other_retries < max_other_retries:
                    other_retries += 1
                    time.sleep(1.0)
                    continue

                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError(f"{name}: {exc}") from exc

    def status(self):
        """Fetch crawl freshness and status."""
        return self._call_tool("cm_get_data_status", {})

    def quotes(self, symbols):
        """Fetch quotes for symbols using prefix batching followed by single quotes."""
        req_set = set(str(s).strip() for s in symbols if str(s).strip())
        found_records = {}
        newest_updated_at = ""

        def _update_record(rec):
            sym = rec.get("symbol")
            if not sym or sym not in req_set:
                return
            if sym not in found_records:
                found_records[sym] = rec
            elif rec.get("series") == "EQ" and found_records[sym].get("series") != "EQ":
                found_records[sym] = rec

        # Route 1: Group by first character to minimize calls
        prefixes = sorted(set(s[0].upper() for s in req_set if s))
        for ch in prefixes:
            try:
                data = self._call_tool(
                    "cm_get_equity_stocks",
                    {"limit": 500, "symbolFilter": ch}
                )
                u = data.get("updatedAt", "")
                if u and u > newest_updated_at:
                    newest_updated_at = u
                for rec in data.get("stocks", []):
                    _update_record(rec)
            except Exception:
                pass

        # Route 2: Fall back to individual quotes for any symbol still missing
        missing = [s for s in req_set if s not in found_records]
        for sym in missing:
            try:
                data = self._call_tool("cm_get_stock_quote", {"symbol": sym})
                u = data.get("updatedAt", "")
                if u and u > newest_updated_at:
                    newest_updated_at = u
                stock = data.get("stock")
                if stock and isinstance(stock, dict):
                    _update_record(stock)
            except Exception:
                pass

        prices = {}
        for sym, rec in found_records.items():
            def _to_float(v):
                try:
                    return float(v) if v is not None else 0.0
                except (ValueError, TypeError):
                    return 0.0

            prices[sym] = {
                "last": _to_float(rec.get("lastTradedPrice")),
                "open": _to_float(rec.get("openPrice")),
                "high": _to_float(rec.get("highPrice")),
                "low": _to_float(rec.get("lowPrice")),
                "prevClose": _to_float(rec.get("preClosePrice")),
                "ts": str(rec.get("latestTimestamp") or "")
            }

        return prices, newest_updated_at

    def close(self):
        self._session.close()


def append_intraday(store_path, prices, updated_at, trade_date):
    """Accumulate same-day snapshots into a local JSON store atomically."""
    time_str = _iso_to_ist_dt(updated_at).strftime("%H:%M:%S")
    store_abs = os.path.abspath(store_path)
    store_dir = os.path.dirname(store_abs)
    existing_data = None

    if os.path.exists(store_abs):
        try:
            with open(store_abs, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "date" in loaded:
                    existing_data = loaded
        except Exception:
            existing_data = None

    # Archive previous readable file if trade date has rolled over
    if existing_data is not None:
        if existing_data.get("date") != trade_date:
            archive_dir = os.path.join(store_dir, "archive")
            os.makedirs(archive_dir, exist_ok=True)
            old_date = existing_data.get("date", "unknown")
            archive_file = os.path.join(archive_dir, f"{old_date}.json")
            try:
                shutil.copy2(store_abs, archive_file)
            except Exception:
                pass
            existing_data = None
        elif existing_data.get("lastUpdatedAt") == updated_at and updated_at:
            return 0

    if existing_data is None:
        data = {
            "date": trade_date,
            "source": "nse-mcp",
            "firstSeen": datetime.now(timezone.utc).isoformat(),
            "lastUpdatedAt": updated_at,
            "snapshots": 1,
            "points": {}
        }
    else:
        data = existing_data
        data["lastUpdatedAt"] = updated_at
        data["snapshots"] = data.get("snapshots", 0) + 1

    points = data.setdefault("points", {})
    appended = 0
    for sym, q in prices.items():
        if "last" in q and q["last"] is not None:
            sym_points = points.setdefault(sym, [])
            sym_points.append([time_str, float(q["last"])])
            appended += 1

    os.makedirs(store_dir, exist_ok=True)
    tmp_path = store_abs + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, store_abs)

    return appended


def live_fallback(prev_snapshot, mcp=None):
    # nifty50.json names its columns sym/last/ffmc/name, while NSE's own payload
    # uses symbol/lastPrice/companyName - both shapes are accepted below.
    """Synthesise live snapshot tuple (idx, stocks, status) when primary feed is down."""
    if not isinstance(prev_snapshot, dict):
        raise RuntimeError("prev_snapshot has no stocks")

    raw_stocks = prev_snapshot.get("stocks")
    if not raw_stocks:
        raise RuntimeError("prev_snapshot has no stocks")

    if isinstance(raw_stocks, dict):
        stocks_list = list(raw_stocks.values())
    elif isinstance(raw_stocks, list):
        stocks_list = list(raw_stocks)
    else:
        raise RuntimeError("prev_snapshot has no stocks")

    if not stocks_list:
        raise RuntimeError("prev_snapshot has no stocks")

    prev_index = prev_snapshot.get("index") or {}
    try:
        prev_index_last = float(prev_index.get("lastPrice") or prev_index.get("last") or 0.0)
    except (ValueError, TypeError):
        prev_index_last = 0.0

    if prev_index_last <= 0:
        raise RuntimeError("prev_snapshot index lastPrice is missing or <= 0")

    symbols = [(s.get("symbol") or s.get("sym")) for s in stocks_list if (s.get("symbol") or s.get("sym"))]
    if not symbols:
        raise RuntimeError("prev_snapshot has no stock symbols")

    client = mcp if mcp is not None else NseMcp()
    should_close = (mcp is None)
    try:
        prices, updated_at = client.quotes(symbols)
    finally:
        if should_close:
            client.close()

    covered = [s for s in symbols if s in prices]
    if len(covered) < 45:
        raise RuntimeError(
            f"MCP covered fewer than 45 symbols: {len(covered)}/{len(symbols)}"
        )

    sum_prev_ffmc = 0.0
    for s in stocks_list:
        try:
            sum_prev_ffmc += float(s.get("ffmc") or 0.0)
        except (ValueError, TypeError):
            pass

    divisor = (sum_prev_ffmc / prev_index_last) if prev_index_last > 0 else 1.0

    sum_live_ffmc = 0.0
    live_stocks = []
    stale_syms = []
    stale_weight = 0.0
    suspect_actions = []

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    for s in stocks_list:
        sym = (s.get("symbol") or s.get("sym"))
        if not sym:
            continue

        try:
            p_last = float(s.get("lastPrice") or s.get("last") or 0.0)
        except (ValueError, TypeError):
            p_last = 0.0

        try:
            p_ffmc = float(s.get("ffmc") or 0.0)
        except (ValueError, TypeError):
            p_ffmc = 0.0

        shares = (p_ffmc / p_last) if p_last > 0 else 0.0

        if sym in prices:
            q = prices[sym]
            live_last = q["last"]
            # Share counts are carried from the last good payload, which is only
            # valid while the capital structure is unchanged. A split, bonus or
            # rights issue moves the exchange's own previous close away from the
            # price we cached - that mismatch is the cheapest corporate-action
            # detector available here, and it means the carried shares are wrong.
            mcp_prev = q.get("prevClose")
            if p_last > 0 and mcp_prev and abs(mcp_prev - p_last) / p_last > 0.10:
                suspect_actions.append(f"{sym} prevClose {mcp_prev} vs cached {p_last}")
            live_ffmc = shares * live_last
            sum_live_ffmc += live_ffmc

            company_name = s.get("name") or s.get("companyName") or sym
            live_stocks.append({
                "symbol": sym,
                "companyName": company_name,
                "lastPrice": live_last,
                "open": q["open"],
                "previousClose": q["prevClose"],
                "dayHigh": q["high"],
                "dayLow": q["low"],
                "ffmc": live_ffmc,
                "priority": 0
            })
        else:
            # Uncovered by the MCP: hold the stock at its last known price. Its
            # market cap stays in the index (otherwise the index would print low
            # by that stock's weight) AND its row stays on the board (otherwise
            # the rows would no longer sum to the index above them). Flagged so
            # the page can mark it rather than imply a fresh print.
            sum_live_ffmc += p_ffmc
            stale_syms.append(sym)
            stale_weight += (p_ffmc / sum_prev_ffmc) if sum_prev_ffmc > 0 else 0.0
            live_stocks.append({
                "symbol": sym,
                "companyName": s.get("name") or s.get("companyName") or sym,
                "lastPrice": p_last,
                "open": num(s.get("open")) or p_last,
                "previousClose": num(s.get("prevClose") or s.get("previousClose")) or p_last,
                "dayHigh": num(s.get("high") or s.get("dayHigh")) or p_last,
                "dayLow": num(s.get("low") or s.get("dayLow")) or p_last,
                "ffmc": p_ffmc,
                "priority": 0,
                "stale": True,
            })

    # A thin or distorted rebuild is worse than an honest refusal: the board
    # would look live while part of it was frozen or mis-scaled.
    if stale_weight > 0.005:
        raise RuntimeError(
            "MCP fallback refused: %d symbols missing carrying %.2f%% of the index (%s)"
            % (len(stale_syms), stale_weight * 100, ", ".join(stale_syms[:5])))
    if suspect_actions:
        raise RuntimeError(
            "MCP fallback refused: corporate action suspected, carried share counts "
            "are stale - " + "; ".join(suspect_actions[:3]))

    index_last = round(sum_live_ffmc / divisor, 2) if divisor > 0 else prev_index_last

    try:
        prev_high = float(prev_index.get("dayHigh") or prev_index.get("high") or index_last)
    except (ValueError, TypeError):
        prev_high = index_last

    try:
        prev_low = float(prev_index.get("dayLow") or prev_index.get("low") or index_last)
    except (ValueError, TypeError):
        prev_low = index_last

    day_high = max(prev_high, index_last)
    day_low = min(prev_low, index_last) if prev_low > 0 else index_last
    last_update_time = _iso_to_ist_dt(updated_at).strftime("%Y-%m-%d %H:%M:%S")

    idx = {
        "symbol": prev_index.get("symbol", "NIFTY 50"),
        "lastPrice": index_last,
        "open": float(prev_index.get("open", 0.0) or 0.0),
        "previousClose": float(prev_index.get("previousClose", 0.0) or 0.0),
        "dayHigh": day_high,
        "dayLow": day_low,
        "lastUpdateTime": last_update_time,
        "priority": 1
    }

    market = prev_snapshot.get("market") or {}
    status = dict(market) if isinstance(market, dict) else {}
    status["source"] = "nse-mcp fallback"
    status["staleSymbols"] = stale_syms

    return idx, live_stocks, status


if __name__ == "__main__":
    try:
        module_dir = os.path.dirname(os.path.abspath(__file__))
        snapshot_path = os.path.normpath(
            os.path.join(module_dir, "..", "site", "data", "nifty50.json")
        )

        if not os.path.exists(snapshot_path):
            raise FileNotFoundError(f"Snapshot not found at {snapshot_path}")

        with open(snapshot_path, "r", encoding="utf-8") as f:
            prev_snapshot = json.load(f)

        mcp = NseMcp()

        # Step 2: Print crawl status
        crawl_status = mcp.status()
        print("Crawl status:", json.dumps(crawl_status, indent=2))

        # Step 3: Fetch quotes for all 50 symbols
        raw_stk = prev_snapshot.get("stocks", [])
        stocks_data = list(raw_stk.values()) if isinstance(raw_stk, dict) else list(raw_stk)
        symbols = [(s.get("symbol") or s.get("sym")) for s in stocks_data if (s.get("symbol") or s.get("sym"))]

        start_calls = mcp.call_count
        t0 = time.time()
        prices, updated_at = mcp.quotes(symbols)
        elapsed = time.time() - t0
        calls_made = mcp.call_count - start_calls

        print(
            f"Quotes: {len(prices)}/{len(symbols)} covered in {elapsed:.2f}s "
            f"across {calls_made} MCP calls (updatedAt: {updated_at})"
        )

        # Step 4: Append intraday series
        intraday_path = os.path.normpath(
            os.path.join(module_dir, "..", "feed", "cache", "mcp_intraday", "today.json")
        )
        trade_date = (prev_snapshot.get("market") or {}).get("tradeDate")
        if not trade_date:
            trade_date = datetime.now(IST).strftime("%Y-%m-%d")

        appended_count = append_intraday(intraday_path, prices, updated_at, trade_date)
        print(f"Intraday points appended: {appended_count} -> {intraday_path}")

        # Step 5: Fallback index synthesis
        idx, live_stocks, fallback_status = live_fallback(prev_snapshot, mcp=mcp)
        prev_idx = prev_snapshot.get("index") or {}
        prev_idx_last = float(prev_idx.get("lastPrice") or prev_idx.get("last") or 0.0)
        synth_last = idx["lastPrice"]
        diff = synth_last - prev_idx_last

        print(
            f"Index Level: synthesised={synth_last:.2f} | snapshot={prev_idx_last:.2f} "
            f"| diff={diff:+.2f}"
        )

        print("Sample stocks:")
        prev_map = {
            (s.get("symbol") or s.get("sym")): float(s.get("lastPrice") or s.get("last") or 0.0)
            for s in stocks_data if s.get("symbol") or s.get("sym")
        }
        for s in live_stocks[:3]:
            sym = s["symbol"]
            print(f"  {sym:<12} MCP last: {s['lastPrice']:>8.2f} | Snapshot: {prev_map.get(sym, 0.0):>8.2f}")

        mcp.close()

    except Exception as exc:
        print(f"Self-test failed: {exc}", file=sys.stderr)
        sys.exit(1)
