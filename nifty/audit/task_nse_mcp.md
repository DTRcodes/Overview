# Task: write `feed/nse_mcp.py` for the NIFTY dashboard

Write ONE complete Python 3.10+ module. Output a single ```python fenced block and nothing
else — no commentary before or after. Standard library + `requests` only.

## What it is for

A dashboard feed (`feed/nifty_feed.py`) currently gets NIFTY 50 live data from NSE's website
JSON API. That API is unofficial and can break. NSE also publishes an official MCP server with
live capital-market data. This module wraps that MCP server so the feed can (a) fall back to it
for prices and (b) accumulate a same-day intraday series from it.

## The MCP server — verified by probing it today, build to these exact facts

Endpoint: `https://mcp.nseindia.in/cmmkt/mcp`, Streamable HTTP MCP, JSON-RPC 2.0 over POST.

- Handshake: POST `initialize` with
  `{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"nifty-dash","version":"1"}}`.
  The response carries header `Mcp-Session-Id`, which every later request must send back.
  Then POST a `notifications/initialized` notification (no id, no response expected).
- Responses are sometimes plain JSON and sometimes SSE (lines beginning `data:`). Handle both:
  if the body starts with `event:` or `data:`, concatenate the payloads of the `data:` lines and
  parse that as JSON.
- A tool call is `tools/call` with `{"name": ..., "arguments": {...}}`. The useful payload is a
  JSON **string** at `result.content[0].text` — parse it.
- Tools that matter (these signatures are exact, both params are REQUIRED):
  - `cm_get_equity_stocks(limit:int, symbolFilter:str)` — `limit` max 500 (pass 500);
    `symbolFilter` is a **symbol PREFIX**, one prefix per call, `""` means everything.
    Returns `{"segment","updatedAt","returned","stocks":[...]}`.
  - `cm_get_stock_quote(symbol:str)` — exactly one symbol, returns `{"updatedAt","stock":{...}}`.
  - `cm_get_data_status()` — crawl freshness.
- A stock record has these keys (verified):
  `type, symbol, series, openPrice, highPrice, lowPrice, preClosePrice, lastTradedPrice,
   indicativeClosePrice, change, perChange, volume, value, fiftyTwoWeekHigh, fiftyTwoWeekLow,
   perChange30d, latestTimestamp`
- IMPORTANT: there is **no** free-float market cap (`ffmc`) and **no** time series of any kind.
  The server crawls NSE every 5 minutes and caches 15 minutes. Every call is a snapshot.
- `cm_get_equity_stocks` with `symbolFilter=""` returns only the first 500 equities, which does
  NOT cover all of NIFTY 50, so never rely on one unfiltered call.

## Required public API

```python
ENDPOINT = "https://mcp.nseindia.in/cmmkt/mcp"

class NseMcp:
    def __init__(self, endpoint=ENDPOINT, timeout=60, session_id=None): ...
    def status(self) -> dict          # cm_get_data_status, parsed
    def quotes(self, symbols) -> tuple[dict, str]
    def close(self) -> None
```

`quotes(symbols)` returns `(prices, updated_at)` where `prices` maps each symbol found to
`{"last": float, "open": float, "high": float, "low": float, "prevClose": float,
  "ts": str}`  (`ts` = that record's `latestTimestamp`), and `updated_at` is the newest
`updatedAt` string seen in the responses.

Strategy for `quotes`, in this order, to keep the call count low:
1. Group the requested symbols by their FIRST CHARACTER. One `cm_get_equity_stocks(500, ch)`
   call per distinct first character. Keep records whose `symbol` is in the requested set.
2. Any symbol still missing: one `cm_get_stock_quote(symbol)` call each.
3. If a symbol appears more than once, prefer `series == "EQ"`.
Skip a symbol silently if both routes fail; never raise for a single missing symbol.

Session handling: create the session lazily on first use and REUSE it across calls. If a call
fails with HTTP 400/404, or a JSON-RPC error whose message mentions "session", discard the
session id, re-handshake once, and retry that call once. Any other failure: retry up to 2 times
with a short sleep, then raise `RuntimeError` with the tool name and the server's message.

## Required module functions

### `append_intraday(store_path, prices, updated_at, trade_date) -> int`

Accumulates a same-day series. The MCP has no history, so the only way to get an intraday
series out of it is to keep the snapshots we have seen.

- `store_path` is a JSON file path. Shape:
  ```json
  {"date":"YYYY-MM-DD","source":"nse-mcp","firstSeen":"<iso>","lastUpdatedAt":"<server updatedAt>",
   "snapshots":<int>,"points":{"RELIANCE":[["HH:MM:SS",1167.7], ...]}}
  ```
- If the file is missing, unreadable, or its `date` differs from `trade_date`, START A NEW ONE
  (the old day is wiped). Before wiping a readable file for a DIFFERENT date, copy it to
  `<dir>/archive/<its date>.json`, creating the directory. Never lose a day silently.
- Deduplicate on the server's crawl: if the stored `lastUpdatedAt` equals `updated_at`, append
  nothing and return 0. The crawl only advances every ~5 min, so polling faster must not pile up
  duplicate points.
- The time written per point is the HH:MM:SS of `updated_at` converted to IST
  (`updated_at` is UTC, ending in `Z`).
- Write atomically: write a `.tmp` next to the target then `os.replace`.
- Return the number of points appended.

### `live_fallback(prev_snapshot, mcp=None) -> tuple[dict, list, dict]`

Builds the same three values `nifty_feed.fetch_live()` returns, so the feed can swap it in when
NSE's own endpoint is down. `prev_snapshot` is a previously written `nifty50.json` (a dict).

The MCP gives no ffmc, so free-float share counts are carried over from the last good snapshot.
They only change when NSE revises free-float factors (quarterly), so they are valid intraday:

```
shares_i  = prev_ffmc_i / prev_last_i          # from prev_snapshot["stocks"]
ffmc_i    = shares_i * live_last_i             # live price from the MCP
divisor   = sum(prev_ffmc_i) / prev_index_last # index divisor, constant intraday
index_last = sum(ffmc_i) / divisor
```

Return:
- `stocks`: one dict per covered symbol with EXACTLY these keys, because the caller reads them
  by name: `symbol, companyName, lastPrice, open, previousClose, dayHigh, dayLow, ffmc, priority`
  (`priority` = 0). Take `companyName` from the previous snapshot's `name`.
- `idx`: `{symbol, lastPrice, open, previousClose, dayHigh, dayLow, lastUpdateTime, priority:1}`.
  `open` and `previousClose` come from `prev_snapshot["index"]`; `lastPrice` is the computed
  `index_last`; `dayHigh`/`dayLow` are the previous snapshot's high/low widened to include
  `index_last`. `lastUpdateTime` is `updated_at` converted to IST as `"YYYY-MM-DD HH:MM:SS"`.
- `status`: `{"status": "...", "tradeDate": ..., "message": ..., "source": "nse-mcp fallback"}` —
  carry `prev_snapshot.get("market")` through and add the `source` key.

Raise `RuntimeError` if `prev_snapshot` has no stocks, or if the MCP covered fewer than 45 of
the previous snapshot's symbols — a thin fallback is worse than an honest failure.

## `__main__` self-test

Running `python feed/nse_mcp.py` must, with no arguments:
1. Load `../site/data/nifty50.json` relative to the module file (resolve properly, work from any cwd).
2. Print the crawl status.
3. Fetch quotes for its 50 symbols; print how many were covered, how many MCP calls were made
   (count them), and the wall-clock seconds taken.
4. Call `append_intraday` into `../feed/cache/mcp_intraday/today.json`; print points appended.
5. Call `live_fallback` and print the synthesised index level beside the previous snapshot's
   index level and the difference, plus 3 sample stocks (symbol, MCP last, snapshot last).
Wrap it so a failure prints a clear message and exits non-zero.

## Style

Match the existing feed: module docstring explaining WHY, short comments only where the reason
is not obvious from the code, no type annotations in signatures, 4-space indent, lines under
100 characters. No external deps beyond `requests`. No logging framework — a small `log()`
using `print` is fine.
